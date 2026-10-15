"""Deep tests: UIPandler static serving -- path traversal / containment / hostile URLs.

An in-process proxy.Server bound to 127.0.0.1 on an ephemeral port serves a
throwaway doc root under KLIPPERFORMAC_DATA; a "secret" file lives OUTSIDE the
doc root and must never be reachable. Upstream port is a refused sentinel so no
test can ever reach the live Moonraker.
"""
import http.client
import importlib
import os
import shutil
import socket
import threading
import time
import types
import urllib.parse

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

import klipperformac.proxy as proxy_mod

STACK_PORTS = frozenset({7125, 7126, 8080, 8081})
UPSTREAM_SENTINEL = 1
SECRET = b"OUTSIDE-DOCROOT-SECRET-9ed3f1a2\n"


@pytest.fixture
def proxymod(tmp_path, monkeypatch):
    monkeypatch.setenv("KLIPPERFORMAC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KLIPPERFORMAC_DATA", str(tmp_path / "data"))
    mod = importlib.reload(proxy_mod)
    monkeypatch.setattr(mod.UIPandler, "upstream_port", UPSTREAM_SENTINEL,
                        raising=False)

    real_create = socket.create_connection

    def create_connection(address, *a, **kw):
        assert address[1] not in STACK_PORTS
        return real_create(address, *a, **kw)

    monkeypatch.setattr(mod, "socket",
                        types.SimpleNamespace(create_connection=create_connection))
    return mod


@pytest.fixture
def tree(tmp_path, stack):
    """Doc root (the real mainsail path) plus a secret file outside it."""
    docroot = stack.paths.WEB_MAINSAIL
    docroot.mkdir(parents=True, exist_ok=True)
    (docroot / "index.html").write_text("<h1>mainsail home</h1>")
    (docroot / "assets").mkdir()
    (docroot / "assets" / "app.js").write_text("console.log(1)")
    sub = tmp_path / "outside"
    sub.mkdir()
    (sub / "secret.txt").write_bytes(SECRET)
    (sub / "passwd-like").write_bytes(b"root:x:0:0::/root:/bin/sh\n")
    return {"docroot": docroot, "outside": sub}


@pytest.fixture
def server(proxymod, tree):
    """proxy.Server on 127.0.0.1:0 serving the doc root; always torn down."""
    httpd = proxymod.Server(("127.0.0.1", 0),
                            lambda *a, **kw: proxymod.UIPandler(
                                *a, directory=str(tree["docroot"]), **kw))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    httpd.port = httpd.server_address[1]
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- helpers ---------------------------------------------------------------
def get(path, server):
    """Well-formed request through http.client; returns (status, body)."""
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        conn.putrequest("GET", path)
        conn.putheader("Host", "127.0.0.1")
        conn.endheaders()
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def raw_get(request_target, server, http_version=b"HTTP/1.0"):
    """Byte-exact request line for hostile targets http.client would refuse.

    Returns the response bytes, or None when the server hangs up without ever
    sending a status line.
    """
    s = socket.create_connection(("127.0.0.1", server.port), timeout=10)
    try:
        s.sendall(b"GET " + request_target + b" " + http_version + b"\r\n"
                  b"Host: 127.0.0.1\r\n\r\n")
        data = b""
        while len(data) < 65536:
            try:
                chunk = s.recv(4096)
            except (socket.timeout, ConnectionResetError, OSError):
                break
            if not chunk:
                break
            data += chunk
        return data or None
    finally:
        s.close()


def leaks(body):
    return SECRET in body or b"passwd-like" in body or b"root:x:0:0:" in body


# --- containment: must hold today ------------------------------------------
TRAVERSAL = [
    b"/../../../etc/passwd",
    b"/%2e%2e/%2e%2e/%2e%2e/etc/passwd",
    b"/..%2f..%2f..%2fetc%2fpasswd",
    b"/%252e%252e/%252e%252e/etc/passwd",
    b"/....//....//etc/passwd",
    b"/..;/..;/etc/passwd",
    b"/%c0%ae%c0%ae/%c0%ae%c0%ae/etc/passwd",
    b"/%ff%ff/%2e%2e/etc/passwd",
    b"/index.html/../../outside/secret.txt",
    b"/assets/../../outside/secret.txt",
    b"/%2e%2e%5c%2e%2e%5coutside%5csecret.txt",
    b"/\\..\\..\\outside\\secret.txt",
    b"/../%2e%2e/outside/secret.txt",
    b"/outside/secret.txt",
    b"/./../outside/secret.txt",
    b"%2f..%2f..%2foutside%2fsecret.txt",
    b"/.../.../.../outside/secret.txt",
    b"/../assets/../outside/secret.txt",
    b"//outside//secret.txt",
    b"/%2F%2E%2E%2Foutside%2Fsecret.txt",
]


