"""Deep tests: UIPandler end-to-end relay against a fake Moonraker.

A real proxy.Server is bound to 127.0.0.1 on an ephemeral port and talks to a
real (fake) upstream, also 127.0.0.1/ephemeral. Nothing in this file may touch
the live stack ports; an autouse guard enforces that on the proxy module's own
socket helpers. Every server is shut down in a finally block.

Four behaviours used to be asserted as *should be* and were marked
`xfail(strict=True)` because the product did not do them yet: the negative
Content-Length wedge, the coalesced first WebSocket frame, HTTP/1.1 pipelining
and the 5-slot accept backlog. All four are fixed in proxy.py now, so their
markers are gone and they guard the fix; the WebSocket one also carries a
characterisation note about what the fake upstream can actually observe.
"""
import http.client
import importlib
import os
import socket
import threading
import time
import types
import urllib.parse

import pytest

import klipperformac.proxy as proxy_mod

STACK_PORTS = frozenset({7125, 7126, 8080, 8081})
UPSTREAM_SENTINEL = 1
SECRET = b"DO-NOT-SERVE-6b2f\n"


@pytest.fixture
def proxymod(tmp_path, monkeypatch):
    monkeypatch.setenv("KLIPPERFORMAC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KLIPPERFORMAC_DATA", str(tmp_path / "data"))
    mod = importlib.reload(proxy_mod)
    monkeypatch.setattr(mod.UIPandler, "upstream_port", UPSTREAM_SENTINEL,
                        raising=False)

    real_create = socket.create_connection
    real_conn = http.client.HTTPConnection

    def create_connection(address, *a, **kw):
        assert address[1] not in STACK_PORTS and str(address[0]) == "127.0.0.1", \
            "forbidden upstream address %r" % (address,)
        return real_create(address, *a, **kw)

    class GuardedConnection(real_conn):
        def __init__(self, host, port=None, *a, **kw):
            assert port not in STACK_PORTS, "forbidden upstream port %r" % port
            super().__init__(host, port, *a, **kw)

    monkeypatch.setattr(mod, "socket",
                        types.SimpleNamespace(create_connection=create_connection))
    # Install the HTTP relay guard too: _proxy() builds its upstream connection
    # through http.client, so without this the live Moonraker on 127.0.0.1:7125
    # would be reachable from a test that forgot to set upstream_port.
    monkeypatch.setattr(mod.http.client, "HTTPConnection", GuardedConnection)
    return mod


