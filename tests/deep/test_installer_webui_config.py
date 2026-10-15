"""Deep tests: installer `_download` (urllib faked) and `_patch_ui_config`.

HARD OFFLINE RULE: urllib.request.urlopen is replaced by a scripted fake in
every test that downloads, and by a tripwire that raises in all others, so no
socket is ever created. `_patch_ui_config` only writes inside the throwaway
KLIPPERFORMAC_DATA/tmp_path from the `stack` fixture.

`xfail(strict=True)` marks encode the expected-robust behaviour of product bugs
that are still present; everything else passes today.
"""
import json
import os
import shutil
import stat
import urllib.error
import urllib.request

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

CHUNK = 65536
URL = "https://github.com/meteyou/mainsail/releases/download/v2.19.0/mainsail.zip"


# --------------------------------------------------------------------------
# fake HTTP layer
# --------------------------------------------------------------------------
class FakeResponse:
    """What installer._download expects: a readable, closable context mgr."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.read_calls = 0
        self.close_calls = 0

    def read(self, size=-1):
        self.read_calls += 1
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def close(self):
        self.close_calls += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeUrlopen:
    """Records every request, then replays queued responses / exceptions."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append((req, timeout))
        if not self.script:
            raise AssertionError("fake urlopen ran out of scripted responses")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def urls(self):
        return [r.full_url if hasattr(r, "full_url") else str(r)
                for r, _ in self.requests]

    @property
    def headers(self):
        return [dict(r.headers) if hasattr(r, "headers") else {}
                for r, _ in self.requests]


@pytest.fixture
def installer_offline(stack, monkeypatch):
    """No real socket, ever, in this file. Returns the reloaded installer."""
    def _boom(*a, **k):
        raise AssertionError("installer reached the network for real")
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    return stack.installer


@pytest.fixture
def fake_http(stack, installer_offline, monkeypatch):
    """install(*script) swaps in a FakeUrlopen and returns it."""
    def install(*script):
        f = FakeUrlopen(script)
        monkeypatch.setattr(installer_offline.urllib.request, "urlopen", f)
        return f
    return install


# --------------------------------------------------------------------------
# _download
# --------------------------------------------------------------------------
def test_download_writes_exactly_the_streamed_bytes(installer_offline,
                                                    fake_http, tmp_path):
    f = fake_http(FakeResponse([b"A" * CHUNK, b"B" * 1000]))
    out = tmp_path / "ui.zip"
    installer_offline._download(URL, out)
    assert out.read_bytes() == b"A" * CHUNK + b"B" * 1000
    assert f.urls == [URL]


def test_download_identifies_itself_with_a_user_agent(installer_offline,
                                                      fake_http, tmp_path):
    f = fake_http(FakeResponse([b"x"]))
    installer_offline._download(URL, tmp_path / "a.zip")
    assert f.headers[0].get("User-agent") == "klipperformac"


def test_download_honours_the_timeout_argument(installer_offline, fake_http,
                                               tmp_path):
    f = fake_http(FakeResponse([b"x"]), FakeResponse([b"y"]))
    installer_offline._download(URL, tmp_path / "a.zip", timeout=7)
    assert f.requests[0][1] == 7
    installer_offline._download(URL, tmp_path / "b.zip")
    assert f.requests[1][1] == 60, "the default must stay bounded"


def test_download_closes_the_response_and_the_file(installer_offline,
                                                   fake_http, tmp_path):
    resp = FakeResponse([b"data"])
    fake_http(resp)
    out = tmp_path / "c.zip"
    installer_offline._download(URL, out)
    assert resp.close_calls == 1


def test_download_stops_at_the_size_cap(installer_offline, fake_http, tmp_path):
    """A runaway server must not fill the disk before setup gives up."""
    resp = FakeResponse([b"Z" * CHUNK] * 4000)
    fake_http(resp)
    out = tmp_path / "huge.zip"
    with pytest.raises(SystemExit) as exc:
        installer_offline._download(URL, out, max_bytes=1024 * 1024)
    assert "size cap" in str(exc.value)
    assert out.stat().st_size <= 1024 * 1024
    assert resp.read_calls <= 1024 * 1024 // CHUNK + 2, "it kept streaming"
    assert resp.close_calls == 1, "the runaway stream must be closed"


