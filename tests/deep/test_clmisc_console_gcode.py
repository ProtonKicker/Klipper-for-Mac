"""Deep tests: ``klipperformac gcode`` and the klippy console PTY helper.

Covered (missed by the first suite):
  * ``_klippy_console`` when ``KLIPPERFORMAC_CONSOLE`` points at: nothing, a
    directory, /dev/null (immediate EOF forever), a FIFO with no peer, a
    symlink loop, an unreadable file, a real PTY that never answers (timeout
    behaviour) and a PTY that answers / errors / floods / speaks unicode.
  * ``cmd_gcode`` routing (console verbs vs HTTP), the URL it builds for
    hostile scripts, ``--status`` JSON handling and its failure paths.

SAFETY: an autouse fixture pins ``KLIPPERFORMAC_CONSOLE`` inside ``tmp_path``
and wraps ``os.open`` so the live klippy console (``/tmp/printer``), any
``/dev/cu.*`` serial device and the real ``~/.klipperformac`` can never be
opened by these tests.  HTTP is faked: no socket is ever created, so ports
7125/8080/8081 are untouched.

``xfail(strict=True)`` marks encode product bugs that are still present.
"""
import argparse
import io
import json
import os
import pty
import pwd
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, unquote, urlparse

import pytest

real_open = os.open
_REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir
# Everything a live stack could be reached through.  These tests are offline:
# opening any of them would mean touching the user's printer or install.
DENY = ("/tmp/printer", "/dev/cu", "/dev/tty.usb", "/dev/tty.usbserial",
        _REAL_HOME + "/.klipperformac", _REAL_HOME + "/KlipperData",
        _REAL_HOME + "/.maklipper", _REAL_HOME + "/Documents/Klipper for Mac")


def _guard_open(path, *args, **kwargs):
    if not isinstance(path, int):
        text = str(os.fspath(path))
        for bad in DENY:
            if text == bad or text.startswith(bad):
                raise AssertionError("offline test refused to open %r" % (text,))
    return real_open(path, *args, **kwargs)


@pytest.fixture(autouse=True)
def sandbox_console(monkeypatch, tmp_path):
    """Every test in this file gets a console path inside its own sandbox."""
    monkeypatch.setattr(os, "open", _guard_open)
    target = tmp_path / "printer"
    monkeypatch.setenv("KLIPPERFORMAC_CONSOLE", str(target))
    yield target


@pytest.fixture
def gcode_args():
    def build(script=None, status=False):
        return argparse.Namespace(script=script or [], status=status)
    return build


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeUrlopen:
    """Stands in for urllib.request.urlopen; records requests, replays bodies."""

    def __init__(self, body=b"{}", exc=None):
        self.body = body
        self.exc = exc
        self.requests = []

    def __call__(self, req, *args, **kwargs):
        if self.exc:
            raise self.exc
        url = req if isinstance(req, str) else req.full_url
        self.requests.append((url, kwargs, getattr(req, "method", None),
                              getattr(req, "data", None)))
        return FakeResponse(self.body)

    @property
    def urls(self):
        return [r[0] for r in self.requests]


class ConsoleSpy:
    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply or "(spied)"

    def __call__(self, cmd, timeout=6):
        self.calls.append((cmd, timeout))
        print(self.reply)


def run_console(stack_cli, cmd, timeout=0.5):
    return stack_cli._klippy_console(cmd, timeout=timeout)


@pytest.fixture
def cli(stack):
    return stack.cli


# ---------------------------------------------------------------------------
# the console path is not there / not usable
# ---------------------------------------------------------------------------
def test_missing_console_is_a_clean_error(cli, sandbox_console, capfd):
    with pytest.raises(SystemExit) as exc:
        run_console(cli, "STATUS")
    assert exc.value.code == 1
    err = capfd.readouterr().err
    assert "No Klipper console at " in err
    assert str(sandbox_console) in err
    assert "is the stack up" in err
    assert "Traceback" not in err