@pytest.mark.parametrize("target", TRAVERSAL)
def test_traversal_never_leaks_outside_file(server, tree, target):
    body = raw_get(target, server) or b""
    assert not leaks(body), "doc root escaped through %r" % target


@pytest.mark.parametrize("target", TRAVERSAL)
def test_traversal_never_serves_200_from_outside(server, target):
    body = raw_get(target, server)
    if body is None:
        pytest.fail("no HTTP response at all for %r" % target)
    assert body.startswith(b"HTTP/1."), body[:40]
    assert b" 200 " not in body.split(b"\r\n")[0], body.split(b"\r\n")[0]


def test_absolute_path_not_disclosed_in_error_pages(server, tree):
    for target in [b"/nope", b"/assets/nope.js", b"/%2e%2e/etc/passwd",
                   b"/nonexistent/../missing"]:
        body = raw_get(target, server) or b""
        assert str(tree["docroot"]).encode() not in body


def test_permission_denied_subtree_is_404_not_crash(server, tree):
    if os.geteuid() == 0:
        pytest.skip("running as root: mode bits do not apply")
    locked = tree["docroot"] / "locked"
    locked.mkdir()
    (locked / "inner.txt").write_bytes(SECRET)
    os.chmod(locked, 0o000)
    try:
        for target in (b"/locked/", b"/locked/inner.txt"):
            status, body = get(target.decode("latin-1"), server)
            assert status in (403, 404), target
            assert not leaks(body)
    finally:
        os.chmod(locked, 0o755)
        shutil.rmtree(locked, ignore_errors=True)


def test_unicode_and_punctuated_names_round_trip(server, tree):
    names = ["打印\u00e4\u00f1\u00f6me.gcode", "\u65e5\u672c\u8a9e/\u30c6\u30b9\u30c8.txt",
             "space name.txt", "percent%25.txt", "hash#tag.txt",
             "query?mark.txt", "a..b.txt", "\u00e9lan.vite.js"]
    for name in names:
        f = tree["docroot"] / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"body-of-" + name.encode("utf-8"))
    try:
        for name in names:
            target = "/" + urllib.parse.quote(name.encode("utf-8"))
            status, body = get(target, server)
            assert status == 200, (target, status)
            assert body.endswith(name.encode("utf-8"))
    finally:
        for name in names:
            f = tree["docroot"] / name
            if f.exists():
                f.unlink()
            if f.parent.is_dir() and not any(f.parent.iterdir()):
                try:
                    f.parent.rmdir()
                except OSError:
                    pass


def test_deep_path_and_overlong_request_line_do_not_kill_server(server, tree):
    deep = "/" + "/".join(["seg%d" % i for i in range(300)]) + "/file.txt"
    status, body = get(deep, server)
    assert status == 404
    long_target = b"/" + b"A" * 200000
    try:
        raw_get(long_target, server)
    except OSError:
        pass  # the server may RST an over-long request line; that is fine
    status, body = get("/index.html", server)
    assert status == 200 and body == b"<h1>mainsail home</h1>"


def test_server_survives_all_hostile_targets(server, tree):
    for target in TRAVERSAL + [b"/%00", b"/%ed%ba%80", b"/" + b"\xff" * 40,
                               b"/" + b"%00" * 50, b"/" * 500]:
        try:
            raw_get(target, server)
        except OSError:
            pass
    status, body = get("/index.html", server)
    assert status == 200


