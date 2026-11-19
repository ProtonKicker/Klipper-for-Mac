"""`klipperformac backup restore` - judged as much by what it leaves untouched as
by what it writes.

The order is the contract: resolve the archive, refuse a running stack, prove the
gzip stream, audit every member name, stage the whole thing into a throwaway tree
inside DATA/backups, parse the configs FROM THE STAGED TREE, snapshot the config
that is live right now - and only then swap, one write-then-replace per file.
Anything failing earlier means the data folder is exactly as it was, which is
what the hostile-archive tests below measure: they look for the file that was NOT
written.
"""
import json
import os
import shutil

import pytest

GOOD_PRINTER = "[printer]\nmax_velocity: 300\n\n[mcu]\nserial: /dev/a\n"
GOOD_MOONRAKER = "[server]\nhost: 127.0.0.1\nklippy_uds_address: /x/klippy.sock\n"


def snapshot_tree(paths):
    """{relpath: bytes} of everything a backup carries, for byte comparisons."""
    got = {}
    data = paths.DATA
    for top in ("config", "gcodes", "database", "misc", "logs"):
        root = data / top
        if not root.is_dir():
            continue
        for cur, _dirs, files in os.walk(str(root)):
            for name in files:
                p = os.path.join(cur, name)
                if os.path.islink(p):
                    continue
                got[os.path.relpath(p, str(data))] = open(p, "rb").read()
    for p in (paths.SETTINGS, paths.LOCKFILE):
        if p.is_file():
            got["app:" + p.name] = p.read_bytes()
    return got


# ------------------------------------------------------------------ happy ---

def test_restore_puts_the_files_back_byte_for_byte(bstack):
    before = snapshot_tree(bstack.paths)
    archive = bstack.backup.create()
    # Wreck the install the way a bad evening does it.
    bstack.paths.PRINTER_CFG.write_text("half edited, gone wrong\n")
    (bstack.paths.PRESETS / "voron2.cfg").write_bytes(b"\x00\x01garbage")
    (bstack.paths.DATA / "gcodes" / "cube.gcode").unlink()
    bstack.paths.SETTINGS.write_text("[]\n")  # hand-edited into junk
    bstack.backup.restore(archive.name)
    after = snapshot_tree(bstack.paths)
    for key, raw in before.items():
        if key.startswith("config/history/"):
            continue  # the version history grows by design; not what is pinned
        assert key in after, key
        assert after[key] == raw, key
    assert (bstack.paths.DATA / "gcodes" / "cube.gcode").is_file()


def test_restore_returns_the_paths_it_wrote(bstack):
    archive = bstack.backup.create()
    bstack.paths.PRINTER_CFG.write_text("gone\n")
    written = bstack.backup.restore(archive.name)
    names = sorted(str(p) for p in written)
    assert str(bstack.paths.PRINTER_CFG) in names
    assert str(bstack.paths.MOONRAKER_CONF) in names
    assert str(bstack.paths.SETTINGS) in names
    assert str(bstack.paths.LOCKFILE) in names


def test_restore_snapshots_the_live_config_first(bstack):
    """cli.snapshot_printer_cfg() is the existing machinery - the same folder
    `klipperformac presets --history` lists and `--restore` reads, so a restore
    that goes sideways is itself undoable."""
    current = bstack.paths.PRINTER_CFG.read_text()
    archive = bstack.backup.create()
    bstack.paths.PRINTER_CFG.write_text("[printer]\nmax_velocity: 1\n")
    bstack.backup.restore(archive.name)
    hist = sorted((bstack.paths.CONFIG / "history").glob("printer-*-before-restore.cfg"))
    assert len(hist) == 1
    assert hist[0].read_text() == "[printer]\nmax_velocity: 1\n"
    assert bstack.paths.PRINTER_CFG.read_text() == current
    assert "before-restore" in hist[0].name


def test_restore_deletes_nothing_the_backup_does_not_carry(bstack):
    """Restoring is not a factory reset: DATA/web was excluded on purpose, a file
    written after the backup is newer than it and nobody asked to lose it. The
    alternative - wiping the folder first - is the destructive surprise this
    command must never be."""
    archive = bstack.backup.create()
    extra = bstack.paths.CONFIG / "made-after-the-backup.cfg"
    extra.write_text("[printer]\nnew: 1\n")
    bstack.paths.PRINTER_CFG.write_text("gone\n")
    bstack.backup.restore(archive.name)
    assert extra.is_file()
    assert (bstack.paths.WEB_MAINSAIL / "index.html").is_file()
    assert (bstack.paths.LOGS / "klippy.log").is_file()