def test_download_of_a_zero_byte_body(installer_offline, fake_http, tmp_path):
    fake_http(FakeResponse([]))
    out = tmp_path / "empty.zip"
    installer_offline._download(URL, out)
    assert out.read_bytes() == b""


def test_download_with_a_zero_cap_writes_nothing(installer_offline, fake_http,
                                                 tmp_path):
    fake_http(FakeResponse([b"x" * 10]))
    out = tmp_path / "z.zip"
    with pytest.raises(SystemExit):
        installer_offline._download(URL, out, max_bytes=0)
    assert out.read_bytes() == b""


def test_download_of_a_200mb_body_streams_to_disk(installer_offline, fake_http,
                                                  tmp_path):
    total = 200 * 1024 * 1024
    fake_http(FakeResponse([b"Q" * CHUNK] * (total // CHUNK)))
    out = tmp_path / "big.zip"
    installer_offline._download(URL, out)
    assert out.stat().st_size == total


def test_download_creates_nothing_besides_the_given_path(
        installer_offline, fake_http, tmp_path):
    fake_http(FakeResponse([b"data"]))
    sub = tmp_path / "deep" / "nested"
    sub.mkdir(parents=True)
    out = sub / "ui.zip"
    installer_offline._download(URL, out)
    assert sorted(p.name for p in sub.iterdir()) == ["ui.zip"]


@pytest.mark.parametrize("error", [
    pytest.param(urllib.error.HTTPError(URL, 404, "Not Found", {}, None),
                 id="http-404"),
    pytest.param(urllib.error.URLError("no route to host"), id="dns-failure"),
    pytest.param(OSError("connection reset by peer"), id="connection-reset"),
    pytest.param(TimeoutError("timed out"), id="socket-timeout"),
])
def test_transport_errors_are_never_mistaken_for_success(
        installer_offline, fake_http, tmp_path, error):
    fake_http(error)
    out = tmp_path / "ui.zip"
    with pytest.raises(OSError):
        installer_offline._download(URL, out)
    assert not out.exists() or out.stat().st_size == 0, (
        "a failed download must not leave a plausible-looking archive")


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: _download() lets urllib errors (HTTPError / URLError / "
    "TimeoutError / OSError) escape untouched and fetch_webui() does not catch "
    "them either (installer.py:181-194 called from installer.py:216), so "
    "`klipperformac setup` on a plane, behind a captive portal, or during a "
    "GitHub outage prints a raw traceback instead of the CLI's SystemExit "
    "user-error, and the user cannot tell 'no network' from 'broken install'."))
def test_failed_download_is_a_clean_user_error(installer_offline, fake_http,
                                               tmp_path):
    fake_http(urllib.error.URLError("no route to host"))
    with pytest.raises(SystemExit) as exc:
        installer_offline._download(URL, tmp_path / "ui.zip")
    assert "download" in str(exc.value).lower()


def test_download_size_cap_is_inclusive_at_the_boundary(
        installer_offline, fake_http, tmp_path):
    """Exactly max_bytes must succeed; one byte more must not."""
    cap = 1024
    fake_http(FakeResponse([b"E" * cap]))
    out = tmp_path / "exact.zip"
    installer_offline._download(URL, out, max_bytes=cap)
    assert out.read_bytes() == b"E" * cap

    fake_http(FakeResponse([b"E" * cap, b"X"]))
    over = tmp_path / "over.zip"
    with pytest.raises(SystemExit):
        installer_offline._download(URL, over, max_bytes=cap)
    assert over.stat().st_size == cap


def test_download_of_many_small_chunks(installer_offline, fake_http, tmp_path):
    """A server may hand out 1 byte at a time; the result must be identical."""
    fake_http(FakeResponse([bytes([i % 256]) for i in range(2000)]))
    out = tmp_path / "drip.zip"
    installer_offline._download(URL, out)
    assert len(out.read_bytes()) == 2000


def test_download_accepts_bytes_written_as_a_path_object(
        installer_offline, fake_http, tmp_path):
    fake_http(FakeResponse([b"abc"]))
    out = tmp_path / "p.zip"
    installer_offline._download(URL, str(out))
    assert out.read_bytes() == b"abc"


# --------------------------------------------------------------------------
# _patch_ui_config
# --------------------------------------------------------------------------
@pytest.fixture
def stage_dir(tmp_path):
    def make(payload, name="config.json", subdir="stage"):
        d = tmp_path / subdir
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        if payload is not None:
            (d / name).write_text(payload)
        return d
    return make


def test_patch_ui_config_points_mainsail_at_the_proxy_origin(stack, stage_dir):
    d = stage_dir(json.dumps({"hostname": "192.168.1.50", "port": 7125,
                              "theme": "dark"}))
    stack.installer._patch_ui_config("mainsail", d)
    data = json.loads((d / "config.json").read_text())
    assert data["hostname"] is None and data["port"] is None
    assert data["theme"] == "dark", "unrelated keys must survive"


def test_patch_ui_config_points_fluidd_at_the_local_socket(stack, stage_dir):
    d = stage_dir(json.dumps({"socket_url": "ws://192.168.1.50:7125/websocket",
                              "title": "Fluid"}), subdir="stage-fluidd")
    stack.installer._patch_ui_config("fluidd", d)
    data = json.loads((d / "config.json").read_text())
    assert data["socket_url"] == "ws://localhost:{}/websocket".format(
        stack.paths.FLUIDD_PORT)
    assert data["title"] == "Fluid"


def test_patch_ui_config_follows_a_relocated_fluidd_port(stack, monkeypatch,
                                                         stage_dir):
    """KLIPPERFORMAC_FLUIDD_PORT changes the origin the UI must talk to."""
    monkeypatch.setattr(stack.paths, "FLUIDD_PORT", 9999)
    d = stage_dir(json.dumps({"socket_url": "ws://old"}), subdir="stage-port")
    stack.installer._patch_ui_config("fluidd", d)
    data = json.loads((d / "config.json").read_text())
    assert data["socket_url"] == "ws://localhost:9999/websocket"


def test_patch_ui_config_is_idempotent(stack, stage_dir):
    d = stage_dir(json.dumps({"hostname": "x", "port": 1, "socket_url": "ws://y"}),
                  subdir="stage-idem")
    before = (d / "config.json").read_bytes()
    stack.installer._patch_ui_config("mainsail", d)
    once = (d / "config.json").read_bytes()
    stack.installer._patch_ui_config("mainsail", d)
    assert (d / "config.json").read_bytes() == once
    assert once != before


def test_patch_ui_config_preserves_unicode_values(stack, stage_dir):
    payload = {"hostname": "x", "port": 1, "name": "Mainsail \U0001f31f 日本語 Ω",
               "nested": {"list": [1, 2.5, None, True]}}
    d = stage_dir(json.dumps(payload), subdir="stage-uni")
    stack.installer._patch_ui_config("mainsail", d)
    got = json.loads((d / "config.json").read_text())
    assert got["name"] == payload["name"]
    assert got["nested"] == payload["nested"]
    assert got["hostname"] is None


def test_patch_ui_config_leaves_the_file_parseable_and_newline_terminated(
        stack, stage_dir):
    d = stage_dir(json.dumps({"hostname": "x", "port": 1}), subdir="stage-eol")
    stack.installer._patch_ui_config("mainsail", d)
    text = (d / "config.json").read_text()
    assert text.endswith("\n")
    json.loads(text)


UNPARSEABLE = [
    pytest.param("", id="empty-file"),
    pytest.param("   \n\t", id="whitespace-only"),
    pytest.param("{", id="lone-brace"),
    pytest.param('{"hostname": "x",}', id="trailing-comma"),
    pytest.param("<html>not json</html>", id="an-html-page"),
    pytest.param('﻿{"hostname": "x"}', id="utf8-bom"),
    pytest.param('{"a": 1}{"b": 2}', id="two-objects-concatenated"),
    pytest.param('{"hostname": # comment\n}', id="json-with-comment"),
    pytest.param(b'{"hostname": "\xff\xfe"}', id="invalid-utf8-bytes"),
    pytest.param(b'{"hostname": \x00}', id="nul-byte"),
]


@pytest.mark.parametrize("payload", UNPARSEABLE)
def test_unparseable_config_json_is_left_byte_identical(stack, tmp_path,
                                                         payload):
    """_patch_ui_config must not touch what it cannot understand."""
    d = tmp_path / "stage-bad"
    d.mkdir()
    cfg = d / "config.json"
    if isinstance(payload, bytes):
        cfg.write_bytes(payload)
    else:
        cfg.write_text(payload)
    before = cfg.read_bytes()
    stack.installer._patch_ui_config("mainsail", d)
    assert cfg.read_bytes() == before


@pytest.mark.parametrize("payload", [
    pytest.param("[]", id="json-array"),
    pytest.param('["hostname"]', id="json-array-of-keys"),
    pytest.param('"a string"', id="json-string"),
    pytest.param("42", id="json-int"),
    pytest.param("4.2", id="json-float"),
    pytest.param("true", id="json-true"),
    pytest.param("false", id="json-false"),
    pytest.param("null", id="json-null"),
])
@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: _patch_ui_config() guards only json.loads() with try/except and "
    "then indexes the parsed value unconditionally (installer.py:249-260). A "
    "syntactically valid but non-object config.json -- which a release asset or "
    "a mirror can ship -- raises TypeError *after* fetch_webui has built the "
    "staging tree, so setup() dies with a traceback instead of reporting the "
    "asset as unusable."))
