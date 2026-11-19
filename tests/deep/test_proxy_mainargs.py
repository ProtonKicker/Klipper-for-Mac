"""Deep tests: klipperformac.proxy main() argument validation (port / bind / doc root).

Everything is offline and local:
  * malformed invocations are checked with a recording stand-in for proxy.Server,
    so no socket is bound at all;
  * tests that need a real listener bind 127.0.0.1 on an EPHEMERAL port (port 0)
    and shut the server down again;
  * an autouse guard refuses any dial to the live stack ports
    (7125/7126/8080/8081/8082) and any upstream host that is not loopback.

Production calls this as
``python -m klipperformac.proxy <port> <docroot> <moonraker_port> [<bind>]``
(klipperformac/process.py:211), so every token below arrives from a wrapper
script, a launchd plist or a shell that expanded a variable.
"""
import http.client
import importlib
import os
import re
import socket
import socketserver
import threading
import time
import types

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

import klipperformac.proxy as proxy_mod

STACK_PORTS = frozenset({7125, 7126, 8080, 8081, 8082})
SENTINEL_UPSTREAM = 1          # privileged + always refused: never a live service
LOOPBACK_TOKENS = frozenset({"127.0.0.1", "::1", "localhost"})
CANONICAL_PORT = re.compile(r"\A(?:0|[1-9][0-9]{0,4})\Z")


@pytest.fixture
def proxymod(tmp_path, monkeypatch):
    """A freshly reloaded proxy module (UIPandler class state is process-global)."""
    monkeypatch.setenv("KLIPPERFORMAC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KLIPPERFORMAC_DATA", str(tmp_path / "data"))
    mod = importlib.reload(proxy_mod)
    monkeypatch.setattr(mod.UIPandler, "upstream_port", SENTINEL_UPSTREAM,
                        raising=False)
    return mod


@pytest.fixture(autouse=True)
def guard_loopback_only(proxymod, monkeypatch):
    """The module under test may only ever dial loopback, never a stack port."""
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

    monkeypatch.setattr(proxymod, "socket",
                        types.SimpleNamespace(create_connection=create_connection))
    monkeypatch.setattr(proxymod, "http",
                        types.SimpleNamespace(
                            client=types.SimpleNamespace(
                                HTTPConnection=GuardedConnection)))


class _FakeHttpd:
    def __init__(self, record):
        self._record = record

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def serve_forever(self, *a, **kw):
        self._record["served"] = True


class _RecordingServer:
    """Stand-in for proxy.Server: records the bind tuple, never touches a socket."""

    def __init__(self, records):
        self.records = records

    def __call__(self, address, handler, *a, **kw):
        rec = {"address": address, "handler": handler, "served": False}
        self.records.append(rec)
        return _FakeHttpd(rec)


@pytest.fixture
def nobind(monkeypatch, proxymod):
    records = []
    monkeypatch.setattr(proxymod, "Server", _RecordingServer(records))
    return records


def argv(port="0", directory="/tmp", upstream="4321", bind=None):
    out = ["prog", port, directory, upstream]
    if bind is not None:
        out.append(bind)
    return out


@pytest.fixture
def docroot(stack):
    root = stack.paths.WEB_MAINSAIL
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text("<h1>mainsail</h1>")
    return root


# ---------------------------------------------------------------------------
# FIXED (deep-hunt): main() took the port tokens straight through int(), which
# is a Python *literal* parser, not an HTTP port parser. proxy.PORT_TOKEN now
# admits only canonical ASCII decimal ("0" or [1-9][0-9]{0,4}) and range-checks
# 0..65535, both before any UIPandler class state is touched.
# Guard: these two tests must keep passing.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("token,secret_port", [
    ("1_0", 10),
    ("8_0_8_0", 8080),
    ("007", 7),
    ("-0", 0),
    ("١٢٣", 123),
    ("８０８０", 8080),
    ("  4321\n", 4321),
    ("\t4321", 4321),
    ("+4321", 4321),
    ("4321 ", 4321),
])
def test_main_listen_port_must_be_canonical_ascii_digits(nobind, token,
                                                         secret_port):
    with pytest.raises(SystemExit):
        proxy_mod.main(argv(port=token))
    assert not nobind, \
        "token %r was accepted and bound port %d" % (token, secret_port)


@pytest.mark.parametrize("token", ["1_0", "١٢٣", " 7125 ", "+7125", "0007125",
                                   "7125\n"])
def test_main_upstream_port_must_be_canonical_ascii_digits(nobind, proxymod,
                                                           token):
    with pytest.raises(SystemExit):
        proxy_mod.main(argv(upstream=token))
    assert proxymod.UIPandler.upstream_port == SENTINEL_UPSTREAM, \
        "a malformed upstream token leaked into UIPandler class state"