def test_restore_leaves_no_staging_tree_behind(bstack):
    archive = bstack.backup.create()
    bstack.backup.restore(archive.name)
    leftovers = [p.name for p in archive.parent.iterdir()
                 if p.name.startswith(".")]
    assert leftovers == []
    assert sorted(p.name for p in archive.parent.iterdir()) == [archive.name]


def test_restore_needs_the_stack_down(bstack, tools, monkeypatch):
    """No --allow-running for this one: swapping the configs a running klippy has
    open leaves two truths, and the printer moves by the invisible one."""
    archive = bstack.backup.create()
    bstack.paths.PRINTER_CFG.write_text("[printer]\nmax_velocity: 9\n")
    tools.fake_stack(bstack, monkeypatch)
    with pytest.raises(SystemExit):
        bstack.backup.restore(archive.name)
    assert bstack.paths.PRINTER_CFG.read_text() == "[printer]\nmax_velocity: 9\n"


def test_restore_refuses_a_data_folder_that_is_not_ours(bstack):
    """The same guard as create, on the write side: this is the command that can
    overwrite a tree, so a junk settings.json value naming ~/Documents has to be
    refused before anything is written into it."""
    archive = bstack.backup.create()
    foreign = bstack.data.parent / "documents"
    foreign.mkdir()
    (foreign / "taxes.pdf").write_bytes(b"%PDF")
    (foreign / "config").mkdir()
    (foreign / "gcodes").mkdir()
    bstack.paths.set_data(foreign)
    with pytest.raises(SystemExit):
        bstack.backup.restore(archive.name)
    assert (foreign / "taxes.pdf").is_file()
    assert (foreign / "taxes.pdf").read_bytes() == b"%PDF"
    assert not (foreign / "printer.cfg").exists()


def test_restore_of_the_only_backup_twice_is_stable(bstack):
    archive = bstack.backup.create()
    first = snapshot_tree(bstack.paths)
    bstack.backup.restore(archive.name)
    bstack.backup.restore(archive.name)
    second = snapshot_tree(bstack.paths)
    for key in first:
        if key.startswith("config/history/"):
            continue
        assert first[key] == second[key], key


# ---------------------------------------------------------------- hostile ---

HOSTILE_NAMES = [
    "/tmp/evil.cfg",
    "~/evil.cfg",
    "../evil.cfg",
    "data/config/../../../../Windows/evil.cfg",
    "data/../../outside.cfg",
    "etc/passwd",
    "data",
    "app",
]


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_restore_refuses_a_traversing_member_and_writes_nothing(bstack, tools,
                                                                name):
    """The zip-slip equivalent, proved on the write side: the archive is refused
    AND the tree is checked for the file it tried to create."""
    members = [("data/config/", "dir", b""),
               ("data/config/printer.cfg", "reg", GOOD_PRINTER),
               ("data/config/moonraker.conf", "reg", GOOD_MOONRAKER),
               (name, "reg", "ESCAPED\n")]
    blob = tools.build(members)
    archive = tools.write_backup(bstack.paths.DATA,
                                 "klipperformac-backup-20260101-000000.tar.gz",
                                 blob)
    before = bstack.paths.PRINTER_CFG.read_text()
    with pytest.raises(SystemExit):
        bstack.backup.restore(archive.name)
    assert bstack.paths.PRINTER_CFG.read_text() == before
    assert not (bstack.data.parent / "evil.cfg").exists()
    assert not (bstack.data.parent / "outside.cfg").exists()
    assert not (bstack.data / "backups" / ".." / "evil.cfg").exists()
    # refused before staging: no temp tree, and no snapshot taken either
    assert [p.name for p in archive.parent.iterdir()] == [archive.name]
    assert not list((bstack.paths.CONFIG / "history").glob("*before-restore*"))


def test_restore_refuses_a_symlink_member_and_creates_no_link(bstack, tools):
    members = [("data/config/", "dir", b""),
               ("data/config/printer.cfg", "reg", GOOD_PRINTER),
               ("data/config/moonraker.conf", "reg", GOOD_MOONRAKER),
               ("data/config/pwned.cfg", "sym", b"", "/etc/passwd")]
    archive = tools.write_backup(
        bstack.paths.DATA, "klipperformac-backup-20260101-000000.tar.gz",
        tools.build(members))
    with pytest.raises(SystemExit):
        bstack.backup.restore(archive.name)
    assert not (bstack.paths.CONFIG / "pwned.cfg").exists()
    assert not (bstack.paths.CONFIG / "pwned.cfg").is_symlink()