def test_valid_but_non_object_config_json_is_handled(stack, tmp_path, payload):
    d = tmp_path / "stage-nonobj"
    d.mkdir()
    (d / "config.json").write_text(payload)
    stack.installer._patch_ui_config("mainsail", d)
    stack.installer._patch_ui_config("fluidd", d)
    assert json.loads((d / "config.json").read_text()) == json.loads(payload)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: the write in _patch_ui_config() is unguarded too: when "
    "config.json cannot be written (read-only staging tree, disk full) the "
    "OSError escapes (installer.py:260) and setup() aborts with a traceback "
    "instead of a user error."))
def test_unwritable_config_json_is_not_fatal(stack, tmp_path):
    d = tmp_path / "stage-ro"
    d.mkdir()
    cfg = d / "config.json"
    cfg.write_text('{"hostname": "x", "port": 1}')
    os.chmod(str(cfg), stat.S_IRUSR)
    try:
        stack.installer._patch_ui_config("mainsail", d)
    finally:
        os.chmod(str(cfg), 0o644)
    assert json.loads(cfg.read_text())["hostname"] == "x"


def test_missing_config_json_is_not_fatal(stack, tmp_path):
    d = tmp_path / "stage-no-config"
    d.mkdir()
    stack.installer._patch_ui_config("mainsail", d)
    assert not (d / "config.json").exists()


