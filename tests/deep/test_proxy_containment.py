"""Deep tests: UIPandler static-serving containment against the REAL data layout.

The production doc root is ``<KLIPPERFORMAC_DATA>/web/mainsail``
(klipperformac/process.py:211 + paths.set_data), and its *parent directories*
hold printer.cfg (which carries printer credentials), klippy.log, the uploaded
gcodes, the other UI's tree and the klipper API unix socket. So "can a request
escape the doc root" is not academic here: an escape reads secrets.

Offline: a proxy.Server bound to 127.0.0.1 on an ephemeral port serves a
throwaway doc root under KLIPPERFORMAC_DATA; the upstream port is a refused
sentinel so no request can ever reach a live Moonraker; every server is shut
down by the fixture.
"""
import http.client
import importlib
import os
import posixpath
import shutil
import socket
import threading
import types
import urllib.parse

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

import klipperformac.proxy as proxy_mod

STACK_PORTS = frozenset({7125, 7126, 8080, 8081, 8082})
SENTINEL_UPSTREAM = 1

CFG = "CFG-MARKER-4f2b-do-not-serve"
LOG = "LOG-MARKER-7c19-do-not-serve"
GCODE = "GCODE-MARKER-0e55-do-not-serve"
FLUIDD = "FLUIDD-MARKER-3ad8-do-not-serve"
OTHER = "OTHERUI-MARKER-9bb6-do-not-serve"


@pytest.fixture
def proxymod(tmp_path, monkeypatch):
    monkeypatch.setenv("KLIPPERFORMAC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KLIPPERFORMAC_DATA", str(tmp_path / "data"))
    mod = importlib.reload(proxy_mod)
    monkeypatch.setattr(mod.UIPandler, "upstream_port", SENTINEL_UPSTREAM,
                        raising=False)

    real_create = socket.create_connection

    def create_connection(address, *a, **kw):
        assert str(address[0]) == "127.0.0.1", \
            "proxy must dial loopback only, got %r" % (address,)
        assert address[1] not in STACK_PORTS, \
            "test tried to reach a live stack port: %r" % (address,)
        return real_create(address, *a, **kw)

    monkeypatch.setattr(mod, "socket",
                        types.SimpleNamespace(create_connection=create_connection))
    return mod


@pytest.fixture
def layout(stack):
    """A realistic KLIPPERFORMAC_DATA tree; returns (docroot, markers)."""
    doc = stack.paths.WEB_MAINSAIL
    doc.mkdir(parents=True, exist_ok=True)
    (doc / "index.html").write_text("MAINSAIL-INDEX-OK")
    (doc / "assets").mkdir(exist_ok=True)
    (doc / "assets" / "app.js").write_text("APPJS")
    (doc / "img").mkdir(exist_ok=True)
    (doc / "img" / "logo.svg").write_text("<svg/>")

    stack.paths.CONFIG.mkdir(parents=True, exist_ok=True)
    (stack.paths.PRINTER_CFG).write_text(
        "[mcu]\nserial: /dev/cu.usbmodem1\n"
        "# password: %s\n" % CFG)
    stack.paths.LOGS.mkdir(parents=True, exist_ok=True)
    (stack.paths.KLIPPY_LOG).write_text("stats %s\n" % LOG)
    stack.paths.RUN.mkdir(parents=True, exist_ok=True)
    (stack.paths.DATA / "gcodes").mkdir(exist_ok=True)
    (stack.paths.DATA / "gcodes" / "part.gcode").write_text("; %s\n" % GCODE)
    fluidd = stack.paths.WEB_FLUIDD
    fluidd.mkdir(parents=True, exist_ok=True)
    (fluidd / "index.html").write_text(FLUIDD)
    other = stack.paths.WEB / "otherui"
    other.mkdir(parents=True, exist_ok=True)
    (other / "secret").write_text(OTHER)

    # a real unix socket file inside the data tree (klippy's API socket)
    sock_path = str(stack.paths.API_SOCKET)
    holder = None
    try:
        holder = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        holder.bind(sock_path)
    except OSError:
        holder = None

    try:
        passwd = open("/etc/passwd", "rb").read(4096)
    except OSError:
        passwd = b""

    markers = [CFG.encode(), LOG.encode(), GCODE.encode(), FLUIDD.encode(),
               OTHER.encode()]
    root_record = b""
    for line in passwd.split(b"\n"):
        if b":0:0:" in line or line.count(b":") >= 4:
            root_record = line.strip()
            break
    if len(root_record) > 8:
        markers.append(root_record)
    yield {"doc": doc, "data": stack.paths.DATA, "markers": markers,
           "root_record": root_record}
    if holder is not None:
        holder.close()
        try:
            os.unlink(sock_path)
        except OSError:
            pass


