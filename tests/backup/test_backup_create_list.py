"""`klipperformac backup create` + `list`: what goes in, where it lands, and
what has to be true about the data folder first.

The inclusion list is a whitelist (see backup.py's module docstring) and it is
the load-bearing part of the feature: a backup that silently swallowed DATA/web/
carries 23 MB of re-fetchable JS, and one that swallowed DATA/run/ archives a
unix socket. Both sides of the list are pinned here, along with the refusals -
a folder this install did not make, or cannot read, has to stop the command
instead of producing a tarball of the wrong tree.
"""
import os
import re
import shutil
import time
from pathlib import Path

import pytest

# populate() writes exactly these and the whitelist archives all of them:
# config/{printer.cfg,moonraker.conf} + 1 preset + 1 history entry, 1 gcode,
# 1 database file, 1 misc template, then app/{settings,lockfile}.json.
PAYLOAD_FILE_COUNT = 9
OUR_NAME = re.compile(r"^klipperformac-backup-\d{8}-\d{6}(-\d+)?\.tar\.gz$")


def test_create_writes_one_timestamped_tarball_into_data_backups(bstack, tools):
    path = bstack.backup.create()
    assert path.parent == bstack.paths.DATA / "backups"
    assert OUR_NAME.match(path.name), path.name
    assert path.is_file() and path.stat().st_size > 0


def test_create_carries_the_documented_list_and_nothing_else(bstack, tools):
    got = tools.names_in(bstack.backup.create())
    # In: the four whitelisted data folders (config/ brings presets/ and
    # history/ along by itself) ...
    assert "data/config/printer.cfg" in got
    assert "data/config/moonraker.conf" in got
    assert "data/config/presets/voron2.cfg" in got
    assert "data/config/history/printer-20260101-000000-manual.cfg" in got
    assert "data/gcodes/cube.gcode" in got
    assert "data/database/moonraker_db.sqlite" in got
    assert "data/misc/templates/t.gcode" in got
    # ... and the two files that live in APP_HOME, under their own prefix.
    assert "app/settings.json" in got
    assert "app/lockfile.json" in got
    assert "manifest.json" in got
    # Out: re-fetchable (web), meaningless (run), forever-growing (logs),
    # recursive (backups), media (timelapse).
    for prefix in ("data/web", "data/run", "data/logs", "data/backups",
                   "data/timelapse"):
        assert not [n for n in got if n.startswith(prefix)], prefix
    assert not [n for n in got if n.startswith("/")]


def test_create_keeps_logs_out_until_asked(bstack, tools):
    with_logs = bstack.backup.create(with_logs=True)
    assert "data/logs/klippy.log" in tools.names_in(with_logs)
    assert "data/logs/moonraker.log" in tools.names_in(with_logs)
    assert tools.manifest_of(with_logs)["with_logs"] is True
    assert "data/logs/klippy.log" not in tools.names_in(bstack.backup.create())


def test_include_logs_is_the_same_flag_as_with_logs(bstack, tools):
    """The roadmap spec's spelling is --with-logs, the task contract's is
    --include-logs; both are advertised, one destination."""
    bstack.cli.main(["backup", "create", "--include-logs"])
    made = list(bstack.backup.backups_dir().glob("*.tar.gz"))
    assert len(made) == 1
    assert tools.manifest_of(made[0])["with_logs"] is True
    bstack.cli.main(["backup", "create", "--with-logs"])
    for extra in bstack.backup.backups_dir().glob("*.tar.gz"):
        assert tools.manifest_of(extra)["with_logs"] is True


def test_manifest_records_sizes_and_where_it_came_from(bstack, tools):
    manifest = tools.manifest_of(bstack.backup.create())
    assert manifest["klipperformac_backup"] == 1
    assert manifest["payload_dirs"] == ["config", "gcodes", "database", "misc"]
    assert manifest["file_count"] == PAYLOAD_FILE_COUNT
    assert manifest["total_bytes"] > 0
    assert manifest["with_logs"] is False
    assert manifest["data_dir"] == str(bstack.paths.DATA)
    assert manifest["app_home"] == str(bstack.paths.APP_HOME)
    assert manifest["tool_version"] == bstack.backup.__version__
    time.strptime(manifest["created_utc"], "%Y-%m-%dT%H:%M:%SZ")


