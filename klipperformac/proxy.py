"""Combined static + reverse-proxy server for the web UIs.

Mainsail (moonraker-js) derives its REST base from the page origin and only
partially honors config.json hostname/port, so a plain static server on a
separate port breaks it (the "initializing" hang). This serves the UI's
static files AND relays Moonraker API paths + /websocket on the same port,
the same job nginx does in a standard Klipper install — stdlib only.

The proxy is the thing a browser talks to, so everything it puts on the wire
(status line, header block, body framing) is built from sanitized values:
neither Moonraker nor the browser may decide how the *other* side's HTTP gets
parsed.
"""
import http.client
import ipaddress
import os
import re
import select
import socket
import socketserver
import sys
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler

API_PREFIXES = (
    "server", "printer", "access", "machine", "job_queue", "history",
    "update", "data_store", "webcam", "extensions", "api", "websocket",
    "moonraker",
)
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate",
              "proxy-authorization", "te", "trailer", "transfer-encoding",
              "upgrade"}
# Responses that by definition carry no body: they must not be given a length.
NO_BODY_STATUS = frozenset({100, 101, 102, 103, 204, 304})

# An argv port token has to be canonical ASCII decimal. int() is a *Python
# literal* parser, so "1_0", "007", "-0", "+8080", "١٢٣" and "  4321\n" each
# quietly become a different number: a typo in a wrapper script or a launchd
# plist would bind (or point at) a port nobody asked for, while every wrapper
# still believes the UI lives where it was told.
PORT_TOKEN = re.compile(r"\A(?:0|[1-9][0-9]{0,4})\Z")
DIGITS = re.compile(r"\A[0-9]+\Z")
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")

MAX_BODY = 512 << 20          # the relay buffers the body, so bound it
MAX_CHUNKS = 1 << 16          # chunked framing must not run away either
MAX_TRAILERS = 64
WS_HANDSHAKE_MAX = 1 << 20    # the 101 answer is copied, never buffered forever
READ_AHEAD = 65536
USAGE = ("usage: python -m klipperformac.proxy <port> <docroot> "
         "<moonraker_port> [bind_address]")


def _usage(message):
    """Reject a malformed invocation with one clean line and a non-zero exit.

    main() is a child process of `klipperformac start` (process.py pops it with
    stdout and stderr on DEVNULL), so a one-line message plus a non-zero exit
    code is the whole contract: an interpreter traceback would tell the
    operator nothing the exit code did not.
    """
    raise SystemExit("klipperformac.proxy: {}\n{}".format(message, USAGE))


def _port_token(raw, what):
    if not PORT_TOKEN.match(raw):
        _usage("{} must be a plain port number 0-65535, not {!r}".format(
            what, raw))
    value = int(raw)
    if value > 65535:
        _usage("{} must be 0-65535, got {}".format(what, value))
    return value


def _bind_token(raw):
    """Accept only a numeric address, and never a wildcard by accident.

    "" and "0" are not errors to socket.bind(): both resolve to INADDR_ANY,
    which would publish the UIs (and, behind them, an unauthenticated
    Moonraker) on every interface, contrary to the loopback-by-default rule.
    Numeric-only also means a hostname typo can never turn into a DNS lookup.
    The LAN opt-in stays the explicit "0.0.0.0" that process.py passes.
    """
    try:
        ipaddress.ip_address(raw)
    except ValueError:
        _usage("bind address must be a numeric IP, got {!r} (use 127.0.0.1; "
               "0.0.0.0 only for an explicit LAN opt-in)".format(raw))
    return raw


def _docroot(raw):
    """Resolve the document root to a usable absolute path, or refuse to start.

    A blank token makes SimpleHTTPRequestHandler serve from the server's CWD
    (the repo root in production), and a path carrying a NUL byte lets the
    process start and then kill its own handler thread on every single request.
    """
    if not raw.strip():
        _usage("the document root must not be empty")
    try:
        path = os.path.abspath(raw)
        os.fsencode(path)              # lone surrogates cannot reach the OS
        if not os.path.isdir(path):
            raise NotADirectoryError(path)
    except (UnicodeEncodeError, ValueError, OSError):
        _usage("the document root {!r} is not a usable directory (install the "
               "UI first: klipperformac ui mainsail)".format(raw))
    return path


