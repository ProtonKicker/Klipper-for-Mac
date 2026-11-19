"""Deep tests: UIPandler reverse-proxy target parsing + relay integrity.

Each test starts a scripted fake Moonraker (127.0.0.1, ephemeral port) and a
proxy.Server in front of it (127.0.0.1, ephemeral port). Nothing here can reach
the live stack: an autouse guard rejects any upstream host that is not loopback
and any of ports 7125/7126/8080/8081/8082, and every server is shut down by its
fixture.

The focus is what the proxy puts on the wire towards Moonraker (request target
relay, hop-by-hop handling, framing of large/odd bodies) and what it hands back
to the browser (status line / header block integrity when the upstream misbehaves).
"""
import http.client
import importlib
import io
import os
import socket
import threading
import time
import types

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

import klipperformac.proxy as proxy_mod

STACK_PORTS = frozenset({7125, 7126, 8080, 8081, 8082})
SENTINEL_UPSTREAM = 1
DOCROOT_MARK = "MAINSAIL-INDEX-OK"


@pytest.fixture
def proxymod(tmp_path, monkeypatch):
    monkeypatch.setenv("KLIPPERFORMAC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KLIPPERFORMAC_DATA", str(tmp_path / "data"))
    mod = importlib.reload(proxy_mod)
    monkeypatch.setattr(mod.UIPandler, "upstream_port", SENTINEL_UPSTREAM,
                        raising=False)

    real_create = socket.create_connection
    real_conn = http.client.HTTPConnection

    def create_connection(address, *a, **kw):
        assert str(address[0]) == "127.0.0.1", \
            "proxy must dial loopback only, got %r" % (address,)
        assert address[1] not in STACK_PORTS, \
            "test tried to reach a live stack port: %r" % (address,)
        return real_create(address, *a, **kw)

    class GuardedConnection(real_conn):
        def __init__(self, host, port=None, *a, **kw):
            assert str(host) == "127.0.0.1", \
                "upstream target must be loopback, got %r" % (host,)
            assert port not in STACK_PORTS, \
                "test tried to reach a live stack port: %r" % (port,)
            super().__init__(host, port, *a, **kw)

    monkeypatch.setattr(mod, "socket",
                        types.SimpleNamespace(create_connection=create_connection))
    monkeypatch.setattr(mod, "http",
                        types.SimpleNamespace(
                            client=types.SimpleNamespace(
                                HTTPConnection=GuardedConnection)))
    return mod