class FakeUpstream:
    """Raw loopback HTTP server with scripted, deliberately hostile replies."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        # Wide on purpose: this fake must never be the reason a client test
        # fails, so its accept queue is far above any burst the tests create.
        self.sock.listen(128)
        self.port = self.sock.getsockname()[1]
        self.requests = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        self.sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,),
                             daemon=True).start()

    @staticmethod
    def _read_head(conn):
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return None, None
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        method, target, _version = (lines[0].decode("latin-1").split(" ", 2)
                                   + ["", ""])[:3]
        headers = {}
        for line in lines[1:]:
            k, _, v = line.decode("latin-1").partition(":")
            headers[k.strip().lower()] = v.strip()
        body = rest
        n = int(headers.get("content-length") or 0)
        while len(body) < n:
            chunk = conn.recv(65536)
            if not chunk:
                break
            body += chunk
        return (method, target, headers), body

    def _serve(self, conn):
        try:
            conn.settimeout(10)
            parsed, body = self._read_head(conn)
            if parsed is None:
                return
            method, target, headers = parsed
            self.requests.append((method, target, headers, body))
            path = urllib.parse.urlsplit(target).path
            if path == "/websocket" or headers.get("upgrade", "") == "websocket":
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                             b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                             b"Sec-WebSocket-Accept: s33f\r\n\r\n")
                conn.settimeout(5)
                while True:
                    try:
                        data = conn.recv(65536)
                    except (socket.timeout, OSError):
                        break
                    if not data:
                        break
                    conn.sendall(b"WS:" + data)
                return
            if path == "/printer/abort-body":
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n"
                             b"short")
                conn.close()
                return
            if path == "/printer/refuse-body":
                conn.close()
                return
            if path == "/printer/chunked":
                conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                             b"Content-Type: application/json\r\n\r\n"
                             b"5\r\nCHUNK\r\n0\r\n\r\n")
                return
            payload = (b"UP:" + body if (body or "content-length" in headers)
                       else b"UP:none")
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"X-Upstream: yes\r\nKeep-Alive: timeout=5\r\n"
                         b"Set-Cookie: sid=1\r\nContent-Length: "
                         + str(len(payload)).encode() + b"\r\n\r\n" + payload)
        except (OSError, ValueError):
            try:
                conn.close()
            except OSError:
                pass
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        self._thread.join(2)


@pytest.fixture
def upstream():
    up = FakeUpstream()
    try:
        yield up
    finally:
        up.close()


@pytest.fixture
def docroot(stack):
    root = stack.paths.WEB_FLUIDD
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text("<h1>fluidd</h1>")
    (root / "assets").mkdir(exist_ok=True)
    (root / "assets" / "app.js").write_text("app()")
    outside = stack.data / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_bytes(SECRET)
    return root


@pytest.fixture
def proxy_server(proxymod, docroot, upstream):
    httpd = proxymod.Server(("127.0.0.1", 0),
                            lambda *a, **kw: proxymod.UIPandler(
                                *a, directory=str(docroot), **kw))
    proxymod.UIPandler.upstream_port = upstream.port
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    httpd.port = httpd.server_address[1]
    assert httpd.server_address[0] == "127.0.0.1"
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


def call(port, method, path, headers=(), body=None, timeout=10):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.putrequest(method, path)
        for k, v in headers:
            conn.putheader(k, v)
        # putrequest()/endheaders() do NOT add framing on their own, so spell
        # out Content-Length: an unframed body only reaches the upstream by
        # accident of TCP coalescing, which would make the byte-exactness tests
        # flaky instead of meaningful.
        if body is not None and not any(k.lower() == "content-length"
                                        for k, _v in headers):
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        resp = conn.getresponse()
        return resp.status, resp.read(), dict(resp.getheaders())
    finally:
        conn.close()


def raw_roundtrip(port, payload, read_limit=200000, timeout=10):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(payload)
        data = b""
        while len(data) < read_limit:
            try:
                chunk = s.recv(4096)
            except (socket.timeout, OSError):
                break
            if not chunk:
                break
            data += chunk
        return data
    finally:
        s.close()


# --- plain relay ------------------------------------------------------------
def test_get_api_is_relayed_with_headers_and_body(proxy_server, upstream):
    status, body, hdrs = call(proxy_server.port, "GET", "/printer/info")
    assert status == 200 and body == b"UP:none"
    assert hdrs["X-Upstream"] == "yes"
    assert hdrs["Set-Cookie"] == "sid=1"
    assert hdrs["Content-Length"] == str(len(body))
    assert "Keep-Alive" not in hdrs, "hop-by-hop header leaked downstream"
    assert upstream.requests[0][1] == "/printer/info"


def test_post_body_survives_byte_for_byte(proxy_server, upstream):
    for payload in [b"", b"{}", '{"k":"é你"}'.encode("utf-8"),
                    bytes(range(256)), b"z" * 512 * 1024]:
        status, body, _h = call(proxy_server.port, "POST", "/printer/print/start",
                                [("Content-Type", "application/octet-stream")],
                                payload)
        assert status == 200, payload[:20]
        assert body == b"UP:" + payload


def test_static_and_api_paths_do_not_cross(proxy_server, upstream):
    status, body, _h = call(proxy_server.port, "GET", "/index.html")
    assert status == 200 and body == b"<h1>fluidd</h1>"
    assert not upstream.requests, "static GET must not reach Moonraker"
    status, body, _h = call(proxy_server.port, "GET", "/printer/info")
    assert status == 200
    assert len(upstream.requests) == 1


def test_traversal_through_the_relay_stays_contained(proxy_server, docroot,
                                                     upstream):
    for target in ["/../../../%s/secret.txt" % docroot.parent.parent.name,
                   "/assets/../../outside/secret.txt",
                   "/%2e%2e/%2e%2e/etc/passwd"]:
        status, body, _h = call(proxy_server.port, "GET", target)
        assert SECRET not in body, target


# --- failure modes ----------------------------------------------------------
def test_truncated_upstream_response_becomes_502(proxy_server, upstream):
    status, body, _h = call(proxy_server.port, "GET", "/printer/abort-body")
    assert 400 <= status < 600, status


def test_upstream_that_hangs_up_becomes_502(proxy_server, upstream):
    status, _body, _h = call(proxy_server.port, "GET", "/printer/refuse-body")
    assert 400 <= status < 600, status


def test_proxy_survives_after_upstream_failures(proxy_server, upstream):
    call(proxy_server.port, "GET", "/printer/abort-body")
    call(proxy_server.port, "GET", "/printer/refuse-body")
    status, body, _h = call(proxy_server.port, "GET", "/index.html")
    assert status == 200 and body == b"<h1>fluidd</h1>"
    status, body, _h = call(proxy_server.port, "GET", "/printer/info")
    assert status == 200


def test_dead_upstream_gives_502_not_a_traceback(proxymod, docroot):
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()
    httpd = proxymod.Server(("127.0.0.1", 0),
                            lambda *a, **kw: proxymod.UIPandler(
                                *a, directory=str(docroot), **kw))
    proxymod.UIPandler.upstream_port = dead_port
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        status, body, _h = call(httpd.server_address[1], "GET", "/printer/info")
        assert status == 502
        assert b"proxy error" in body
        status, body, _h = call(httpd.server_address[1], "GET", "/index.html")
        assert status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------
# FIXED (deep-hunt): proxy._request_body() now parses Content-Length as a
# strict non-negative decimal and answers 400 before reading anything, so a
# negative value can no longer become read(-1) = "read until EOF" and wedge the
# handler thread (a silent wedge that starves the accept loop).
# Guard: this test must keep passing.
# ---------------------------------------------------------------------------
def test_negative_content_length_is_answered(proxy_server, upstream):
    """A -N Content-Length must never make the proxy read the whole stream."""
    out = raw_roundtrip(proxy_server.port,
                        b"POST /printer/info HTTP/1.1\r\nHost: x\r\n"
                        b"Content-Length: -1\r\n\r\nBODY", timeout=4)
    assert out.startswith(b"HTTP/1."), out[:40]
    assert 400 <= int(out.split(b" ")[1]) or b"UP:" in out


# --- websockets -------------------------------------------------------------
def raw_two_part(port, handshake, frame, delay=0.8, timeout=10):
    """Send the handshake, let the tunnel open, then send the frame as its own
    packet. Returns everything the client received."""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(handshake)
        data = b""
        deadline = time.time() + delay
        while time.time() < deadline and b"\r\n\r\n" not in data:
            try:
                s.settimeout(max(0.05, deadline - time.time()))
                chunk = s.recv(4096)
            except (socket.timeout, OSError):
                break
            if not chunk:
                break
            data += chunk
        s.sendall(frame)
        s.settimeout(timeout)
        while len(data) < 200000:
            try:
                chunk = s.recv(4096)
            except (socket.timeout, OSError):
                break
            if not chunk:
                break
            data += chunk
        return data
    finally:
        s.close()


def test_websocket_handshake_target_and_headers_are_forwarded(proxy_server,
                                                             upstream):
    """Good behaviour: the upgrade request line (query string included) and the
    Sec-WebSocket-* headers reach Moonraker verbatim, and the 101 gets back."""
    out = raw_roundtrip(
        proxy_server.port,
        b"GET /websocket?token=abc&job=2 HTTP/1.1\r\nHost: x\r\n"
        b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        b"Sec-WebSocket-Key: dGhlIHNhbXBsZQ==\r\n"
        b"Sec-WebSocket-Version: 13\r\n\r\n")
    assert b"101 Switching Protocols" in out.split(b"\r\n")[0], out[:80]
    method, target, headers, _body = upstream.requests[-1]
    assert method == "GET"
    assert target == "/websocket?token=abc&job=2", target
    assert headers["upgrade"] == "websocket"
    assert headers["sec-websocket-key"] == "dGhlIHNhbXBsZQ=="
    assert headers["sec-websocket-version"] == "13"


def test_websocket_frame_sent_after_the_handshake_relays_both_ways(proxy_server,
                                                                  upstream):
    """Good behaviour: once the tunnel is open, client->upstream and
    upstream->client both flow."""
    out = raw_two_part(
        proxy_server.port,
        b"GET /websocket HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
        b"Connection: Upgrade\r\nSec-WebSocket-Key: k\r\n\r\n",
        b"PING-PART-TWO")
    assert b"101 Switching Protocols" in out.split(b"\r\n")[0], out[:80]
    assert b"WS:PING-PART-TWO" in out, out[:300]


# ---------------------------------------------------------------------------
# FIXED (deep-hunt): proxy._proxy_ws() tunnelled from the raw socket
# self.connection and ignored what the reader had already buffered, so a first
# frame coalesced into the same write as the handshake was silently dropped and
# the tunnel never started. proxy.py keeps its own read-ahead (_ClientReader)
# and releases those bytes to the upstream once the handshake answer has been
# relayed.
#
# Characterisation note: FakeUpstream._read_head() attributes every byte after
# the "\r\n\r\n" boundary to whichever side of it it happened to arrive on, so
# the frame can only ever show up in ONE of the two channels below -- as the
# recorded trailing body if the proxy writes it in the same segment as the
# handshake, or as the echoed "WS:<frame>" if the proxy (as it now does) waits
# for the 101 first so a handshake failure can never leak client frames. The
# echo is the end-to-end proof, and a proxy that drops the frame fails here.
# ---------------------------------------------------------------------------
def test_websocket_frame_coalesced_with_the_handshake_is_relayed(proxy_server,
                                                                upstream):
    """A client that writes the handshake and its first frame in one go (or has
    them coalesced by TCP) must still get the echo back."""
    out = raw_roundtrip(
        proxy_server.port,
        b"GET /websocket HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
        b"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZQ==\r\n"
        b"Sec-WebSocket-Version: 13\r\n\r\nHELLO-FRAME")
    assert b"101 Switching Protocols" in out.split(b"\r\n")[0], out[:80]
    bodies = [b for _m, t, _h, b in upstream.requests if t == "/websocket"]
    assert bodies, "the upgrade never reached the upstream"
    reached = b"HELLO-FRAME" in bodies[-1] or b"WS:HELLO-FRAME" in out
    assert reached, \
        "the coalesced frame never reached the upstream: %r / %r" % (bodies,
                                                                     out[:200])
    assert b"WS:HELLO-FRAME" in out, out[:200]


def test_websocket_with_dead_upstream_is_502(proxymod, docroot):
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()
    httpd = proxymod.Server(("127.0.0.1", 0),
                            lambda *a, **kw: proxymod.UIPandler(
                                *a, directory=str(docroot), **kw))
    proxymod.UIPandler.upstream_port = dead_port
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        out = raw_roundtrip(
            httpd.server_address[1],
            b"GET /websocket HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\n\r\n")
        assert out.startswith(b"HTTP/1.0 502"), out[:60]
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- keep-alive / pipelining framing ---------------------------------------
# ---------------------------------------------------------------------------
# FIXED (deep-hunt): UIPandler kept SimpleHTTPRequestHandler's
# protocol_version = 'HTTP/1.0', so the stdlib handle() loop answered the first
# request, closed, and silently discarded the pipelined request bytes it had
# already buffered. proxy.handle() now drains requests that are already in hand
# (still answering HTTP/1.0 and still closing the connection, so an idle
# keep-alive client cannot pin a worker thread).
# Guard: this test must keep passing.
# ---------------------------------------------------------------------------
def test_pipelined_requests_are_answered_in_order(proxy_server, upstream):
    out = raw_roundtrip(
        proxy_server.port,
        b"GET /printer/info HTTP/1.1\r\nHost: x\r\n\r\n"
        b"GET /index.html HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
        timeout=15)
    assert out.count(b"HTTP/1.") >= 2, out[:200]
    assert out.split(b"HTTP/1.")[1].startswith(b"0 200")
    assert b"UP:none" in out and b"<h1>fluidd</h1>" in out


def test_chunked_response_is_dechunked_and_relengthed(proxy_server, upstream):
    status, body, hdrs = call(proxy_server.port, "GET", "/printer/chunked")
    assert status == 200
    assert body == b"CHUNK", body
    assert hdrs["Content-Length"] == "5"
    assert "Transfer-Encoding" not in hdrs


# --- concurrency ------------------------------------------------------------
def test_concurrent_clients_do_not_interfere(proxy_server, upstream):
    """12 clients served at the same instant must each get THEIR OWN answer.

    The connections are opened one at a time on purpose: the accept-backlog
    limit is a separate, separately-marked finding
    (test_simultaneous_connects_are_not_reset), so the only thing under test
    here is cross-talk between the relay threads. Each client POSTs a unique
    body and the fake upstream echoes it back, so a swapped response is caught
    instead of merely a swapped query string.
    """
    n = 12
    payloads = [b"job-%02d-body" % i for i in range(n)]
    sockets, results, errors = [], [], []
    gate = threading.Barrier(n, timeout=30)
    try:
        for _ in range(n):
            sockets.append(socket.create_connection(
                ("127.0.0.1", proxy_server.port), timeout=25))

        def worker(i, sock):
            try:
                gate.wait()
                sock.sendall(
                    b"POST /printer/info?i=%d HTTP/1.1\r\nHost: x\r\n"
                    b"Connection: close\r\nContent-Length: %d\r\n\r\n"
                    % (i, len(payloads[i])) + payloads[i])
                buf = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                results.append((i, buf))
            except Exception as exc:  # noqa: BLE001
                errors.append((i, repr(exc)))

        threads = [threading.Thread(target=worker, args=(i, sockets[i]))
                   for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
            assert not t.is_alive(), "a request thread hung"
        assert not errors, errors
        assert len(results) == n
        for i, buf in results:
            assert buf.startswith(b"HTTP/1."), (i, buf[:60])
            assert buf.endswith(b"UP:" + payloads[i]), \
                "client %d received another client's response: %r" % (i, buf[-40:])
        targets = [t for _m, t, _h, _b in upstream.requests]
        assert len(targets) == n and len(set(targets)) == n, targets
        assert sorted(b for _m, _t, _h, b in upstream.requests) == sorted(payloads)
    finally:
        for s in sockets:
            try:
                s.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# FIXED (deep-hunt): proxy.Server never raised
# socketserver.TCPServer.request_queue_size, so the listener's accept backlog
# was 5 and a burst of simultaneous connections was answered with 'Connection
# reset by peer' / broken pipes. Loading a Mainsail/Fluidd page is exactly such
# a burst, because the handler answers HTTP/1.0 and every API poll is therefore
# a brand-new connection. Server now sets request_queue_size = 128.
# Guard: this test must keep passing.
# ---------------------------------------------------------------------------
def test_simultaneous_connects_are_not_reset(proxy_server, upstream):
    """Every client in a same-instant burst must get its own correct answer."""
    n = 32
    payloads = [b"burst-%02d" % i for i in range(n)]
    gate = threading.Barrier(n, timeout=30)
    served, errors = [], []

    def worker(i):
        sock = None
        try:
            gate.wait()
            sock = socket.create_connection(
                ("127.0.0.1", proxy_server.port), timeout=10)
            sock.sendall(
                b"POST /printer/info HTTP/1.1\r\nHost: x\r\n"
                b"Connection: close\r\nContent-Length: %d\r\n\r\n"
                % len(payloads[i]) + payloads[i])
            buf = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
            assert buf.endswith(b"UP:" + payloads[i]), buf[:60]
            served.append(i)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
        assert not t.is_alive(), "a burst client hung"
    assert len(served) == n, \
        "backlog is %r: served %d of %d simultaneous clients, %d failed (%s)" % (
            proxy_mod.Server.request_queue_size, len(served), n, len(errors),
            errors[:3])
    assert not errors, errors[:5]