# ---------------------------------------------------------------------------
# FIXED (deep-hunt): a URL-encoded NUL (or a lone surrogate from an invalid %ED
# sequence) used to survive translate_path and blow up inside open()/os.stat
# with ValueError/UnicodeEncodeError. The handler emitted no HTTP response at
# all -- the client just saw the connection closed -- and the traceback went to
# the parent's stderr. UIPandler._static_request_ok() now screens the
# translated path (os.fsencode + realpath) and answers 400 instead.
# Guard: these two tests must keep passing.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("target", [b"/%00", b"/index.html%00",
                                    b"/index.html%00.png", b"/assets%00/app.js",
                                    b"/%00/etc/passwd"])
def test_encoded_nul_gets_an_http_error_status(server, target):
    body = raw_get(target, server)
    assert body is not None, "connection closed without any HTTP response"
    assert body.startswith(b"HTTP/1.")
    assert body.split(b" ")[1] in (b"400", b"404")


@pytest.mark.parametrize("target", [b"/%ed%ba%80", b"/%ed%b0%80x",
                                    b"/%ed%a0%80", b"/assets/%ed%bf%bf/x"])
def test_surrogate_escape_gets_an_http_error_status(server, target):
    body = raw_get(target, server)
    assert body is not None, "connection closed without any HTTP response"
    assert body.startswith(b"HTTP/1.")
    assert body.split(b" ")[1] in (b"400", b"404")


@pytest.mark.parametrize("target", [b"/assets/%f0%9f%92", b"/%c0%80%af",
                                    b"/%c0%af", b"/%e0%80%af",
                                    b"/%f0%9f%92%80", b"/%c1%9c", b"/%ff",
                                    b"/%ed", b"/%2", b"/%", b"/%41%41"])
def test_malformed_but_safe_percent_encodings_get_http_404(server, tree, target):
    body = raw_get(target, server)
    assert body is not None, "connection closed without any HTTP response"
    assert body.startswith(b"HTTP/1.")
    assert body.split(b" ")[1] == b"404"
    assert not leaks(body)


# ---------------------------------------------------------------------------
# FIXED (deep-hunt): translate_path() collapsed ".." but never resolved
# symlinks, so a symlink inside the doc root (web installs legitimately contain
# them, and the doc root is user-writable) read any file the Klipper-for-Mac
# user can open. UIPandler._static_request_ok() now compares os.path.realpath()
# of the translated path against the real doc root and answers 403; links that
# stay inside the tree keep working (test_proxy_containment.py).
# Guard: these two tests must keep passing.
# ---------------------------------------------------------------------------
def test_file_symlink_stays_inside_docroot(server, tree):
    link = tree["docroot"] / "linked.txt"
    os.symlink(str(tree["outside"] / "secret.txt"), str(link))
    try:
        status, body = get("/linked.txt", server)
        assert status in (403, 404), "symlink target served: %s" % status
        assert not leaks(body)
    finally:
        link.unlink()


def test_directory_symlink_stays_inside_docroot(server, tree):
    link = tree["docroot"] / "dlink"
    os.symlink(str(tree["outside"]), str(link))
    try:
        for target in ("/dlink/secret.txt", "/dlink/", "/dlink/passwd-like"):
            status, body = get(target, server)
            assert status in (403, 404), (target, status)
            assert not leaks(body)
    finally:
        link.unlink()


# --- hypothesis properties -------------------------------------------------
SEGMENTS = st.sampled_from(
    ["..", ".", "%2e%2e", "%2E%2E", "...", "", "/", "assets", "index.html",
     "nope", "%2f", "\\", "%5c", "..;", "~", "outside", "secret.txt",
     "\u00e9", "%c0%ae", "x" * 5, ".", "..%2f"])


@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
          derandomize=True)
@given(segments=st.lists(SEGMENTS, min_size=1, max_size=8))
def test_property_no_lexical_path_reaches_outside(server, tree, segments):
    target = "/" + "/".join(segments)
    try:
        body = raw_get(target.encode("utf-8", "surrogateescape"), server) or b""
    except OSError:
        return  # transport-level rejection is not a containment failure
    assert not leaks(body), "leak through %r" % target


@settings(max_examples=25, deadline=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
          derandomize=True)
@given(path=st.sampled_from(["/index.html", "/assets/app.js", "/nope",
                             "/assets/", "/index.html?x=1"]))
def test_property_static_serving_is_idempotent(server, path):
    s1, b1 = get(path, server)
    s2, b2 = get(path, server)
    assert (s1, b1) == (s2, b2)