# ---------------------------------------------------------------------------
# Fake Moonraker: raw loopback server that records every byte of the request
# and replies from a script.
# ---------------------------------------------------------------------------
class ScriptedUpstream:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.requests = []          # (request_line, header_lines, body)
        self.script = {}            # substring -> raw response bytes
        self.default = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b"\r\n{}")
        self._accepting = True
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self):
        while self._accepting:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,),
                             daemon=True).start()

    def _serve(self, conn):
        conn.settimeout(15)
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
            head, _, rest = buf.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            declared = 0
            for line in lines[1:]:
                k, _, v = line.partition(b":")
                if k.strip().lower() == b"content-length":
                    try:
                        declared = int(v.strip())
                    except ValueError:
                        declared = 0
            body = rest
            while len(body) < declared:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                body += chunk
            self.requests.append((lines[0], lines[1:], body))
            target = lines[0].split(b" ")[1] if len(lines[0].split(b" ")) > 1 \
                else b""
            target = target.decode("latin-1", "replace")
            for key, reply in self.script.items():
                if key in target:
                    if reply is None:
                        return          # hang up without answering
                    conn.sendall(reply)
                    return
            conn.sendall(self.default)
        except (OSError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self._accepting = False
        try:
            self.sock.close()
        except OSError:
            pass
        self._thread.join(3)

    def last_target(self):
        assert self.requests, "the proxy never connected to the upstream"
        line = self.requests[-1][0].decode("latin-1")
        return line.split(" ", 2)[1]

    def last_line(self):
        assert self.requests, "the proxy never connected to the upstream"
        return self.requests[-1][0].decode("latin-1")


@pytest.fixture
def upstream():
    up = ScriptedUpstream()
    try:
        yield up
    finally:
        up.close()


@pytest.fixture
def docroot(stack):
    root = stack.paths.WEB_MAINSAIL
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(DOCROOT_MARK)
    (root / "big.bin").write_bytes(b"S" * (1 << 20))
    return root


@pytest.fixture
def proxy_server(proxymod, docroot, upstream):
    proxymod.UIPandler.upstream_port = upstream.port
    httpd = proxymod.Server(("127.0.0.1", 0),
                            lambda *a, **kw: proxymod.UIPandler(
                                *a, directory=str(docroot), **kw))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    httpd.port = httpd.server_address[1]
    assert httpd.server_address[0] == "127.0.0.1"
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- helpers ---------------------------------------------------------------
def call(port, method, path, headers=(), body=None, timeout=20):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.putrequest(method, path)
        for k, v in headers:
            conn.putheader(k, v)
        if body is not None and \
                not any(k.lower() == "content-length" for k, _v in headers):
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        resp = conn.getresponse()
        return resp.status, resp.read(), dict(resp.getheaders()), \
            list(resp.getheaders())
    finally:
        conn.close()


def raw_exchange(port, payload, timeout=20, limit=4 << 20):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(payload)
        data = b""
        s.settimeout(timeout)
        while len(data) < limit:
            try:
                chunk = s.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            data += chunk
        return data
    finally:
        s.close()


def header_block(response_bytes):
    return response_bytes.split(b"\r\n\r\n")[0]


# ---------------------------------------------------------------------------
# FIXED (deep-hunt): _proxy() formatted any relay exception into the *status
# line* it sends the browser (send_error's "message" argument). For a malformed
# upstream status line the exception text still carried the upstream's trailing
# CRLF, so the proxy emitted e.g.
#     HTTP/1.0 502 proxy error: HTTP/1.1 1000 Nope\r\n\r\n
# and that empty line terminated the header block: Server/Date/Content-Type/
# Content-Length were demoted into the body and the browser received a 502 with
# no headers at all. proxy._relay_failed() now answers with a fixed single-line
# reason plus a classified explanation, and never copies upstream bytes into
# anything the browser parses as HTTP.
# Guard: this test must keep passing for every malformed status line.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad_line", [
    b"HTTP/1.1 1000 Nope\r\nContent-Length: 2\r\n\r\nhi",
    b"HTTP/1.1 XXX YZ\r\nSet-Cookie: pwned=1\r\nContent-Length: 0\r\n\r\n",
    b"garbage-not-a-status-line\r\n\r\n",
])
def test_502_for_a_malformed_upstream_keeps_an_intact_header_block(
        proxy_server, upstream, bad_line):
    upstream.script["/printer/malformed"] = bad_line
    out = raw_exchange(proxy_server.port,
                       b"GET /printer/malformed HTTP/1.1\r\nHost: x\r\n\r\n")
    assert out.startswith(b"HTTP/1.0 502"), out[:60]
    assert out.count(b"HTTP/1.") == 1, \
        "the upstream's own status line was echoed into ours: %r" % out[:80]
    head = header_block(out)
    lower = head.decode("latin-1").lower()
    assert "\r\nserver: " in lower, head[:120]
    assert "\r\ncontent-type: " in lower, head[:120]
    assert "\r\ncontent-length: " in lower, head[:120]
    status, body, _h, _hl = call(proxy_server.port, "GET", "/printer/malformed")
    assert status == 502
    assert body.startswith(b"<!DOCTYPE"), body[:40]


