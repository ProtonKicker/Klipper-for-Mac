"""`klipperformac backup prune` - the command that deletes, so its guardrails are
the interesting part.

Three rules, each pinned: keep the newest N (default 10, never 0), only delete
files this tool named inside DATA/backups, and re-check that per file at delete
time instead of trusting how the list was built. `uninstall --data` is the house
precedent for why: the folder you delete is never the one you think you named.
"""
import os
import time

import pytest

NAME = "klipperformac-backup-{}.tar.gz"
STAMPS = ["202601{:02d}-000000".format(i) for i in range(1, 20)]


def plant(bstack, tools, stamps, blob=None):
    """Write backups with known names (create() stamps the current second, which
    a boundary test cannot wait for)."""
    where = bstack.backup.backups_dir()
    where.mkdir(parents=True, exist_ok=True)
    blob = blob if blob is not None else tools.build(
        [("data/config/printer.cfg", "reg", "[printer]\nmax_velocity: 1\n"),
         ("data/config/moonraker.conf", "reg", "[server]\nhost: 127.0.0.1\n")])
    made = []
    for stamp in stamps:
        path = where / NAME.format(stamp)
        path.write_bytes(blob)
        made.append(path)
    return made


def test_prune_keeps_the_newest_ten_by_default(bstack, tools):
    plant(bstack, tools, STAMPS[:13])
    removed = bstack.backup.prune()
    assert len(removed) == 3
    assert [p.name for p in removed] == [NAME.format(s) for s in STAMPS[:3]]
    left = sorted(p.name for p in (bstack.paths.DATA / "backups").iterdir())
    assert len(left) == 10
    assert NAME.format(STAMPS[3]) in left


def test_prune_honours_keep(bstack, tools):
    made = plant(bstack, tools, STAMPS[:5])
    assert [p.name for p in bstack.backup.prune(keep=2)] == [
        NAME.format(s) for s in STAMPS[:3]]
    left = sorted(p.name for p in (bstack.paths.DATA / "backups").iterdir())
    assert left == sorted(NAME.format(s) for s in STAMPS[3:5])
    assert made[4].is_file()  # the newest is never the one that goes


def test_prune_keep_one_keeps_exactly_one(bstack, tools):
    plant(bstack, tools, STAMPS[:3])
    assert len(bstack.backup.prune(keep=1)) == 2


@pytest.mark.parametrize("keep", [0, -1, -10, "3", 3.0, True, None])
def test_prune_refuses_a_keep_that_would_wipe_the_folder(bstack, tools, keep):
    """keep >= 1 is not a preference: `prune --keep 0` reading as "delete every
    backup I have" is the difference between a housekeeping command and data
    loss. A typo'd value exits 1 and deletes nothing."""
    plant(bstack, tools, STAMPS[:4])
    with pytest.raises(SystemExit) as exc:
        bstack.backup.prune(keep=keep)
    assert exc.value.code == 1
    assert len(list((bstack.paths.DATA / "backups").glob("*.tar.gz"))) == 4


def test_prune_with_room_to_spare_says_so(bstack, tools, capsys):
    plant(bstack, tools, STAMPS[:2])
    assert bstack.backup.prune(keep=10) == []
    assert "nothing to prune" in capsys.readouterr().out


def test_prune_deletes_nothing_when_there_is_no_backup_folder(bstack, capsys):
    assert bstack.backup.prune() == []
    assert "nothing to prune" in capsys.readouterr().out
    assert not bstack.backup.backups_dir().exists()


def test_prune_leaves_foreign_names_alone(bstack, tools, capsys):
    """Only files matching the name create() writes are candidates - so a tarball
    somebody dropped in the folder (or a backup from a renamed install) is never
    this command's to delete."""
    where = bstack.backup.backups_dir()
    where.mkdir(parents=True, exist_ok=True)
    keepers = ["holiday-2026.tar.gz", "klipperformac-backup.tar.gz",
               "klipperformac-backup-2026-01-01.tar.gz",
               "klipperformac-backup-20260101-000000.tar.gz.bak",
               "moonraker-db-backup-20260101.tar.gz"]
    for name in keepers:
        (where / name).write_bytes(b"\x1f\x8b")
    plant(bstack, tools, STAMPS[:4])
    capsys.readouterr()
    assert len(bstack.backup.prune(keep=1)) == 3
    left = sorted(p.name for p in where.iterdir())
    assert left == sorted(keepers + [NAME.format(STAMPS[3])])


def test_prune_never_deletes_a_folder(bstack, tools):
    """`prune` walks files, and a directory named like a backup is not a file:
    deleting a tree is not in this command's vocabulary."""
    where = bstack.backup.backups_dir()
    where.mkdir(parents=True)
    (where / NAME.format("20260101-000000")).mkdir()
    (where / NAME.format("20260101-000000") / "precious.txt").write_text("x\n")
    plant(bstack, tools, STAMPS[1:3])
    removed = bstack.backup.prune(keep=1)
    assert [p.name for p in removed] == [NAME.format(STAMPS[1])]
    assert (where / NAME.format("20260101-000000") / "precious.txt").is_file()


