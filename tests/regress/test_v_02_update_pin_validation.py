"""Regression (hunt finding [6], group F3): `update --pin COMP=<junk>` must be
stopped by ref validation, and must never destroy a working checkout.

As filed, `klipperformac update --pin 'klipper='` accepted an empty tag, printed
"Pinned: klipper=", deleted the pristine klipper checkout and then dumped a raw
subprocess.CalledProcessError traceback, leaving the install with no klipper
tree and a garbage pin.

Two things fixed it, both in code this group owns:
  * installer.checkout() / installer.fetch_webui() validate the ref shape FIRST
    (installer._validate_ref) and only then touch the filesystem, and
  * checkout() fetches into a `<name>.new` staging tree and swaps it in, so even
    a well-formed ref that fails to fetch cannot delete the tree that works
    today (see test_v_03_checkout_mid_clone_failure.py).

These tests drive the real CLI entry point - cli.main(["update", "--pin", ...]) -
with a fake git standing in for subprocess.run, so they prove the CLI path
really does go through the installer's validation and that a rejected pin never
reaches a command line. The venv/pip/c_helper steps are stubbed: they are not
the subject here and a throwaway home has no venv.

Also fixed in cli.py: cmd_update now validates every `--pin` value before it is
saved to lockfile.json, so a rejected pin never persists and never becomes the
next setup() run's problem.
"""
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

SHA = "461c4e3722c3a897fba1c6b3f0780a5315043842"

JUNK_PINS = [
    pytest.param("klipper=", "klipper", id="empty-tag"),
    pytest.param("klipper=   ", "klipper", id="whitespace-tag"),
    pytest.param("klipper=not-a-real-ref;rm -rf /", "klipper", id="shell-junk"),
    pytest.param("klipper=a=b=c", "klipper", id="punctuation-junk"),
    pytest.param("klipper=-b", "klipper", id="option-injection"),
    pytest.param("klipper=../../etc/passwd", "klipper", id="traversal"),
    pytest.param("klipper=" + "A" * 400, "klipper", id="over-long"),
    pytest.param("klipper=v1\nmaster", "klipper", id="newline-in-tag"),
    pytest.param("moonraker=", "moonraker", id="empty-moonraker-tag"),
    pytest.param("moonraker=--upload-pack=evil", "moonraker", id="ext-args"),
    pytest.param("mainsail=", "mainsail", id="empty-mainsail-tag"),
    pytest.param("fluidd=", "fluidd", id="empty-fluidd-tag"),
]


