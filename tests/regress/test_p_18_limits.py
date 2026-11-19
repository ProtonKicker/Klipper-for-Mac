"""Regression: the reverse proxy bounds what one connection can cost.

Finding (hunt1 raw index 18, minor -- but reachable by any LAN host once
`klipperformac lan on` is set):

* the relay read the whole declared request body into one bytes object
  (MAX_BODY was 512 MiB, and a chunked body had no size answer at all), so a
  handful of concurrent large POSTs could OOM the Mac;
* socketserver.ThreadingTCPServer gave every connection a thread with no
  ceiling, and a websocket tunnel holds its thread for as long as the socket
  lives;
* `Upgrade: websocket` on any path opened the tunnel, so a document-root path
  (the live probe used /printer.cfg) was relayed upstream past the API_PREFIXES
  gate and answered by Moonraker instead of by the file server.

klipperformac/proxy.py now caps the buffered body (413 before a byte is read,
for both Content-Length and chunked framing), caps live connections with a
bounded semaphore and answers the overflow with a clean 503, and reserves the
tunnel for proxied routes.

Everything here runs in-process on ephemeral ports against a throwaway
KLIPPERFORMAC_HOME; the live stack (7125/8080/8081) is never contacted.
"""
import io
import socket
import threading
import time
import types

import pytest

import klipperformac.proxy as proxymod
from tests.proxy_testkit import start_server

STACK_PORTS = frozenset({7125, 8080, 8081})
UPSTREAM_SENTINEL = 4321


def free_slots(server):
    """How many connections may still be given a thread right now."""
    return getattr(server._slots, "_value", 0)


class Recorder:
    """Stand-in for http.client: records, never connects."""

    def __init__(self, response=None):
        self.calls = []
        self.response = response

    def factory(self):
        outer = self

        class _Conn:
            def __init__(self, host, port=None, *a, **kw):
                assert port not in STACK_PORTS, "live stack port %r" % (port,)
                assert str(host) == "127.0.0.1", "upstream must stay loopback"
                self.rec = {"host": host, "port": port, "requests": []}
                outer.calls.append(self.rec)

            def request(self, method, url, body, headers):
                self.rec["requests"].append({"method": method, "url": url,
                                             "body": body,
                                             "headers": dict(headers)})

            def getresponse(self):
                return outer.response or _Response()

            def close(self):
                pass

        return _Conn


class _Response:
    status, reason = 200, "OK"

    def getheaders(self):
        return [("Content-Type", "application/json")]

    def read(self):
        return b"{}"


def relayed(mod, raw_request, recorder, peer="127.0.0.1"):
    real_http, saved = mod.http, mod.UIPandler.upstream_port
    mod.http = types.SimpleNamespace(
        client=types.SimpleNamespace(HTTPConnection=recorder.factory()))
    mod.UIPandler.upstream_port = UPSTREAM_SENTINEL
    try:
        h = object.__new__(mod.UIPandler)
        h.directory = "/tmp"
        h.client_address = (peer, 51111)
        h.server = None
        h.connection = None
        h.close_connection = False
        h.request_version = "HTTP/1.1"
        h.rfile = io.BytesIO(raw_request)
        h.wfile = io.BytesIO()
        h.stdout = h.wfile
        h.stderr = io.BytesIO()
        h.handle_one_request()
        out = h.wfile.getvalue()
    finally:
        mod.http, mod.UIPandler.upstream_port = real_http, saved
    return h, out


def status_line(out):
    return out.split(b"\r\n")[0].decode("latin-1", "replace")


def status_code(out):
    parts = status_line(out).split(" ")
    return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else -1


# ---------------------------------------------------------------------------
# the shipped limits themselves
# ---------------------------------------------------------------------------
def test_the_buffered_body_cap_is_bounded():
    """A request body is buffered in full by the relay, so the cap is the
    memory ceiling. It shipped at 512 MiB; it must not creep back up."""
    assert proxymod.MAX_BODY <= 100 << 20, proxymod.MAX_BODY
    assert proxymod.MAX_BODY >= 1 << 20, "an upload still has to fit"


def test_the_concurrency_cap_is_bounded():
    assert isinstance(proxymod.MAX_CONCURRENT, int)
    assert 1 <= proxymod.MAX_CONCURRENT <= 256, proxymod.MAX_CONCURRENT
    assert proxymod.Server.max_concurrent == proxymod.MAX_CONCURRENT