def test_prune_ignores_a_symlinked_backup(bstack, tools):
    """A symlink with our exact name is not a regular file this tool wrote, and
    following it would let DATA/backups decide what gets deleted somewhere
    else."""
    outside = bstack.data.parent / "somewhere-else.tar.gz"
    outside.write_bytes(b"\x1f\x8b")
    where = bstack.backup.backups_dir()
    where.mkdir(parents=True)
    (where / NAME.format("20260101-000000")).symlink_to(outside)
    plant(bstack, tools, STAMPS[1:2])
    assert [p.name for p in bstack.backup.prune(keep=1)] == []
    assert outside.is_file()
    assert (where / NAME.format("20260101-000000")).is_symlink()


def test_prune_never_touches_a_file_outside_the_backup_folder(bstack, tools):
    """Identical names live in other folders on every real machine; prune builds
    its list from DATA/backups and then re-checks the realpath of each candidate,
    so neither a bad list nor a moved folder can make it delete elsewhere."""
    plant(bstack, tools, STAMPS[:2])
    elsewhere = bstack.paths.CONFIG / NAME.format("20260101-000000")
    elsewhere.write_bytes(b"\x1f\x8b")
    home = bstack.paths.APP_HOME / NAME.format("20260101-000000")
    home.write_bytes(b"\x1f\x8b")
    bstack.backup.prune(keep=1)
    assert elsewhere.is_file()
    assert home.is_file()


def test_prune_orders_by_the_name_not_the_mtime(bstack, tools):
    """A restored or rsync-copied folder has fresh mtimes on the oldest backups.
    The stamp in the name is the authority, or 'keep the newest 10' would keep the
    ten that were touched last and delete the ten that were taken last."""
    where = bstack.backup.backups_dir()
    plant(bstack, tools, STAMPS[:5])
    future = time.time() + 400000
    for stamp in STAMPS[:3]:  # the three OLDEST by name, newest by mtime
        os.utime(str(where / NAME.format(stamp)), (future, future))
    removed = [p.name for p in bstack.backup.prune(keep=2)]
    assert removed == [NAME.format(s) for s in STAMPS[:3]]


def test_prune_orders_same_second_backups_by_their_suffix(bstack, tools):
    """create() gives the second backup of a second -1, the third -2 ... so the
    unsuffixed one is the oldest of that second and the first to go."""
    stamp = "20260101-000000"
    where = bstack.backup.backups_dir()
    where.mkdir(parents=True)
    names = ["klipperformac-backup-{}.tar.gz".format(stamp)]
    names += ["klipperformac-backup-{}-{}.tar.gz".format(stamp, i)
              for i in range(1, 12)]
    for name in names:
        (where / name).write_bytes(b"\x1f\x8b")
    removed = [p.name for p in bstack.backup.prune(keep=2)]
    assert removed[0] == names[0]  # the plain name is the oldest
    assert removed[1] == names[1]  # then -1, -2, ... in numeric order
    assert len(removed) == 10


def test_prune_ten_collisions_sort_numerically_not_lexically(bstack, tools):
    """'-10' sorts before '-2' as text. Prune compares the suffix as a number, so
    the newest of eleven same-second backups is the one with the highest counter."""
    where = bstack.backup.backups_dir()
    where.mkdir(parents=True)
    stamp = "20260101-000000"
    names = ["klipperformac-backup-{}-{}.tar.gz".format(stamp, i)
             for i in range(1, 11)]
    for name in names:
        (where / name).write_bytes(b"\x1f\x8b")
    removed = [p.name for p in bstack.backup.prune(keep=1)]
    assert len(removed) == 9
    assert (where / names[-1]).is_file()  # -10 is the newest


def test_prune_dry_run_deletes_nothing(bstack, tools, capsys):
    plant(bstack, tools, STAMPS[:4])
    capsys.readouterr()
    removed = bstack.backup.prune(keep=1, dry_run=True)
    out = capsys.readouterr().out
    assert len(removed) == 3
    assert "would delete" in out
    assert "dry run" in out
    assert len(list((bstack.paths.DATA / "backups").glob("*.tar.gz"))) == 4


def test_prune_reports_what_is_left(bstack, tools, capsys):
    plant(bstack, tools, STAMPS[:6])
    bstack.backup.prune(keep=3)
    out = capsys.readouterr().out
    assert "deleted: {}".format(NAME.format(STAMPS[0])) in out
    assert "3 backup(s) left" in out


def test_prune_of_a_folder_it_cannot_read(bstack, tools, monkeypatch):
    """An unreadable DATA/backups is one clean line, never a traceback - and it
    deletes nothing on the way out."""
    where = plant(bstack, tools, STAMPS[:2])[0].parent

    def boom(self):
        raise OSError("I/O error")
    monkeypatch.setattr(type(where), "iterdir", boom)
    with pytest.raises(SystemExit):
        bstack.backup.prune(keep=1)
    assert where.is_dir()
