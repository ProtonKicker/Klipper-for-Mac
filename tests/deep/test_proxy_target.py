"""Deep tests: UIPandler reverse-proxy target parsing and request relaying.

No sockets are opened for the request-construction tests: http.client is
replaced with a recorder inside the proxy module only, so every assertion is
about what the proxy would put on the wire towards Moonraker. An autouse guard
makes any attempt to reach the live stack ports fail loudly.
"""
import importlib
import io
import json
import types

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

import klipperformac.proxy as proxy_mod

# Function-scoped fixtures (the reloaded proxy module, the recorder) are
# deliberately shared across generated examples: they hold no per-example state.
HS = dict(deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture,
                                 HealthCheck.too_slow])

STACK_PORTS = frozenset({7125, 7126, 8080, 8081})
UPSTREAM_SENTINEL = 1


@pytest.fixture
def proxymod(tmp_path, monkeypatch):
    monkeypatch.setenv("KLIPPERFORMAC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KLIPPERFORMAC_DATA", str(tmp_path / "data"))
    mod = importlib.reload(proxy_mod)
    monkeypatch.setattr(mod.UIPandler, "upstream_port", UPSTREAM_SENTINEL,
                        raising=False)
    return mod


class RecordedResponse:
    def __init__(self, status=200, reason="OK", headers=None, body=b"{}"):
        self.status, self.reason = status, reason
        self.headers = headers if headers is not None else [
            ("Content-Type", "application/json")]
        self._body = body

    def getheaders(self):
        return list(self.headers)

    def read(self):
        return self._body


class Recorder:
    """Stand-in for http.client.HTTPConnection: records, never connects."""

    def __init__(self, response=None, raise_on_request=None):
        self.calls = []
        self.closed = 0
        self.response = response or RecordedResponse()
        self.raise_on_request = raise_on_request

    def factory(self):
        outer = self

        class _Conn:
            def __init__(self, host, port=None, *a, **kw):
                assert port not in STACK_PORTS, \
                    "refused to touch live stack port %r" % (port,)
                assert str(host) == "127.0.0.1", \
                    "upstream target must always be loopback, got %r" % (host,)
                self.rec = {"host": host, "port": port,
                            "timeout": kw.get("timeout"), "requests": []}
                outer.calls.append(self.rec)

            def request(self, method, url, body, headers):
                if outer.raise_on_request is not None:
                    raise outer.raise_on_request
                self.rec["requests"].append(
                    {"method": method, "url": url, "body": body,
                     "headers": dict(headers),
                     "header_items": list(headers.items())})

            def getresponse(self):
                return outer.response

            def close(self):
                outer.closed += 1

        return _Conn


