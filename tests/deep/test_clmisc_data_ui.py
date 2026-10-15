"""Deep tests: `klipperformac data --set`, `open`, `ui` and the `status`
surface - the misc CLI branches the first suite did not reach.

Safety contract these tests enforce (and obey):
  * every relocation target is an absolute path inside the pytest sandbox, or
    a single relative component with the process cwd moved into the sandbox.
    "." / ".." are never passed to relocate_data - resolving them would aim the
    data folder at the shared temp directory.
  * `~` and `/` are only ever *refused*; the refusal happens before any mkdir,
    so the real home is never written to.
  * no process is started or stopped: process.running/stop/start are stubbed.
  * no browser is launched: webbrowser.open is stubbed and recorded.

``xfail(strict=True)`` marks encode product bugs that are still present.
"""
import argparse
import json
import os
import shutil
import stat
import webbrowser
from urllib.parse import urlparse

import pytest
from hypothesis import HealthCheck, assume, example, given, settings, strategies as st


def data_args(set_=None):
    return argparse.Namespace(set=set_)


def ui_args(name=None, no_open=True):
    return argparse.Namespace(name=name, no_open=no_open)


def open_args():
    return argparse.Namespace()


def status_args():
    return argparse.Namespace()


class Browser:
    def __init__(self):
        self.urls = []

    def open(self, url, *a, **k):
        self.urls.append(url)
        return True


def write_lockfile(paths, payload):
    """Record a lockfile (the sandbox home is created lazily by the product)."""
    paths.LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
    paths.LOCKFILE.write_text(payload)
    return paths.LOCKFILE


def expect_die(fn, *a, **k):
    with pytest.raises(SystemExit) as exc:
        fn(*a, **k)
    assert exc.value.code == 1
    return exc


def seed_data(paths, files=("config/printer.cfg", "logs/klippy.log")):
    for rel in files:
        p = paths.DATA / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("seed:" + rel)
    return paths.DATA


def tree(root):
    """{relative path: content} for every file under root."""
    out = {}
    if not root.exists():
        return out
    for dirpath, dirnames, filenames in os.walk(str(root)):
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                out[os.path.relpath(full, str(root))] = open(full).read()
            except OSError:
                out[os.path.relpath(full, str(root))] = "<unreadable>"
    return out


# ---------------------------------------------------------------------------
# `data` with no --set: read-only reporting
# ---------------------------------------------------------------------------
def test_data_reports_the_current_folder_when_it_does_not_exist_yet(stack, capfd):
    shutil.rmtree(stack.data, ignore_errors=True)
    stack.cli.cmd_data(data_args())
    out = capfd.readouterr().out
    assert out.startswith("Data folder: " + str(stack.data))
    assert "contents" not in out


def test_data_lists_visible_entries_in_order(stack, capfd):
    seed_data(stack.paths)
    (stack.data / ".hidden").mkdir()
    (stack.data / "gcodes").mkdir()
    (stack.data / "Timelapse").mkdir()
    stack.cli.cmd_data(data_args())
    out = capfd.readouterr().out
    assert "contents:" in out
    entries = out.split("contents:")[1].strip().split(", ")
    assert ".hidden" not in entries
    assert entries == sorted(entries)
    assert entries == ["Timelapse", "config", "gcodes", "logs"]


def test_data_reports_an_empty_folder_as_empty(stack, capfd):
    shutil.rmtree(stack.data, ignore_errors=True)
    stack.data.mkdir(parents=True)
    stack.cli.cmd_data(data_args())
    assert "(empty)" in capfd.readouterr().out


def test_data_lists_awkward_but_legal_names_without_crashing(stack, capfd):
    stack.data.mkdir(parents=True, exist_ok=True)
    for name in ("with space", "ünïcode", "quote'd", "dollar$HOME",
                 'double"quoted', "back`tick`", "semi;colon"):
        (stack.data / name).mkdir()
    stack.cli.cmd_data(data_args())
    out = capfd.readouterr().out
    for name in ("with space", "ünïcode", "quote'd", "dollar$HOME",
                 'double"quoted', "back`tick`", "semi;colon"):
        assert name in out