def test_config_json_as_a_directory_is_not_fatal(stack, tmp_path):
    d = tmp_path / "stage-dir"
    d.mkdir()
    (d / "config.json").mkdir()
    stack.installer._patch_ui_config("fluidd", d)
    assert (d / "config.json").is_dir()


def test_config_json_as_a_symlink_to_a_valid_object(stack, tmp_path):
    d = tmp_path / "stage-link"
    d.mkdir()
    real = d / "real.json"
    real.write_text('{"hostname": "x", "port": 1, "keep": 2}')
    os.symlink(str(real), str(d / "config.json"))
    stack.installer._patch_ui_config("mainsail", d)
    data = json.loads(real.read_text())
    assert data["hostname"] is None and data["keep"] == 2


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: _patch_ui_config() rewrites config.json for ANY component name "
    "(installer.py:246-260): an unknown component still has its file parsed, "
    "re-serialised (indent=2, ensure_ascii, hostname/port added) and replaced, "
    "so a call for a component the function does not know silently mutates a "
    "file it was never asked to change. Unknown components must be a no-op."))
def test_unknown_component_leaves_the_file_byte_identical(stack, tmp_path):
    original = '{"hostname": "x",  "port": 8080, "name": "é中\U0001f31f"}'
    d = tmp_path / "stage-unknown"
    d.mkdir()
    (d / "config.json").write_text(original)
    stack.installer._patch_ui_config("klipper", d)
    assert (d / "config.json").read_text() == original