@settings(max_examples=80, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture,
                                 HealthCheck.too_slow])
@given(token=st.one_of(
    st.from_regex(r"\A[0-9]{1,5}\Z", fullmatch=True),
    st.from_regex(r"\A[-+ 0-9_١٢٣６７８]{1,6}\Z", fullmatch=True),
))
def test_property_only_canonical_decimal_tokens_are_accepted(nobind, token):
    try:
        proxy_mod.main(argv(port=token))
    except SystemExit:
        return
    except Exception:
        pytest.fail("main(%r) escaped with a raw traceback" % (token,))
    assert CANONICAL_PORT.match(token), \
        "non-canonical token %r was accepted" % token
    assert nobind[-1]["address"][1] == int(token)


# ---------------------------------------------------------------------------
# FIXED (deep-hunt): argv[4] used to be handed to socket.bind() verbatim. ""
# and "0" are not errors -- they resolve to INADDR_ANY -- so a wrapper that
# expanded an empty $BIND silently published Mainsail/Fluidd on every
# interface, which AGENTS.md forbids as a default. proxy._bind_token() now
# accepts numeric addresses only (no DNS either), so an ambiguous token is a
# usage error; the LAN opt-in stays the explicit "0.0.0.0" process.py passes.
# Guard: this test must keep passing.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("token", ["", "0"])
def test_main_ambiguous_bind_token_must_not_wildcard(nobind, token):
    # Evidence first, without binding anything: prove the token is a wildcard.
    resolved = socket.getaddrinfo(token, 0, socket.AF_INET, socket.SOCK_STREAM,
                                  0, socket.AI_PASSIVE)
    got = [r[4][0] for r in resolved]
    assert got and all(a not in LOOPBACK_TOKENS for a in got), \
        "expected %r to resolve to a wildcard address, got %r" % (token, got)
    try:
        proxy_mod.main(argv(bind=token))
    except SystemExit:
        return
    bound = nobind[-1]["address"][0]
    assert bound in LOOPBACK_TOKENS, \
        "%r silently bound the non-loopback address %r" % (token, bound)


# --- token handling that must be correct today and after any fix -------------
def test_main_defaults_to_loopback_and_serves(nobind):
    proxy_mod.main(argv(bind=None))
    assert nobind[-1]["address"][0] == "127.0.0.1"
    assert nobind[-1]["served"] is True


def test_main_explicit_loopback_bind_is_recorded_verbatim(nobind):
    proxy_mod.main(argv(bind="127.0.0.1"))
    assert nobind[-1]["address"] == ("127.0.0.1", 0)


@pytest.mark.parametrize("token", ["0", "1", "80", "443", "1024", "8080",
                                   "65534", "65535"])
def test_main_canonical_ports_are_passed_through_exactly(nobind, token):
    proxy_mod.main(argv(port=token, bind="127.0.0.1"))
    assert nobind[-1]["address"] == ("127.0.0.1", int(token))


def test_server_class_is_restart_safe():
    """allow_reuse_address + daemon_threads: `klipperformac restart` must not
    wedge on a TIME_WAIT socket, and a stuck handler must not block interpreter
    exit (a websocket tunnel that never closes lives until process death)."""
    assert proxy_mod.Server.allow_reuse_address is True
    assert proxy_mod.Server.daemon_threads is True
    assert issubclass(proxy_mod.Server, socketserver.ThreadingTCPServer)


# ---------------------------------------------------------------------------
# FIXED (deep-hunt): main() had no error handling around bind(). A perfectly
# canonical port that is simply unavailable -- the ordinary desktop case of
# something else already on 8080 -- produced an interpreter traceback out of
# socketserver instead of "port is already in use". main() now translates the
# bind OSError into a SystemExit.
# Guard: this test must keep passing.
# ---------------------------------------------------------------------------
def test_main_port_in_use_must_be_a_clean_error(proxymod, docroot):
    occupied = socket.socket()
    occupied.bind(("127.0.0.1", 0))
    occupied.listen(1)
    port = occupied.getsockname()[1]
    try:
        with pytest.raises(SystemExit):
            proxy_mod.main(argv(port=str(port), directory=str(docroot)))
    finally:
        occupied.close()