@pytest.fixture
def recorder(proxymod, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(proxymod, "http",
                        types.SimpleNamespace(
                            client=types.SimpleNamespace(
                                HTTPConnection=rec.factory())))
    return rec


# --- harness: drive the handler with a byte-exact request, no sockets -------
def handle(proxymod, raw_request, docroot="/tmp"):
    """Feed exactly one request to UIPandler; return (handler, response bytes)."""
    h = object.__new__(proxymod.UIPandler)
    h.directory = docroot
    h.client_address = ("127.0.0.1", 54321)
    h.server = None
    h.connection = None
    h.close_connection = False
    h.request_version = "HTTP/1.1"
    h.rfile = io.BytesIO(raw_request)
    h.wfile = io.BytesIO()
    h.stdout = h.wfile
    h.stderr = io.BytesIO()
    h.handle_one_request()
    return h, h.wfile.getvalue()


def request_line(path, method="GET", headers=()):
    lines = ["%s %s HTTP/1.1" % (method, path), "Host: 127.0.0.1:8080"]
    lines += ["%s: %s" % (k, v) for k, v in headers]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def forwarded(calls):
    assert calls, "proxy did not open a connection to any upstream"
    return calls[-1]["requests"][0]


# --- is_api: which requests are treated as Moonraker API paths --------------
@pytest.mark.parametrize("path,expect", [
    ("/server/printer/info", True),
    ("/printer/info", True),
    ("/printer", True),
    ("/access/whoami", True),
    ("/machine/info", True),
    ("/job_queue/status", True),
    ("/history/list?limit=10", True),
    ("/update/status", True),
    ("/data_store/list", True),
    ("/webcam/index.m3u8", True),
    ("/extensions/list", True),
    ("/api/v1/printerobjects", True),
    ("/websocket", True),
    ("/moonraker/x", True),
    ("/server?x=1", True),
    ("/", False),
    ("/index.html", False),
    ("/assets/app.js", False),
    ("/config.json", False),
    ("/favicon.ico", False),
    # the prefix must match a whole first segment, never a substring
    ("/serverlog.txt", False),
    ("/printers.x", False),
    ("/apiv1", False),
    ("/webcams", False),
    ("/extensionsx", False),
    ("/job_queueish", False),
    # traversal must not smuggle an API path past the classifier
    ("/%2e%2e/server/info", False),
    ("/../server/info", False),
    ("/./server/info", False),
    ("/x/server/info", False),
    ("/%2fserver/info", False),
    # case sensitive on purpose: Moonraker's router is case sensitive
    ("/Printer/info", False),
    ("/WEBSOCKET", False),
])
def test_is_api_classification(proxymod, path, expect):
    h = object.__new__(proxymod.UIPandler)
    h.path = path
    assert proxymod.UIPandler.is_api(h) is expect, path


def test_is_api_is_stable_across_repeated_calls(proxymod):
    h = object.__new__(proxymod.UIPandler)
    h.path = "/printer/info"
    first = proxymod.UIPandler.is_api(h)
    assert all(proxymod.UIPandler.is_api(h) == first for _ in range(50))
    assert h.path == "/printer/info", "is_api must not mutate the request path"


@settings(max_examples=120, **HS)
@given(prefix=st.sampled_from(list(proxy_mod.API_PREFIXES)),
       rest=st.text(max_size=14))
def test_property_api_prefix_is_always_proxied(proxymod, prefix, rest):
    h = object.__new__(proxymod.UIPandler)
    h.path = "/" + prefix + "/" + rest
    assert proxymod.UIPandler.is_api(h) is True


@settings(max_examples=120, **HS)
@given(first=st.text(alphabet=st.characters(exclude_characters="/?#"),
                     min_size=1, max_size=14).filter(
    lambda s: s not in proxy_mod.API_PREFIXES))
def test_property_otherwise_first_segment_is_static(proxymod, first):
    h = object.__new__(proxymod.UIPandler)
    h.path = "/" + first + "/x"
    assert proxymod.UIPandler.is_api(h) is False


# --- target selection: the upstream is always loopback + configured port ----
def test_target_is_always_loopback_upstream(proxymod, recorder):
    proxymod.UIPandler.upstream_port = 4321
    for path in ["/printer/info", "/server/printer/info", "/api/x"]:
        handle(proxymod, request_line(path))
    calls = recorder.calls
    assert len(calls) == 3
    assert all(c["port"] == 4321 for c in calls)
    assert recorder.closed == len(calls), "upstream connections must be closed"


@pytest.mark.parametrize("path", [
    "http://evil.example:9999/printer/info",
    "//evil.example/printer/info",
    "http:127.0.0.1:9999/printer/info",
    "*"], ids=["absolute-uri", "protocol-relative", "host-like", "star"])
def test_attacker_controlled_uri_form_never_changes_target(proxymod, recorder,
                                                           path):
    proxymod.UIPandler.upstream_port = 4321
    h, out = handle(proxymod, request_line(path, method="POST",
                                           headers=[("Content-Length", "0")]))
    call = recorder.calls[0]
    assert (call["host"], call["port"]) == ("127.0.0.1", 4321)
    # Whatever odd request-target form the client picks, only the parsed path
    # is relayed and the connection target never moves (Recorder asserts any
    # non-loopback host or live stack port outright).
    assert forwarded(recorder.calls)["url"] == h.path


@pytest.mark.parametrize("host_header", ["evil.test", "localhost",
                                         "\u00e9xample", "127.0.0.1:9"])
def test_client_host_header_is_not_relayed(proxymod, recorder, host_header):
    proxymod.UIPandler.upstream_port = 4321
    raw = ("GET /printer/info HTTP/1.1\r\nHost: %s\r\n\r\n"
           % host_header).encode("latin-1")
    handle(proxymod, raw)
    keys = {k.lower() for k, _ in forwarded(recorder.calls)["header_items"]}
    assert "host" not in keys, \
        "the browser-facing Host must not be relayed to Moonraker"


def test_credentials_and_cookies_are_relayed(proxymod, recorder):
    proxymod.UIPandler.upstream_port = 4321
    handle(proxymod, request_line("/printer/info", headers=[
        ("Cookie", "session=abc"), ("Authorization", "Bearer t"),
        ("X-Forwarded-For", "10.0.0.1")]))
    hdrs = {k.lower(): v for k, v in forwarded(recorder.calls)["header_items"]}
    assert hdrs["cookie"] == "session=abc"
    assert hdrs["authorization"] == "Bearer t"


def test_connection_header_dropped_but_others_kept(proxymod, recorder):
    proxymod.UIPandler.upstream_port = 4321
    handle(proxymod, request_line("/printer/info", headers=[
        ("Connection", "keep-alive"), ("Accept", "*/*")]))
    keys = {k.lower() for k, _ in forwarded(recorder.calls)["header_items"]}
    assert "connection" not in keys and "accept" in keys


@pytest.mark.parametrize("body", [
    b"", b"{}", '{"file":"a gcode.\u00e9"}'.encode("utf-8"),
    bytes(range(256)) * 40, b"x" * 1024 * 1024],
    ids=["empty", "json", "utf8", "binary", "1MB"])
def test_request_body_is_relayed_byte_exact(proxymod, recorder, body):
    proxymod.UIPandler.upstream_port = 4321
    head = ("POST /printer/print/start HTTP/1.1\r\nHost: x\r\n"
            "Content-Length: %d\r\nContent-Type: application/octet-stream\r\n"
            "\r\n" % len(body)).encode("latin-1")
    handle(proxymod, head + body)
    rec = forwarded(recorder.calls)
    assert rec["body"] == body
    assert rec["headers"].get("Content-Length") == str(len(body))


@settings(max_examples=40, **HS)
@given(body=st.binary(min_size=0, max_size=600))
def test_property_body_roundtrip_through_post(proxymod, recorder, body):
    proxymod.UIPandler.upstream_port = 4321
    head = ("POST /api/x HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n"
            % len(body)).encode("latin-1")
    handle(proxymod, head + body)
    assert forwarded(recorder.calls)["body"] == body


@settings(max_examples=60, **HS)
@given(prefix=st.sampled_from(list(proxy_mod.API_PREFIXES)),
       suffix=st.from_regex(r"(/[A-Za-z0-9_.%-]{0,8}){0,3}", fullmatch=True),
       query=st.text(alphabet="abco=&%20?#", max_size=8))
def test_property_api_paths_are_relayed_without_rewrite(proxymod, recorder,
                                                       prefix, suffix, query):
    proxymod.UIPandler.upstream_port = 4321
    path = "/" + prefix + suffix + (("?" + query) if query else "")
    handle(proxymod, request_line(path))
    assert forwarded(recorder.calls)["url"] == path, path


@pytest.mark.parametrize("method", ["FROB", "PROPFIND", "TRACE"])
def test_unknown_methods_get_a_well_formed_error(proxymod, recorder, method):
    """Unsupported verbs must be answered, not crash or hang."""
    proxymod.UIPandler.upstream_port = 4321
    raw = ("%s /printer/info HTTP/1.1\r\nHost: x\r\n\r\n" % method
           ).encode("latin-1")
    _h, out = handle(proxymod, raw)
    assert out.startswith(b"HTTP/1."), out[:60]
    assert 400 <= int(out.split(b" ")[1]) < 600


def test_many_headers_are_relayed(proxymod, recorder):
    proxymod.UIPandler.upstream_port = 4321
    headers = [("X-Junk-%d" % i, "v" * 80) for i in range(50)]
    _h, out = handle(proxymod, request_line("/printer/info", headers=headers))
    assert forwarded(recorder.calls)["url"] == "/printer/info"
    assert len(recorder.calls) == 1


@pytest.mark.parametrize("count", [101, 400])
def test_header_count_above_the_limit_gets_431_not_a_crash(proxymod, recorder,
                                                           count):
    proxymod.UIPandler.upstream_port = 4321
    headers = [("X-Junk-%d" % i, "v") for i in range(count)]
    _h, out = handle(proxymod, request_line("/printer/info", headers=headers))
    assert out.startswith(b"HTTP/1.0 431"), out[:60]
    assert not recorder.calls


def test_overlong_header_line_gets_431_not_a_crash(proxymod, recorder):
    proxymod.UIPandler.upstream_port = 4321
    raw = (b"GET /printer/info HTTP/1.1\r\nHost: x\r\nX-Big: " + b"k" * 100000
           + b"\r\n\r\n")
    _h, out = handle(proxymod, raw)
    assert out.startswith(b"HTTP/1.0 431"), out[:60]
    assert not recorder.calls


# --- response relay ---------------------------------------------------------
def test_response_relay_preserves_status_headers_and_body(proxymod, recorder):
    proxymod.UIPandler.upstream_port = 4321
    payload = json.dumps({"result": "ok"}).encode()
    recorder.response = RecordedResponse(
        status=201, reason="Created", body=payload,
        headers=[("Content-Type", "application/json"),
                 ("Set-Cookie", "a=1"), ("Set-Cookie", "b=2"),
                 ("Cache-Control", "no-store"), ("Connection", "close"),
                 ("Keep-Alive", "timeout=5"), ("Transfer-Encoding", "chunked"),
                 ("Content-Length", str(len(payload)))])
    _h, out = handle(proxymod, request_line("/printer/info"))
    head, _, body = out.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    assert lines[0] == b"HTTP/1.0 201 Created"
    joined = head.decode("latin-1")
    assert "Set-Cookie: a=1" in joined and "Set-Cookie: b=2" in joined
    assert "Cache-Control: no-store" in joined
    for stripped in ("Connection:", "Keep-Alive:", "Transfer-Encoding:"):
        assert stripped not in joined, stripped
    assert ("Content-Length: %d" % len(payload)) in joined
    assert body == payload


def test_upstream_failure_becomes_clean_502(proxymod, recorder):
    proxymod.UIPandler.upstream_port = 4321
    recorder.raise_on_request = OSError(111, "Connection refused")
    _h, out = handle(proxymod, request_line("/printer/info"))
    assert out.startswith(b"HTTP/1.0 502"), out[:60]
    assert b"Content-Length" in out.split(b"\r\n\r\n")[0]


def test_upstream_error_status_is_not_rewritten(proxymod, recorder):
    proxymod.UIPandler.upstream_port = 4321
    recorder.response = RecordedResponse(status=500, reason="Server Error",
                                         body=b"boom")
    _h, out = handle(proxymod, request_line("/printer/info"))
    assert out.startswith(b"HTTP/1.0 500 Server Error"), out[:60]
    assert out.endswith(b"boom")


# ---------------------------------------------------------------------------
# BUG DEEP: Content-Length is read with headers.get() (FIRST value) but
# forwarded from a dict built out of headers.items() (LAST value), so a client
# sending two conflicting Content-Length headers makes the proxy relay a
# declared length that does not match the body it forwards: upstream framing
# desync / request smuggling. Ambiguous lengths must be rejected (400) or
# normalised to the value that was actually read.
# ---------------------------------------------------------------------------
@pytest.mark.xfail(strict=True,
                   reason="BUG DEEP: duplicate conflicting Content-Length is "
                          "forwarded as the last value while the body is cut at "
                          "the first value, desynchronising the upstream "
                          "connection (smuggling vector)")
def test_ambiguous_content_length_is_not_relayed_inconsistent(proxymod,
                                                             recorder):
    proxymod.UIPandler.upstream_port = 4321
    raw = (b"POST /printer/print/start HTTP/1.1\r\nHost: x\r\n"
           b"Content-Length: 5\r\nContent-Length: 60\r\n\r\nHELLO"
           b"GET /printer/second HTTP/1.1\r\nHost: x\r\n\r\n")
    handle(proxymod, raw)
    rec = forwarded(recorder.calls)
    cl = [v for k, v in rec["header_items"] if k.lower() == "content-length"]
    assert len(cl) <= 1, "two Content-Length values relayed: %r" % (cl,)
    if cl:
        assert int(cl[0]) == len(rec["body"]), \
            "relayed Content-Length %r != body length %d" % (cl, len(rec["body"]))


# ---------------------------------------------------------------------------
# BUG DEEP: a chunked request body is never read. _body() only honours
# Content-Length, so a chunked POST is relayed with "Transfer-Encoding:
# chunked" and an empty body: the chunks stay in the proxy's own buffer (where
# they are parsed as the next pipelined request) and the upstream waits for a
# body until the 15 s timeout. Browser uploads that use chunked encoding fail.
# ---------------------------------------------------------------------------
@pytest.mark.xfail(strict=True,
                   reason="BUG DEEP: proxy does not de-chunk (or reject) "
                          "Transfer-Encoding: chunked request bodies -- the "
                          "payload is never forwarded while the chunked header "
                          "is")
def test_chunked_request_body_is_forwarded(proxymod, recorder):
    proxymod.UIPandler.upstream_port = 4321
    chunks = (b"POST /api/x HTTP/1.1\r\nHost: x\r\n"
              b"Transfer-Encoding: chunked\r\nContent-Type: application/json\r\n"
              b"\r\n5\r\nhello\r\n0\r\n\r\n")
    handle(proxymod, chunks)
    rec = forwarded(recorder.calls)
    assert rec["body"] == b"hello", "chunked payload lost (body=%r)" % (rec["body"],)
    keys = {k.lower() for k, _ in rec["header_items"]}
    assert "transfer-encoding" not in keys, "hop-by-hop TE relayed upstream"


@pytest.mark.parametrize("cl", ["abc", "5;6", "0x10", "5 5", "1e3", "-abc",
                                "+-5", "5,5"])
def test_unparsable_content_length_gets_an_error_status(proxymod, recorder,
                                                         cl):
    """int() failures must be an HTTP error status, never a crash."""
    proxymod.UIPandler.upstream_port = 4321
    raw = ("POST /api/x HTTP/1.1\r\nHost: x\r\nContent-Length: %s\r\n\r\nHELLO"
           % cl).encode("latin-1")
    _h, out = handle(proxymod, raw)
    assert out.startswith(b"HTTP/1."), out[:40]
    assert 400 <= int(out.split(b" ")[1]) < 600


@pytest.mark.parametrize("cl", ["0", "-5", "", "   5   ", "+5", "1_0",
                                "99999999999999999999"])
def test_odd_content_length_still_produces_an_http_response(proxymod,
                                                            recorder, cl):
    """Framing edge cases must be answered with a well-formed response."""
    proxymod.UIPandler.upstream_port = 4321
    raw = ("POST /api/x HTTP/1.1\r\nHost: x\r\nContent-Length: %s\r\n\r\nHELLO"
           % cl).encode("latin-1")
    _h, out = handle(proxymod, raw)
    assert out.startswith(b"HTTP/1."), out[:60]


# --- methods: static vs proxied --------------------------------------------
def test_options_on_static_path_is_204_not_proxied(proxymod, recorder):
    proxymod.UIPandler.upstream_port = 4321
    _h, out = handle(proxymod, request_line("/index.html", method="OPTIONS"))
    assert out.startswith(b"HTTP/1.0 204 No Content"), out[:40]
    assert not recorder.calls, "static OPTIONS must not reach Moonraker"


def test_options_on_api_path_is_proxied(proxymod, recorder):
    proxymod.UIPandler.upstream_port = 4321
    handle(proxymod, request_line("/printer/info", method="OPTIONS"))
    assert forwarded(recorder.calls)["method"] == "OPTIONS"


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_write_methods_are_relayed_with_their_verb(proxymod, recorder, method):
    proxymod.UIPandler.upstream_port = 4321
    raw = ("%s /server/files/gcodes/a.gcode HTTP/1.1\r\nHost: x\r\n"
           "Content-Length: 2\r\n\r\nHI").encode("latin-1") % \
        method.encode("latin-1")
    handle(proxymod, raw)
    rec = forwarded(recorder.calls)
    assert rec["method"] == method
    assert rec["body"] == b"HI"
    assert rec["url"] == "/server/files/gcodes/a.gcode"


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "DELETE"])
def test_query_and_fragment_are_relayed_verbatim(proxymod, recorder, method):
    proxymod.UIPandler.upstream_port = 4321
    path = "/printer/info?a=1&b=%20#frag"
    raw = ("%s %s HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n"
           % (method, path)).encode("latin-1")
    handle(proxymod, raw)
    assert forwarded(recorder.calls)["url"] == path