@pytest.fixture
def server(proxymod, layout):
    httpd = proxymod.Server(("127.0.0.1", 0),
                            lambda *a, **kw: proxymod.UIPandler(
                                *a, directory=str(layout["doc"]), **kw))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    httpd.port = httpd.server_address[1]
    assert httpd.server_address[0] == "127.0.0.1"
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- helpers ----------------------------------------------------------------
def get(port, target, method="GET", headers=(("Host", "127.0.0.1"),)):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.putrequest(method, target)
        for k, v in headers:
            conn.putheader(k, v)
        conn.endheaders()
        resp = conn.getresponse()
        return resp.status, resp.read(), dict(resp.getheaders())
    finally:
        conn.close()


def raw(port, target_bytes, http_version=b"HTTP/1.1"):
    """Byte-exact request line, for targets http.client would normalise/refuse."""
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        s.sendall(b"GET " + target_bytes + b" " + http_version + b"\r\n"
                  b"Host: 127.0.0.1\r\n\r\n")
        data = b""
        s.settimeout(10)
        while len(data) < 200000:
            try:
                chunk = s.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            data += chunk
        return data or None
    finally:
        s.close()


def leaks(body, layout):
    return [m for m in layout["markers"] if m and m in body]


# ---------------------------------------------------------------------------
# Containment: the doc root must be a hard boundary.
# ---------------------------------------------------------------------------
RAW_TARGETS = [
    b"/../config/printer.cfg",
    b"/../../config/printer.cfg",
    b"/../../../..%2fconfig%2fprinter.cfg",
    b"/%2e%2e/config/printer.cfg",
    b"/%2E%2E/%2E%2E/logs/klippy.log",
    b"/..%2f..%2f..%2fetc%2fpasswd",
    b"/%c0%ae%c0%ae/%c0%ae%c0%ae/etc/passwd",
    b"/....//....//....//etc/passwd",
    b"/..;/..;/config/printer.cfg",
    b"/gcodes/../../config/printer.cfg",
    b"/index.html/../../config/printer.cfg",
    b"/assets/../../../logs/klippy.log",
    b"/web/fluidd/index.html",
    b"/../fluidd/index.html",
    b"/../../web/otherui/secret",
    b"/run/klippy.sock",
    b"/../run/klippy.sock",
    b"/%2e%2e%5c%2e%2e%5cconfig%5cprinter.cfg",
    b"/\\..\\..\\config\\printer.cfg",
    b"//etc/passwd",
    b"///etc/passwd",
    b"/./../../etc/passwd",
    b"/~/config/printer.cfg",
    b"/%7e/config/printer.cfg",
    b"/~unknownuser/config/printer.cfg",
    b"/C:",
    b"/%2F%2Fetc%2Fpasswd",
    b"/printer.cfg",
    b"/config/printer.cfg",
    b"/logs/klippy.log",
    b"/gcodes/part.gcode",
    b"/assets/../../gcodes/part.gcode",
    b"/....../....../etc/passwd",
    b"/%252e%252e/%252e%252e/etc/passwd",
    b"/..%252f..%252fetc%252fpasswd",
]


@pytest.mark.parametrize("target", RAW_TARGETS)
def test_traversal_never_reads_a_file_outside_the_docroot(server, layout,
                                                          target):
    body = raw(server.port, target) or b""
    assert not leaks(body, layout), "doc root escaped through %r" % target