@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.dictionaries(
    st.text(st.characters(min_codepoint=32, max_codepoint=0x2aff),
            min_size=1, max_size=12),
    st.one_of(st.none(), st.booleans(), st.integers(-10, 10),
              st.floats(allow_nan=False),
              st.text(st.characters(min_codepoint=32, max_codepoint=0x2aff),
                      max_size=20)),
    max_size=8))
def test_patch_ui_config_never_loses_a_key_and_always_round_trips(
        stack, tmp_path, payload):
    """Property: no key is dropped, only the proxy-relevant ones are changed,
    and the file is always valid JSON afterwards."""
    d = tmp_path / "prop"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir()
    path = d / "config.json"
    path.write_text(json.dumps(payload))
    stack.installer._patch_ui_config("mainsail", d)
    got = json.loads(path.read_text())
    assert set(payload) <= set(got), "keys were dropped"
    assert set(got) - set(payload) <= {"hostname", "port"}
    for key, value in payload.items():
        if key in ("hostname", "port"):
            assert got[key] is None
        else:
            assert got[key] == value or (
                isinstance(value, float) and abs(value - got[key]) < 1e-9)
    stack.installer._patch_ui_config("fluidd", d)
    got2 = json.loads(path.read_text())
    assert set(payload) <= set(got2)
    assert set(got2) - set(payload) <= {"hostname", "port", "socket_url"}
    if "socket_url" in payload:
        assert got2["socket_url"].startswith("ws://localhost:")
    for key, value in payload.items():
        if key in ("hostname", "port", "socket_url"):
            continue
        assert got2[key] == value or (
            isinstance(value, float) and abs(value - got2[key]) < 1e-9)


@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.text(st.characters(min_codepoint=1, max_codepoint=0x2aff),
               max_size=200))
def test_patch_ui_config_never_leaves_a_half_written_file(stack, tmp_path,
                                                          junk):
    d = tmp_path / "fuzz"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir()
    path = d / "config.json"
    path.write_text(junk)
    before = path.read_bytes()
    try:
        stack.installer._patch_ui_config("mainsail", d)
    except Exception:                       # noqa: BLE001 - bug already filed
        pass
    after = path.read_bytes()
    try:
        parsed = json.loads(before.decode("utf-8"))
    except Exception:
        parsed = None
    if not isinstance(parsed, dict):
        assert after == before, "unparseable input was half-written"
    else:
        json.loads(after.decode("utf-8"))   # still valid JSON


@settings(max_examples=25, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(st.text(st.characters(min_codepoint=32, max_codepoint=0x10ff),
                        min_size=1, max_size=60), max_size=200))
def test_patch_ui_config_handles_a_bulky_config_without_quadratic_blues(
        stack, tmp_path, keys):
    payload = {k: i for i, k in enumerate(keys)}
    d = tmp_path / "bulky"
    d.mkdir(exist_ok=True)
    path = d / "config.json"
    path.write_text(json.dumps(payload))
    stack.installer._patch_ui_config("mainsail", d)
    got = json.loads(path.read_text())
    assert len(got) >= len(payload)
    assert got["hostname"] is None and got["port"] is None
