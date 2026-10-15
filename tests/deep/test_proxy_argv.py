"""Deep tests: klipperformac.proxy main() argv validation and wiring.

Everything runs offline: docroots live under the throwaway KLIPPERFORMAC_DATA,
servers bind 127.0.0.1 on ephemeral ports only (port "0"), and an autouse guard
makes any attempt to touch the live stack ports (7125/8080/8081) fail loudly.
"""
import http.client
import importlib
import json
import socket
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import klipperformac.proxy as proxy_mod

STACK_PORTS = frozenset({7125, 7126, 8080, 8081})
UPSTREAM_SENTINEL = 1  # privileged port: always refused, never a live service


class _Abort(Exception):
    pass


@pytest.fixture
def proxymod(tmp_path, monkeypatch):
    """A freshly reloaded proxy module (UIPandler class state is process-global)."""
    monkeypatch.setenv("KLIPPERFORMAC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KLIPPERFORMAC_DATA", str(tmp_path / "data"))
    mod = importlib.reload(proxy_mod)
    monkeypatch.setattr(mod.UIPandler, "upstream_port", UPSTREAM_SENTINEL,
                        raising=False)
    return mod


@pytest.fixture(autouse=True)
def guard_no_stack_ports(proxymod, monkeypatch):
    """proxy.py may only ever dial loopback, and never a live stack port."""
    real_create = socket.create_connection
    real_conn = http.client.HTTPConnection

    def create_connection(address, *a, **kw):
        assert address[1] not in STACK_PORTS, \
            "test tried to reach live stack port %r" % (address,)
        assert str(address[0]) == "127.0.0.1", \
            "proxy must only ever dial loopback, got %r" % (address,)
        return real_create(address, *a, **kw)

    class GuardedConnection(real_conn):
        def __init__(self, host, port=None, *a, **kw):
            assert port not in STACK_PORTS, \
                "test tried to reach live stack port %r" % (port,)
            super().__init__(host, port, *a, **kw)

    # Patch the module's own references only: no global stdlib mutation.
    monkeypatch.setattr(proxymod, "socket",
                        types.SimpleNamespace(create_connection=create_connection))
    monkeypatch.setattr(proxymod, "http",
                        types.SimpleNamespace(
                            client=types.SimpleNamespace(
                                HTTPConnection=GuardedConnection)))


class _FakeServer:
    def __init__(self, record):
        self._record = record

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def serve_forever(self, *a, **kw):
        self._record["served"] = True


class _NoBindServer:
    """Stand-in for proxy.Server: records construction, never touches a socket."""

    def __init__(self, records):
        self.records = records

    def __call__(self, address, handler, *a, **kw):
        rec = {"address": address, "handler": handler}
        self.records.append(rec)
        return _FakeServer(rec)


@pytest.fixture
def nobind(monkeypatch, proxymod):
    records = []
    monkeypatch.setattr(proxymod, "Server", _NoBindServer(records))
    return records


def _argv(port="0", directory="/tmp", upstream="0", bind=None):
    argv = ["prog", port, directory, upstream]
    if bind is not None:
        argv.append(bind)
    return argv


# ---------------------------------------------------------------------------
# BUG DEEP: main() performs no argv validation. Every malformed invocation
# surfaces as a raw interpreter traceback (ValueError / OverflowError /
# IndexError / socket.gaierror) instead of a usage error with a clean exit
# code, e.g. `python -m klipperformac.proxy 808o <dir> 7125`.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["abc", "8080.5", "", "   ", "1e3", "0x1f90",
                                 "nan", "inf", "-", "8,080", "١٢٣", "+8080-",
                                 "५", "8080 8081"])
@pytest.mark.xfail(strict=True,
                   reason="BUG DEEP: proxy.main() parses argv with a bare int(); "
                          "a non-numeric port raises a ValueError traceback "
                          "instead of a clean SystemExit usage error")
def test_main_non_numeric_port_must_be_clean_error(nobind, bad):
    with pytest.raises(SystemExit):
        proxy_mod.main(_argv(port=bad))
    assert not nobind, "a server was constructed despite a malformed port"


@pytest.mark.parametrize("bad", ["-1", "-8080", "65536", "999999",
                                 "99999999999999999999", "70000"])
@pytest.mark.xfail(strict=True,
                   reason="BUG DEEP: out-of-range ports are never validated; "
                          "main() either dies with OverflowError from "
                          "socket.bind() or silently accepts the value")
def test_main_port_out_of_range_must_be_clean_error(nobind, bad):
    with pytest.raises(SystemExit):
        proxy_mod.main(_argv(port=bad))


@pytest.mark.parametrize("bad", ["junk", "7125.5", "", "abc", "-1", "65536"])
@pytest.mark.xfail(strict=True,
                   reason="BUG DEEP: the upstream port gets the same unvalidated "
                          "int() treatment as the listen port")
def test_main_bad_upstream_port_must_be_clean_error(nobind, bad):
    with pytest.raises(SystemExit):
        proxy_mod.main(_argv(upstream=bad))


@pytest.mark.parametrize("argv", [["prog"], ["prog", "8080"],
                                  ["prog", "8080", "/tmp"], []])
@pytest.mark.xfail(strict=True,
                   reason="BUG DEEP: missing required argv produces an IndexError "
                          "traceback instead of a usage message")
def test_main_missing_arguments_must_be_clean_error(nobind, argv):
    with pytest.raises(SystemExit):
        proxy_mod.main(argv)