# ---------------------------------------------------------------------------
# FIXED (deep-hunt): a doc root containing a NUL byte was accepted by main();
# every static request then died inside isdir()/open() with "ValueError:
# embedded null byte", socketserver killed the handler thread and the browser
# got no HTTP response at all -- a blank UI with nothing diagnosable in the log
# (UIPandler silences handler logging). proxy._docroot() now screens the token
# and refuses to start before the listener exists.
# Guard: this test must keep passing.
# ---------------------------------------------------------------------------
def test_main_docroot_with_nul_must_be_a_clean_error(proxymod, docroot):
    """main() must refuse to start, not start and then die on every request."""
    captured = []

    class RecordingServer(proxymod.Server):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            captured.append(self)

    saved = proxymod.Server
    proxymod.Server = RecordingServer
    outcome = []

    def run():
        try:
            proxy_mod.main(argv(directory=str(docroot) + "\x00evil"))
        except SystemExit:
            outcome.append("clean-exit")
        except BaseException as e:  # noqa: BLE001
            outcome.append("raw-traceback: %r" % (e,))
        else:
            outcome.append("served-forever")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 8
        while time.time() < deadline and not outcome and not captured:
            time.sleep(0.01)
        assert not captured, \
            "main() served with a NUL in the doc root instead of refusing it"
        assert outcome == ["clean-exit"], \
            "main() must exit with a usage error, got %r" % (outcome,)
    finally:
        proxymod.Server = saved
        time.sleep(0.1)          # let the daemon thread reach serve_forever()
        for httpd in captured:
            httpd.shutdown()
            httpd.server_close()
        thread.join(5)


# --- hostile-but-legal doc roots: degrade to HTTP, never to a crash ---------
@pytest.mark.parametrize("kind", ["missing", "regular-file", "space-only",
                                  "quotes-and-backslash", "unicode", "overlong",
                                  "trailing-slashes", "relative"])
def test_main_answers_http_for_any_hostile_docroot(proxymod, stack, tmp_path,
                                                   kind):
    if kind == "missing":
        directory = str(tmp_path / "nope" / "deeper" / "mainsail")
    elif kind == "regular-file":
        directory = tmp_path / "a-file"
        directory.write_text("i am not a directory")
        directory = str(directory)
    elif kind == "space-only":
        directory = str(tmp_path / " ")
        os.mkdir(directory)
    elif kind == "quotes-and-backslash":
        directory = str(tmp_path / "d i r'\"\\x")
        os.makedirs(directory)
    elif kind == "unicode":
        directory = str(tmp_path / "ūnicödé" / "中文" / "mainsail")
        os.makedirs(directory)
    elif kind == "overlong":
        directory = str(tmp_path / ("l" * 200) / ("m" * 200))
    elif kind == "trailing-slashes":
        directory = str(stack.paths.WEB_MAINSAIL) + "///"
        stack.paths.WEB_MAINSAIL.mkdir(parents=True, exist_ok=True)
        (stack.paths.WEB_MAINSAIL / "index.html").write_text("REAL")
    else:
        directory = "web/mainsail-relative"      # resolved against the CWD

    httpd = proxymod.Server(("127.0.0.1", 0),
                            lambda *a, **kw: proxymod.UIPandler(
                                *a, directory=directory, **kw))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        assert httpd.server_address[0] == "127.0.0.1"
        for target in ("/index.html", "/", "/assets/app.js"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", target)
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            assert resp.status in (200, 301, 403, 404), (target, resp.status)
            assert b"Traceback" not in body
            assert str(tmp_path).encode("utf-8", "surrogateescape") not in body
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_main_end_to_end_real_ephemeral_loopback_bind(proxymod, docroot):
    """main() itself, on a real 127.0.0.1 ephemeral port, static + relay split."""
    captured = []

    class RecordingServer(proxymod.Server):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            captured.append(self)

    saved = proxymod.Server
    proxymod.Server = RecordingServer
    errors = []

    def run():
        try:
            proxy_mod.main(argv(port="0", directory=str(docroot),
                                upstream="4321"))
        except BaseException as e:  # noqa: BLE001 - surfaced to the test
            errors.append(e)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not captured and time.time() < deadline:
            time.sleep(0.01)
        assert captured, "main() never constructed a server"
        assert not errors, errors
        httpd = captured[0]
        host, port = httpd.server_address
        assert host == "127.0.0.1" and port > 0, httpd.server_address
        conn = http.client.HTTPConnection(host, port, timeout=10)
        conn.request("GET", "/index.html")
        resp = conn.getresponse()
        assert resp.status == 200 and resp.read() == b"<h1>mainsail</h1>"
        conn.close()
        assert proxymod.UIPandler.upstream_port == 4321
    finally:
        proxymod.Server = saved
        if captured:
            captured[0].shutdown()
            captured[0].server_close()
        thread.join(5)
        assert not thread.is_alive(), "main() did not return after shutdown"
