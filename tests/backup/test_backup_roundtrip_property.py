"""Roundtrip property: whatever `create` archives, `restore` puts back byte for
byte - for every data tree, not just the one fixture I liked.

A hand-written roundtrip test proves the happy path once. The property that
matters is "no file is corrupted, truncated, dropped, or added by a restore",
and that is a statement about names and bytes nobody can enumerate: filenames
with spaces, dots, quotes, unicode, 8-bit content, nested preset folders, empty
files. hypothesis picks the tree; the comparison is the whole tree.

Offline only: a throwaway KLIPPERFORMAC_HOME per example, no stack, no network,
no printer. The name alphabet is what APFS will actually accept (a filesystem
refusing an unassigned codepoint with EILSEQ is not a backup bug, and a test that
pretends otherwise is noise).
"""
import os
import shutil
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

HS = dict(deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture,
                                 HealthCheck.too_slow])

# Every printable ASCII name character except "/" (a separator, and the whole
# point of the guard is that it never arrives from an archive), plus a few
# non-ASCII ones a real user's folder has: a German umlaut (cp1252 configs),
# Japanese, an emoji, and characters that stress case-folding.
_ALPHABET = ("".join(chr(c) for c in range(0x20, 0x7F) if chr(c) != "/")
             + "éñÄß日本語🖨Ω·")
_NAME = st.text(alphabet=_ALPHABET, min_size=1, max_size=24).filter(
    lambda n: n not in (".", "..") and n.strip() and not n.startswith("."))
# Any bytes at all: a latin-1 comment, a NUL in a gcode, an empty file. Real
# gcodes are MBs; 4 KB keeps a 25-example sweep under a second.
_BYTES = st.binary(min_size=0, max_size=4096)
_SUBDIRS = ("", "presets", "history", "presets/voron", "KAMP", "templates")


@st.composite
def data_tree(draw):
    """{relative path: bytes} under DATA, always including the two configs a
    restore refuses to be without."""
    tree = {"config/printer.cfg": b"[printer]\nmax_velocity: 300\n[mcu]\n"
                                  b"serial: /dev/a\n",
            "config/moonraker.conf": b"[server]\nhost: 127.0.0.1\n"}
    for folder in draw(st.lists(st.sampled_from(_SUBDIRS), unique=True,
                                max_size=4)):
        prefix = "config/" + (folder + "/" if folder else "")
        for _ in range(draw(st.integers(min_value=0, max_value=3))):
            tree[prefix + draw(_NAME)] = draw(_BYTES)
    for folder in ("gcodes", "database", "misc"):
        for _ in range(draw(st.integers(min_value=0, max_value=3))):
            tree[folder + "/" + draw(_NAME)] = draw(_BYTES)
    return tree


def rel_tree(paths, drop_history=False):
    """{relpath: bytes} of everything the whitelist carries, walked by hand.

    The version history is the one folder a restore is expected to grow (the
    config it is about to overwrite is snapshotted first), so it is only
    compared when the caller says it is the thing under test."""
    got = {}
    for top in ("config", "gcodes", "database", "misc"):
        root = paths.DATA / top
        if not root.is_dir():
            continue
        for cur, _dirs, files in os.walk(str(root)):
            for name in files:
                full = os.path.join(cur, name)
                if os.path.islink(full):
                    continue
                rel = os.path.relpath(full, str(paths.DATA))
                if drop_history and rel.startswith("config/history/"):
                    continue
                got[rel] = Path(full).read_bytes()
    return got


def full_snapshot(bstack):
    got = rel_tree(bstack.paths, drop_history=True)
    for path in (bstack.paths.SETTINGS, bstack.paths.LOCKFILE):
        if path.is_file():
            got["app:" + path.name] = path.read_bytes()
    return got


def wipe(bstack, paths):
    for top in ("config", "gcodes", "database", "misc"):
        shutil.rmtree(str(paths / top), ignore_errors=True)
    for path in (bstack.paths.SETTINGS, bstack.paths.LOCKFILE):
        if path.is_file():
            path.unlink()


@given(tree=data_tree())
@settings(**HS, max_examples=25)
def test_every_backup_restores_byte_for_byte(bstack, tools, tree):
    data = bstack.paths.DATA
    for rel, raw in tree.items():
        path = data / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    expected = full_snapshot(bstack)
    expected_all = rel_tree(bstack.paths)  # with the version history this time
    assert expected, "a tree with no files proves nothing"
    archive = bstack.backup.create()
    # Destroy it the way a dead disk does: every whitelisted folder gone, plus the
    # two files the archive carries from APP_HOME. run/ and logs/ stay, so
    # looks_like_data_dir() still recognises the folder being restored over.
    wipe(bstack, data)
    assert not rel_tree(bstack.paths)
    bstack.paths.APP_HOME.mkdir(parents=True, exist_ok=True)
    bstack.backup.restore(archive.name)
    assert full_snapshot(bstack) == expected
    after = rel_tree(bstack.paths)
    # The history folder is in too, and nothing that was not in the archive
    # appeared: the receipt is skipped here because there was no live config to
    # snapshot after the wipe.
    assert after == expected_all


