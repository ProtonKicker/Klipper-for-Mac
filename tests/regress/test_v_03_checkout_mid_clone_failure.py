"""Regression (hunt finding [20], group F3, severity critical): a fetch that
dies mid-flight must never cost the user the checkout that works today.

As filed, installer.checkout() did `shutil.rmtree(dest)` and *then* ran
`git clone --branch <ref>`, so a typo'd tag, a yanked tag, a rate-limited
GitHub or a connection that dies halfway deleted the pristine
~/.klipperformac/klipper tree and then dumped a raw CalledProcessError. On a
machine that cannot reach GitHub that is an unrecoverable install.

Fixed in klipperformac/installer.py by the stage-then-swap policy: every
upstream tree is fetched into `<name>.new`, verified, and only then moved into
place (the tree it replaces is kept restorable until the replacement is in
position). These tests use a fake git that writes PARTIAL content into the
staging directory and then fails, exactly like a clone interrupted mid-transfer,
and prove byte-for-byte that the old checkout survives.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

SHA = "461c4e3722c3a897fba1c6b3f0780a5315043842"
OLD_REF = "v0.11.0"


def cp(rc=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


@pytest.fixture
def working(stack):
    """Decoy home with a populated checkout + matching ref marker per component."""
    for comp in ("klipper", "moonraker"):
        dest = stack.installer.DESTS[comp]
        (dest / "klippy").mkdir(parents=True, exist_ok=True)
        (dest / "klippy" / "klippy.py").write_text("# pristine upstream file\n")
        (dest / "CANARY.txt").write_text("IRREPLACEABLE-PINNED-SOURCE " + comp + "\n")
        (dest / ".git" / "refs").mkdir(parents=True)
        stack.installer._ref_marker(comp).write_text(OLD_REF + "\n")
    return stack


def tree(dest):
    """{relative path: bytes} of everything in a tree, .git metadata included."""
    if not dest.is_dir():
        return None
    return {str(p.relative_to(dest)): p.read_bytes()
            for p in sorted(dest.rglob("*")) if p.is_file()}


def mid_clone_failure(written):
    """A `_run` stand-in that behaves like git dying during the transfer.

    `written` files really appear in the staging directory first - a partial
    clone is what a dropped connection leaves behind, and it must never be
    mistaken for a successful fetch.
    """
    def _fake(cmd, **kw):
        argv = [str(c) for c in cmd]
        joined = " ".join(argv)
        if "clone" in argv:
            stage = Path(argv[-1])
            stage.mkdir(parents=True, exist_ok=True)
            for name in written:
                (stage / name).write_text("half a file, network died\n")
            raise subprocess.CalledProcessError(
                128, argv, b"", b"fatal: early EOF\n"
                               b"fatal: index-pack failed")
        if "fetch" in argv:
            stage = Path(argv[2])
            (stage / "klippy").mkdir(parents=True, exist_ok=True)
            (stage / "klippy" / "klippy.py").write_text("partial\n")
            raise subprocess.CalledProcessError(
                128, argv, b"", b"error: RPC failed; curl 18 transfer closed "
                               b"with outstanding read data remaining")
        if "rev-parse HEAD" in joined:
            return cp(0, (SHA + "\n").encode())
        return cp(0)
    return _fake


@pytest.mark.parametrize("component", ["klipper", "moonraker"])
def test_a_clone_that_dies_midway_leaves_the_checkout_byte_identical(
        working, monkeypatch, component):
    dest = working.installer.DESTS[component]
    keep = tree(dest)
    monkeypatch.setattr(working.installer, "_run",
                        mid_clone_failure(["CANARY.txt", "klippy/partial.py"]))
    with pytest.raises(SystemExit) as exc:
        working.installer.checkout(component, "v9.9.9", force=True)
    msg = str(exc.value)
    assert "Could not fetch {} @ v9.9.9".format(component) in msg
    assert "was left untouched" in msg
    assert tree(dest) == keep, "the working checkout was modified"
    assert (dest / "CANARY.txt").read_text().startswith("IRREPLACEABLE")
    # the half-written staging tree is thrown away, never swapped in
    assert not (dest.parent / (dest.name + ".new")).exists()
    assert not (dest.parent / (dest.name + ".old")).exists()
    assert not (dest / "klippy" / "partial.py").exists()
    # and the marker still names the ref that is really on disk, so a later
    # `update` cannot short-circuit on a fetch that never happened
    assert working.installer._ref_marker(component).read_text().strip() == OLD_REF


@pytest.mark.parametrize("component", ["klipper", "moonraker"])
def test_a_commit_fetch_that_dies_midway_leaves_the_checkout(working,
                                                             monkeypatch,
                                                             component):
    """The 40-hex pin path (git init + fetch + checkout) is the one setup()
    really uses: same guarantee when it dies at the fetch."""
    dest = working.installer.DESTS[component]
    keep = tree(dest)
    monkeypatch.setattr(working.installer, "_run", mid_clone_failure([]))
    with pytest.raises(SystemExit) as exc:
        working.installer.checkout(component, SHA, force=True)
    assert "left untouched" in str(exc.value)
    assert tree(dest) == keep
    assert not (dest.parent / (dest.name + ".new")).exists()


def test_the_dead_ref_error_is_a_clean_cli_exit_not_a_traceback(working,
                                                                monkeypatch,
                                                                capsys):
    """`update --pin` end to end: exit status 1 + one readable message, and the
    install stays functional (the file klippy would import is still there)."""
    dest = working.installer.DESTS["klipper"]
    monkeypatch.setattr(working.installer, "_run",
                        mid_clone_failure(["CANARY.txt"]))
    for name in ("create_venv", "install_requirements", "test_chelper_build"):
        monkeypatch.setattr(working.installer, name, lambda *a, **k: None)
    with pytest.raises(SystemExit) as exc:
        working.cli.main(["update", "--pin", "klipper=v9.9.9-does-not-exist"])
    assert not isinstance(exc.value.code, int) or exc.value.code != 0
    assert "Traceback" not in capsys.readouterr().err
    assert (dest / "klippy" / "klippy.py").exists()


def test_a_stale_staging_tree_from_a_crash_is_cleared_before_the_next_fetch(
        working, monkeypatch):
    """Crash mid-clone, re-run: the leftover .new must not be merged with or
    reused for the new fetch."""
    dest = working.installer.DESTS["klipper"]
    stage = dest.parent / (dest.name + ".new")
    stage.mkdir(parents=True)
    (stage / "STALE_FROM_THE_CRASHED_RUN.py").write_text("garbage\n")

    def working_clone(cmd, **kw):
        argv = [str(c) for c in cmd]
        if "clone" in argv:
            target = Path(argv[-1])
            leftovers = ([p.name for p in target.iterdir()]
                         if target.is_dir() else [])
            assert "STALE_FROM_THE_CRASHED_RUN.py" not in leftovers, \
                "git was pointed at the leftover staging tree"
            (target / "klippy").mkdir(parents=True, exist_ok=True)
            (target / "klippy" / "klippy.py").write_text("# new upstream\n")
        if "rev-parse HEAD" in " ".join(argv):
            return cp(0, (SHA + "\n").encode())
        return cp(0)

    monkeypatch.setattr(working.installer, "_run", working_clone)
    assert working.installer.checkout("klipper", "v1.0.0", force=True) == SHA
    assert (dest / "klippy" / "klippy.py").read_text() == "# new upstream\n"
    assert not (dest / "STALE_FROM_THE_CRASHED_RUN.py").exists()
    assert not stage.exists()
    assert not (dest.parent / (dest.name + ".old")).exists()
    assert working.installer._ref_marker("klipper").read_text().strip() == "v1.0.0"


def test_a_failed_swap_rolls_the_previous_checkout_straight_back(working,
                                                                 monkeypatch):
    """Disk full at the moment of the swap: the old tree must come back into
    place, not stay parked at `<name>.old` (or vanish)."""
    dest = working.installer.DESTS["klipper"]
    keep = tree(dest)
    real_rename = os.rename

    def rename_that_fills_up(src, dst):
        if Path(src).name == dest.name + ".new":       # stage -> dest: ENOSPC
            raise OSError(28, "No space left on device")
        return real_rename(str(src), str(dst))         # dest -> .old, rollback

    monkeypatch.setattr(working.installer.os, "rename", rename_that_fills_up)

    def working_clone(cmd, **kw):
        argv = [str(c) for c in cmd]
        if "clone" in argv:
            target = Path(argv[-1])
            (target / "klippy").mkdir(parents=True, exist_ok=True)
            (target / "klippy" / "klippy.py").write_text("# new upstream\n")
        if "rev-parse HEAD" in " ".join(argv):
            return cp(0, (SHA + "\n").encode())
        return cp(0)

    monkeypatch.setattr(working.installer, "_run", working_clone)
    with pytest.raises(SystemExit) as exc:
        working.installer.checkout("klipper", "v1.0.0", force=True)
    assert "Could not install klipper" in str(exc.value)
    assert "previous version is still in place" in str(exc.value)
    assert tree(dest) == keep, "the rollback did not restore the checkout"
    assert not (dest.parent / (dest.name + ".new")).exists()
    assert not (dest.parent / (dest.name + ".old")).exists()


def test_update_apply_with_a_dying_network_changes_nothing_at_all(working,
                                                                  monkeypatch,
                                                                  capsys):
    """The zero-typo route from the finding: `update --apply` with any transient
    git failure. Pins are only persisted once every step succeeded, so the
    lockfile, both checkouts and the ref markers must be untouched."""
    keep = {c: tree(working.installer.DESTS[c]) for c in ("klipper", "moonraker")}
    working.versions.save_pins(dict(working.versions.DEFAULT_PINS))
    monkeypatch.setattr(working.installer, "_run",
                        mid_clone_failure(["CANARY.txt"]))
    for name in ("create_venv", "install_requirements", "test_chelper_build"):
        monkeypatch.setattr(working.installer, name, lambda *a, **k: None)
    monkeypatch.setattr(working.versions, "check_updates",
                        lambda: ([("moonraker", "v0.11.0", "v0.12.0")], []))
    with pytest.raises(SystemExit) as exc:
        working.cli.main(["update", "--apply"])
    assert "Could not fetch" in str(exc.value)
    assert "Traceback" not in capsys.readouterr().err
    for comp in ("klipper", "moonraker"):
        assert tree(working.installer.DESTS[comp]) == keep[comp]
        assert working.installer._ref_marker(comp).read_text().strip() == OLD_REF
    saved = json.loads(working.paths.LOCKFILE.read_text())
    assert saved["moonraker"] == "v0.11.0", "a failed --apply persisted new pins"
    assert not (working.paths.APP_HOME / "klipper.new").exists()
    assert not (working.paths.APP_HOME / "moonraker.new").exists()


@pytest.mark.parametrize("flaky", ["truncated-zip", "network-drop"])
def test_a_webui_fetch_that_dies_midway_keeps_the_installed_ui(working,
                                                              monkeypatch,
                                                              flaky):
    """Same policy for the web roots: a broken release asset must not cost the
    user the Mainsail/Fluidd tree that is serving the dashboard today."""
    dest = working.paths.WEB_MAINSAIL
    dest.mkdir(parents=True)
    (dest / "index.html").write_text("<title>the UI that works</title>\n")
    keep = tree(dest)
    working.installer._ref_marker("mainsail").write_text(OLD_REF + "\n")

    def bad_download(url, dest_path, **kw):
        if flaky == "network-drop":
            raise OSError("curl (18) transfer closed with outstanding read "
                          "data remaining")
        with open(dest_path, "wb") as f:                 # half an archive
            f.write(b"PK\x03\x04" + b"\x00" * 40)

    monkeypatch.setattr(working.installer, "_download", bad_download)
    with pytest.raises(SystemExit) as exc:
        working.installer.fetch_mainsail("v2.19.1")
    assert "Nothing was installed" in str(exc.value) or \
        "not a valid zip file" in str(exc.value)
    assert tree(dest) == keep
    assert not (dest.parent / (dest.name + ".new")).exists()
    assert working.installer._ref_marker("mainsail").read_text().strip() == OLD_REF
