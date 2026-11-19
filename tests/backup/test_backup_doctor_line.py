"""`backup.doctor_lines()` - the bridge to `klipperformac doctor`, kept honest.

cmd_doctor() is not this task's file to edit (another agent is in it right now),
so the backup answer is a function in the house's existing doctor shape
(cam.doctor() returns [(ok, message)]) and a test that pins what it promises:
cheap, never raising, and honest about a machine with no backups at all.
"""
import time

import pytest


def test_no_folder_and_no_config_is_information_not_a_failure(bstack):
    """Nothing to back up yet is not a failure - that is the line a machine with
    no install at all has to print."""
    bstack.paths.PRINTER_CFG.unlink()
    lines = bstack.backup.doctor_lines()
    assert len(lines) == 1
    ok, msg = lines[0]
    assert ok is True and "no backups yet" in msg


def test_a_config_and_no_backups_is_a_warning(bstack):
    """The state a fresh install is really in: there IS something to lose and
    nothing has been kept yet. doctor is the command that catches that."""
    assert bstack.paths.PRINTER_CFG.is_file()
    ok, msg = bstack.backup.doctor_lines()[0]
    assert ok is False
    assert "before the next print" in msg


def test_a_fresh_backup_reads_ok(bstack):
    bstack.backup.create()
    ok, msg = bstack.backup.doctor_lines()[0]
    assert ok is True
    assert "1 archive(s)" in msg and "newest 0 day(s) ago" in msg


def test_a_stale_backup_reads_as_a_warning(bstack, tools):
    """The name is the timestamp (a copied folder's mtime lies), so renaming the
    archive is exactly how a month-old backup looks to this line."""
    where = bstack.backup.backups_dir()
    where.mkdir(parents=True)
    old = "klipperformac-backup-{}.tar.gz".format(
        time.strftime("%Y%m%d-%H%M%S",
                      time.localtime(time.time() - 45 * 86400)))
    tools.write_backup(bstack.paths.DATA, old,
                       tools.build([("data/config/printer.cfg", "reg",
                                     "[printer]\nmax_velocity: 1\n")]))
    ok, msg = bstack.backup.doctor_lines()[0]
    assert ok is False
    # The stamp in the name has second precision and the age is measured against
    # the clock, so compare the number rather than the exact string.
    days = int(msg.split(" is ")[1].split(" days old")[0])
    assert 44 <= days <= 45
    assert "klipperformac backup create" in msg


def test_an_undated_archive_falls_back_to_its_mtime(bstack, tools):
    """A backup renamed by hand has no stamp in its name, so its age comes from
    the file itself - reported, not skipped, because "1 archive in the folder" is
    still the answer a user needs."""
    where = bstack.backup.backups_dir()
    where.mkdir(parents=True)
    tools.write_backup(bstack.paths.DATA, "holiday-snapshot.tar.gz",
                       tools.build([("data/config/printer.cfg", "reg",
                                     "[printer]\n")]))
    ok, msg = bstack.backup.doctor_lines()[0]
    assert ok is True
    assert "1 archive(s)" in msg and "newest 0 day(s) ago" in msg


def test_an_archive_whose_stat_fails_has_no_age(bstack):
    """_age_days() returns None rather than guessing: no stamp in the name and no
    usable mtime (a dangling link) means "undated" in the line, never "0 days" -
    which would be doctor claiming a backup is fresh when it knows nothing."""
    where = bstack.backup.backups_dir()
    where.mkdir(parents=True)
    dangling = where / "holiday-snapshot.tar.gz"
    dangling.symlink_to(where / "gone-with-the-wind")
    assert bstack.backup._age_days(dangling) is None
    # and a stamped name needs no stat at all
    fresh = where / "klipperformac-backup-20260101-000000.tar.gz"
    fresh.write_bytes(b"\x1f\x8b")
    assert bstack.backup._age_days(fresh) > 200


def test_an_unreadable_backup_folder_is_a_line_never_a_crash(bstack, monkeypatch):
    """doctor must print every line it has: AGENTS.md 4 makes `doctor` the gate,
    so a permission problem here is a [!] row, not a traceback that eats the rest
    of the report."""
    bstack.backup.create()

    def boom(self):
        raise OSError("I/O error")
    monkeypatch.setattr(type(bstack.backup.backups_dir()), "iterdir", boom)
    lines = bstack.backup.doctor_lines()
    assert len(lines) == 1
    assert lines[0][0] is False
    assert "cannot read" in lines[0][1]


def test_the_doctor_shape_matches_cams(bstack):
    """The house contract for these lines: (bool, str) pairs that a caller can
    print and use to flip its own ok flag - the same tuple cam.doctor() yields."""
    bstack.backup.create()
    for ok, msg in bstack.backup.doctor_lines():
        assert isinstance(ok, bool)
        assert isinstance(msg, str) and msg
    assert bstack.backup.DOCTOR_STALE_DAYS > 0


def test_the_line_is_printable_by_the_shape_cmd_doctor_already_uses(bstack,
                                                                    capsys):
    """Proves the two-line integration: every tuple formats, and the boolean is
    what would drive doctor's own `ok`."""
    bstack.backup.create()
    capsys.readouterr()
    all_ok = True
    for ok, msg in bstack.backup.doctor_lines():
        print(("[ok] " if ok else "[!] ") + msg)
        all_ok = all_ok and ok
    out = capsys.readouterr().out
    assert out.startswith("[ok] backups:")
    assert all_ok is True


@pytest.mark.parametrize("count,keep,expect_left", [(1, 10, 1), (12, 10, 10)])
def test_doctor_counts_what_prune_leaves(bstack, tools, count, keep,
                                         expect_left):
    where = bstack.backup.backups_dir()
    where.mkdir(parents=True)
    now = time.time()
    for i in range(count):
        # Hours apart, so every name is stamped inside DOCTOR_STALE_DAYS and the
        # line is about the count, not about age.
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now - i * 3600))
        tools.write_backup(
            bstack.paths.DATA, "klipperformac-backup-{}.tar.gz".format(stamp),
            tools.build([("data/config/printer.cfg", "reg",
                          "[printer]\nmax_velocity: 1\n")]))
    bstack.backup.prune(keep=keep)
    ok, msg = bstack.backup.doctor_lines()[0]
    assert ok is True
    assert "{} archive(s)".format(expect_left) in msg