def test_the_websocket_handshake_buffer_is_bounded():
    """The tunnel copies upstream's answer until the header block ends; that
    read must stop somewhere no matter what the peer sends."""
    assert proxymod.WS_HANDSHAKE_MAX <= 4 << 20, proxymod.WS_HANDSHAKE_MAX


# ---------------------------------------------------------------------------
# Content-Length over the cap: 413, and not one byte of the body is read
# ---------------------------------------------------------------------------
def test_body_over_the_cap_is_refused_before_it_is_read(proxymod_patched):
    mod = proxymod_patched            # MAX_BODY patched down to 4096
    rec = Recorder()
    head = (b"POST /printer/gcode/script HTTP/1.1\r\nHost: x\r\n"
            b"Content-Length: 4097\r\n\r\n")
    _h, out = relayed(mod, head + b"z" * 4097, rec)
    assert status_code(out) == 413, status_line(out)
    assert b"request entity too large" in out.split(b"\r\n")[0].lower() or \
        b"entity too large" in out.lower(), out[:120]
    assert not rec.calls, "an oversized body must never reach the upstream"


def test_body_at_the_cap_is_still_relayed(proxymod_patched):
    mod = proxymod_patched
    rec = Recorder()
    body = b"z" * 4096
    head = (b"POST /printer/gcode/script HTTP/1.1\r\nHost: x\r\n"
            b"Content-Length: %d\r\n\r\n" % len(body))
    _h, out = relayed(mod, head + body, rec)
    assert status_code(out) == 200, status_line(out)
    assert rec.calls[-1]["requests"][0]["body"] == body


def test_oversized_content_length_is_refused_without_reading_the_body(
        proxymod_patched):
    """The client that declares 4 GiB and sends nothing must be answered
    immediately: reading first is what made the old code pay RSS for a request
    that ended in a 502."""
    mod = proxymod_patched
    rec = Recorder()
    head = (b"POST /server/files/upload HTTP/1.1\r\nHost: x\r\n"
            b"Content-Length: 999999999999\r\n\r\n")
    _h, out = relayed(mod, head + b"", rec)     # no body arrives at all
    assert status_code(out) == 413, status_line(out)
    assert not rec.calls


# ---------------------------------------------------------------------------
# chunked framing over the cap: 413, not a "malformed body" 400
# ---------------------------------------------------------------------------
def test_chunked_body_over_the_cap_gets_413(proxymod_patched):
    mod = proxymod_patched
    rec = Recorder()
    chunks = (b"POST /printer/gcode/script HTTP/1.1\r\nHost: x\r\n"
              b"Transfer-Encoding: chunked\r\n\r\n")
    # 4096 bytes of payload in 1 KiB chunks, then one chunk too many
    body = b"".join(b"400\r\n" + b"z" * 1024 + b"\r\n" for _ in range(4))
    body += b"401\r\n" + b"z" * 1025 + b"\r\n"
    _h, out = relayed(mod, chunks + body, rec)
    assert status_code(out) == 413, status_line(out)
    assert not rec.calls, "a chunked runaway must never reach the upstream"


def test_chunked_body_under_the_cap_is_still_relayed(proxymod_patched):
    mod = proxymod_patched
    rec = Recorder()
    head = (b"POST /printer/gcode/script HTTP/1.1\r\nHost: x\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n")
    body = b"400\r\n" + b"z" * 1024 + b"\r\n" + b"0\r\n\r\n"
    _h, out = relayed(mod, head + body, rec)
    assert status_code(out) == 200, status_line(out)
    assert rec.calls[-1]["requests"][0]["body"] == b"z" * 1024