@pytest.mark.parametrize("target", RAW_TARGETS)
def test_traversal_never_answers_200_with_outside_content(server, layout,
                                                          target):
    body = raw(server.port, target)
    assert body is not None, "connection closed with no HTTP response: %r" % target
    assert body.startswith(b"HTTP/1."), body[:40]
    status = int(body.split(b" ")[1])
    if status == 200:
        # Only acceptable when the bytes really came from inside the doc root.
        assert leaks(body, layout) == [], "200 with outside content for %r" % target
        text = body.decode("latin-1")
        assert "MAINSAIL-INDEX-OK" in text or "APPJS" in text or \
            "<svg/>" in text or "Directory listing" in text, \
            "unexpected 200 body for %r" % target


def test_etc_passwd_is_not_served_even_by_absolute_path(server, layout):
    for target in ("/etc/passwd", "/../../etc/passwd", "/%2e%2e/etc/passwd",
                   "/etc/passwd/"):
        status, body, _h = get(server.port, target)
        assert layout["root_record"] not in body, target


def test_the_two_ui_trees_never_bleed_into_each_other(server, layout):
    """Mainsail is served on 8080, Fluidd on 8081 by separate processes; a
    request to either must never see the other's files."""
    status, body, _h = get(server.port, "/index.html")
    assert status == 200 and body == b"MAINSAIL-INDEX-OK"
    status, body, _h = get(server.port, "/../fluidd/index.html")
    assert FLUIDD.encode() not in body
    status2, body2, _h = get(server.port, "/web/fluidd/index.html")
    assert FLUIDD.encode() not in body2


def test_dot_directories_inside_the_data_tree_are_not_reachable(server, layout):
    for target in ("/../", "/../../", "/../../../", "/../config/",
                   "/../logs/", "/..%2f", "/%2e%2e/"):
        status, body, _h = get(server.port, urllib.parse.quote(target, safe="/%"))
        assert not leaks(body, layout), target


def test_no_absolute_filesystem_path_is_echoed_to_the_client(server, layout):
    doc = str(layout["doc"])
    data = str(layout["data"])
    for target in ("/nope", "/assets/nope.js", "/nope/", "/%2e%2e/etc/passwd",
                   "/nonexistent/../missing", "/../config/printer.cfg"):
        _status, body, _h = get(server.port, target)
        assert doc.encode() not in body, target
        assert data.encode() not in body, target
    # the autoindex page (no index.html in a subtree) must not leak it either
    (layout["doc"] / "noindex").mkdir(exist_ok=True)
    (layout["doc"] / "noindex" / "visible.txt").write_text("V")
    try:
        status, body, _h = get(server.port, "/noindex/")
        assert status == 200
        assert doc.encode() not in body and data.encode() not in body
        assert b"visible.txt" in body
    finally:
        shutil.rmtree(str(layout["doc"] / "noindex"), ignore_errors=True)


# --- legal-but-nasty names must round trip ---------------------------------
NAMES = ["nor\nml.txt", "per%cent.txt", "plus+name.txt", "sp ace.txt",
         "dotdot..txt", "ünïcödé ñ.txt", "100%.txt", "back\\slash.txt",
         "que?ry.txt", "has#hash.txt", "a" * 200 + ".txt",
         "tab\tname.txt", "é.txt", "中文/文件.txt", "tilde~name.txt"]


def test_awkward_but_legal_filenames_are_served_byte_exact(server, layout):
    made = []
    for name in NAMES:
        f = layout["doc"] / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"BODY-" + name.encode("utf-8"))
        made.append(f)
    try:
        for name in NAMES:
            target = "/" + urllib.parse.quote(name, safe="/")
            status, body, _h = get(server.port, target)
            assert status == 200, (target, status)
            assert body == b"BODY-" + name.encode("utf-8"), target
            assert not leaks(body, layout)
    finally:
        for f in reversed(made):
            try:
                f.unlink()
                if f.parent.is_dir() and not any(f.parent.iterdir()):
                    f.parent.rmdir()
            except OSError:
                pass