def _oneline(value, limit=200):
    """Collapse control bytes so a value can never split a header block.

    RFC 9112 4.1 forbids CR/LF in the status line and in field values, and the
    upstream's own text is the only part of a proxied response a peer controls.
    """
    text = CONTROL_CHARS.sub(" ", str(value)).strip()
    return text[:limit]


class _ClientReader:
    """The client's read side, with its read-ahead kept visible.

    socket.makefile('rb') gives a BufferedReader whose peek() has to issue a
    blocking socket read whenever its buffer happens to be empty. This server
    needs the opposite question -- "has the client already sent more bytes?" --
    once per request (pipelining) and once per websocket upgrade (a first frame
    coalesced into the handshake segment), so the read-ahead lives here where
    answering costs no syscall and can never wait for data that has not arrived.
    """

    def __init__(self, sock, bufsize=READ_AHEAD):
        self._sock = sock
        self._size = bufsize if bufsize and bufsize > 0 else READ_AHEAD
        self._buf = bytearray()
        self._eof = False
        self._closed = False

    # -- file-object surface used by BaseHTTPRequestHandler -----------------
    def readable(self):
        return True

    @property
    def closed(self):
        return self._closed

    def close(self):
        self._closed = True
        self._eof = True          # so no read loop can spin on a closed reader
        del self._buf[:]

    def fileno(self):
        return self._sock.fileno()

    def flush(self):
        pass

    def _fill(self):
        """One socket read into the buffer; False once the peer has closed."""
        if self._eof or self._closed:
            return False
        try:
            data = self._sock.recv(self._size)
        except (OSError, ValueError):
            self._eof = True
            raise
        if not data:
            self._eof = True
            return False
        self._buf += data
        return True

    def readline(self, hint=-1):
        """One line, at most `hint` bytes: stdlib readline semantics."""
        limit = self._size if hint is None or hint < 0 else hint
        while True:
            end = self._buf.find(b"\n")
            if end >= 0:
                take = min(end + 1, limit)
                break
            if len(self._buf) >= limit or self._eof:
                take = min(limit, len(self._buf))
                break
            self._fill()
        line = bytes(self._buf[:take])
        del self._buf[:take]
        return line

    def read(self, size=-1):
        """Exactly size bytes, or less at EOF; all remaining bytes when -1."""
        if size is None or size < 0:
            while self._fill():
                pass
            data = bytes(self._buf)
            del self._buf[:]
            return data
        while len(self._buf) < size and not self._eof:
            self._fill()
        take = min(size, len(self._buf))
        data = bytes(self._buf[:take])
        del self._buf[:take]
        return data

    # -- the two questions the stdlib reader cannot answer cheaply ----------
    def pending(self):
        """True when more bytes are already in hand (never blocks)."""
        if self._buf:
            return True
        if self._eof or self._closed:
            return False
        try:
            ready, _, _ = select.select([self._sock], [], [], 0)
        except (OSError, ValueError):
            return False
        return bool(ready)

    def take_buffered(self):
        """Consume and return whatever the headers left in the buffer."""
        if not self._buf:
            return b""
        data = bytes(self._buf)
        del self._buf[:]
        return data