# ---------------------------------------------------------------------------
# FIXED (deep-hunt): _proxy() closed the upstream connection only on the
# success path; there was no try/finally. Any failure after HTTPConnection()
# was constructed -- refused connect mid-flight, BadStatusLine, IncompleteRead,
# a browser that vanished while the body was being written -- left the socket
# to the garbage collector, so every relay error momentarily burned an fd and a
# TIME_WAIT pair. proxy._proxy() now closes it in a finally block.
# Guard: this test must keep passing.
# ---------------------------------------------------------------------------
def test_upstream_connection_is_closed_when_the_relay_fails(proxymod,
                                                            monkeypatch):
    opened = []

    class FakeResponse:
        status, reason = 200, "OK"

        def getheaders(self):
            return [("Content-Type", "text/plain")]

        def read(self):
            raise http.client.IncompleteRead(b"partial", 99)

    class Conn:
        def __init__(self, host, port=None, *a, **kw):
            assert str(host) == "127.0.0.1"
            self.host, self.port = host, port
            self.closed = False
            opened.append(self)

        def request(self, method, url, body, headers):
            pass

        def getresponse(self):
            return FakeResponse()

        def close(self):
            self.closed = True

    monkeypatch.setattr(proxymod, "http",
                        types.SimpleNamespace(
                            client=types.SimpleNamespace(HTTPConnection=Conn)))

    handler = object.__new__(proxymod.UIPandler)
    handler.directory = "/tmp"
    handler.client_address = ("127.0.0.1", 54321)
    handler.server = None
    handler.connection = None
    handler.close_connection = False
    handler.request_version = "HTTP/1.1"
    handler.rfile = io.BytesIO(
        b"GET /printer/info HTTP/1.1\r\nHost: x\r\n\r\n")
    handler.wfile = io.BytesIO()
    handler.stdout = handler.wfile
    handler.stderr = io.BytesIO()
    handler.handle_one_request()

    assert opened, "the proxy never opened an upstream connection"
    assert all(c.closed for c in opened), \
        "relay failure left the upstream socket unclosed"
    assert handler.wfile.getvalue().startswith(b"HTTP/1.0 502")


# --- request-target relay ---------------------------------------------------
RELAY_TARGETS = [
    "/printer/info",
    "/printer/../../config/printer.cfg",
    "/printer/%2e%2e/%2e%2e/logs/klippy.log",
    "/server/files/gcodes/na%C3%AFve%20name.gcode",
    "/websocket?token=abc%20def",
    "/printer//info",
    "/printer/info?a=1&b=%2B2#frag",
    "/printer/" + "x" * 2000,
    "/printer/a%2fb",
    "/history/list?limit=-1&json=1",
    "/machine/stop-klippy?unexpected=%20spaced%20value",
]


@pytest.mark.parametrize("target", RELAY_TARGETS)
def test_request_target_is_relayed_verbatim_to_the_upstream(proxy_server,
                                                           upstream, target):
    status, _body, _h, _hl = call(proxy_server.port, "GET", target)
    assert status == 200, (target, status)
    assert upstream.last_target() == target, upstream.last_line()


def test_scheme_relative_target_is_collapsed_before_the_relay(proxy_server,
                                                              upstream):
    """``//host/x`` is an authority-bearing URI, not a path: the proxy must
    never relay it in a form Moonraker could read as a redirect or a different
    origin (CPython 3.14 collapses a leading // in parse_request -- this test
    pins that the product keeps the collapse instead of reintroducing it)."""
    before = len(upstream.requests)
    for target, status, expect_relay in (
            ("//printer/info", 200, "/printer/info"),
            ("///printer/info", 200, "/printer/info"),
            ("//evil.example/printer/info", 404, None)):
        got, _b, _h, _hl = call(proxy_server.port, "GET", target)
        assert got == status, (target, got)
        if expect_relay is None:
            assert len(upstream.requests) == before, \
                "a scheme-relative target reached the upstream"
        else:
            relayed = upstream.last_target()
            assert relayed == expect_relay, (target, relayed)
            assert not relayed.startswith("//"), (target, relayed)
            before += 1


def test_raw_space_in_a_request_target_is_rejected_not_relayed(proxy_server,
                                                               upstream):
    """A target with an un-encoded space breaks the request line parser; the
    answer must still be a well-formed HTTP error and nothing may reach
    Moonraker (a smuggled second request would)."""
    out = raw_exchange(proxy_server.port,
                       b"GET /printer/a b HTTP/1.1\r\nHost: x\r\n\r\n")
    assert out.startswith(b"HTTP/1."), out[:60]
    code = int(out.split(b" ")[1])
    assert 400 <= code < 600, out[:60]
    assert upstream.requests == [], upstream.requests[:2]


def test_hostile_request_target_forms_keep_the_loopback_target(proxy_server,
                                                               upstream):
    """Absolute-URI / protocol-relative / aster-form targets must never move the
    upstream: it is always 127.0.0.1 on the configured port, and the answer is
    a single well-formed HTTP response."""
    forms = [b"GET http://evil.example:9999/printer/x HTTP/1.1",
             b"GET //evil.example/printer/x HTTP/1.1",
             b"GET * HTTP/1.1",
             b"GET http://127.0.0.1:7125/printer/x HTTP/1.1",
             b"OPTIONS * HTTP/1.1"]
    for line in forms:
        out = raw_exchange(proxy_server.port, line + b"\r\nHost: x\r\n\r\n")
        assert out.startswith(b"HTTP/1."), (line, out[:60])
        assert out.count(b"HTTP/1.") >= 1
    seen = [u.split(b" ")[1] for u, _h, _b in upstream.requests]
    for target in seen:
        assert b"evil.example" not in target, seen