@pytest.fixture
def proxymod_patched(monkeypatch, tmp_path):
    """The proxy module with MAX_BODY small enough to test in microseconds."""
    monkeypatch.setenv("KLIPPERFORMAC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KLIPPERFORMAC_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(proxymod, "MAX_BODY", 4096, raising=False)
    return proxymod


# ---------------------------------------------------------------------------
# bounded concurrency: a clean 503, and the slot comes back
# ---------------------------------------------------------------------------
class CappedServer(proxymod.Server):
    max_concurrent = 1


@pytest.fixture
def capped(tmp_path):
    """A real proxy server that will only ever run one connection at a time."""
    docroot = tmp_path / "ui"
    docroot.mkdir()
    (docroot / "index.html").write_text("<html>ui</html>\n")
    old = proxymod.UIPandler.upstream_port
    proxymod.UIPandler.upstream_port = UPSTREAM_SENTINEL
    httpd = CappedServer(("127.0.0.1", 0),
                         lambda *a, **kw: proxymod.UIPandler(
                             *a, directory=str(docroot), **kw))
    start_server(httpd)
    try:
        yield httpd, httpd.server_address[1], docroot
    finally:
        proxymod.UIPandler.upstream_port = old
        httpd.shutdown()
        httpd.server_close()


def read_response(conn, timeout=6):
    conn.settimeout(timeout)
    out = b""
    while True:
        try:
            chunk = conn.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        out += chunk
        if b"\r\n\r\n" in out:
            # keep reading until the declared body is complete (or EOF), so the
            # framing of the answer itself is what the test asserts on
            head, _sep, body = out.partition(b"\r\n\r\n")
            want = None
            for line in head.split(b"\r\n")[1:]:
                k, _s, v = line.partition(b":")
                if k.strip().lower() == b"content-length":
                    want = int(v.strip() or 0)
            if want is None or len(body) >= want:
                break
    return out


def test_second_connection_is_refused_with_a_clean_503(capped):
    """Over the cap the peer gets a complete HTTP answer, not a hang and not a
    reset: a browser retries it, and no thread was spent on it."""
    httpd, port, _docroot = capped
    holder = socket.create_connection(("127.0.0.1", port), timeout=6)
    try:
        # an unfinished body keeps the single worker thread busy, so the next
        # connection is genuinely over the limit
        holder.sendall(b"POST /printer/gcode/script HTTP/1.1\r\nHost: x\r\n"
                       b"Content-Length: 100000\r\n\r\n" + b"x")
        deadline = time.time() + 5
        while free_slots(httpd) == 1 and time.time() < deadline:
            time.sleep(0.02)
        assert free_slots(httpd) == 0, "the first connection took no slot"
        conn = socket.create_connection(("127.0.0.1", port), timeout=6)
        try:
            conn.sendall(b"GET /server/info HTTP/1.1\r\nHost: x\r\n\r\n")
            out = read_response(conn)
        finally:
            conn.close()
        assert status_line(out).startswith("HTTP/1.1 503"), status_line(out)
        head, _sep, body = out.partition(b"\r\n\r\n")
        lowered = head.decode("latin-1").lower()
        assert "content-length: %d" % len(body) in lowered, head
        assert "connection: close" in lowered, head
        assert "retry-after" in lowered, head
        assert body.strip(), "a 503 with no explanation is not an answer"
    finally:
        holder.close()


def test_the_slot_comes_back_when_a_connection_ends(capped):
    """A leaked slot would starve the UI for good: after the holder is closed
    the same server must answer the next request normally again."""
    httpd, port, _docroot = capped
    holder = socket.create_connection(("127.0.0.1", port), timeout=6)
    holder.sendall(b"GET /server/info HTTP/1.1\r\nHost: x\r\n"
                   b"Content-Length: 100000\r\n\r\n" + b"x")
    deadline = time.time() + 5
    while free_slots(httpd) == 1 and time.time() < deadline:
        time.sleep(0.02)
    assert free_slots(httpd) == 0
    holder.close()
    deadline = time.time() + 5
    while free_slots(httpd) == 0 and time.time() < deadline:
        time.sleep(0.02)
    assert free_slots(httpd) == 1, "the connection slot was never released"
    conn = socket.create_connection(("127.0.0.1", port), timeout=6)
    try:
        conn.sendall(b"GET /index.html HTTP/1.1\r\nHost: x\r\n\r\n")
        out = read_response(conn)
    finally:
        conn.close()
    assert status_code(out) == 200, status_line(out)


# ---------------------------------------------------------------------------
# the websocket tunnel is only for proxied routes
# ---------------------------------------------------------------------------
def test_upgrade_header_on_a_static_path_is_not_relayed(tmp_path):
    """`Upgrade: websocket` must not be a skeleton key for the API_PREFIXES
    gate: a document-root target is answered here, upstream never sees it."""
    docroot = tmp_path / "ui"
    docroot.mkdir()
    rec = Recorder()
    raw = (b"GET /printer.cfg HTTP/1.1\r\nHost: x\r\n"
           b"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
    mod = proxymod
    real_http, saved = mod.http, mod.UIPandler.upstream_port
    mod.http = types.SimpleNamespace(
        client=types.SimpleNamespace(HTTPConnection=rec.factory()))
    mod.UIPandler.upstream_port = UPSTREAM_SENTINEL
    try:
        h = object.__new__(mod.UIPandler)
        h.directory = str(docroot)
        h.client_address = ("127.0.0.1", 51111)
        h.server = None
        h.connection = None
        h.close_connection = False
        h.request_version = "HTTP/1.1"
        h.rfile = io.BytesIO(raw)
        h.wfile = io.BytesIO()
        h.stdout = h.wfile
        h.stderr = io.BytesIO()
        h.handle_one_request()
        out = h.wfile.getvalue()
    finally:
        mod.http, mod.UIPandler.upstream_port = real_http, saved
    assert not rec.calls, "a static path was relayed upstream"
    assert status_code(out) == 404, status_line(out)
    assert b"Tornado" not in out and b"tornado" not in out


def test_upgrade_header_on_an_api_path_still_opens_the_tunnel():
    """The other half: the gate must not close on real websocket clients, which
    Mainsail/Fluidd address as /websocket."""
    mod = proxymod
    called = []
    saved = mod.UIPandler._proxy_ws
    mod.UIPandler._proxy_ws = lambda self: called.append(self.path)
    try:
        _h, _out = relayed(mod, (b"GET /websocket HTTP/1.1\r\nHost: x\r\n"
                                 b"Upgrade: websocket\r\n"
                                 b"Connection: Upgrade\r\n\r\n"), Recorder())
        _h, _out = relayed(mod, (b"GET /printer/info HTTP/1.1\r\nHost: x\r\n"
                                 b"Upgrade: websocket\r\n"
                                 b"Connection: Upgrade\r\n\r\n"), Recorder())
    finally:
        mod.UIPandler._proxy_ws = saved
    assert called == ["/websocket", "/printer/info"], called


def test_websocket_tunnel_stops_at_its_handshake_buffer_bound(monkeypatch):
    """An upstream that never ends its handshake header block cannot make this
    proxy buffer forever: the copy stops at WS_HANDSHAKE_MAX."""
    reads = []
    to_client = []

    class _Endless:
        """A peer whose handshake answer is one long run of bytes, no CRLFCRLF."""

        def sendall(self, data):
            reads.append(data)

        def recv(self, _size):
            return b"A" * 65536

        def setblocking(self, _flag):
            pass

        def close(self):
            pass

    class _Collector:
        def sendall(self, data):
            to_client.append(data)

        def recv(self, _size):
            return b""

    monkeypatch.setattr(proxymod, "WS_HANDSHAKE_MAX", 4096, raising=False)
    monkeypatch.setattr(proxymod, "socket", types.SimpleNamespace(
        create_connection=lambda *a, **kw: _Endless(), error=socket.error))
    monkeypatch.setattr(proxymod, "select", types.SimpleNamespace(
        select=lambda *a, **kw: ([], [], [])))
    saved = proxymod.UIPandler.upstream_port
    proxymod.UIPandler.upstream_port = UPSTREAM_SENTINEL
    h = object.__new__(proxymod.UIPandler)
    h.directory = "/tmp"
    h.client_address = ("127.0.0.1", 51111)
    h.server = None
    h.request_version = "HTTP/1.1"
    h.command, h.path = "GET", "/websocket"
    h.close_connection = False
    h.connection = _Collector()
    h.rfile = io.BytesIO(b"")
    h.wfile = io.BytesIO()
    h.stdout = h.wfile
    h.stderr = io.BytesIO()
    h.headers = types.SimpleNamespace(
        items=lambda: [("Host", "x"), ("Upgrade", "websocket"),
                       ("Connection", "Upgrade")],
        get=lambda key, default="": dict(
            [("host", "x"), ("upgrade", "websocket")]
        ).get(key.lower(), default))
    try:
        proxymod.UIPandler._proxy_ws(h)
    finally:
        proxymod.UIPandler.upstream_port = saved
    forwarded = b"".join(to_client)
    assert forwarded, "the handshake answer was never relayed to the browser"
    assert len(reads) == 1, "the tunnel never wrote the upgrade upstream"
    assert len(forwarded) <= 4096 + 65536, \
        "the tunnel buffered past its handshake bound: %d bytes" % (
            len(forwarded),)
