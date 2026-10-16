"""Deep tests: the LEGACY migration branches of ``klipperformac/paths.py``.

Two branches run at *import time* and are therefore invisible to the first
suite (which always sets KLIPPERFORMAC_HOME + KLIPPERFORMAC_DATA):

  * ``_resolve_data()`` -> ``~/KlipperData`` is **renamed** into
    ``~/.klipperformac/data`` when the default is still missing
    (paths.py:46-53), and ``~/Documents/Klipper for Mac`` wins over it
    (paths.py:43-45).
  * the ``~/.maklipper`` one-time home migration, including the rewrite of
    ``.maklipper`` -> ``.klipperformac`` inside ``APP_HOME/venv/bin/*``
    (paths.py:102-120).

Both are exercised by pointing ``HOME`` at a throwaway directory and doing
``importlib.reload(paths)``, exactly the pattern the ``stack`` fixture uses.

HARD SAFETY RULES honoured by this file:
  * ``HOME`` is *always* rewritten to a sandbox before any reload, so the real
    ``~/KlipperData`` / ``~/.maklipper`` / ``~/.klipperformac`` can never be
    renamed.  The autouse fixture asserts it.
  * teardown reloads ``paths`` with throwaway KLIPPERFORMAC_HOME/DATA set (so
    no legacy branch can fire against the real home afterwards).
  * nothing here touches ports, /dev/cu.*, or spawns a process.

The bugs this battery found are fixed in ``klipperformac/paths.py``; the tests
that encoded them (``xfail(strict=True)``, "BUG DEEP: ...") keep their
assertions and now pass.
"""
import importlib
import json
import os
import pwd
import stat
import sys

import pytest

paths_mod = importlib.import_module("klipperformac.paths")

# Throwaway locations used only for the *restored* baseline; never the real home.
_ENV_KEYS = ("HOME", "KLIPPERFORMAC_HOME", "KLIPPERFORMAC_DATA")


def baseline(root):
    """An inert, per-test, pytest-cleaned environment binding."""
    return dict(HOME=str(root),
                KLIPPERFORMAC_HOME=str(root / ".klipperformac"),
                KLIPPERFORMAC_DATA=str(root / ".klipperformac" / "data"))


def _apply_env(mapping):
    """Bind the three variables, treating None as 'unset'.

    The teardown below restores a snapshot taken with os.environ.get(), whose
    missing keys are None: writing that back as text left
    KLIPPERFORMAC_HOME='None' (and HOME='None') in the real environment for the
    rest of the pytest session, so any later test that reloaded paths without
    setting the variables itself died on the absolute-path guard instead of
    running - a pass/fail that depended on which files ran first."""
    for key in _ENV_KEYS:
        value = mapping.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path):
    """Reload paths with HOME sandboxed per test, then restore a safe baseline.

    Teardown deliberately leaves the module bound to a throwaway
    KLIPPERFORMAC_HOME/DATA pair: with both set, ``_resolve_data`` returns the
    env value and the ``.maklipper`` branch is skipped, so the state can never
    point at - let alone rename - a real ``~/KlipperData`` or ``~/.maklipper``,
    on this machine or any other.  The ``stack`` fixture (used by every other
    test that needs paths) rebinds the whole module graph itself.
    """
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    sandbox = tmp_path / "sandbox-home"
    sandbox.mkdir()
    _apply_env(dict(baseline(tmp_path), HOME=str(sandbox)))
    importlib.reload(paths_mod)
    yield {"home": sandbox, "tmp": tmp_path}
    _apply_env(baseline(tmp_path))
    importlib.reload(paths_mod)
    _apply_env(saved)


def reload_paths(home=None, **wanted):
    """Re-import paths with the given (sandboxed) environment.

    ``home`` is the value of HOME; ``app_home`` / ``data`` are the
    KLIPPERFORMAC_HOME / KLIPPERFORMAC_DATA overrides.  A None value means
    "unset this variable", which is what makes a legacy branch reachable.
    """
    env = {"HOME": home, "KLIPPERFORMAC_HOME": wanted.get("app_home"),
           "KLIPPERFORMAC_DATA": wanted.get("data")}
    _apply_env({k: v for k, v in env.items() if v is not None})
    return importlib.reload(paths_mod)