def test_default_console_path_is_the_klipper_pty(cli, monkeypatch, capfd):
    """With the env var unset the console defaults to /tmp/printer; the guard
    says 'is the stack up' and never opens it in this process."""
    monkeypatch.delenv("KLIPPERFORMAC_CONSOLE", raising=False)
    opened = []
    monkeypatch.setattr(os, "path", os.path)

    def no_exists(path):
        opened.append(str(path))
        return False

    monkeypatch.setattr(os.path, "exists", no_exists)
    with pytest.raises(SystemExit) as exc:
        run_console(cli, "STATUS")
    assert exc.value.code == 1
    assert opened and opened[0] == "/tmp/printer"
    assert "/tmp/printer" in capfd.readouterr().err


def test_console_symlink_loop_is_reported_not_raised(cli, sandbox_console, capfd):
    """os.path.exists() swallows ELOOP, so this stays the 'no console' path."""
    loop = sandbox_console
    os.symlink(loop, str(loop))
    assert not os.path.exists(str(loop))
    with pytest.raises(SystemExit) as exc:
        run_console(cli, "STATUS")
    assert exc.value.code == 1
    assert "Traceback" not in capfd.readouterr().err


@pytest.mark.xfail(strict=True, reason="BUG DEEP: _klippy_console checks "
                   "os.path.exists() but then os.open()s the path unchecked, so "
                   "KLIPPERFORMAC_CONSOLE pointing at a directory dies with a raw "
                   "IsADirectoryError traceback instead of 'no console'")
def test_console_path_that_is_a_directory_is_a_clean_error(cli, sandbox_console, capfd):
    os.mkdir(str(sandbox_console))
    with pytest.raises(SystemExit) as exc:
        run_console(cli, "STATUS")
    assert exc.value.code == 1
    assert "Traceback" not in capfd.readouterr().err


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
@pytest.mark.xfail(strict=True, reason="BUG DEEP: an unreadable console node "
                   "(mode 000, or a pty owned by another user) escapes from "
                   "_klippy_console as a PermissionError traceback")
def test_unreadable_console_path_is_a_clean_error(cli, sandbox_console, capfd):
    sandbox_console.write_text("x\n")
    os.chmod(str(sandbox_console), 0o000)
    try:
        with pytest.raises(SystemExit) as exc:
            run_console(cli, "STATUS")
        assert exc.value.code == 1
        assert "Traceback" not in capfd.readouterr().err
    finally:
        os.chmod(str(sandbox_console), 0o600)


@pytest.mark.xfail(strict=True, reason="BUG DEEP: _klippy_console() guards only "
                   "os.path.exists(), so KLIPPERFORMAC_CONSOLE pointing at a unix "
                   "socket (e.g. the klippy API socket, a plausible typo) escapes "
                   "os.open as a raw OSError traceback")
def test_console_path_that_is_a_socket_is_a_clean_error(cli, sandbox_console, capfd,
                                                        monkeypatch):
    import socket as socket_module

    sock = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    sock.bind(str(sandbox_console))
    sock.listen(1)
    try:
        with pytest.raises(SystemExit) as exc:
            run_console(cli, "STATUS")
        assert exc.value.code == 1
        assert "Traceback" not in capfd.readouterr().err
    finally:
        sock.close()


def test_fifo_console_without_a_peer_does_not_hang(cli, sandbox_console, capfd):
    """A stale mkfifo(1) node: opening O_RDWR succeeds and our own write loops
    back, which must not wedge the CLI."""
    os.mkfifo(str(sandbox_console))
    start = time.time()
    run_console(cli, "STATUS", timeout=0.5)
    assert time.time() - start < 5.0
    assert "Traceback" not in capfd.readouterr().err


@pytest.mark.xfail(strict=True, reason="BUG DEEP: _klippy_console never notices "
                   "EOF - os.read() returns b'' and select() keeps reporting the fd "
                   "ready, so `gcode STATUS` against a dead console node busy-spins "
                   "at 100% CPU for the whole timeout (6s by default)")