class UIPandler(SimpleHTTPRequestHandler):
    upstream_port = 7125

    # --- request wiring -----------------------------------------------------
    def setup(self):
        """StreamRequestHandler.setup() with a visible read-ahead."""
        BaseHTTPRequestHandler.setup(self)
        stale = self.rfile
        self.rfile = _ClientReader(self.connection, self.rbufsize)
        stale.close()          # nothing was read into it yet; drop the makefile

    # --- per-request gates --------------------------------------------------
    def _malformed_target(self):
        """A raw control byte in the target is never relayed to anyone."""
        if CONTROL_CHARS.search(self.path):
            self.send_error(400, "malformed request path")
            return True
        return False

    def is_api(self):
        first = self.path.lstrip("/").split("/", 1)[0].split("?", 1)[0]
        return first in API_PREFIXES

    def _static_request_ok(self):
        """Keep the document root a hard boundary, and answer instead of dying.

        translate_path() percent-decodes with errors='surrogatepass', so "%00"
        and "%ed%a0%80" survive into open()/os.stat() as a NUL byte or a lone
        surrogate: ValueError / UnicodeEncodeError, no HTTP response at all,
        and (handler logging is silenced here) not even a log line. It also
        collapses ".." but never resolves symlinks, so a link inside the
        user-writable doc root reaches any file this account can open
        (printer.cfg, klippy.log, ~/.ssh).
        """
        translated = self.translate_path(self.path)
        try:
            os.fsencode(translated)
            resolved = os.path.realpath(translated)
            root = os.path.realpath(self.directory)
        except (UnicodeEncodeError, ValueError, OSError):
            self.send_error(400, "malformed request path")
            return False
        if resolved != root and not resolved.startswith(root + os.sep):
            self.send_error(403, "outside the document root")
            return False
        return True

    def send_error(self, code, message=None, explain=None):
        """Remember that the byte stream is no longer trustworthy to frame on.

        Once an error page has been written the connection is done: any bytes
        the client happened to pipeline behind the request that failed are
        dropped instead of answered out of context.
        """
        self._framing_ok = False
        return BaseHTTPRequestHandler.send_error(self, code, message, explain)

    # --- the reverse-proxy half ---------------------------------------------
    def _chunked(self, value):
        return "chunked" in [coding.strip().lower()
                             for coding in value.replace(",", " ").split()]

    def _read_chunked(self):
        """De-chunk a request body into one byte string (None if malformed).

        The relay forwards a fixed Content-Length, so the chunks have to be
        decoded here: leaving them in the stream makes the remainder look like
        a second pipelined request, and the upstream just waits for a body.
        """
        parts, total, chunks = [], 0, 0
        while True:
            chunks += 1
            if chunks > MAX_CHUNKS:
                return None
            line = self.rfile.readline(128)
            if not line:
                return None
            try:
                size = int(line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                return None
            if size == 0:
                for _ in range(MAX_TRAILERS):      # trailer section, then blank
                    trailer = self.rfile.readline(1024)
                    if not trailer or len(trailer) >= 1024:
                        return None
                    if trailer in (b"\r\n", b"\n"):
                        return b"".join(parts)
                return None
            total += size
            if total > MAX_BODY:
                return None
            while size:
                piece = self.rfile.read(min(size, 65536))
                if not piece:
                    return None
                parts.append(piece)
                size -= len(piece)
            if self.rfile.read(2) != b"\r\n":
                return None                   # each chunk ends with CRLF

    def _request_body(self):
        """Return (body, rejected): the bytes to relay, or None for no body.

        Content-Length is parsed as a strict non-negative decimal and read
        exactly that far; the forwarded Content-Length is then re-written from
        the bytes actually taken, so a client that sends two conflicting values
        can never make proxy and upstream disagree about where the body ends
        (that disagreement is request smuggling), and a negative one can never
        turn into read(-1) = "until EOF" and wedge the worker.
        """
        lengths = [v for k, v in self.headers.items()
                   if k.lower() == "content-length"]
        codings = [v for k, v in self.headers.items()
                   if k.lower() == "transfer-encoding"]
        chunked = any(self._chunked(v) for v in codings)
        if codings and not chunked:
            self.send_error(400, "unsupported transfer coding")
            return None, True
        if chunked:
            if lengths:            # RFC 9112 6.1: ambiguous framing, refuse it
                self.send_error(400, "conflicting body framing")
                return None, True
            body = self._read_chunked()
            if body is None:
                self.send_error(400, "malformed chunked body")
                return None, True
            return body, False
        if not lengths:
            return None, False
        declared = lengths[0].strip()
        if not DIGITS.match(declared) or len(declared) > 15:
            self.send_error(400, "invalid Content-Length")
            return None, True
        size = int(declared)
        if size > MAX_BODY:
            self.send_error(413, "request entity too large")
            return None, True
        return self.rfile.read(size), False

    def _forward_headers(self, body):
        """Client headers minus what must never cross a proxy hop (RFC 9112 9.6).

        Host is dropped so http.client puts the upstream origin on the wire
        instead of the browser-facing one (virtual-host smuggling), and the
        request side now uses the same HOP_BY_HOP table the response side has
        always applied -- including the credentials in Proxy-Authorization,
        which otherwise travel to Moonraker in clear text for no reason.
        """
        headers = {}
        for k, v in self.headers.items():
            low = k.lower()
            if low in HOP_BY_HOP or low == "host" or low == "content-length":
                continue
            headers[k] = v
        if body is not None:
            headers["Content-Length"] = str(len(body))
        return headers

    def _relay_failed(self, exc):
        """Answer 502 without letting upstream bytes frame the browser's view.

        BaseHTTPRequestHandler writes the `message` argument straight into the
        status line, and http.client hands a BadStatusLine the offending line
        with its terminating CRLF still attached: interpolating it ended the
        header block early and demoted Server/Content-Type/Content-Length into
        the body, i.e. a response-splitting primitive against the UI. The real
        detail is handed to log_error (silent here, like all handler logging)
        instead of to the browser.
        """
        self.log_error("proxy error: %s", _oneline(repr(exc), limit=160))
        if isinstance(exc, TimeoutError):
            detail = "the Moonraker API did not answer in time"
        elif isinstance(exc, ConnectionError):
            detail = "the Moonraker API connection was refused or lost"
        elif isinstance(exc, OSError):
            detail = "the Moonraker API could not be reached"
        elif isinstance(exc, (HTTPException, UnicodeError)):
            detail = "the Moonraker API returned a response this proxy cannot parse"
        else:
            detail = "the request to the Moonraker API failed"
        self.send_error(502, "proxy error", detail)

    def _relay_response(self, resp, data):
        """Re-frame the upstream answer: sanitized status line, real length."""
        status = resp.status
        head_only = self.command == "HEAD"
        bodyless = status in NO_BODY_STATUS
        declared = None
        headers = []
        for k, v in resp.getheaders():
            low = k.lower()
            if low in HOP_BY_HOP:
                continue
            if low == "content-length":
                declared = _oneline(v, limit=32)
                continue
            headers.append((k, _oneline(v)))
        self.send_response(status, _oneline(resp.reason) or None)
        for k, v in headers:
            self.send_header(k, v)
        if head_only:
            # RFC 9110 9.3.2: a HEAD response reports the size the GET would
            # give, so the UIs can use HEAD for existence/size checks.
            self.send_header("Content-Length",
                             declared if declared and DIGITS.match(declared)
                             else str(len(data)))
        elif not bodyless:
            # The length must describe the bytes actually written here, not
            # whatever an upstream claimed alongside a body we re-read.
            self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head_only and not bodyless and data:
            self.wfile.write(data)

    def _proxy(self):
        """Relay one HTTP request to Moonraker and re-frame the answer."""
        try:
            body, rejected = self._request_body()
        except (OSError, UnicodeError) as exc:
            self._relay_failed(exc)
            return
        if rejected:
            return
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.upstream_port,
                                              timeout=15)
        except Exception as exc:                  # noqa: BLE001 - answered as 502
            self._relay_failed(exc)
            return
        try:
            try:
                conn.request(self.command, self.path, body,
                             self._forward_headers(body))
                resp = conn.getresponse()
                data = resp.read()
            except Exception as exc:              # noqa: BLE001 - answered as 502
                self._relay_failed(exc)
                return
        finally:
            # Every refused / timed-out / truncated / client-aborted path has to
            # release the fd and its TIME_WAIT pair, not wait for the collector.
            conn.close()
        try:
            self._relay_response(resp, data)
        except OSError:
            self.close_connection = True          # the browser went away

    # --- the websocket tunnel ------------------------------------------------
    def _proxy_ws(self):
        """Tunnel one websocket upgrade: handshake upstream, then raw bytes.

        The handshake is relayed verbatim -- Upgrade and the Sec-WebSocket-*
        fields are exactly the hop-by-hop headers this hop must not strip. The
        upstream's answer is forwarded to the browser *before* any client frame
        that was coalesced into the handshake segment is released: otherwise
        head and frame reach the upstream in one segment and a server that
        reads the handshake line-by-line loses the frame as trailing body, so
        the socket never gets off the ground (curl, python clients and a fast
        reconnect all write it this way).
        """
        try:
            up = socket.create_connection(("127.0.0.1", self.upstream_port),
                                          timeout=15)
        except OSError:
            self.send_error(502, "websocket upstream unavailable")
            return
        head = "{} {} {}\r\n".format(self.command, self.path,
                                     self.request_version)
        for k, v in self.headers.items():
            head += "{}: {}\r\n".format(k, v)
        head += "\r\n"
        self.close_connection = True
        client = self.connection
        try:
            try:
                buffered = self.rfile.take_buffered()
            except AttributeError:
                buffered = b""
            up.sendall(head.encode("latin-1", "replace"))
            reply = b""
            while (b"\r\n\r\n" not in reply and len(reply) < WS_HANDSHAKE_MAX):
                chunk = up.recv(65536)
                if not chunk:
                    break
                reply += chunk
            client.sendall(reply)
            if buffered:
                up.sendall(buffered)
            up.setblocking(False)
            while True:
                r, _, _ = select.select([client, up], [], [], 300)
                if not r:
                    break
                for s in r:
                    try:
                        data = s.recv(65536)
                    except OSError:
                        data = b""
                    if not data:
                        return
                    peer = up if s is client else client
                    try:
                        peer.sendall(data)
                    except OSError:
                        return
        except OSError:
            pass                    # a peer that vanished is not an HTTP error
        finally:
            try:
                up.close()
            except OSError:
                pass

    # --- routing ---------------------------------------------------------------
    def _wants_upgrade(self):
        return (self.path == "/websocket"
                or self.headers.get("Upgrade", "").lower() == "websocket")

    def do_GET(self):
        if self._malformed_target():
            return
        if self._wants_upgrade():
            self._proxy_ws()
            return
        if self.is_api():
            self._proxy()
            return
        if self._static_request_ok():
            SimpleHTTPRequestHandler.do_GET(self)

    def do_HEAD(self):
        if self._malformed_target():
            return
        if self._wants_upgrade():
            self._proxy_ws()
            return
        if self.is_api():
            self._proxy()
            return
        if self._static_request_ok():
            SimpleHTTPRequestHandler.do_HEAD(self)

    def do_POST(self):
        if not self._malformed_target():
            self._proxy()

    def do_PUT(self):
        if not self._malformed_target():
            self._proxy()

    def do_DELETE(self):
        if not self._malformed_target():
            self._proxy()

    def do_OPTIONS(self):
        if self._malformed_target():
            return
        if self.is_api() or self.path == "/websocket":
            self._proxy()
        else:
            self.send_response(204)
            self.end_headers()

    def handle(self):
        """Answer HTTP/1.0 (one persistent answer per connection) but never
        discard the requests a client already pipelined behind it.

        The stdlib loop keeps going while close_connection is False; with
        protocol_version HTTP/1.0 it stops after the first request and silently
        drops any further request that had already arrived in the same TCP
        segment. Draining what is in hand answers those requests and still
        closes the connection, so an idle keep-alive client cannot pin a worker
        thread.
        """
        self._framing_ok = True
        self.close_connection = True
        self.handle_one_request()
        while not self.close_connection or (self._framing_ok
                                            and self._pipelined_request_pending()):
            self.handle_one_request()

    def _pipelined_request_pending(self):
        """True only when the next request has already been received."""
        try:
            return self.rfile.pending()
        except AttributeError:
            return False

    def log_message(self, fmt, *args):
        pass  # keep stdout quiet; logs belong to moonraker/klippy


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    # socketserver's default backlog is 5. Loading a UI page is a burst of
    # same-instant connections (the handler answers HTTP/1.0, so every API poll
    # is a fresh connection), and everything past the backlog is reset by the
    # kernel instead of queued.
    request_queue_size = 128


def main(argv):
    if len(argv) < 4:
        _usage("missing arguments (need a port, a document root and a "
               "Moonraker port)")
    port = _port_token(argv[1], "port")
    directory = _docroot(argv[2])
    upstream = _port_token(argv[3], "moonraker port")
    bind = _bind_token(argv[4]) if len(argv) > 4 else "127.0.0.1"
    UIPandler.upstream_port = upstream
    handler = lambda *a, **kw: UIPandler(*a, directory=directory, **kw)
    try:
        httpd = Server((bind, port), handler)
    except OSError as exc:
        raise SystemExit("klipperformac.proxy: cannot listen on {}:{} -- {}".format(
            bind, port, _oneline(exc, limit=160)))
    with httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    main(sys.argv)