def touch_tree(root, files):
    for rel in files:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("payload:" + rel)
    return root


def snapshot(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(str(root)):
        for name in filenames:
            full = os.path.join(dirpath, name)
            out[os.path.relpath(full, str(root))] = open(full).read()
        for name in dirnames:
            out[os.path.relpath(os.path.join(dirpath, name), str(root)) + "/"] = ""
    return out


# ---------------------------------------------------------------------------
# ~/KlipperData  ->  ~/.klipperformac/data   (the RENAMING branch)
# ---------------------------------------------------------------------------
def test_legacy_klipperdata_is_renamed_into_default_data(isolated_paths):
    """Documented behaviour: a returning user's data folder is moved (not
    copied, not merged, not left behind) when the default slot is free."""
    home = isolated_paths["home"]
    legacy = home / "KlipperData"
    touch_tree(legacy, ["config/printer.cfg", "config/moonraker.conf",
                        "logs/klippy.log", "web/index.html"])
    before = snapshot(legacy)
    legacy_ino = legacy.stat().st_ino

    p = reload_paths(home=home)

    assert p.DATA == home / ".klipperformac" / "data"
    assert not legacy.exists(), "legacy folder must be gone after the move"
    assert snapshot(p.DATA) == before
    # A rename keeps the inode: proves no copy-then-delete (which could lose
    # data if the process is killed halfway).
    assert p.DATA.stat().st_ino == legacy_ino
    assert p.KLIPPY_LOG.parent == p.LOGS and p.LOGS.is_dir()


def test_legacy_migration_creates_missing_app_home_parent(isolated_paths):
    """.klipperformac does not exist yet: mkdir(parents=True) must make room."""
    home = isolated_paths["home"]
    legacy = home / "KlipperData"
    touch_tree(legacy, ["config/printer.cfg"])
    assert not (home / ".klipperformac").exists()

    p = reload_paths(home=home)

    assert (p.DATA / "config" / "printer.cfg").is_file()


def test_legacy_migration_is_exactly_once_and_idempotent(isolated_paths):
    """Re-importing (every CLI invocation) must not re-move, error, or create
    stray folders once the legacy folder is gone."""
    home = isolated_paths["home"]
    legacy = home / "KlipperData"
    touch_tree(legacy, ["config/printer.cfg"])

    first = reload_paths(home=home)
    data = first.DATA
    second = reload_paths(home=home)
    third = reload_paths(home=home)

    assert second.DATA == data == third.DATA
    assert (data / "config" / "printer.cfg").is_file()
    # nothing like "data 2", "data.1", "KlipperData" left in the home
    assert sorted(x.name for x in (home / ".klipperformac").iterdir()) == ["data"]
    assert not legacy.exists()


def test_default_data_present_means_legacy_is_left_untouched(isolated_paths):
    """"never moved without data --set": the free slot check must be strict."""
    home = isolated_paths["home"]
    legacy = touch_tree(home / "KlipperData", ["config/printer.cfg", "old.txt"])
    default = touch_tree(home / ".klipperformac" / "data",
                         ["config/printer.cfg", "new.txt"])
    legacy_before = snapshot(legacy)

    p = reload_paths(home=home)

    assert p.DATA == default
    assert legacy.exists() and snapshot(legacy) == legacy_before, \
        "legacy folder must not be merged into or deleted"
    assert (default / "new.txt").is_file()
    assert not (default / "old.txt").exists()


def test_legacy_folder_is_never_written_to_settings_json(isolated_paths):
    """The migration is implicit; it must not silently claim the user chose
    the folder (a settings entry would pin the path forever)."""
    home = isolated_paths["home"]
    touch_tree(home / "KlipperData", ["config/printer.cfg"])

    p = reload_paths(home=home)

    assert p.load_settings() == {}
    assert not p.SETTINGS.exists()


@pytest.mark.parametrize("files", [
    ["a" * 200 + ".cfg"],                       # very long name
    ["中文.cfg", "emoji-🐐.cfg"],                # unicode names
    ["deep/" * 12 + "leaf.cfg"],                # deep nesting
    ["with space/and\ttab.cfg"],                # whitespace in components
    ["a.cfg", ".hidden.cfg", ".dot/dir/x.cfg"],  # hidden entries
])
def test_legacy_migration_survives_awkward_but_legal_names(isolated_paths, files):
    home = isolated_paths["home"]
    legacy = touch_tree(home / "KlipperData", files)
    before = snapshot(legacy)

    p = reload_paths(home=home)

    assert snapshot(p.DATA) == before


def test_legacy_migration_survives_large_folder(isolated_paths):
    """1000 files must move as one rename (no per-file work at import)."""
    home = isolated_paths["home"]
    touch_tree(home / "KlipperData", ["config/p%04d.cfg" % i for i in range(1000)])

    p = reload_paths(home=home)

    assert len(list((p.DATA / "config").iterdir())) == 1000


def test_legacy_symlink_is_moved_as_a_symlink(isolated_paths):
    """A ~/KlipperData symlink (Time Machine / external volume habit) must not
    be followed and shredded: the link itself is what gets renamed."""
    home = isolated_paths["home"]
    real = touch_tree(home / "Real", ["config/printer.cfg"])
    os.symlink(str(real), str(home / "KlipperData"))

    p = reload_paths(home=home)

    assert p.DATA.is_symlink()
    assert os.readlink(str(p.DATA)) == str(real)
    assert (real / "config" / "printer.cfg").is_file()
    assert not (home / "KlipperData").exists()


def test_unwritable_app_home_parent_falls_back_to_legacy(isolated_paths):
    """mkdir of the app home failing (OSError) must keep using the legacy
    folder rather than crash the import."""
    home = isolated_paths["home"]
    legacy = touch_tree(home / "KlipperData", ["config/printer.cfg"])
    before = snapshot(legacy)
    os.chmod(str(home), stat.S_IRUSR | stat.S_IXUSR)  # 0o500
    try:
        p = reload_paths(home=home)
    finally:
        os.chmod(str(home), stat.S_IRWXU)

    assert p.DATA == legacy
    assert snapshot(legacy) == before


def test_legacy_folder_data_value_beats_legacy_migration(isolated_paths):
    """settings.json wins; the legacy folder stays put for the user."""
    home = isolated_paths["home"]
    legacy = touch_tree(home / "KlipperData", ["config/printer.cfg"])
    chosen = touch_tree(home / "chosen", ["config/printer.cfg"])
    app = home / ".klipperformac"
    app.mkdir()
    (app / "settings.json").write_text(json.dumps({"data": str(chosen)}))

    p = reload_paths(home=home)

    assert p.DATA == chosen
    assert legacy.is_dir() and (legacy / "config" / "printer.cfg").is_file()


def test_env_data_beats_legacy_migration(isolated_paths):
    home = isolated_paths["home"]
    legacy = touch_tree(home / "KlipperData", ["config/printer.cfg"])

    p = reload_paths(home=home, data=str(home / "envdata"))

    assert p.DATA == home / "envdata"
    assert legacy.is_dir()


def test_tilde_in_env_and_settings_is_expanded(isolated_paths):
    """A '~'-style value must not stay literal (klippy would open '~/x')."""
    home = isolated_paths["home"]
    p = reload_paths(home=home, data="~/Sandbox Data")
    assert p.DATA == home / "Sandbox Data"

    app = home / ".klipperformac"
    app.mkdir(exist_ok=True)
    (app / "settings.json").write_text(json.dumps({"data": "~/Sandbox Conf"}))
    p = reload_paths(home=home)
    assert p.DATA == home / "Sandbox Conf"


# ---------------------------------------------------------------------------
# ~/Documents/Klipper for Mac takes precedence over ~/KlipperData
# ---------------------------------------------------------------------------
def test_legacy_documents_wins_and_is_not_moved(isolated_paths):
    home = isolated_paths["home"]
    docs = touch_tree(home / "Documents" / "Klipper for Mac", ["config/printer.cfg"])
    klipperdata = touch_tree(home / "KlipperData", ["logs/klippy.log"])
    before = snapshot(klipperdata)

    p = reload_paths(home=home)

    assert p.DATA == docs
    assert docs.is_dir() and not docs.is_symlink()
    assert snapshot(klipperdata) == before


def test_legacy_documents_only_needs_to_exist_once(isolated_paths):
    """A stray empty 'Klipper for Mac' folder hijacks the data location: the
    app must still end up with a usable (creatable) layout."""
    home = isolated_paths["home"]
    docs = home / "Documents" / "Klipper for Mac"
    docs.mkdir(parents=True)

    p = reload_paths(home=home)

    assert p.DATA == docs
    p.ensure_dirs()
    assert (docs / "config" / "presets").is_dir()
    assert (docs / "logs").is_dir()


# ---------------------------------------------------------------------------
# settings.json "data" values that are not strings / not usable
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", [123, 1.5, True, ["a", "b"], [1], {"p": "/x"}])
def test_nonstring_data_setting_is_not_a_fatal_crash(isolated_paths, value):
    home = isolated_paths["home"]
    app = home / ".klipperformac"
    app.mkdir()
    (app / "settings.json").write_text(json.dumps({"data": value}))

    p = reload_paths(home=home)

    assert isinstance(p.DATA, type(home))


def test_relative_data_setting_is_rejected_or_absolutised(isolated_paths, tmp_path):
    home = isolated_paths["home"]
    app = home / ".klipperformac"
    app.mkdir()
    (app / "settings.json").write_text(json.dumps({"data": "relative/data"}))
    elsewhere = tmp_path / "launched-from-here"
    elsewhere.mkdir()
    cwd = os.getcwd()
    os.chdir(str(elsewhere))
    try:
        p = reload_paths(home=home)
        assert p.DATA.is_absolute()
        assert not (elsewhere / "relative").exists(), \
            "a data tree must not be created under the launch directory"
    finally:
        os.chdir(cwd)


def test_blankish_data_setting_values_do_not_break_import(isolated_paths):
    """"", null and false are falsy -> fall back to the default; whitespace-only
    is the interesting one and is covered by the test below."""
    home = isolated_paths["home"]
    app = home / ".klipperformac"
    app.mkdir()
    for value in ("", None, False, 0):
        (app / "settings.json").write_text(json.dumps({"data": value}))
        p = reload_paths(home=home)
        assert p.DATA == app / "data", "value %r" % (value,)


def test_whitespace_only_data_setting_is_not_accepted(isolated_paths):
    home = isolated_paths["home"]
    app = home / ".klipperformac"
    app.mkdir()
    (app / "settings.json").write_text(json.dumps({"data": "   "}))
    cwd = os.getcwd()
    os.chdir(str(home))
    try:
        p = reload_paths(home=home)
        assert p.DATA == app / "data"
    finally:
        os.chdir(cwd)


def test_malformed_settings_json_falls_back_cleanly(isolated_paths):
    """Unparseable / truncated / binary settings.json must never stop a reload
    (the non-object-root case is owned by the camprefs deep suite)."""
    home = isolated_paths["home"]
    app = home / ".klipperformac"
    app.mkdir()
    for raw in ("", "{not json", b"\x00\xff binary", '{"data": ',
                '{\'data\': "x"}', "[", "  ", '{"data": "/ok"} no trailing'):
        (app / "settings.json").write_bytes(raw if isinstance(raw, bytes)
                                            else raw.encode("utf-8"))
        p = reload_paths(home=home)
        assert p.DATA == app / "data", "payload %r" % (raw,)
    # a *valid* settings file still wins over the legacy folder
    (app / "settings.json").write_text(json.dumps({"data": str(home / "pick")}))
    p = reload_paths(home=home)
    assert p.DATA == home / "pick"


def test_settings_json_as_a_directory_is_survivable(isolated_paths):
    home = isolated_paths["home"]
    app = home / ".klipperformac"
    (app / "settings.json").mkdir(parents=True)

    p = reload_paths(home=home)

    assert p.DATA == app / "data"


def test_data_path_occupied_by_a_file_is_not_fatal(isolated_paths):
    """A regular file where the data folder belongs (crashed install, leftover)
    must not make every command raise FileExistsError out of ensure_dirs()."""
    home = isolated_paths["home"]
    app = home / ".klipperformac"
    app.mkdir()
    (app / "data").write_text("i am a file, not a folder\n")

    p = reload_paths(home=home)
    with pytest.raises(SystemExit):
        p.ensure_dirs()


# ---------------------------------------------------------------------------
# ~/.maklipper -> ~/.klipperformac home migration
# ---------------------------------------------------------------------------
def test_maklipper_home_is_renamed_and_venv_paths_rewritten(isolated_paths):
    home = isolated_paths["home"]
    mk = home / ".maklipper"
    vbin = mk / "venv" / "bin"
    vbin.mkdir(parents=True)
    (vbin / "activate").write_text(
        'VIRTUAL_ENV="/Users/x/.maklipper/venv"\nexport VIRTUAL_ENV\n')
    (vbin / "python").write_text("#!/Users/x/.maklipper/venv/bin/python\n")
    (mk / "settings.json").write_text(json.dumps({"cam": {"device": "FaceTime"}}))
    touch_tree(mk / "klipper" / "klippy", ["klippy.py"])

    p = reload_paths(home=home, data=str(home / "sanddata"))

    assert p.APP_HOME == home / ".klipperformac"
    assert not (home / ".maklipper").exists()
    venv_bin = p.APP_HOME / "venv" / "bin"
    assert (venv_bin / "activate").read_text() == (
        'VIRTUAL_ENV="/Users/x/.klipperformac/venv"\nexport VIRTUAL_ENV\n')
    assert (venv_bin / "python").read_text().endswith(
        ".klipperformac/venv/bin/python\n")
    assert p.load_settings() == {"cam": {"device": "FaceTime"}}
    assert (p.KLIPPER / "klippy" / "klippy.py").is_file()


def test_maklipper_migration_skipped_when_env_home_is_set(isolated_paths):
    home = isolated_paths["home"]
    mk = touch_tree(home / ".maklipper", ["marker.txt"])
    other = isolated_paths["tmp"] / "explicit-home"

    p = reload_paths(home=home, app_home=other, data=str(other / "data"))

    assert p.APP_HOME == other
    assert (mk / "marker.txt").is_file()
    assert not other.exists(), "migration must not touch the explicit home"


def test_maklipper_kept_when_new_home_already_exists(isolated_paths):
    home = isolated_paths["home"]
    mk = touch_tree(home / ".maklipper", ["a.txt"])
    new = touch_tree(home / ".klipperformac", ["b.txt"])

    p = reload_paths(home=home, data=str(home / "sanddata"))

    assert p.APP_HOME == new
    assert (mk / "a.txt").is_file() and (new / "b.txt").is_file()


def test_maklipper_migration_survives_binary_and_directory_entries(isolated_paths):
    """The venv/bin rewrite loop must skip things that are not text without
    aborting the import."""
    home = isolated_paths["home"]
    vbin = home / ".maklipper" / "venv" / "bin"
    vbin.mkdir(parents=True)
    (vbin / "not-a-dir").mkdir()
    (vbin / "binary").write_bytes(b"\xff\xfe\x00 .maklipper \x00 junk")
    (vbin / "real").write_text("#!/Users/x/.maklipper/venv/bin/python\n")
    loop = vbin / "loop"
    os.symlink(loop, loop)

    p = reload_paths(home=home, data=str(home / "sanddata"))

    assert (p.APP_HOME / "venv" / "bin" / "real").read_text().count(".klipperformac") == 1
    assert (p.APP_HOME / "venv" / "bin" / "binary").read_bytes().startswith(b"\xff\xfe\x00")
    assert (p.APP_HOME / "venv" / "bin" / "not-a-dir").is_dir()


def test_maklipper_migration_only_rewrites_inside_venv_bin(isolated_paths):
    home = isolated_paths["home"]
    mk = home / ".maklipper"
    (mk / "venv" / "bin").mkdir(parents=True)
    (mk / "venv" / "bin" / "activate").write_text(".maklipper\n")
    old = "/Users/x/.maklipper"
    (mk / "lib").mkdir()
    (mk / "lib" / "python.py").write_text(old + "/lib/python\n")
    (mk / "klipper").mkdir()
    (mk / "klipper" / "notes.txt").write_text(old + "/klipper\n")
    (mk / "venv" / "lib").mkdir()
    (mk / "venv" / "lib" / "keep.txt").write_text(old + "/venv/lib\n")

    p = reload_paths(home=home, data=str(home / "sanddata"))

    for rel in ("lib/python.py", "klipper/notes.txt", "venv/lib/keep.txt"):
        assert ".maklipper" in (p.APP_HOME / rel).read_text(), rel
    assert ".klipperformac" in (p.APP_HOME / "venv" / "bin" / "activate").read_text()


def test_maklipper_readonly_home_leaves_everything_in_place(isolated_paths):
    home = isolated_paths["home"]
    mk = touch_tree(home / ".maklipper", ["venv/bin/activate"])
    (mk / "venv" / "bin" / "activate").write_text(".maklipper\n")
    os.chmod(str(home), stat.S_IRUSR | stat.S_IXUSR)
    try:
        p = reload_paths(home=home, data=str(home / "sanddata"))
    finally:
        os.chmod(str(home), stat.S_IRWXU)

    assert not p.APP_HOME.exists()
    assert (mk / "venv" / "bin" / "activate").read_text() == ".maklipper\n"


def test_stray_maklipper_file_is_not_hijacked_as_the_app_home(isolated_paths):
    home = isolated_paths["home"]
    (home / ".maklipper").write_text("not a folder at all\n")

    p = reload_paths(home=home, data=str(home / "sanddata"))

    assert (home / ".maklipper").is_file()
    assert not p.APP_HOME.exists()
    p.ensure_dirs()                     # must not raise


def test_sandboxed_import_never_resolves_into_the_real_home(isolated_paths):
    """Guardrail for this file itself: with HOME sandboxed, not one path
    constant may point at the developer's real home directory - that is where
    a live ~/.klipperformac, ~/KlipperData or ~/.maklipper would be renamed.
    ``pwd`` is used because os.environ['HOME'] is the sandbox by design."""
    home = isolated_paths["home"]
    touch_tree(home / ".maklipper", ["marker"])
    touch_tree(home / "KlipperData", ["marker"])
    real_home = pwd.getpwuid(os.getuid()).pw_dir

    p = reload_paths(home=home)

    constants = [getattr(p, name) for name in dir(p)
                 if isinstance(getattr(p, name), type(home))]
    offenders = [str(c) for c in constants
                 if str(c) == real_home or str(c).startswith(real_home + os.sep)]
    assert offenders == [], offenders
    assert str(p.APP_HOME).startswith(str(home))
    assert str(p.DATA).startswith(str(home))
    # the legacy folder was migrated inside the sandbox only
    assert (p.DATA / "marker").is_file()


def test_both_legacy_layouts_migrate_together(isolated_paths):
    """A user coming from maklipper with a ~/KlipperData folder gets both moves,
    or neither - never half a migration with no message."""
    home = isolated_paths["home"]
    mk = home / ".maklipper"
    (mk / "venv" / "bin").mkdir(parents=True)
    (mk / "venv" / "bin" / "python").write_text("#!/bin/sh\n")
    (mk / "settings.json").write_text(json.dumps({"lan": False}))
    (mk / "klipper").mkdir()
    touch_tree(home / "KlipperData", ["config/printer.cfg"])

    p = reload_paths(home=home)

    assert not (home / ".maklipper").exists()
    assert (p.APP_HOME / "venv" / "bin" / "python").is_file()
    assert (p.APP_HOME / "config" / "printer.cfg").is_file() or \
        (p.DATA / "config" / "printer.cfg").is_file()