def test_eof_console_does_not_busy_spin(cli, monkeypatch, capfd):
    monkeypatch.setenv("KLIPPERFORMAC_CONSOLE", "/dev/null")
    start_cpu = time.process_time()
    start_wall = time.time()
    run_console(cli, "STATUS", timeout=0.5)
    cpu = time.process_time() - start_cpu
    wall = time.time() - start_wall
    assert cpu < 0.05, "burned %.2fs of CPU waiting for a console at EOF" % cpu
    assert wall < 0.2, "held the terminal for %.2fs" % wall


# ---------------------------------------------------------------------------
# a real PTY: the timeout behaviour the brief asks about
# ---------------------------------------------------------------------------
def _open_sandbox_pty():
    master, slave = pty.openpty()
    name = os.ttyname(slave)
    os.close(slave)          # the CLI must be the only slave reader
    return master, name


def test_deaf_console_returns_within_the_timeout(cli, monkeypatch, capfd):
    """Console open, klippy not answering: bounded by `timeout`, says so."""
    master, name = _open_sandbox_pty()
    try:
        monkeypatch.setenv("KLIPPERFORMAC_CONSOLE", name)
        start = time.time()
        run_console(cli, "STATUS", timeout=0.6)
        took = time.time() - start
        out = capfd.readouterr().out
        assert 0.2 <= took <= 2.5, "console wait took %.2fs" % took
        assert "(no response)" in out
    finally:
        os.close(master)


def test_answering_console_stops_at_the_terminal_ok(cli, monkeypatch, capfd):
    master, name = _open_sandbox_pty()
    try:
        monkeypatch.setenv("KLIPPERFORMAC_CONSOLE", name)
        threading.Timer(0.05, lambda: os.write(master, b"TICK 1\nTICK 2\nok\n")).start()
        start = time.time()
        run_console(cli, "STATUS", timeout=5)
        took = time.time() - start
        out = capfd.readouterr().out
        assert "TICK 1" in out and "TICK 2" in out
        assert took < 2.0, "did not stop at 'ok' (%.2fs)" % took
    finally:
        os.close(master)


def test_console_error_reply_is_surfaced(cli, monkeypatch, capfd):
    master, name = _open_sandbox_pty()
    try:
        monkeypatch.setenv("KLIPPERFORMAC_CONSOLE", name)
        threading.Timer(0.05, lambda: os.write(
            master, b"!! Unknown command: NOPE\n")).start()
        run_console(cli, "NOPE", timeout=5)
        out = capfd.readouterr().out
        assert "!!" in out
    finally:
        os.close(master)


def test_console_unicode_and_undecodable_bytes_do_not_crash(cli, monkeypatch, capfd):
    master, name = _open_sandbox_pty()
    payload = "温度 ✓ ok-ish\n".encode("utf-8") + b"\xff\xfe broken\n" + b"ok\n"
    try:
        monkeypatch.setenv("KLIPPERFORMAC_CONSOLE", name)
        threading.Timer(0.05, lambda: os.write(master, payload)).start()
        run_console(cli, "STATUS", timeout=5)
        out = capfd.readouterr().out
        assert "温度" in out
    finally:
        os.close(master)


def test_console_endless_flood_is_still_bounded(cli, monkeypatch, capfd):
    """A chatty console (no terminator) must stop at the deadline, not earlier
    and not never."""
    master, name = _open_sandbox_pty()
    stop = [False]

    def flood():
        chunk = b"NOISE " * 600 + b"\n"
        while not stop[0]:
            try:
                os.write(master, chunk)
            except OSError:
                return
            time.sleep(0.01)

    t = threading.Thread(target=flood, daemon=True)
    t.start()
    try:
        monkeypatch.setenv("KLIPPERFORMAC_CONSOLE", name)
        start = time.time()
        run_console(cli, "STATUS", timeout=0.5)
        took = time.time() - start
        assert took <= 3.0, "flooded console outran the deadline (%.2fs)" % took
    finally:
        stop[0] = True
        t.join(timeout=2)
        os.close(master)
    capfd.readouterr()


# ---------------------------------------------------------------------------
# cmd_gcode routing: console verbs never touch HTTP
# ---------------------------------------------------------------------------
CONSOLE_VERBS = ["STATUS", "RESTART", "FIRMWARE_RESTART", "HELP",
                 "QUERY_ENDSTOPS"]


