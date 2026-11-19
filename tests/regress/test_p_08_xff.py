"""Regression: the reverse proxy must tell Moonraker who really connected.

Finding (hunt1 raw index 8, critical): every relay went to
http.client.HTTPConnection("127.0.0.1", ...), so with no forwarding headers
Moonraker's `request.remote_ip` was this proxy's own loopback address for
*every* caller. [authorization].trusted_clients (127.0.0.1, ::1) then matched
a LAN stranger and handed it the whole REST API and the /websocket control
channel with no credential -- and a client that wrote its own
X-Forwarded-For (or X-Real-Ip, which upstream reads *after* XFF, so it wins)
got to choose the address it was trusted as.

The proxy side of the fix is in klipperformac/proxy.py: every identity header
the browser sent is dropped, and X-Forwarded-For / X-Real-Ip /
X-Forwarded-Proto / X-Forwarded-Host are re-written from the peer address of
the accepted socket. These tests pin both halves -- the replacement and the
stripping -- on the HTTP relay and on the websocket handshake.

Upstream was not touched. No test here contacts the live stack: every port is
ephemeral or a closed sentinel.
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
UPSTREAM_SENTINEL = 4321           # closed: a real relay attempt would fail
LAN_IP = "192.168.12.77"           # a peer address no client gets to write
ORIGIN = "192.168.12.160:8080"     # the browser-facing Host in these requests
# Everything upstream consults for "who is this caller", alias included.
IDENTITY = ("x-forwarded-for", "x-real-ip", "x-forwarded-proto",
            "x-forwarded-host", "x-scheme", "forwarded")


# ---------------------------------------------------------------------------
# Harness A: a real in-process proxy on an ephemeral port, with a recording
# upstream on another ephemeral port.
# ---------------------------------------------------------------------------
class RecordingUpstream:
    """HTTP stub: records the raw request head of every connection."""

    def __init__(self):
        self.heads = []
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        assert self.port not in STACK_PORTS
        self.sock.listen(16)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                conn, _addr = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    def _handle(self, conn):
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            self.heads.append(data.split(b"\r\n\r\n", 1)[0].decode("latin-1"))
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def last_head(self):
        deadline = time.time() + 5
        while not self.heads and time.time() < deadline:
            time.sleep(0.02)
        assert self.heads, "the proxy relayed nothing to the upstream"
        return self.heads[-1]

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


@pytest.fixture
def proxy_up(tmp_path, monkeypatch):
    """Start the proxy in-process; yield (port, upstream, docroot)."""
    monkeypatch.setenv("KLIPPERFORMAC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KLIPPERFORMAC_DATA", str(tmp_path / "data"))
    docroot = tmp_path / "docroot"
    docroot.mkdir()
    (docroot / "index.html").write_text("<html>ui</html>\n")
    up = RecordingUpstream()
    httpd = proxymod.Server(
        ("127.0.0.1", 0),
        lambda *a, **kw: proxymod.UIPandler(*a, directory=str(docroot), **kw))
    saved = proxymod.UIPandler.upstream_port
    proxymod.UIPandler.upstream_port = up.port
    start_server(httpd)
    try:
        yield httpd.server_address[1], up, str(docroot)
    finally:
        proxymod.UIPandler.upstream_port = saved
        httpd.shutdown()
        httpd.server_close()
        up.close()


def roundtrip(port, request, timeout=6):
    conn = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    out = b""
    try:
        conn.sendall(request)
        conn.settimeout(timeout)
        while b"\r\n\r\n" not in out:
            chunk = conn.recv(65536)
            if not chunk:
                break
            out += chunk
    except socket.timeout:
        pass
    finally:
        conn.close()
    return out


def field_lines(head, name):
    """Every value of `name` in a raw request head, case-insensitively."""
    values = []
    for line in head.split("\r\n")[1:]:
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == name:
            values.append(value.strip())
    return values


# ---------------------------------------------------------------------------
# Harness B: handler-level relay inspection. http.client is swapped inside the
# proxy module only, so the assertion is about what would go on the wire, and
# client_address is the test's to choose -- which is the point: the forwarded
# address is the socket's peer, never anything the request carried.
# ---------------------------------------------------------------------------
class RecordedResponse:
    status, reason = 200, "OK"

    def getheaders(self):
        return [("Content-Type", "application/json")]

    def read(self):
        return b"{}"


class Recorder:
    def __init__(self):
        self.calls = []

    def factory(self):
        outer = self

        class _Conn:
            def __init__(self, host, port=None, *a, **kw):
                assert port not in STACK_PORTS, "live stack port %r" % (port,)
                assert str(host) == "127.0.0.1", "upstream must stay loopback"
                self.rec = {"host": host, "port": port, "requests": []}
                outer.calls.append(self.rec)

            def request(self, method, url, body, headers):
                self.rec["requests"].append(
                    {"method": method, "url": url, "body": body,
                     "items": list(headers.items())})

            def getresponse(self):
                return RecordedResponse()

            def close(self):
                pass

        return _Conn


def make_handler(mod, raw_request, peer=LAN_IP, directory="/tmp"):
    """A UIPandler fed from memory: no socket is bound or connected."""
    h = object.__new__(mod.UIPandler)
    h.directory = directory
    h.client_address = (peer, 51111)
    h.server = None
    h.connection = None
    h.close_connection = False
    h.request_version = "HTTP/1.1"
    h.rfile = io.BytesIO(raw_request)
    h.wfile = io.BytesIO()
    h.stdout = h.wfile
    h.stderr = io.BytesIO()
    return h


def relayed(mod, raw_request, peer=LAN_IP):
    """The header items the proxy would send upstream, plus the recorder."""
    rec = Recorder()
    real_http, saved = mod.http, mod.UIPandler.upstream_port
    mod.http = types.SimpleNamespace(
        client=types.SimpleNamespace(HTTPConnection=rec.factory()))
    mod.UIPandler.upstream_port = UPSTREAM_SENTINEL
    try:
        make_handler(mod, raw_request, peer).handle_one_request()
    finally:
        mod.http, mod.UIPandler.upstream_port = real_http, saved
    assert rec.calls, "the request was never relayed to the upstream"
    return rec.calls[-1]["requests"][0]["items"], rec


def request_line(path, headers=(), method="GET"):
    lines = ["%s %s HTTP/1.1" % (method, path), "Host: " + ORIGIN]
    lines += ["%s: %s" % kv for kv in headers]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


# --- the HTTP relay ---------------------------------------------------------
def test_relayed_request_carries_our_xff_from_the_accepted_socket(proxy_up):
    """In-process proxy on an ephemeral port: the relayed request must carry
    X-Forwarded-For holding the peer of the accepted socket -- here the
    loopback address the test connected from -- where before it carried
    nothing at all."""
    port, up, _docroot = proxy_up
    out = roundtrip(port, b"GET /printer/info HTTP/1.1\r\nHost: h\r\n\r\n")
    assert out.startswith(b"HTTP/1.0 200"), out[:60]
    head = up.last_head()
    assert field_lines(head, "x-forwarded-for") == ["127.0.0.1"], head
    assert field_lines(head, "x-real-ip") == ["127.0.0.1"], head


def test_client_supplied_xff_never_reaches_the_upstream(proxy_up):
    """The spoofed address is gone and exactly one XFF line -- ours -- is left.

    Before the fix this relayed `X-Forwarded-For: 8.8.8.8` verbatim, so the
    caller picked the identity Moonraker trusted.
    """
    port, up, _docroot = proxy_up
    roundtrip(port, b"GET /printer/info HTTP/1.1\r\nHost: h\r\n"
                    b"X-Forwarded-For: 8.8.8.8\r\nX-Real-Ip: 8.8.8.8\r\n\r\n")
    head = up.last_head()
    assert "8.8.8.8" not in head, head
    assert field_lines(head, "x-forwarded-for") == ["127.0.0.1"], head
    assert field_lines(head, "x-real-ip") == ["127.0.0.1"], head


def test_xff_reports_the_socket_peer_of_a_lan_caller():
    """A LAN caller must be relayed as its own address so that upstream judges
    it on its own merits instead of as loopback."""
    items, _rec = relayed(proxymod, request_line("/printer/info"))
    hdrs = {k.lower(): v for k, v in items}
    assert hdrs["x-forwarded-for"] == LAN_IP
    assert hdrs["x-real-ip"] == LAN_IP
    assert hdrs["x-forwarded-proto"] == "http"


def test_every_identity_alias_is_stripped_and_replaced():
    """X-Scheme beats X-Forwarded-Proto, and X-Real-Ip beats X-Forwarded-For,
    in upstream's own reader: leaving any alias client-writable would keep the
    loopback-trust bypass open, so the proxy owns all of them."""
    spoof = "127.0.0.1"
    items, _rec = relayed(proxymod, request_line("/printer/info", [
        ("X-Forwarded-For", spoof),
        ("X-Real-Ip", spoof),
        ("X-Forwarded-Proto", "https"),
        ("X-Scheme", "https"),
        ("Forwarded", "for=%s;by=%s" % (spoof, spoof)),
    ]))
    hdrs = {k.lower(): v for k, v in items}
    assert hdrs["x-forwarded-for"] == LAN_IP
    assert hdrs["x-real-ip"] == LAN_IP
    assert hdrs["x-forwarded-proto"] == "http", "this hop is plain HTTP"
    assert "x-scheme" not in hdrs
    assert "forwarded" not in hdrs
    for name in IDENTITY:
        values = [v for k, v in items if k.lower() == name]
        assert len(values) <= 1, (name, values)
        assert spoof not in values, (name, values)
    assert hdrs["x-forwarded-for"] != spoof
    assert hdrs["x-real-ip"] != spoof


def test_identity_headers_appear_once_even_when_the_client_repeats_them():
    """A dict built from headers.items() keeps one value per spelling; the
    proxy must not add a second copy upstream can disagree with."""
    items, _rec = relayed(proxymod, request_line("/printer/info", [
        ("X-Forwarded-For", "10.0.0.1"), ("x-forwarded-for", "10.0.0.2"),
        ("X-REAL-IP", "10.0.0.3")]))
    assert [v for k, v in items if k.lower() == "x-forwarded-for"] == [LAN_IP]
    assert [v for k, v in items if k.lower() == "x-real-ip"] == [LAN_IP]
    assert not [v for k, v in items if k.lower() == "x-real-ip"
                and v == "10.0.0.3"]


def test_forwarded_host_is_echoed_only_when_it_is_a_bare_origin():
    """X-Forwarded-Host carries the browser-facing origin (Host itself is
    dropped so http.client writes the upstream one); a Host that is not a bare
    origin is not echoed at all, so nothing can ride through it."""
    items, _rec = relayed(proxymod, request_line("/printer/info"))
    hdrs = {k.lower(): v for k, v in items}
    assert hdrs["x-forwarded-host"] == ORIGIN
    assert "host" not in hdrs, "the browser-facing Host must not be relayed"
    items2, _rec2 = relayed(proxymod,
                            b"GET /printer/info HTTP/1.1\r\n"
                            b"Host: two words here\r\n\r\n")
    hdrs2 = {k.lower(): v for k, v in items2}
    assert "x-forwarded-host" not in hdrs2
    assert hdrs2["x-forwarded-for"] == LAN_IP


def test_local_browser_is_still_trusted_by_upstream(proxy_up):
    """The fix must not lock the operator out: a browser on this Mac is still
    relayed as 127.0.0.1, which [authorization] trusts without an API key,
    while a LAN caller is now judged as itself."""
    port, up, _docroot = proxy_up
    out = roundtrip(port, b"GET /server/info HTTP/1.1\r\nHost: h\r\n\r\n")
    assert out.startswith(b"HTTP/1.0 200"), out[:60]
    assert field_lines(up.last_head(), "x-forwarded-for") == ["127.0.0.1"]


# --- the websocket handshake ------------------------------------------------
def test_websocket_handshake_replaces_the_client_xff(proxy_up):
    """/websocket is the control channel, so it takes the same rule: the
    handshake must carry this proxy's peer address, not the client's claim."""
    port, up, _docroot = proxy_up
    roundtrip(port, b"GET /websocket HTTP/1.1\r\nHost: h\r\n"
                    b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    b"Sec-WebSocket-Key: dGhlIHNhbXBsZQ==\r\n"
                    b"X-Forwarded-For: 8.8.8.8\r\nX-Real-Ip: 8.8.8.8\r\n\r\n")
    head = up.last_head()
    lines = [l.lower() for l in head.split("\r\n")]
    assert any(l.startswith("upgrade: websocket") for l in lines), head
    assert any(l.startswith("sec-websocket-key:") for l in lines), head
    assert "8.8.8.8" not in head, head
    assert field_lines(head, "x-forwarded-for") == ["127.0.0.1"], head
    assert field_lines(head, "x-real-ip") == ["127.0.0.1"], head


def test_websocket_handshake_reports_the_socket_peer(monkeypatch):
    """Same rule with a LAN peer, which only the handler-level harness can
    express: the address written into the tunnelled handshake is
    client_address', and the framing headers still travel."""
    from http import client as http_client

    sent_to_upstream = []
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    dialer = socket.socket()
    dialer.connect(listener.getsockname())
    accepted, _addr = listener.accept()
    client_a, client_b = socket.socketpair()
    try:
        # the 101 is queued first, so the handshake read cannot block
        accepted.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                         b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                         b"Sec-WebSocket-Accept: s\r\n\r\n")

        class _Tunnel:
            def __init__(self, sock, sink=None):
                self._sock = sock
                self._sink = sink

            def sendall(self, data):
                if self._sink is not None:
                    self._sink.append(data)
                self._sock.sendall(data)

            def recv(self, size):
                return self._sock.recv(size)

            def setblocking(self, flag):
                self._sock.setblocking(flag)

            def fileno(self):
                return self._sock.fileno()

            def close(self):
                pass                    # the test owns the lifetime

        monkeypatch.setattr(proxymod, "socket", types.SimpleNamespace(
            create_connection=lambda *a, **kw: _Tunnel(dialer,
                                                       sent_to_upstream),
            error=socket.error))
        monkeypatch.setattr(proxymod, "select", types.SimpleNamespace(
            select=lambda *a, **kw: ([], [], [])))
        h = make_handler(proxymod, b"")
        h.connection = _Tunnel(client_a)
        h.command, h.path = "GET", "/websocket"
        h.headers = http_client.parse_headers(io.BytesIO(
            b"Host: " + ORIGIN.encode("latin-1") + b"\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZQ==\r\n"
            b"X-Forwarded-For: 8.8.8.8\r\nX-Real-Ip: 8.8.8.8\r\n\r\n"))
        proxymod.UIPandler._proxy_ws(h)
    finally:
        for s in (accepted, dialer, listener, client_a, client_b):
            try:
                s.close()
            except OSError:
                pass
    assert sent_to_upstream, "the handshake never reached the upstream"
    head = sent_to_upstream[0].split(b"\r\n\r\n", 1)[0].decode("latin-1")
    assert field_lines(head, "x-forwarded-for") == [LAN_IP], head
    assert field_lines(head, "x-real-ip") == [LAN_IP], head
    assert field_lines(head, "x-forwarded-proto") == ["http"], head
    assert any(l.lower().startswith("upgrade: websocket")
               for l in head.split("\r\n")), head
    assert any(l.lower().startswith("sec-websocket-key:")
               for l in head.split("\r\n")), head
    assert "8.8.8.8" not in head, head


# --- the static half is untouched -------------------------------------------
def test_static_document_root_request_never_opens_an_upstream_connection(
        tmp_path):
    """Adding identity headers must not turn the file server into a proxy: a
    document-root path is answered locally, exactly as before."""
    docroot = tmp_path / "ui"
    docroot.mkdir()
    (docroot / "app.js").write_text("//asset\n")
    rec = Recorder()
    real_http, saved = proxymod.http, proxymod.UIPandler.upstream_port
    proxymod.http = types.SimpleNamespace(
        client=types.SimpleNamespace(HTTPConnection=rec.factory()))
    proxymod.UIPandler.upstream_port = UPSTREAM_SENTINEL
    try:
        h = make_handler(proxymod, request_line("/app.js"),
                         directory=str(docroot))
        h.handle_one_request()
        out = h.wfile.getvalue()
    finally:
        proxymod.http, proxymod.UIPandler.upstream_port = real_http, saved
    assert out.startswith(b"HTTP/1.0 200"), out[:60]
    assert not rec.calls, "a static path must never reach the upstream"