@given(tree=data_tree())
@settings(**HS, max_examples=10)
def test_restore_is_idempotent(bstack, tools, tree):
    """Restoring the same backup twice writes the same bytes twice. Anything else
    means the second restore saw what the first one left behind."""
    data = bstack.paths.DATA
    for rel, raw in tree.items():
        path = data / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    archive = bstack.backup.create()
    bstack.backup.restore(archive.name)
    first = full_snapshot(bstack)
    bstack.backup.restore(archive.name)
    assert full_snapshot(bstack) == first


@given(blob=st.binary(min_size=32, max_size=600))
@settings(**HS, max_examples=25)
def test_a_non_backup_never_reaches_the_disk(bstack, tools, blob):
    """Whatever bytes a file in DATA/backups happens to hold - random noise, a
    photo, half a download - restore refuses it and leaves the tree exactly as it
    was. No traceback, no partial write, no staging tree left behind."""
    before = full_snapshot(bstack)
    stamp = "2026{:02d}{:02d}-{:02d}{:02d}00".format(
        blob[0] % 12 + 1, blob[1] % 28 + 1, blob[2] % 24, blob[3] % 60)
    archive = tools.write_backup(
        bstack.paths.DATA, "klipperformac-backup-{}.tar.gz".format(stamp),
        bytes(blob[4:]))
    with pytest.raises(SystemExit):
        bstack.backup.restore(archive.name)
    assert full_snapshot(bstack) == before
    assert [p.name for p in archive.parent.iterdir()
            if p.name.startswith(".")] == []


@given(name=st.text(min_size=1, max_size=60))
@settings(**HS, max_examples=60)
def test_no_name_can_escape_the_backup_folder(bstack, tools, name):
    """The --from value is one filename, so every string a user can type - or a
    script can be fed - has to end up either as a real file in DATA/backups or as
    a refusal. Nothing in between, and nothing written on the way out."""
    where = bstack.backup.backups_dir()
    where.mkdir(parents=True, exist_ok=True)
    target = where / "klipperformac-backup-20260101-000000.tar.gz"
    if not target.exists():
        target.write_bytes(tools.build([
            ("data/config/printer.cfg", "reg", "[printer]\nmax_velocity: 1\n"),
            ("data/config/moonraker.conf", "reg", "[server]\nhost: 127.0.0.1\n")]))
    before = sorted(str(p) for p in bstack.paths.DATA.rglob("*"))
    try:
        got = bstack.backup._resolve_source(name)
    except SystemExit:
        got = None
    if got is not None:
        # Resolved means: a real regular file, not a symlink, and a .tar.gz.
        # A BARE name additionally has to land inside DATA/backups under exactly
        # the name asked for - no normalisation onto a sibling. A name with a
        # separator in it is an explicit path, which is allowed on purpose (a
        # backup copied from another Mac, or one on an external disk).
        assert got.is_file() and not got.is_symlink()
        assert got.name.endswith(".tar.gz")
        if "/" not in name and name != "~" and not name.startswith("~/"):
            assert os.path.realpath(str(got.parent)) == os.path.realpath(
                str(where))
            assert got.name == name
    assert sorted(str(p) for p in bstack.paths.DATA.rglob("*")) == before


def test_the_fixture_tree_itself_roundtrips(bstack, tools):
    """Not a hypothesis case: the exact shipped layout (all four whitelisted
    folders, plus logs/, web/ and run/), so the property above is anchored to a
    tree this tool really makes."""
    before = full_snapshot(bstack)
    archive = bstack.backup.create()
    (bstack.paths.DATA / "config" / "printer.cfg").write_text("broken\n")
    bstack.backup.restore(archive.name)
    after = full_snapshot(bstack)
    assert after == before
    assert bstack.paths.PRINTER_CFG.read_text() != "broken\n"
    # ...and the one addition is the receipt: the config that was live got
    # snapshotted before the swap, into the folder `presets --history` lists.
    added = rel_tree(bstack.paths, drop_history=False)
    receipts = [k for k in added if "before-restore" in k]
    assert len(receipts) == 1
    assert added[receipts[0]] == b"broken\n"