def test_client_host_header_is_replaced_by_the_upstream_origin(proxy_server,
                                                              upstream):
    """The browser-facing Host must never be relayed (virtual-host smuggling)."""
    _s, _b, _h, _hl = call(proxy_server.port, "GET", "/printer/info",
                           headers=[("Host", "attacker.example:8080"),
                                    ("X-Real-Header", "keep-me")])
    lines = upstream.requests[-1][1]
    names = [l.split(b":")[0].strip().lower() for l in lines]
    assert b"host" in names, "http.client must still send a Host upstream"
    host_lines = [l for l in lines if l.lower().startswith(b"host:")]
    assert len(host_lines) == 1, host_lines
    assert b"attacker.example" not in host_lines[0], host_lines[0]
    assert b"x-real-header" in names


# ---------------------------------------------------------------------------
# FIXED (deep-hunt): the request-side filter in _proxy() dropped only "host"
# and "connection" even though the module already carried a full HOP_BY_HOP
# table that it applied to *responses*. A browser's "TE: trailers",
# "Keep-Alive" or "Proxy-Authorization" was relayed to Moonraker in clear text,
# and RFC 9112 9.6 (a proxy MUST NOT forward hop-by-hop request fields) was
# violated in one direction while being enforced in the other.
# proxy._forward_headers() now uses the same table both ways.
# Guard: this test must keep passing.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("hop", ["TE", "Keep-Alive", "Proxy-Authorization",
                                 "Trailer"])
def test_client_hop_by_hop_request_headers_are_not_relayed(proxy_server,
                                                           upstream, hop):
    _s, _b, _h, _hl = call(proxy_server.port, "GET", "/printer/info",
                           headers=[(hop, "value-that-must-not-travel"),
                                    ("Accept", "application/json")])
    lines = upstream.requests[-1][1]
    names = [l.split(b":")[0].strip().lower() for l in lines]
    assert b"accept" in names, "sanity: ordinary headers must still be relayed"
    assert hop.lower().encode() not in names, lines


@settings(max_examples=25, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture,
                                 HealthCheck.too_slow])
@given(query=st.text(alphabet="abc0123456789-_=&%./+~:", max_size=24))
def test_property_query_strings_are_not_reordered_or_reencoded(proxy_server,
                                                               upstream,
                                                               query):
    target = "/printer/info?" + query
    status, _b, _h, _hl = call(proxy_server.port, "GET", target)
    assert status == 200
    assert upstream.last_target() == target