def test_restore_refuses_a_huge_member_name(bstack, tools):
    members = [("data/config/", "dir", b""),
               ("data/config/printer.cfg", "reg", GOOD_PRINTER),
               ("data/config/moonraker.conf", "reg", GOOD_MOONRAKER),
               ("data/config/" + "A" * 500 + ".cfg", "reg", "x\n")]
    archive = tools.write_backup(
        bstack.paths.DATA, "klipperformac-backup-20260101-000000.tar.gz",
        tools.build(members))
    with pytest.raises(SystemExit):
        bstack.backup.restore(archive.name)
    assert not list(bstack.paths.CONFIG.glob("AAA*"))


def test_restore_refuses_a_non_gzip_file(bstack, tools):
    """A plain tar, or a text file, renamed to .tar.gz: nothing is read as a
    member and nothing is written."""
    raw = tools.build([("data/config/printer.cfg", "reg", GOOD_PRINTER)], gz=False)
    archive = tools.write_backup(
        bstack.paths.DATA, "klipperformac-backup-20260101-000000.tar.gz", raw)
    before = bstack.paths.PRINTER_CFG.read_text()
    with pytest.raises(SystemExit):
        bstack.backup.restore(archive.name)
    assert bstack.paths.PRINTER_CFG.read_text() == before