@pytest.mark.parametrize("verb", CONSOLE_VERBS)
def test_console_verbs_use_the_pty_and_no_network(cli, gcode_args, monkeypatch, verb):
    spy = ConsoleSpy()
    monkeypatch.setattr(cli, "_klippy_console", spy)
    net = FakeUrlopen()
    monkeypatch.setattr(urllib.request, "urlopen", net)
    cli.cmd_gcode(gcode_args([verb]))
    assert spy.calls == [(verb, 6)]
    assert net.requests == []


@pytest.mark.parametrize("verb", CONSOLE_VERBS)
def test_console_verb_matching_is_case_insensitive(cli, gcode_args, monkeypatch, verb):
    spy = ConsoleSpy()
    monkeypatch.setattr(cli, "_klippy_console", spy)
    cli.cmd_gcode(gcode_args([verb.lower()]))
    assert len(spy.calls) == 1


def test_console_verb_with_trailing_space_still_routes_to_the_pty(
        cli, gcode_args, monkeypatch):
    spy = ConsoleSpy()
    monkeypatch.setattr(cli, "_klippy_console", spy)
    cli.cmd_gcode(gcode_args(["  STATUS  "]))
    assert len(spy.calls) == 1


def test_console_verb_with_extra_words_is_sent_as_gcode(cli, gcode_args, monkeypatch):
    """'STATUS' plus anything else is not a known console verb: it must go over
    HTTP rather than being half-recognised."""
    spy = ConsoleSpy()
    monkeypatch.setattr(cli, "_klippy_console", spy)
    net = FakeUrlopen()
    monkeypatch.setattr(urllib.request, "urlopen", net)
    cli.cmd_gcode(gcode_args(["STATUS", "extra"]))
    assert spy.calls == []
    assert len(net.requests) == 1


def test_real_gcode_is_posted_to_the_configured_loopback_port(
        cli, gcode_args, monkeypatch):
    net = FakeUrlopen()
    monkeypatch.setattr(urllib.request, "urlopen", net)
    cli.cmd_gcode(gcode_args(["G1", "X10", "F600"]))
    (url, kwargs, method, data), = net.requests
    parsed = urlparse(url)
    assert parsed.scheme == "http" and parsed.hostname == "localhost"
    assert parsed.port == cli.paths.MOONRAKER_PORT
    assert parsed.path == "/printer/gcode/script"
    assert method == "POST" and data == b""
    assert kwargs["timeout"] == 10
    assert parse_qs(parsed.query)["script"] == ["G1 X10 F600"]


def test_gcode_never_targets_a_non_loopback_host(cli, gcode_args, monkeypatch):
    net = FakeUrlopen()
    monkeypatch.setattr(urllib.request, "urlopen", net)
    for script in (["G28"], ["M106", "S255"]):
        cli.cmd_gcode(gcode_args(script))
    for (url, _kw, _m, _d) in net.requests:
        assert urlparse(url).hostname in ("localhost", "127.0.0.1")


def test_gcode_console_branch_never_opens_a_socket(cli, monkeypatch, capfd):
    """Even a broken console must not fall through to HTTP."""
    monkeypatch.setenv("KLIPPERFORMAC_CONSOLE", str(cli.paths.DATA / "absent"))

    def no_socket(*a, **k):
        raise AssertionError("cmd_gcode opened a real socket")

    import socket as socket_module
    monkeypatch.setattr(socket_module, "socket", no_socket)
    with pytest.raises(SystemExit):
        cli.cmd_gcode(argparse.Namespace(script=["STATUS"], status=False))