def test_two_backups_in_one_second_both_survive(bstack, tools):
    """The name is stamped, not random (it is the sort key and the prune rule),
    so a same-second clash gets -1, -2 ... and nothing is ever overwritten."""
    made = [bstack.backup.create() for _ in range(4)]
    assert len(set(made)) == 4
    assert all(OUR_NAME.match(p.name) for p in made)
    assert all(p.stat().st_size > 0 for p in made)
    junk = [p.name for p in made[0].parent.iterdir()
            if p.name.endswith(".partial") or p.name.startswith(".restore")]
    assert junk == []  # write-then-replace leaves no scaffolding behind


def test_create_refuses_a_missing_data_folder(bstack, tools):
    shutil.rmtree(str(bstack.paths.DATA))
    with pytest.raises(SystemExit) as exc:
        bstack.backup.create()
    assert exc.value.code == 1


def test_create_refuses_a_data_folder_it_cannot_read(bstack, tools):
    """The contract's "refuses if DATA unreadable" - a folder this tool cannot
    list is not a folder it can promise a restore from."""
    if os.geteuid() == 0:
        pytest.skip("root ignores the permission bits this guard relies on")
    os.chmod(str(bstack.paths.DATA), 0o000)
    try:
        with pytest.raises(SystemExit) as exc:
            bstack.backup.create()
        assert exc.value.code == 1
    finally:
        os.chmod(str(bstack.paths.DATA), 0o755)
    assert not bstack.backup.backups_dir().exists()


def test_create_refuses_a_data_folder_that_is_a_file(bstack, tools):
    shutil.rmtree(str(bstack.paths.DATA))
    bstack.paths.DATA.write_text("not a folder\n")
    with pytest.raises(SystemExit):
        bstack.backup.create()


def test_create_refuses_a_folder_that_is_not_a_data_folder(bstack, tools):
    """paths.looks_like_data_dir() is the guard `uninstall --data` earned: one
    junk settings.json value must not turn a housekeeping command into a tar of
    somebody's photo library."""
    foreign = bstack.data.parent / "photos"
    foreign.mkdir()
    (foreign / "holiday.jpg").write_bytes(b"\xff\xd8\xff\xe0")
    bstack.paths.set_data(foreign)
    with pytest.raises(SystemExit):
        bstack.backup.create()
    assert not (foreign / "backups").exists()
    assert [p.name for p in foreign.iterdir()] == ["holiday.jpg"]


def test_create_refuses_a_protected_folder(bstack, tools):
    """paths.protected_data_dir(): the home folder is never a data folder."""
    bstack.paths.set_data(Path.home())
    with pytest.raises(SystemExit):
        bstack.backup.create()


def test_create_refuses_a_symlinked_data_folder(bstack, tools):
    real = bstack.data.parent / "real-data"
    shutil.move(str(bstack.paths.DATA), str(real))
    link = bstack.data.parent / "data"
    link.symlink_to(real)
    bstack.paths.set_data(link)
    with pytest.raises(SystemExit):
        bstack.backup.create()
    assert not (real / "backups").exists()


def test_create_wants_the_stack_down_because_sqlite_is_live(bstack, tools,
                                                           monkeypatch):
    """Moonraker holds DATA/database/moonraker_db.sqlite open; copying it now can
    capture a half-written database, which is a backup that restores as junk."""
    tools.fake_stack(bstack, monkeypatch)
    with pytest.raises(SystemExit):
        bstack.backup.create()
    assert not bstack.backup.backups_dir().exists()


def test_create_allow_running_warns_instead_of_refusing(bstack, tools,
                                                        monkeypatch, capsys):
    fake = tools.fake_stack(bstack, monkeypatch)
    assert fake["moonraker"]  # the state that made the test above refuse
    path = bstack.backup.create(allow_running=True)
    assert path.is_file()
    assert "may be inconsistent" in capsys.readouterr().out


def test_only_caffeinate_running_is_not_a_running_stack(bstack, tools,
                                                        monkeypatch):
    """A sleep assertion is not a held database - cmd_doctor() discounts
    caffeinate the same way."""
    tools.fake_stack(bstack, monkeypatch, caffeinate=True)
    assert bstack.backup.create().is_file()