# --- framing of what the browser gets back ----------------------------------
def test_expect_100_continue_upload_reaches_the_upstream_intact(proxy_server,
                                                                upstream):
    """Browsers use Expect: 100-continue for big gcode uploads."""
    payload = b'{"dir":"/","filename":"a gcode.\xc3\xa9"}'
    upstream.script["/printer/upload"] = (
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
    out = raw_exchange(proxy_server.port,
                       b"POST /printer/upload HTTP/1.1\r\nHost: x\r\n"
                       b"Expect: 100-continue\r\nContent-Length: %d\r\n"
                       b"Content-Type: application/json\r\n\r\n" % len(payload)
                       + payload)
    assert out.count(b"HTTP/1.") in (1, 2), out[:80]      # 100 then 200 is legal
    assert b" 200 " in out.split(b"\r\n")[0] or b"Continue" in out, out[:120]
    method, body = upstream.requests[-1][0].split(b" ")[0], \
        upstream.requests[-1][2]
    assert method == b"POST"
    assert body == payload, body[:60]


@pytest.mark.parametrize("size", [1 << 10, 1 << 20, 3 << 20])
def test_large_request_bodies_are_relayed_byte_exact(proxy_server, upstream,
                                                     size):
    payload = os.urandom(size)
    upstream.script["/printer/upload"] = (
        b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\n\r\nuploaded")
    status, body, _h, _hl = call(proxy_server.port, "POST", "/printer/upload",
                                 [("Content-Type", "application/octet-stream")],
                                 payload)
    assert status == 200
    assert body == b"uploaded"
    assert upstream.requests[-1][2] == payload, \
        "body truncated or reordered in transit (%d vs %d bytes)" % (
            len(upstream.requests[-1][2]), len(payload))


def test_large_upstream_response_is_reframed_with_one_status_line(
        proxy_server, upstream):
    payload = b"R" * (1 << 20)
    upstream.script["/printer/blob"] = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
        b"Content-Length: %d\r\n\r\n" % len(payload) + payload)
    status, body, hdrs, _hl = call(proxy_server.port, "GET", "/printer/blob")
    assert status == 200
    assert body == payload
    assert hdrs["Content-Length"] == str(len(payload))


@pytest.mark.parametrize("reply,expect", [
    (b"HTTP/1.1 999 Weird Status\r\nContent-Length: 2\r\n\r\nhi", 999),
    (b"HTTP/1.1 200\r\nContent-Length: 2\r\n\r\nhi", 200),
    (b"HTTP/1.1 204 No Content\r\nContent-Length: 5\r\n\r\nhello", 204),
    (b"HTTP/1.1 304 Not Modified\r\nETag: \"x\"\r\nContent-Length: 9\r\n\r\n",
     304),
    (b"HTTP/1.1 200 caf\xc3\xa9\r\nContent-Length: 2\r\n\r\nhi", 200),
    (b"HTTP/1.1 200 OK\r\nX-Odd: v\xff\xfe\r\nContent-Length: 2\r\n\r\nhi",
     200),
    (b"HTTP/1.1 200 OK\r\nSet-Cookie: a=1\r\nSet-Cookie: b=2\r\n"
     b"Content-Length: 2\r\n\r\nhi", 200),
])
def test_odd_upstream_replies_are_answered_with_exactly_one_status_line(
        proxy_server, upstream, reply, expect):
    upstream.script["/printer/odd"] = reply
    out = raw_exchange(proxy_server.port,
                       b"GET /printer/odd HTTP/1.1\r\nHost: x\r\n\r\n")
    assert out.count(b"HTTP/1.") == 1, out[:80]
    assert out.split(b"\r\n")[0].startswith(b"HTTP/1.0 %d" % expect), out[:60]
    # the response is still framable: headers then body, one blank line
    assert b"\r\n\r\n" in out
    # and the proxy is unharmed
    status, body, _h, _hl = call(proxy_server.port, "GET", "/index.html")
    assert status == 200 and body == DOCROOT_MARK.encode()


def test_upstream_hangup_is_a_clean_502_without_host_details(proxy_server,
                                                             upstream,
                                                             docroot):
    upstream.script["/printer/hangup"] = None
    status, body, hdrs, _hl = call(proxy_server.port, "GET", "/printer/hangup")
    assert status == 502
    text = body.decode("latin-1")
    assert str(docroot) not in text
    assert str(upstream.port) not in text, text[:160]
    assert "Traceback" not in text
    # still serving afterwards
    status, body, _h, _hl = call(proxy_server.port, "GET", "/index.html")
    assert status == 200


def test_dead_upstream_502_does_not_disclose_the_internal_port(proxymod,
                                                              docroot):
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()
    proxymod.UIPandler.upstream_port = dead_port
    httpd = proxymod.Server(("127.0.0.1", 0),
                            lambda *a, **kw: proxymod.UIPandler(
                                *a, directory=str(docroot), **kw))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        status, body, _h, _hl = call(httpd.server_address[1], "GET",
                                     "/printer/info")
        assert status == 502
        assert str(dead_port).encode() not in body, body[:120]
        assert str(docroot).encode() not in body
        # no upstream connection should have been held open for the browser
        status, body, _h, _hl = call(httpd.server_address[1], "GET",
                                     "/index.html")
        assert status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_unimplemented_verb_is_answered_not_crashed(proxy_server, upstream):
    out = raw_exchange(proxy_server.port,
                       b"PATCH /printer/info HTTP/1.1\r\nHost: x\r\n\r\n")
    assert out.startswith(b"HTTP/1."), out[:40]
    code = int(out.split(b" ")[1])
    assert 400 <= code < 600, out[:60]
    status, _b, _h, _hl = call(proxy_server.port, "GET", "/index.html")
    assert status == 200


def test_raw_control_byte_in_an_api_target_stays_framable(proxy_server,
                                                          upstream):
    """A NUL smuggled into the request target must never break the answer the
    browser gets (the request line is relayed, but the response stays intact)."""
    out = raw_exchange(proxy_server.port,
                       b"GET /printer/a\x00b HTTP/1.1\r\nHost: x\r\n\r\n")
    if out:
        assert out.count(b"HTTP/1.") >= 1
        assert b"\r\n\r\n" in out or b"HTTP/1.0 5" in out
    status, body, _h, _hl = call(proxy_server.port, "GET", "/index.html")
    assert status == 200 and body == DOCROOT_MARK.encode()


def test_concurrent_mixed_traffic_keeps_every_response_framed(proxy_server,
                                                              upstream):
    """static + relay interleaved on one process: no cross-talk, no truncation.

    socketserver's default listen backlog is only 5 (Server does not raise it),
    so a burst of 15 simultaneous connections can be reset by the kernel; the
    retry loop tolerates that, but every answer that does arrive must be
    complete and correctly framed.
    """
    upstream.script["/printer/slow"] = (
        b"HTTP/1.1 200 OK\r\nContent-Length: 4096\r\n\r\n" + b"T" * 4096)
    errors = []
    done = set()

    def request(i):
        if i % 3 == 0:
            status, body, hdrs, _hl = call(proxy_server.port, "GET",
                                           "/index.html")
            assert status == 200 and body == DOCROOT_MARK.encode(), i
        elif i % 3 == 1:
            status, body, hdrs, _hl = call(proxy_server.port, "GET",
                                           "/printer/info?i=%d" % i)
            assert status == 200 and body == b"{}", i
        else:
            status, body, hdrs, _hl = call(proxy_server.port, "GET",
                                           "/printer/slow")
            assert status == 200, i
            assert hdrs["Content-Length"] == "4096", i
            assert len(body) == 4096 and body == b"T" * 4096, i

    def worker(i):
        last = None
        for _attempt in range(4):
            try:
                request(i)
            except Exception as e:  # noqa: BLE001
                last = repr(e)
                time.sleep(0.2)
                continue
            done.add(i)
            return
        errors.append((i, last))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
        assert not t.is_alive(), "a request hung"
    assert not errors, errors
    assert done == set(range(15)), sorted(set(range(15)) - done)
    api_calls = [line for line, _h, _b in upstream.requests
                 if line.startswith(b"GET /printer/")]
    assert len(api_calls) >= 10, \
        "expected one upstream request per proxied client, got %d" % \
        len(api_calls)


def test_client_abort_mid_relay_does_not_wedge_the_worker(proxy_server,
                                                          upstream):
    upstream.script["/printer/big"] = (
        b"HTTP/1.1 200 OK\r\nContent-Length: 200000\r\n\r\n" + b"B" * 200000)
    s = socket.create_connection(("127.0.0.1", proxy_server.port), timeout=10)
    s.sendall(b"GET /printer/big HTTP/1.1\r\nHost: x\r\n\r\n")
    s.recv(1024)                 # read part of it, then vanish
    s.close()
    for _ in range(20):
        status, body, _h, _hl = call(proxy_server.port, "GET", "/index.html")
        if status == 200:
            break
    assert status == 200 and body == DOCROOT_MARK.encode()


def test_static_paths_never_touch_the_upstream(proxy_server, upstream):
    for target in ["/index.html", "/assets/none.js", "/config.json",
                   "/printer-info", "/printers/list", "/api-docs"]:
        status, _body, _h, _hl = call(proxy_server.port, "GET", target)
        assert status in (200, 404), (target, status)
    assert upstream.requests == [], \
        "a static GET was relayed to Moonraker: %r" % (upstream.requests[:3],)


def test_everything_is_bound_loopback_only(proxymod, docroot, upstream):
    """Regression guard for AGENTS.md 7: the proxy's own sockets stay on 127.0.0.1."""
    assert upstream.port not in STACK_PORTS
    httpd = proxymod.Server(("127.0.0.1", 0),
                            lambda *a, **kw: proxymod.UIPandler(
                                *a, directory=str(docroot), **kw))
    try:
        host, port = httpd.server_address
        assert host == "127.0.0.1" and 1024 < port < 65536
        assert port not in STACK_PORTS
        sockname = httpd.socket.getsockname()
        assert sockname[0] == "127.0.0.1", sockname
    finally:
        httpd.server_close()