def test_data_empty_string_set_means_show_not_move(stack, capfd, tmp_path):
    """`--set ""` must not be read as 'relocate to the empty path'."""
    target = tmp_path / "never-made"
    stack.cli.cmd_data(data_args(""))
    out = capfd.readouterr().out
    assert out.startswith("Data folder: ")
    assert not target.exists()
    assert stack.paths.load_settings() == {}


# ---------------------------------------------------------------------------
# relocate_data: the good path
# ---------------------------------------------------------------------------
def test_relocate_creates_a_missing_target_and_moves_everything(stack):
    old = seed_data(stack.paths)
    target = stack.data.parent / "moved"

    msg = stack.cli.relocate_data(str(target))

    assert "moved 2 item(s)" in msg
    assert stack.paths.DATA == target
    assert tree(target) == {"config/printer.cfg": "seed:config/printer.cfg",
                            "logs/klippy.log": "seed:logs/klippy.log"}
    assert tree(old) == {}, "source must not keep stale copies"


def test_relocate_records_the_choice_in_settings_json(stack):
    seed_data(stack.paths)
    target = stack.data.parent / "chosen"
    stack.cli.relocate_data(str(target))
    assert stack.paths.load_settings()["data"] == str(target)


def test_relocate_repoints_every_derived_path(stack):
    seed_data(stack.paths)
    target = stack.data.parent / "again"
    stack.cli.relocate_data(str(target))
    p = stack.paths
    assert p.PRINTER_CFG == target / "config" / "printer.cfg"
    assert p.MOONRAKER_CONF == target / "config" / "moonraker.conf"
    assert p.KLIPPY_LOG == target / "logs" / "klippy.log"
    assert p.API_SOCKET == target / "run" / "klippy.sock"
    assert p.PRESETS == target / "config" / "presets"
    assert p.TIMELAPSE == target / "timelapse"


def test_relocate_creates_the_standard_directory_layout(stack):
    seed_data(stack.paths)
    target = stack.data.parent / "layout"
    stack.cli.relocate_data(str(target))
    for d in (stack.paths.CONFIG, stack.paths.PRESETS, stack.paths.RUN,
              stack.paths.LOGS, stack.paths.WEB):
        assert d.is_dir(), d


def test_relocating_to_the_same_folder_twice_is_a_no_op(stack, capfd):
    seed_data(stack.paths)
    target = stack.data.parent / "same"
    stack.cli.relocate_data(str(target))
    before = tree(target)
    settings_before = stack.paths.load_settings()

    msg = stack.cli.relocate_data(str(target))

    assert "already set to" in msg
    assert tree(target) == before
    assert stack.paths.load_settings() == settings_before


def test_relocate_round_trip_returns_to_the_original(stack):
    first = seed_data(stack.paths)
    before = tree(first)
    second = stack.data.parent / "elsewhere"
    stack.cli.relocate_data(str(second))
    assert tree(second) == before
    stack.cli.relocate_data(str(first))
    assert stack.paths.DATA == first
    assert tree(first) == before


def test_relocate_leaves_colliding_entries_in_place_and_keeps_both(stack, capfd):
    old = seed_data(stack.paths)
    target = stack.data.parent / "busy"
    (target / "config").mkdir(parents=True)
    (target / "config" / "printer.cfg").write_text("DESTINEE\n")

    stack.cli.relocate_data(str(target))

    err = capfd.readouterr().out
    assert "already exists" in err and "left" in err
    assert (target / "config" / "printer.cfg").read_text() == "DESTINEE\n"
    assert (old / "config" / "printer.cfg").read_text() == \
        "seed:config/printer.cfg", "source file must survive a collision"


def test_relocate_does_not_touch_the_stack_when_it_is_not_running(stack, monkeypatch):
    seed_data(stack.paths)
    calls = []
    monkeypatch.setattr(stack.process, "running", lambda: None)
    monkeypatch.setattr(stack.process, "stop", lambda *a: calls.append("stop"))
    monkeypatch.setattr(stack.process, "start", lambda *a: calls.append("start"))
    stack.cli.relocate_data(str(stack.data.parent / "quiet"))
    assert calls == []