def test_nfc_and_nfd_spellings_of_the_same_name_are_treated_consistently(
        server, layout):
    """macOS volumes are normalisation-insensitive; both spellings the browser
    might send must agree instead of one of them 404-ing a real file."""
    f = layout["doc"] / "café.txt"
    f.write_bytes(b"NFC-BODY")
    try:
        nfc = "/caf%C3%A9.txt"
        nfd = "/cafe%CC%81.txt"
        s1, b1, _ = get(server.port, nfc)
        s2, b2, _ = get(server.port, nfd)
        assert s1 == s2, (s1, s2)
        assert b1 == b2
        assert s1 == 200 and b1 == b"NFC-BODY"
    finally:
        f.unlink()


# --- symlinks that do not escape --------------------------------------------
def test_dangling_and_looping_symlinks_answer_http_and_do_not_hang(server,
                                                                   layout):
    doc = layout["doc"]
    made = []
    for name in ("dangling", "selfloop", "loopa", "loopb"):
        link = doc / name
        made.append(link)
    os.symlink(str(doc / "not-there"), str(doc / "dangling"))
    os.symlink("selfloop", str(doc / "selfloop"))
    os.symlink("loopb", str(doc / "loopa"))
    os.symlink("loopa", str(doc / "loopb"))
    try:
        for target in ("/dangling", "/selfloop", "/selfloop/", "/loopa",
                       "/loopa/", "/loopb/x.txt", "/dangling/../index.html"):
            status, body, _h = get(server.port, target)
            assert status in (200, 301, 403, 404), (target, status)
            assert not leaks(body, layout), target
        # the worker pool must still be healthy
        status, body, _h = get(server.port, "/index.html")
        assert status == 200 and body == b"MAINSAIL-INDEX-OK"
    finally:
        for link in made:
            try:
                link.unlink()
            except OSError:
                pass


def test_symlinks_that_stay_inside_the_docroot_keep_working(server, layout):
    """A fix for the outside-symlink hole must not break links inside the tree:
    real UI installs ship symlinked asset directories."""
    doc = layout["doc"]
    links = [doc / "assets-link", doc / "logo-link"]
    os.symlink(str(doc / "assets"), str(doc / "assets-link"))
    os.symlink(str(doc / "img" / "logo.svg"), str(doc / "logo-link"))
    try:
        status, body, _h = get(server.port, "/assets-link/app.js")
        assert status == 200 and body == b"APPJS"
        status, body, _h = get(server.port, "/logo-link")
        assert status == 200 and body == b"<svg/>"
    finally:
        for link in links:
            try:
                link.unlink()
            except OSError:
                pass


# --- permission edge --------------------------------------------------------
@pytest.mark.skipif(os.geteuid() == 0, reason="mode bits do not apply to root")
def test_unreadable_docroot_and_files_degrade_to_http_not_a_crash(proxymod,
                                                                  layout):
    locked_root = layout["data"] / "locked-web"
    locked_root.mkdir(exist_ok=True)
    (locked_root / "index.html").write_text("LOCKED-INDEX")
    os.chmod(str(locked_root), 0o000)
    readable_root = layout["doc"]
    (readable_root / "locked.txt").write_bytes(b"LOCKED-BODY")
    os.chmod(str(readable_root / "locked.txt"), 0o000)
    httpd = proxymod.Server(("127.0.0.1", 0),
                            lambda *a, **kw: proxymod.UIPandler(
                                *a, directory=str(locked_root), **kw))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        for target in ("/", "/index.html", "/locked.txt"):
            status, body, _h = get(httpd.server_address[1], target)
            assert status in (403, 404), (target, status)
            assert b"LOCKED-INDEX" not in body and b"LOCKED-BODY" not in body
            assert str(locked_root).encode() not in body
        httpd.shutdown()
        httpd = proxymod.Server(("127.0.0.1", 0),
                                lambda *a, **kw: proxymod.UIPandler(
                                    *a, directory=str(readable_root), **kw))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        status, body, _h = get(httpd.server_address[1], "/locked.txt")
        assert status in (403, 404), status
        assert body != b"LOCKED-BODY"
        status, body, _h = get(httpd.server_address[1], "/index.html")
        assert status == 200 and body == b"MAINSAIL-INDEX-OK"
    finally:
        httpd.shutdown()
        httpd.server_close()
        os.chmod(str(locked_root), 0o755)
        os.chmod(str(readable_root / "locked.txt"), 0o644)
        shutil.rmtree(str(locked_root), ignore_errors=True)
        try:
            (readable_root / "locked.txt").unlink()
        except OSError:
            pass