@pytest.mark.parametrize("bad", ["not-an-host.invalid", "999.999.999.999",
                                 "1.2.3.4.5", "8080"])
@pytest.mark.xfail(strict=True,
                   reason="BUG DEEP: the bind address (argv[4]) is handed "
                          "straight to socket.bind(); an invalid one yields a "
                          "raw gaierror/OSError traceback")
def test_main_bad_bind_must_be_clean_error(nobind, bad):
    with pytest.raises(SystemExit):
        proxy_mod.main(_argv(bind=bad))


@pytest.mark.xfail(strict=True,
                   reason="BUG DEEP: an empty doc root argv[2] is accepted "
                          "silently, and translate_path then resolves requested "
                          "paths relative to the server's CWD (the repo root in "
                          "production), exposing arbitrary files outside the UI "
                          "directory")
def test_main_blank_docroot_must_be_clean_error(nobind, proxymod, monkeypatch):
    with pytest.raises(SystemExit):
        proxy_mod.main(_argv(directory=""))
    # If main() ever learns to validate, the handler must never fall back to CWD.
    seen = {}

    def spy(self, *a, **kw):
        seen.update(kw)
        raise _Abort()

    monkeypatch.setattr(proxymod.UIPandler, "__init__", spy)
    with pytest.raises(_Abort):
        nobind[0]["handler"](object(), ("127.0.0.1", 5), object())
    assert seen.get("directory") not in (None, ""), \
        "doc root must be resolved to an absolute path before serving"


# ---------------------------------------------------------------------------
# Wiring properties: must hold today, and must still hold after a fix.
# ---------------------------------------------------------------------------
def test_main_defaults_to_loopback_bind(nobind):
    """AGENTS.md: no LAN exposure by default."""
    proxy_mod.main(_argv(bind=None))
    assert nobind and nobind[0]["address"][0] == "127.0.0.1"


def test_main_honours_explicit_loopback_bind(nobind):
    proxy_mod.main(_argv(bind="127.0.0.1"))
    assert nobind[0]["address"] == ("127.0.0.1", 0)


def test_main_wires_upstream_port_and_serves(nobind, proxymod):
    proxy_mod.main(_argv(port="0", upstream="4321"))
    assert proxymod.UIPandler.upstream_port == 4321
    assert nobind[0].get("served") is True


def test_main_passes_docroot_to_handler(nobind, proxymod, tmp_path, monkeypatch):
    docroot = tmp_path / "web" / "mainsail"
    docroot.mkdir(parents=True)
    seen = {}

    def spy(self, *a, **kw):
        seen.update(kw)
        raise _Abort()

    monkeypatch.setattr(proxymod.UIPandler, "__init__", spy)
    proxy_mod.main(_argv(port="0", upstream="0", directory=str(docroot),
                         bind="127.0.0.1"))
    factory = nobind[0]["handler"]
    with pytest.raises(_Abort):
        factory(object(), ("127.0.0.1", 5), object())
    assert seen.get("directory") == str(docroot)


@pytest.mark.parametrize("junk", ["--extra", "8080", "x"])
def test_main_extra_argv_is_inert(nobind, junk):
    """A 5th argument beyond bind must not change how the server is built."""
    proxy_mod.main(["prog", "0", "/tmp", "0", "127.0.0.1", junk])
    assert nobind[0]["address"] == ("127.0.0.1", 0)


def test_main_end_to_end_real_ephemeral_bind(proxymod, stack, tmp_path):
    """Run main() for real on 127.0.0.1:0 and prove both halves of its job."""
    docroot = stack.paths.WEB_MAINSAIL
    docroot.mkdir(parents=True, exist_ok=True)
    (docroot / "index.html").write_text("<h1>mainsail</h1>")
    payload = json.dumps({"status": "ready"}).encode()

    class Upstream(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    up_port = up.server_address[1]
    up_thread = threading.Thread(target=up.serve_forever, daemon=True)
    up_thread.start()

    captured = []

    class RecordingServer(proxymod.Server):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            captured.append(self)

    errors = []

    def run():
        try:
            proxy_mod.main(["prog", "0", str(docroot), str(up_port)])
        except BaseException as e:  # noqa: BLE001 - surfaced to the test
            errors.append(e)

    saved = proxymod.Server
    proxymod.Server = RecordingServer
    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        deadline = time.time() + 10
        while not captured and time.time() < deadline:
            time.sleep(0.01)
        assert captured, "main() never constructed a server"
        assert not errors, errors
        httpd = captured[0]
        host, port = httpd.server_address
        assert host == "127.0.0.1", "default bind must be loopback"
        assert port != 0, "should have bound an ephemeral port"

        conn = http.client.HTTPConnection(host, port, timeout=10)
        conn.request("GET", "/index.html")
        r = conn.getresponse()
        assert r.status == 200 and r.read() == b"<h1>mainsail</h1>"
        conn.close()

        conn = http.client.HTTPConnection(host, port, timeout=10)
        conn.request("GET", "/printer/info")
        r = conn.getresponse()
        assert r.status == 200
        assert json.loads(r.read()) == {"status": "ready"}
        assert int(r.getheader("Content-Length")) == len(payload)
        conn.close()
    finally:
        proxymod.Server = saved
        if captured:
            captured[0].shutdown()
            captured[0].server_close()
        up.shutdown()
        up.server_close()
        t.join(5)
        up_thread.join(5)
        assert not t.is_alive() and not up_thread.is_alive()