def test_relocate_stops_and_restart_a_running_stack(stack, monkeypatch):
    seed_data(stack.paths)
    calls = []
    monkeypatch.setattr(stack.process, "running", lambda: {"klipper": 4242})
    monkeypatch.setattr(stack.process, "stop", lambda *a: calls.append("stop"))
    monkeypatch.setattr(stack.process, "start", lambda *a: calls.append("start"))
    msg = stack.cli.relocate_data(str(stack.data.parent / "hot"))
    assert calls == ["stop", "start"]
    assert "restarted" in msg


def test_relocate_rewrites_config_paths_into_the_new_folder(stack):
    old = seed_data(stack.paths)
    (old / "config" / "printer.cfg").write_text(
        "[virtual_sdcard]\npath: " + str(old / "gcodes") + "\n")
    (old / "config" / "moonraker.conf").write_text(
        "[server]\nklippy_uds_address: " + str(old / "run" / "klippy.sock") + "\n")
    target = stack.data.parent / "synced"
    stack.cli.relocate_data(str(target))
    assert "path: " + str(target / "gcodes") in \
        (target / "config" / "printer.cfg").read_text()
    assert "klippy_uds_address: " + str(target / "run" / "klippy.sock") in \
        (target / "config" / "moonraker.conf").read_text()


def test_relocate_to_a_symlink_resolves_the_target(stack):
    seed_data(stack.paths)
    real = stack.data.parent / "real-dest"
    real.mkdir()
    link = stack.data.parent / "link-dest"
    os.symlink(str(real), str(link))
    stack.cli.relocate_data(str(link))
    assert stack.paths.DATA == real.resolve()


def test_relocate_survives_unicode_and_spaced_targets(stack):
    seed_data(stack.paths)
    target = stack.data.parent / "Klipper Daten 🐐"
    stack.cli.relocate_data(str(target))
    assert tree(target) == {"config/printer.cfg": "seed:config/printer.cfg",
                            "logs/klippy.log": "seed:logs/klippy.log"}


def test_relocate_through_a_deeper_missing_parent(stack):
    seed_data(stack.paths)
    target = stack.data.parent / "a" / "b" / "c"
    stack.cli.relocate_data(str(target))
    assert stack.paths.DATA == target and (target / "config").is_dir()


# Only names APFS can actually represent: BMP, no NUL, no path magic.
_legal_name = st.text(alphabet=st.characters(min_codepoint=0x20,
                                             max_codepoint=0x02FF,
                                             blacklist_characters="/"),
                      min_size=1, max_size=24).filter(
    lambda n: "\x00" not in n and ".." not in n and n.strip() == n
    and n not in (".", "", "/") and not n.endswith("."))


@given(name=_legal_name)
@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@example(name="normal")
@example(name="ünïcode")
@example(name="with space")
@example(name="a" * 24)
def test_any_sandboxed_target_name_moves_the_contents_fully(stack, name):
    old = seed_data(stack.paths)
    before = tree(old)
    target = stack.data.parent / name
    assume(name not in ("data", "home") and target != old)
    assume(not target.exists() and not target.is_symlink())
    stack.cli.relocate_data(str(target))
    assert stack.paths.DATA == target
    assert tree(target) == before
    assert tree(old) == {}


@pytest.mark.xfail(strict=True, reason="BUG DEEP: relocate_data() lets mkdir's "
                   "OSError escape, so a destination the filesystem cannot name "
                   "(supplementary-plane characters -> Errno 92 'Illegal byte "
                   "sequence') kills `klipperformac data --set` with a raw traceback "
                   "instead of a one-line error")
@given(name=st.text(min_size=1, max_size=30).filter(
    lambda n: "\x00" not in n and ".." not in n and "/" not in n
    and n.strip() == n and n not in (".", "")))