def cp(rc=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


class FakeGit:
    """installer._run stand-in that behaves like a healthy git.

    `clone` really creates the staging tree (the way git would), so a swap that
    follows swaps real content, and no network is ever touched.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kw):
        argv = [str(c) for c in cmd]
        self.calls.append(argv)
        joined = " ".join(argv)
        if "rev-parse HEAD" in joined:
            return cp(0, (SHA + "\n").encode())
        if "clone" in argv:
            stage = Path(argv[-1])
            (stage / "klippy").mkdir(parents=True, exist_ok=True)
            (stage / "klippy" / "klippy.py").write_text("#fetched from upstream\n")
        elif "init" in argv:
            Path(argv[-1]).mkdir(parents=True, exist_ok=True)
        return cp(0)

    def joined_all(self):
        return [" ".join(c) for c in self.calls]


@pytest.fixture
def installed(stack, monkeypatch):
    """Decoy home with a working, marked klipper + moonraker checkout, fake git,
    and the build steps stubbed out so a pin error is the error you see."""
    for comp in ("klipper", "moonraker"):
        dest = stack.installer.DESTS[comp]
        (dest / "klippy").mkdir(parents=True, exist_ok=True)
        (dest / "CANARY.txt").write_text("IRREPLACEABLE-PINNED-SOURCE " + comp)
        (dest / "klippy" / "klippy.py").write_text("#upstream\n")
        stack.installer._ref_marker(comp).write_text(SHA + "\n")
    stack.versions.save_pins(dict(stack.versions.DEFAULT_PINS))
    fake = FakeGit()
    monkeypatch.setattr(stack.installer, "_run", fake)
    for name in ("create_venv", "install_requirements", "test_chelper_build"):
        monkeypatch.setattr(stack.installer, name, lambda *a, **k: None)
    # a download attempt means a ref got past validation with no guard: the
    # stub answers with a one-file release zip, offline, for VALID tags only
    monkeypatch.setattr(stack.installer, "_download", _offline_download)
    monkeypatch.setattr(stack.installer.urllib.request, "urlopen", _no_network)
    return fake


def _offline_download(url, dest_path, **kw):
    with zipfile.ZipFile(dest_path, "w") as z:
        z.writestr("index.html", "<!doctype html><title>offline release</title>\n")


def _no_network(*a, **k):
    raise AssertionError("these tests must stay offline")


def run_update(stack, spec):
    """Returns (SystemExit code/message, leaked-exception-or-None)."""
    try:
        stack.cli.main(["update", "--pin", spec])
    except SystemExit as e:
        return e.code, None
    except BaseException as e:                    # any traceback is a failure
        return None, e
    return 0, None


def tree(dest):
    """{relative path: content} of a checkout, ignoring git metadata."""
    if not dest.is_dir():
        return None
    return {str(p.relative_to(dest)): p.read_text()
            for p in sorted(dest.rglob("*"))
            if p.is_file() and ".git" not in p.parts}


@pytest.mark.parametrize("spec,comp", JUNK_PINS)
def test_junk_pin_is_a_clean_error_not_a_traceback(stack, installed, spec, comp):
    code, exc = run_update(stack, spec)
    assert exc is None, "update --pin leaked {!r}".format(exc)
    assert isinstance(code, str), "the user gets a message, not a bare status"
    assert "Invalid {} pin".format(comp) in str(code), str(code)
    junk = spec.partition("=")[2].strip()
    if junk:
        for joined in installed.joined_all():
            assert junk not in joined, "junk pin reached a command line: " + joined


@pytest.mark.parametrize("spec,comp", JUNK_PINS)
def test_junk_pin_leaves_that_component_exactly_as_it_was(stack, installed,
                                                           spec, comp):
    dest = (stack.installer.DESTS[comp] if comp in stack.installer.DESTS
            else getattr(stack.paths, "WEB_" + comp.upper()))
    before = tree(dest)
    run_update(stack, spec)
    assert tree(dest) == before, "{} changed under a rejected pin".format(comp)
    if comp in stack.installer.DESTS:
        assert not dest.parent.joinpath(dest.name + ".new").exists()
        assert not dest.parent.joinpath(dest.name + ".old").exists()
        assert stack.installer._ref_marker(comp).read_text().strip() == SHA


def test_unknown_component_is_refused_without_running_git(stack, installed):
    code, exc = run_update(stack, "bambu=v1.0")
    assert exc is None and code == 1
    assert installed.calls == []


def test_a_well_formed_but_missing_ref_keeps_the_checkout(stack, installed,
                                                           monkeypatch):
    """'v9.9.9-does-not-exist' has a valid SHAPE, so git does run - and when the
    clone fails, the tree that works today must still be on disk, untouched,
    behind a CLI error that says so (no CalledProcessError traceback)."""
    dest = stack.installer.DESTS["klipper"]
    keep = tree(dest)

    def failing_clone(cmd, **kw):
        argv = [str(c) for c in cmd]
        if "clone" in argv:
            raise subprocess.CalledProcessError(
                128, argv, b"",
                b"fatal: Remote branch v9.9.9-does-not-exist not found in "
                b"upstream origin")
        if "rev-parse HEAD" in " ".join(argv):
            return cp(0, (SHA + "\n").encode())
        return cp(0)

    monkeypatch.setattr(stack.installer, "_run", failing_clone)
    code, exc = run_update(stack, "klipper=v9.9.9-does-not-exist")
    assert exc is None, "a missing ref must be a clean CLI error, not a traceback"
    assert isinstance(code, str) and "Could not fetch klipper" in str(code)
    assert "left untouched" in str(code)
    assert tree(dest) == keep
    assert not (dest.parent / "klipper.new").exists()
    assert not (dest.parent / "klipper.old").exists()
    # the marker still names the ref that is actually on disk
    assert stack.installer._ref_marker("klipper").read_text().strip() == SHA


def test_a_valid_pin_still_provisions_through_the_cli(stack, installed,
                                                      monkeypatch):
    """The guard must not break the happy path: a real tag is re-fetched and
    both the ref and the resolved commit are recorded."""
    fetched = []
    monkeypatch.setattr(stack.installer, "fetch_mainsail",
                        lambda tag: fetched.append(("mainsail", tag)))
    monkeypatch.setattr(stack.installer, "fetch_fluidd",
                        lambda tag: fetched.append(("fluidd", tag)))
    code, exc = run_update(stack, "klipper=v1.3.0")
    assert exc is None, repr(exc)
    assert any("--branch v1.3.0" in j for j in installed.joined_all())
    assert stack.installer._ref_marker("klipper").read_text().strip() == "v1.3.0"
    saved = json.loads(stack.paths.LOCKFILE.read_text())
    assert saved["klipper"] == "v1.3.0" and saved["klipper_sha"] == SHA
    assert fetched == [("mainsail", "v2.19.0"), ("fluidd", "v1.37.6")]


def test_a_junk_pin_is_not_persisted_to_lockfile(stack, installed):
    """A rejected `update --pin` must leave lockfile.json naming the previous
    good ref, not a junk value that setup() would have to reject later."""
    code, exc = run_update(stack, "klipper=a=b=c")
    assert exc is None
    assert isinstance(code, str) and "Invalid klipper pin" in str(code)
    saved = json.loads(stack.paths.LOCKFILE.read_text())
    assert saved["klipper"] == stack.versions.DEFAULT_PINS["klipper"]
    dest = stack.installer.DESTS["klipper"]
    assert "IRREPLACEABLE" in (dest / "CANARY.txt").read_text()