def test_restore_refuses_a_truncated_archive(bstack):
    archive = bstack.backup.create()
    before = snapshot_tree(bstack.paths)
    blob = archive.read_bytes()
    archive.write_bytes(blob[:len(blob) // 2])
    with pytest.raises(SystemExit):
        bstack.backup.restore(archive.name)
    after = snapshot_tree(bstack.paths)
    for key in before:
        assert after[key] == before[key]


def test_restore_refuses_an_empty_archive(bstack, tools):
    archive = tools.write_backup(
        bstack.paths.DATA, "klipperformac-backup-20260101-000000.tar.gz",
        tools.build([]))
    with pytest.raises(SystemExit):
        bstack.backup.restore(archive.name)


def test_restore_refuses_an_archive_without_a_printer_cfg(bstack, tools):
    """A backup that cannot boot the printer is refused out loud - it is not
    restored "as much as possible"."""
    archive = tools.write_backup(
        bstack.paths.DATA, "klipperformac-backup-20260101-000000.tar.gz",
        tools.build([("data/config/", "dir", b""),
                     ("data/config/moonraker.conf", "reg", GOOD_MOONRAKER),
                     ("data/gcodes/cube.gcode", "reg", "G28\n")]))
    before = snapshot_tree(bstack.paths)
    with pytest.raises(SystemExit) as exc:
        bstack.backup.restore(archive.name)
    assert exc.value.code == 1
    # Nothing was written, not even the file the archive does carry.
    assert snapshot_tree(bstack.paths) == before


def test_restore_refuses_a_broken_config_inside_the_archive(bstack, tools):
    """Staged first, validated second: this archive is complete and its names are
    clean, and it still writes nothing, because the printer.cfg inside it would
    halt klippy on the next start."""
    archive = tools.write_backup(
        bstack.paths.DATA, "klipperformac-backup-20260101-000000.tar.gz",
        tools.build([("data/config/", "dir", b""),
                     ("data/config/printer.cfg", "reg", "no section header\n"),
                     ("data/config/moonraker.conf", "reg", GOOD_MOONRAKER)]))
    before = bstack.paths.PRINTER_CFG.read_text()
    with pytest.raises(SystemExit):
        bstack.backup.restore(archive.name)
    assert bstack.paths.PRINTER_CFG.read_text() == before
    assert not list((bstack.paths.CONFIG / "history").glob("*before-restore*"))


def test_restore_refuses_a_printer_cfg_without_a_printer_section(bstack, tools):
    archive = tools.write_backup(
        bstack.paths.DATA, "klipperformac-backup-20260101-000000.tar.gz",
        tools.build([("data/config/", "dir", b""),
                     ("data/config/printer.cfg", "reg", "[mcu]\nserial: /dev/a\n"),
                     ("data/config/moonraker.conf", "reg", GOOD_MOONRAKER)]))
    with pytest.raises(SystemExit):
        bstack.backup.restore(archive.name)
    assert bstack.paths.PRINTER_CFG.read_text() != "[mcu]\nserial: /dev/a\n"


def test_restore_refuses_a_traversing_or_double_dot_name(bstack, tools):
    for bad in ("../x.tar.gz", "config/../x.tar.gz", ".hidden.tar.gz",
                "sub/dir.tar.gz", "a\x00b.tar.gz"):
        with pytest.raises(SystemExit):
            bstack.backup.restore(bad)


def test_restore_refuses_a_name_that_is_too_long(bstack, tools):
    """A backup name is a filename; cli._safe_name()'s limits apply here too, and
    the message has to say that instead of an ENAMETOOLONG traceback."""
    with pytest.raises(SystemExit):
        bstack.backup.restore("A" * 5000 + ".tar.gz")


def test_restore_refuses_the_symlink_in_the_backup_folder(bstack, tools):
    """Not only is a symlink not a backup - following one is how a restore ends up
    reading a file nobody chose."""
    outside = bstack.data.parent / "outside.tar.gz"
    outside.write_bytes(tools.build([
        ("data/config/", "dir", b""),
        ("data/config/printer.cfg", "reg", GOOD_PRINTER),
        ("data/config/moonraker.conf", "reg", GOOD_MOONRAKER)]))
    where = bstack.backup.backups_dir()
    where.mkdir(parents=True, exist_ok=True)
    link = where / "klipperformac-backup-20260101-000000.tar.gz"
    link.symlink_to(outside)
    with pytest.raises(SystemExit):
        bstack.backup.restore(link.name)


def test_restore_needs_a_name(bstack):
    bstack.backup.create()
    with pytest.raises(SystemExit):
        bstack.backup.restore(None)


def test_restore_writes_the_backup_not_through_a_symlinked_destination(bstack):
    """The staged file lands on a path that happens to be a symlink - to
    something outside the data folder. os.replace() swaps the LINK, so the
    backup's bytes end up in DATA/config/printer.cfg as a regular file and the
    file the link pointed at is never written to. The link is not ours to
    follow, and this test is the proof."""
    archive = bstack.backup.create()
    victim = bstack.data.parent / "shellrc"
    victim.write_text("export HOME_IS_A_PRINTER=1\n")
    bstack.paths.PRINTER_CFG.unlink()
    bstack.paths.PRINTER_CFG.symlink_to(victim)
    bstack.backup.restore(archive.name)
    assert not bstack.paths.PRINTER_CFG.is_symlink()
    assert bstack.paths.PRINTER_CFG.read_text() == archive_config(bstack, archive)
    assert victim.read_text() == "export HOME_IS_A_PRINTER=1\n"


def archive_config(bstack, archive):
    """printer.cfg as the archive holds it (read straight out of the tar)."""
    import tarfile
    with tarfile.open(str(archive), "r:gz") as tf:
        return tf.extractfile("data/config/printer.cfg").read().decode()


def test_restore_of_an_archive_written_by_hand_restores_the_files_it_carries(
        bstack, tools):
    """`tar czf` on another Mac produces a valid archive with no manifest. The
    files are still checked and still restored - the format is the contract, not
    the metadata."""
    tools.populate(bstack, logs=False)
    bstack.paths.PRINTER_CFG.write_text(GOOD_PRINTER)
    bstack.paths.MOONRAKER_CONF.write_text(GOOD_MOONRAKER)
    archive = tools.write_backup(
        bstack.paths.DATA, "klipperformac-backup-20260101-000000.tar.gz",
        tools.build([("data/", "dir", b""),
                     ("data/config/", "dir", b""),
                     ("data/config/printer.cfg", "reg", GOOD_PRINTER),
                     ("data/config/moonraker.conf", "reg", GOOD_MOONRAKER)]))
    bstack.backup.restore(archive.name)
    assert bstack.paths.PRINTER_CFG.read_text() == GOOD_PRINTER
    assert bstack.paths.MOONRAKER_CONF.read_text() == GOOD_MOONRAKER


def test_restore_makes_the_folders_it_needs(bstack, tools):
    """A brand-new data folder that only has config/ (looks_like_data_dir() is
    satisfied by printer.cfg): a backup carrying database/ and misc/ has to
    create them rather than fail halfway."""
    archive = bstack.backup.create()
    for name in ("database", "misc", "gcodes", "logs", "web"):
        shutil.rmtree(str(bstack.paths.DATA / name), ignore_errors=True)
    bstack.backup.restore(archive.name)
    assert (bstack.paths.DATA / "database" / "moonraker_db.sqlite").is_file()
    assert (bstack.paths.DATA / "misc" / "templates" / "t.gcode").is_file()
    assert (bstack.paths.DATA / "gcodes" / "cube.gcode").is_file()


def test_restore_refuses_to_write_a_file_over_a_directory(bstack, tools):
    """The one shape mismatch a restore cannot decide for itself: DATA/gcodes is
    a folder on disk and a plain file in the archive. Refused before anything is
    written - deleting a folder to make room for a file is not what "restore"
    was asked to do."""
    archive = tools.write_backup(
        bstack.paths.DATA, "klipperformac-backup-20260101-000000.tar.gz",
        tools.build([("data/config/", "dir", b""),
                     ("data/config/printer.cfg", "reg", GOOD_PRINTER),
                     ("data/config/moonraker.conf", "reg", GOOD_MOONRAKER),
                     ("data/gcodes", "reg", "I AM A FILE NOW\n")]))
    before = snapshot_tree(bstack.paths)
    with pytest.raises(SystemExit):
        bstack.backup.restore(archive.name)
    assert bstack.paths.DATA.joinpath("gcodes").is_dir()
    assert (bstack.paths.DATA / "gcodes" / "cube.gcode").is_file()
    assert snapshot_tree(bstack.paths) == before


def test_restore_reports_the_data_folder_it_wrote_into(bstack, capsys):
    archive = bstack.backup.create()
    bstack.backup.restore(archive.name)
    out = capsys.readouterr().out
    assert "Restored" in out and str(bstack.paths.DATA) in out
    assert "klipperformac restart" in out


def test_restore_refuses_a_config_size_bomb_before_staging_it(bstack, tools):
    """The size cap is checked from the tar HEADERS, so a "printer.cfg" of 8 MB
    never reaches the disk. Refusing after writing would be a disk bomb with a
    nice message attached, and the staging tree lives on the same volume as the
    data folder it was protecting."""
    big = b"\0" * (bstack.backup.MAX_VALIDATE_BYTES + 1)
    archive = tools.write_backup(
        bstack.paths.DATA, "klipperformac-backup-20260101-000000.tar.gz",
        tools.build([("data/config/", "dir", b""),
                     ("data/config/printer.cfg", "reg", big),
                     ("data/config/moonraker.conf", "reg", GOOD_MOONRAKER)]))
    before = bstack.paths.PRINTER_CFG.read_text()
    with pytest.raises(SystemExit):
        bstack.backup.restore(archive.name)
    assert bstack.paths.PRINTER_CFG.read_text() == before
    assert [p.name for p in archive.parent.iterdir()
            if p.name.startswith(".")] == []


def test_a_giant_gcode_is_still_a_legitimate_backup(bstack, tools):
    """The cap is on config files, not on size in general: a 20 MB print is the
    reason people make backups, and refusing it would make the command useless on
    a real machine."""
    archive = tools.write_backup(
        bstack.paths.DATA, "klipperformac-backup-20260101-000000.tar.gz",
        tools.build([("data/config/", "dir", b""),
                     ("data/config/printer.cfg", "reg", GOOD_PRINTER),
                     ("data/config/moonraker.conf", "reg", GOOD_MOONRAKER),
                     ("data/gcodes/", "dir", b""),
                     ("data/gcodes/big.gcode", "reg", b"G1 X1\n" * 300000)]))
    assert bstack.backup.verify(archive.name)[0] is True
    bstack.backup.restore(archive.name)
    assert (bstack.paths.DATA / "gcodes" / "big.gcode").is_file()


def test_restore_reads_a_cp1252_config_through_the_house_reader(bstack, tools):
    """The staged validation uses paths.read_config_text() - the same reader
    up/doctor/lan use - so a vendor config with one German comment byte restores
    exactly as it would boot."""
    text = "[printer]\nmax_velocity: 300  # Größe\n"
    archive = tools.write_backup(
        bstack.paths.DATA, "klipperformac-backup-20260101-000000.tar.gz",
        tools.build([("data/config/", "dir", b""),
                     ("data/config/printer.cfg", "reg", text.encode("cp1252")),
                     ("data/config/moonraker.conf", "reg", GOOD_MOONRAKER)]))
    bstack.backup.restore(archive.name)
    assert bstack.paths.PRINTER_CFG.read_bytes() == text.encode("cp1252")