@settings(max_examples=80, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@example(name="\U0001239a")
@example(name="\U0001f410")
def test_relocate_never_raises_anything_but_a_clean_exit(stack, name):
    """Any unusable destination is a message, never a Python exception."""
    old = seed_data(stack.paths)
    target = stack.data.parent / name
    assume(target != old and not target.is_symlink())
    try:
        stack.cli.relocate_data(str(target))
    except SystemExit:
        pass


# ---------------------------------------------------------------------------
# relocate_data: the guardrails
# ---------------------------------------------------------------------------
def test_relocate_refuses_the_home_directory(stack):
    from pathlib import Path
    seed_data(stack.paths)
    old = stack.paths.DATA
    home = Path.home()
    msg = stack.cli.relocate_data("~")
    assert "Refusing" in msg
    assert stack.paths.DATA == old
    assert stack.paths.load_settings() == {}
    assert tree(old) == {"config/printer.cfg": "seed:config/printer.cfg",
                         "logs/klippy.log": "seed:logs/klippy.log"}
    assert not (home / "config").exists()


def test_relocate_refuses_the_filesystem_root(stack):
    seed_data(stack.paths)
    old = stack.paths.DATA
    msg = stack.cli.relocate_data("/")
    assert "Refusing" in msg
    assert stack.paths.DATA == old
    assert stack.paths.load_settings() == {}


def test_relocate_refuses_home_with_trailing_slashes(stack):
    seed_data(stack.paths)
    old = stack.paths.DATA
    for spelling in ("~/", "~//", str(__import__("pathlib").Path.home()) + "/"):
        msg = stack.cli.relocate_data(spelling)
        assert "Refusing" in msg, spelling
        assert stack.paths.DATA == old


@pytest.mark.xfail(strict=True, reason="BUG DEEP: relocate_data() expanduser()s "
                   "but never resolves a relative `data --set` target, so "
                   "settings.json stores 'reldir' and every later command reads a "
                   "data folder relative to whatever cwd it happens to be started "
                   "from (printer.cfg 'disappears')")
def test_relocate_relative_target_is_absolutised_or_refused(stack, tmp_path,
                                                            monkeypatch):
    old = seed_data(stack.paths)
    before = tree(old)
    cwd = tmp_path / "launched-from"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    stack.cli.relocate_data("reldir")

    assert stack.paths.DATA.is_absolute(), stack.paths.DATA
    assert stack.paths.load_settings() == {} or \
        os.path.isabs(stack.paths.load_settings()["data"])
    assert tree(old) == before, "source must not be emptied into a cwd-relative dir"


@pytest.mark.xfail(strict=True, reason="BUG DEEP: relocate_data() accepts a "
                   "whitespace-only target ('data --set \"  \")' and creates a "
                   "folder literally named '   '; blank values must be rejected")
def test_relocate_whitespace_target_is_refused(stack, tmp_path, monkeypatch):
    old = seed_data(stack.paths)
    before = tree(old)
    cwd = tmp_path / "ws-cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    msg = stack.cli.relocate_data("   ")

    assert "Refusing" in msg or "Invalid" in msg
    assert stack.paths.DATA == old
    assert tree(old) == before
    assert list(cwd.iterdir()) == []


@pytest.mark.xfail(strict=True, reason="BUG DEEP: relocate_data() moves items "
                   "before it validates, so a destination inside the current data "
                   "folder ('data --set <DATA>/backup') half-moves config/ and "
                   "logs/ into the new subdir and then dies with shutil.Error "
                   "'Cannot move a directory into itself' - printer.cfg is then "
                   "invisible to every command")
def test_relocate_target_inside_the_data_folder_is_refused(stack, capfd):
    old = seed_data(stack.paths)
    before = tree(old)
    nested = old / "backup"
    nested.mkdir()

    with pytest.raises(SystemExit) as exc:
        stack.cli.relocate_data(str(nested))

    assert exc.value.code == 1
    assert tree(old) == before, "no file may have been moved"
    assert stack.paths.DATA == old


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
@pytest.mark.xfail(strict=True, reason="BUG DEEP: relocate_data() does not wrap "
                   "shutil.move, so relocating into a non-writable folder raises a "
                   "raw PermissionError traceback (AGENTS.md: no traceback for a "
                   "user-facing path problem)")
def test_relocate_into_read_only_folder_is_a_clean_error(stack, capfd):
    old = seed_data(stack.paths)
    before = tree(old)
    target = stack.data.parent / "locked"
    target.mkdir()
    os.chmod(str(target), stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(SystemExit) as exc:
            stack.cli.relocate_data(str(target))
        assert exc.value.code == 1
        assert "Traceback" not in capfd.readouterr().err
        assert tree(old) == before
        assert stack.paths.load_settings() == {}
    finally:
        os.chmod(str(target), stat.S_IRWXU)


@pytest.mark.xfail(strict=True, reason="BUG DEEP: `data --set` with a path whose "
                   "parent is a regular file raises NotADirectoryError out of mkdir "
                   "instead of a clean error")
def test_relocate_through_a_file_parent_is_a_clean_error(stack, capfd):
    seed_data(stack.paths)
    blocker = stack.data.parent / "blocker"
    blocker.write_text("i am a file\n")
    with pytest.raises(SystemExit) as exc:
        stack.cli.relocate_data(str(blocker / "sub"))
    assert exc.value.code == 1
    assert "Traceback" not in capfd.readouterr().err
    assert stack.paths.load_settings() == {}


@pytest.mark.xfail(strict=True, reason="BUG DEEP: `data --set` with an "
                   "over-long path component (> NAME_MAX) escapes mkdir's OSError "
                   "as a traceback")
def test_relocate_with_an_over_long_name_is_a_clean_error(stack, capfd):
    seed_data(stack.paths)
    old = stack.paths.DATA
    before = tree(old)
    with pytest.raises(SystemExit) as exc:
        stack.cli.relocate_data(str(stack.data.parent / ("L" * 400)))
    assert exc.value.code == 1
    assert "Traceback" not in capfd.readouterr().err
    assert tree(old) == before


def test_relocate_into_a_path_with_a_newline_is_still_sandboxed(stack, capfd):
    """A newline in a target name is legal on APFS: it must be handled as data,
    never as a message that could forge a new CLI line."""
    seed_data(stack.paths)
    old = stack.paths.DATA
    before = tree(old)
    weird = stack.data.parent / "evil\n[!] pwned"
    stack.cli.relocate_data(str(weird))
    assert stack.paths.DATA == weird
    assert tree(weird) == before


def test_relocate_does_not_start_any_process(stack, monkeypatch):
    import subprocess
    seed_data(stack.paths)
    monkeypatch.setattr(stack.process, "running", lambda: None)
    real_popen = subprocess.Popen
    called = []

    def spy(*a, **k):
        called.append(a)
        return real_popen(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", spy)
    monkeypatch.setattr(subprocess, "call", lambda *a, **k: called.append(a))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
    stack.cli.relocate_data(str(stack.data.parent / "silent"))
    assert called == []


# ---------------------------------------------------------------------------
# `data --set` through the real argparse entry point
# ---------------------------------------------------------------------------
def test_main_data_set_prints_the_result_and_exits_cleanly(stack, capfd, tmp_path):
    seed_data(stack.paths)
    target = tmp_path / "cli-target"
    stack.cli.main(["data", "--set", str(target)])
    out = capfd.readouterr().out
    assert "Data folder: " + str(target) in out
    assert json.loads((stack.paths.APP_HOME / "settings.json").read_text())["data"] \
        == str(target)


def test_main_data_refuses_home_without_writing_anything(stack, capfd, monkeypatch):
    real_home = __import__("pathlib").Path.home()
    before = sorted(os.listdir(str(real_home)))
    stack.cli.main(["data", "--set", "~"])
    out = capfd.readouterr().out
    assert "Refusing" in out
    assert sorted(os.listdir(str(real_home))) == before


# ---------------------------------------------------------------------------
# `open` / `ui`: garbage UI names and garbage lockfiles
# ---------------------------------------------------------------------------
def test_open_uses_the_ui_recorded_in_the_lockfile(stack, monkeypatch):
    browser = Browser()
    monkeypatch.setattr(webbrowser, "open", browser.open)
    write_lockfile(stack.paths, json.dumps({"ui": "mainsail"}))
    stack.cli.cmd_open(open_args())
    assert browser.urls == ["http://localhost:{}".format(stack.paths.WEB_PORT)]


def test_open_defaults_to_fluidd_with_no_lockfile(stack, monkeypatch):
    browser = Browser()
    monkeypatch.setattr(webbrowser, "open", browser.open)
    stack.cli.cmd_open(open_args())
    assert browser.urls == ["http://localhost:{}".format(stack.paths.FLUIDD_PORT)]


@pytest.mark.parametrize("payload", [
    "", "{", "not json", "null", "[]", '"mainsail"', "42", "true",
    "{}", '{"ui": null}', '{"ui": ""}', '{"ui": 0}', '{"ui": false}',
    '{"ui": ["mainsail"]}', '{"ui": {"nested": 1}}', '{"ui": true}',
    '{"ui": "Mainsail"}', '{"ui": "mainsail; rm -rf /"}',
    '{"ui": "../../../etc/passwd"}', '{"ui": "fluidd\\n[!] pwned"}',
    '{"ui": "' + "x" * 5000 + '"}', '{"ui": "\\u0000mainsail"}',
    '{"ui": "mainsail", "extra": [1,2,3]}',
])
def test_open_always_produces_exactly_one_loopback_url(stack, monkeypatch, payload):
    browser = Browser()
    monkeypatch.setattr(webbrowser, "open", browser.open)
    write_lockfile(stack.paths, payload)
    stack.cli.cmd_open(open_args())
    assert len(browser.urls) == 1
    url = browser.urls[0]
    parsed = urlparse(url)
    assert parsed.scheme == "http" and parsed.hostname == "localhost"
    assert parsed.port in (stack.paths.WEB_PORT, stack.paths.FLUIDD_PORT)
    assert "\n" not in url


@pytest.mark.parametrize("ui", ["mainsail", "fluidd"])
def test_ui_switch_records_the_default_and_opens(stack, monkeypatch, ui):
    browser = Browser()
    monkeypatch.setattr(webbrowser, "open", browser.open)
    (stack.paths.WEB / ui).mkdir(parents=True)
    (stack.paths.WEB / ui / "index.html").write_text("<html></html>")
    stack.cli.cmd_ui(ui_args(ui, no_open=False))
    assert json.loads(stack.paths.LOCKFILE.read_text())["ui"] == ui
    expected = stack.paths.WEB_PORT if ui == "mainsail" else stack.paths.FLUIDD_PORT
    assert browser.urls == ["http://localhost:{}".format(expected)]


@pytest.mark.parametrize("name", [
    "Mainsail", "MAINSAIL", "main sail", " mainsail", "fluidd2", "webcam",
    "mainsail/", "/mainsail", "..", "../evil", "..%2Fmainsail", "mainsail\\",
    "氟idd", "fluidd\nrm -rf /", "$FLUIDD", "fluidd;id", "<script>", "m"*4000,
])
def test_ui_rejects_every_name_that_is_not_mainsail_or_fluidd(stack, monkeypatch,
                                                             capfd, name):
    browser = Browser()
    monkeypatch.setattr(webbrowser, "open", browser.open)
    expect_die(stack.cli.cmd_ui, ui_args(name))
    err = capfd.readouterr().err
    assert "UI is 'mainsail' or 'fluidd'" in err
    assert not stack.paths.LOCKFILE.exists()
    assert browser.urls == []


def test_ui_names_outside_the_two_choices_never_reach_the_filesystem(stack,
                                                                     monkeypatch):
    """The WEB dir is joined with the name; nothing may be created outside it."""
    monkeypatch.setattr(webbrowser, "open", Browser().open)
    (stack.paths.WEB / "mainsail").mkdir(parents=True)
    with pytest.raises(SystemExit):
        stack.cli.cmd_ui(ui_args("../../escape"))
    assert list(stack.paths.WEB.parent.rglob("escape*")) == []
    assert list(stack.paths.DATA.rglob("*escape*")) == []
    assert (stack.paths.WEB / "mainsail").is_dir()


def test_ui_with_no_name_only_reports_and_opens(stack, monkeypatch):
    browser = Browser()
    monkeypatch.setattr(webbrowser, "open", browser.open)
    stack.cli.cmd_ui(ui_args(None, no_open=False))
    assert not stack.paths.LOCKFILE.exists()
    assert len(browser.urls) == 1


def test_ui_argparse_rejects_unknown_names_before_any_handler(stack, monkeypatch,
                                                             capfd):
    browser = Browser()
    monkeypatch.setattr(webbrowser, "open", browser.open)
    with pytest.raises(SystemExit) as exc:
        stack.cli.main(["ui", "Mainsail"])
    assert exc.value.code == 2
    assert browser.urls == []
    assert not stack.paths.LOCKFILE.exists()


def test_ui_switch_warns_when_the_files_are_not_downloaded(stack, monkeypatch,
                                                           capfd):
    browser = Browser()
    monkeypatch.setattr(webbrowser, "open", browser.open)
    stack.cli.cmd_ui(ui_args("mainsail", no_open=False))
    out = capfd.readouterr().out
    assert "not downloaded yet" in out
    assert "setup" in out
    assert json.loads(stack.paths.LOCKFILE.read_text())["ui"] == "mainsail"


def test_ui_and_open_never_leave_localhost(stack, monkeypatch):
    browser = Browser()
    monkeypatch.setattr(webbrowser, "open", browser.open)
    stack.cli.cmd_ui(ui_args("fluidd", no_open=False))
    stack.cli.cmd_open(open_args())
    for url in browser.urls:
        assert urlparse(url).hostname == "localhost"


# ---------------------------------------------------------------------------
# status: what happens when nothing is there yet
# ---------------------------------------------------------------------------
@pytest.fixture
def quiet_status(stack, monkeypatch):
    """status without network, processes, camera or shaper probing."""
    monkeypatch.setattr(stack.process, "running", lambda: None)
    monkeypatch.setattr(stack.shaper, "readiness",
                        lambda: {"needed": False, "healthy": True,
                                 "python_found": None, "venv_exists": False,
                                 "detail": "not needed"})
    monkeypatch.setattr(stack.cam, "running", lambda: None)
    monkeypatch.setattr(stack.cam, "helper_path", lambda: None)
    monkeypatch.setattr(stack.versions, "check_updates", lambda: ([], []))
    return stack


def test_status_with_nothing_installed_says_stopped(quiet_status, capfd):
    quiet_status.cli.cmd_status(status_args())
    out = capfd.readouterr().out
    assert out.strip() == "stopped"


def test_status_stopped_reads_no_logs_and_writes_nothing(quiet_status, capfd):
    before = tree(quiet_status.data) if quiet_status.data.exists() else {}
    quiet_status.cli.cmd_status(status_args())
    capfd.readouterr()
    assert (tree(quiet_status.data) if quiet_status.data.exists() else {}) == before
    assert not quiet_status.paths.LOGS.exists()


def test_status_lists_services_and_the_log_hint_when_running(quiet_status, monkeypatch,
                                                             capfd):
    monkeypatch.setattr(quiet_status.process, "running", lambda: {
        "web_mainsail": True, "web_fluidd": True, "moonraker": True,
        "klipper": False, "caffeinate": True})
    monkeypatch.setattr(quiet_status.cam, "helper_path", lambda: None)
    quiet_status.cli.cmd_status(status_args())
    out = capfd.readouterr().out
    assert "running" in out
    assert "klipper" in out and "DOWN" in out
    assert "url" in out and "localhost" in out
    assert "camera" in out and "not installed" in out


def test_status_prints_update_errors_but_exits_zero(quiet_status, monkeypatch, capfd):
    monkeypatch.setattr(quiet_status.process, "running", lambda: {
        "web_mainsail": True, "web_fluidd": True, "moonraker": True,
        "klipper": True, "caffeinate": True})
    monkeypatch.setattr(quiet_status.versions, "check_updates",
                        lambda: ([], ["klipper update check failed: offline"]))
    quiet_status.cli.cmd_status(status_args())
    out = capfd.readouterr().out
    assert "[!] klipper update check failed: offline" in out
    assert "Traceback" not in out


def test_status_shows_available_updates_compactly(quiet_status, monkeypatch, capfd):
    monkeypatch.setattr(quiet_status.process, "running", lambda: {
        "moonraker": True, "klipper": True, "web_mainsail": True,
        "web_fluidd": True, "caffeinate": True})
    monkeypatch.setattr(quiet_status.versions, "check_updates",
                        lambda: ([("moonraker", "v0.11.0", "v9.9.9")], []))
    quiet_status.cli.cmd_status(status_args())
    out = capfd.readouterr().out
    assert "updates available: moonraker v0.11.0->v9.9.9" in out


def test_status_does_not_open_any_socket(quiet_status, monkeypatch):
    import socket as socket_module

    def guarded(*a, **k):
        raise AssertionError("cmd_status opened a socket")

    monkeypatch.setattr(quiet_status.process, "running", lambda: {
        "moonraker": True, "klipper": True, "web_mainsail": True,
        "web_fluidd": True, "caffeinate": True})
    monkeypatch.setattr(socket_module, "socket", guarded)
    quiet_status.cli.cmd_status(status_args())