@pytest.mark.parametrize("script", [
    ["G1 X1 Y2 Z3 E4 F5"],
    ["M117", "hello, world"],
    ["SET_GCODE_VARIABLE", "MACRO=v1", "VALUE=1"],
    ["G4", "P0.5", "#comment"],
    ["M117", "%percent&and+plus"],
    ["M117", "温度 ✓"],
    ["M117", "a\tb"],
    ["g1", "x-1.5"],
])
def test_gcode_scripts_are_percent_encoded_into_one_query_value(
        cli, gcode_args, monkeypatch, script):
    net = FakeUrlopen()
    monkeypatch.setattr(urllib.request, "urlopen", net)
    cli.cmd_gcode(gcode_args(script))
    (url, _kw, _m, _d), = net.requests
    assert parse_qs(urlparse(url).query, keep_blank_values=True)["script"] == [
        " ".join(script)]


def test_gcode_query_injection_cannot_add_parameters(cli, gcode_args, monkeypatch):
    """A script containing '&script=' or a '#' must stay one opaque value: no
    extra query parameters, no fragment."""
    net = FakeUrlopen()
    monkeypatch.setattr(urllib.request, "urlopen", net)
    evil = 'M117 x&script=RESTART#frag'
    cli.cmd_gcode(gcode_args(evil.split()))
    (url, _kw, _m, _d), = net.requests
    parsed = urlparse(url)
    assert parsed.fragment == ""
    assert list(parse_qs(parsed.query)) == ["script"]
    assert parse_qs(parsed.query)["script"] == [evil]


from hypothesis import HealthCheck, example, given, settings, strategies as st

_gcode_text = st.text(max_size=120)


@given(script=st.lists(_gcode_text, max_size=6).filter(
    lambda parts: " ".join(parts).strip().upper() not in CONSOLE_VERBS))