def test_create_skips_symlinks_it_could_never_restore(bstack, tools, capsys):
    """A restore writes regular files only (the zip-slip guard refuses symlink
    members), so archiving one would produce a backup that could be listed and
    never put back. It is skipped, and said out loud."""
    victim = bstack.data.parent / "outside.cfg"
    victim.write_text("[printer]\nmax_velocity: 1\n")
    (bstack.paths.CONFIG / "linked.cfg").symlink_to(victim)
    got = tools.names_in(bstack.backup.create())
    assert "data/config/linked.cfg" not in got
    assert "symlink" in capsys.readouterr().out
    assert victim.read_text() == "[printer]\nmax_velocity: 1\n"


def test_create_warns_when_there_is_no_printer_cfg(bstack, tools, capsys):
    bstack.paths.PRINTER_CFG.unlink()
    path = bstack.backup.create()
    assert path.is_file()
    assert "will not verify as restorable" in capsys.readouterr().out
    ok, lines = bstack.backup.verify()
    assert ok is False
    assert any("printer.cfg is missing" in line for _good, line in lines)


def test_list_before_any_backup_is_a_hint_not_an_error(bstack, tools, capsys):
    assert bstack.backup.list_backups() == []
    assert "No backups yet" in capsys.readouterr().out


def test_list_shows_every_backup_newest_first(bstack, tools, capsys):
    where = bstack.backup.backups_dir()
    where.mkdir(parents=True)
    for stamp in ("20260101-000000", "20260102-000000", "20260103-000000"):
        bstack.backup.create()
        newest = sorted(where.glob("klipperformac-backup-*.tar.gz"))[-1]
        newest.rename(where / "klipperformac-backup-{}.tar.gz".format(stamp))
    found = bstack.backup.list_backups()
    out = capsys.readouterr().out
    assert [p.name for p in found] == [
        "klipperformac-backup-20260103-000000.tar.gz",
        "klipperformac-backup-20260102-000000.tar.gz",
        "klipperformac-backup-20260101-000000.tar.gz"]
    assert "3 backup(s)" in out
    assert "no logs" in out


def test_list_marks_names_prune_will_leave_alone(bstack, tools, capsys):
    where = bstack.backup.backups_dir()
    where.mkdir(parents=True)
    (where / "holiday-snapshot.tar.gz").write_bytes(b"\x1f\x8b")
    bstack.backup.create()
    capsys.readouterr()
    bstack.backup.list_backups()
    out = capsys.readouterr().out
    assert "not a klipperformac name" in out
    assert "holiday-snapshot.tar.gz" in out


def test_list_survives_a_truncated_or_foreign_archive(bstack, tools, capsys):
    """`list` is a read-only view of a folder the user can touch by hand: one
    junk file must not stop it printing the others, the way the presets listing
    refuses to crash on a cp1252 config."""
    where = bstack.backup.backups_dir()
    where.mkdir(parents=True)
    (where / "klipperformac-backup-20260101-000000.tar.gz").write_bytes(
        b"\x1f\x8b\x08\x00truncated")
    (where / "klipperformac-backup-20260102-000000.tar.gz").write_bytes(
        b"not gzip at all")
    good = bstack.backup.create()
    assert len(bstack.backup.list_backups()) == 3
    out = capsys.readouterr().out
    assert "no manifest" in out
    assert good.name in out


def test_list_ignores_dot_entries_and_symlinks(bstack, tools):
    where = bstack.backup.backups_dir()
    where.mkdir(parents=True)
    real = bstack.data.parent / "elsewhere.tar.gz"
    real.write_bytes(b"\x1f\x8b")
    (where / "klipperformac-backup-20260101-000000.tar.gz").symlink_to(real)
    (where / ".restore-leftover").mkdir()
    (where / "subdir.tar.gz").mkdir()
    assert bstack.backup.list_backups() == []
    assert real.is_file()


def test_backup_of_an_empty_but_valid_data_folder(bstack, tools):
    """A brand-new install has config/printer.cfg and nothing else - no
    database/, no misc/, no logs/. A whitelisted folder that is simply not there
    is not an error, it is just absent from the archive."""
    for name in ("database", "misc", "logs", "web", "gcodes"):
        shutil.rmtree(str(bstack.paths.DATA / name), ignore_errors=True)
    path = bstack.backup.create()
    got = tools.names_in(path)
    assert "data/config/printer.cfg" in got
    assert not [n for n in got if n.startswith("data/gcodes")]
    assert tools.manifest_of(path)["file_count"] == 6