# --- the relay half must not turn into a file reader ------------------------
@pytest.mark.parametrize("target", [
    "/printer/../../config/printer.cfg",
    "/printer/%2e%2e/%2e%2e/logs/klippy.log",
    "/server/files/../../../etc/passwd",
    "/websocket/../../config/printer.cfg",
    "/api/../../config/printer.cfg",
])
def test_api_prefixed_traversal_is_never_served_from_disk(server, layout,
                                                          target):
    status, body, _h = get(server.port, target)
    assert not leaks(body, layout), target
    assert status in (502, 504, 400, 501), (target, status, body[:60])


# --- translate_path: the pure invariant (no sockets) -----------------------
def translated(mod, docroot, target):
    h = object.__new__(mod.UIPandler)
    h.directory = str(docroot)
    return mod.UIPandler.translate_path(h, target)


@pytest.mark.parametrize("target", RAW_TARGETS + [
    "/", "", "//", "/..", "/../..", "/a/../b", "/a/./b", "/%2e", "/%2e%2e",
    "/assets/../../assets/app.js", "/x/" * 200, "/" + "y" * 5000,
])
def test_translate_path_stays_under_the_docroot_lexically(proxymod, layout,
                                                          target):
    if isinstance(target, bytes):
        target = target.decode("latin-1")
    doc = os.path.realpath(str(layout["doc"]))
    out = translated(proxymod, doc, target)
    assert out == doc or out.startswith(doc + os.sep), (target, out)


SEGMENTS = st.sampled_from(
    ["..", ".", "%2e%2e", "%2E%2E", "...", "", "/", "assets", "index.html",
     "config", "printer.cfg", "logs", "klippy.log", "gcodes", "part.gcode",
     "run", "klippy.sock", "..%2f", "%2f", "~", "img", "logo.svg", "nope"])


@settings(max_examples=80, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture,
                                 HealthCheck.too_slow])
@given(segments=st.lists(SEGMENTS, min_size=1, max_size=8))
def test_property_translate_path_never_escapes(proxymod, layout, segments):
    doc = os.path.realpath(str(layout["doc"]))
    target = "/" + "/".join(segments)
    out = translated(proxymod, doc, target)
    assert out == doc or out.startswith(doc + os.sep), (target, out)
    assert posixpath.normpath(out).startswith(doc), (target, out)


@settings(max_examples=40, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture,
                                 HealthCheck.too_slow])
@given(segments=st.lists(SEGMENTS, min_size=1, max_size=6))
def test_property_hostile_urls_never_leak_and_always_answer(server, layout,
                                                            segments):
    target = "/" + "/".join(segments)
    body = raw(server.port, target.encode("utf-8", "surrogateescape"))
    assert body is not None, "no HTTP response for %r" % target
    assert body.startswith(b"HTTP/1."), body[:40]
    assert not leaks(body, layout), target


def test_server_is_still_healthy_after_the_whole_battery(server, layout):
    for target in RAW_TARGETS:
        try:
            raw(server.port, target)
        except OSError:
            pass
    status, body, _h = get(server.port, "/index.html")
    assert status == 200 and body == b"MAINSAIL-INDEX-OK"
    status, body, _h = get(server.port, "/assets/app.js")
    assert status == 200 and body == b"APPJS"