@settings(max_examples=120, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@example(["M117", "a b"])
@example(["G1", "X-1.5", "%20"])
@example(["SET_GCODE_VARIABLE", "MACRO=x", "VALUE=&script=STATUS"])
@example(["M117", "#"])
@example(["M117", "+"])
def test_any_script_round_trips_through_the_url_unchanged(cli, gcode_args,
                                                          monkeypatch, script):
    net = FakeUrlopen()
    monkeypatch.setattr(urllib.request, "urlopen", net)
    cli.cmd_gcode(gcode_args(script))
    (url, _kw, _m, _d), = net.requests
    parsed = urlparse(url)
    assert parsed.path == "/printer/gcode/script"
    # only one parameter, and nothing raw that could change the request
    query = parsed.query
    assert query.startswith("script=")
    assert "&" not in query and "#" not in query and " " not in query
    assert parse_qs(query, keep_blank_values=True) == {"script": [" ".join(script)]}


def test_huge_script_is_encoded_without_truncation(cli, gcode_args, monkeypatch):
    net = FakeUrlopen()
    monkeypatch.setattr(urllib.request, "urlopen", net)
    big = "M117 " + "x" * 200000
    cli.cmd_gcode(gcode_args([big]))
    (url, _kw, _m, _d), = net.requests
    assert parse_qs(urlparse(url).query)["script"] == [big]


@pytest.mark.xfail(strict=True, reason="BUG DEEP: `klipperformac gcode` with no "
                   "arguments POSTs an empty script and prints 'sent: ' - the CLI "
                   "should explain that there is nothing to send")
def test_empty_script_is_rejected_not_sent(cli, gcode_args, monkeypatch, capfd):
    net = FakeUrlopen()
    monkeypatch.setattr(urllib.request, "urlopen", net)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_gcode(gcode_args([]))
    assert exc.value.code == 1
    assert net.requests == []
    assert "sent:" not in capfd.readouterr().out


# ---------------------------------------------------------------------------
# gcode --status
# ---------------------------------------------------------------------------
def test_status_prints_pretty_json_and_truncates(cli, gcode_args, monkeypatch, capfd):
    payload = {"result": {"heaters": ["h" * 4000]}}
    net = FakeUrlopen(body=json.dumps(payload).encode())
    monkeypatch.setattr(urllib.request, "urlopen", net)
    cli.cmd_gcode(gcode_args(status=True))
    out, err = capfd.readouterr()
    assert err == ""
    assert 0 < len(out.rstrip("\n")) <= 1500
    assert out.startswith('{\n  "result"')


def test_status_url_is_the_objects_query(cli, gcode_args, monkeypatch):
    net = FakeUrlopen(body=b'{"result": {}}')
    monkeypatch.setattr(urllib.request, "urlopen", net)
    cli.cmd_gcode(gcode_args(status=True))
    (url, _kw, method, _d), = net.requests
    assert urlparse(url).path == "/printer/objects/query"
    assert "print_stats" in urlparse(url).query
    assert method is None          # plain GET


@pytest.mark.parametrize("body", [
    b"<!DOCTYPE html><h1>502 Bad Gateway</h1>",
    b"",
    b"not json at all",
    b'{"result": {"truncated',
])
@pytest.mark.xfail(strict=True, reason="BUG DEEP: cmd_gcode parses the --status "
                   "body with json.loads() OUTSIDE the try/except, so anything that "
                   "answers on the moonraker port with non-JSON (a proxy error page, "
                   "a half-written response) dies with a JSONDecodeError traceback")
def test_status_with_a_non_json_body_is_a_clean_error(cli, gcode_args, monkeypatch,
                                                     capfd, body):
    monkeypatch.setattr(urllib.request, "urlopen", FakeUrlopen(body=body))
    with pytest.raises(SystemExit) as exc:
        cli.cmd_gcode(gcode_args(status=True))
    assert exc.value.code == 1
    assert "Traceback" not in capfd.readouterr().err


@pytest.mark.parametrize("body", [b"\xff\xfe", b"\x80\x81 g\x00"])
def test_status_with_an_undecodable_body_is_a_clean_error(cli, gcode_args,
                                                          monkeypatch, capfd, body):
    """The decode happens inside the try, so undecodable bytes already get the
    documented 'not reachable' exit (this is the good half of the --status
    error handling; it must stay that way)."""
    monkeypatch.setattr(urllib.request, "urlopen", FakeUrlopen(body=body))
    with pytest.raises(SystemExit) as exc:
        cli.cmd_gcode(gcode_args(status=True))
    assert exc.value.code == 1
    err = capfd.readouterr().err
    assert "Moonraker not reachable" in err and "Traceback" not in err


@pytest.mark.parametrize("body", [b'{"result": {}}', b"[1,2,3]", b'"a string"',
                                 b"null", b"123"])
def test_status_accepts_any_json_document(cli, gcode_args, monkeypatch, capfd, body):
    """Valid JSON of any shape must print and exit 0 - never a traceback."""
    monkeypatch.setattr(urllib.request, "urlopen", FakeUrlopen(body=body))
    cli.cmd_gcode(gcode_args(status=True))
    out, err = capfd.readouterr()
    assert err == "" or "Traceback" not in err


def test_status_unreachable_moonraker_is_a_clean_error(cli, gcode_args, monkeypatch,
                                                       capfd):
    monkeypatch.setattr(urllib.request, "urlopen",
                        FakeUrlopen(exc=urllib.error.URLError("connection refused")))
    with pytest.raises(SystemExit) as exc:
        cli.cmd_gcode(gcode_args(status=True))
    assert exc.value.code == 1
    err = capfd.readouterr().err
    assert "Moonraker not reachable" in err
    assert "Traceback" not in err


def test_script_rejection_message_names_the_failure(cli, gcode_args, monkeypatch,
                                                    capfd):
    monkeypatch.setattr(urllib.request, "urlopen",
                        FakeUrlopen(exc=urllib.error.HTTPError(
                            "http://localhost:7125/printer/gcode/script",
                            400, "Bad Request", {}, None)))
    with pytest.raises(SystemExit) as exc:
        cli.cmd_gcode(gcode_args(["G1 X"]))
    assert exc.value.code == 1
    err = capfd.readouterr().err
    assert "rejected" in err and "Traceback" not in err


def test_status_huge_valid_payload_is_bounded(cli, gcode_args, monkeypatch, capfd):
    body = json.dumps({"result": {"history": ["x" * 1000] * 500}}).encode()
    monkeypatch.setattr(urllib.request, "urlopen", FakeUrlopen(body=body))
    cli.cmd_gcode(gcode_args(status=True))
    out, _ = capfd.readouterr()
    assert len(out) <= 1600