def test_non_prefix_asset_named_like_websocket_is_not_proxied(proxymod,
                                                             recorder):
    """A UI asset whose first segment is not a prefix must stay local."""
    proxymod.UIPandler.upstream_port = 4321
    _h, out = handle(proxymod, request_line("/websocket-client.js"),
                    docroot="/tmp/no-such-docroot-deep-proxy")
    assert not recorder.calls
    assert b" 404 " in out.split(b"\r\n")[0]


@pytest.mark.parametrize("path", ["/printer/info", "/server/printer/info"])
def test_get_with_upgrade_header_uses_the_websocket_tunnel(proxymod, recorder,
                                                           monkeypatch, path):
    """A websocket upgrade on an API path must not be relayed as plain HTTP."""
    proxymod.UIPandler.upstream_port = 4321
    raw = ("GET %s HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
           "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n\r\n"
           % path).encode("latin-1")
    called = {}

    def fake_ws(self):
        called["ws"] = True

    monkeypatch.setattr(proxymod.UIPandler, "_proxy_ws", fake_ws)
    handle(proxymod, raw)
    assert called.get("ws") is True
    assert not recorder.calls, "upgrade request must not go through _proxy"


# ---------------------------------------------------------------------------
# BUG DEEP: HEAD on an API path reports "Content-Length: 0" because the proxy
# drops the upstream's Content-Length and substitutes len(body). A HEAD
# response must carry the size the GET would report (RFC 9110 9.3.2), so the
# UIs cannot use HEAD for size/existence checks through the proxy.
# ---------------------------------------------------------------------------
@pytest.mark.xfail(strict=True,
                   reason="BUG DEEP: HEAD responses through the proxy report "
                          "Content-Length: 0 instead of the upstream's declared "
                          "size")
def test_head_relays_upstream_declared_content_length(proxymod, recorder):
    proxymod.UIPandler.upstream_port = 4321
    recorder.response = RecordedResponse(
        status=200, reason="OK", body=b"",
        headers=[("Content-Type", "text/plain"), ("Content-Length", "1234")])
    _h, out = handle(proxymod, request_line("/server/files/gcodes/a.gcode",
                                           method="HEAD"))
    head = out.split(b"\r\n\r\n")[0].decode("latin-1")
    assert "Content-Length: 1234" in head, head
