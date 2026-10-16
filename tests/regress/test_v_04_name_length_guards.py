"""Regression (hunt finding [5], the part this group owns): a name or pin that
is too long for the filesystem must be a clean CLI error, never a raw
OSError(ENAMETOOLONG) traceback.

The reporter's victim was `klipperformac presets --save <400 chars>` in cli.py
(_safe_name has no length or control-character bound and shutil.copy2 is
unguarded) - that half lives in group F1's file and is filed as needsFixInF1.

The same lesson IS already enforced in klipperformac/installer.py, where wave 1
bounded NAME_MAX everywhere a name we do not control reaches the filesystem or
git: _safe_extract() (a zip member name) and _validate_ref() (a pin from
lockfile.json / `update --pin`). Those are this group's guards for the bug
family, pinned here so they cannot regress.
"""
import io
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

LONG = "A" * 400                      # > NAME_MAX (255) -> ENAMETOOLONG
MAX_NAME = "B" * (255 - 4) + ".cfg"   # exactly NAME_MAX bytes: legal
SHA = "461c4e3722c3a897fba1c6b3f0780a5315043842"


def cp(rc=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


def make_zip(names):
    """An in-memory archive with exactly these member names."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in names:
            z.writestr(name, "content\n")
    buf.seek(0)
    return zipfile.ZipFile(buf)


def extract(stack, target, names):
    target.mkdir(parents=True, exist_ok=True)
    stack.installer._safe_extract(make_zip(names), target)


def test_an_over_long_zip_member_name_is_refused_not_crashed(stack, tmp_path):
    """Without the NAME_MAX bound this raised `OSError: [Errno 63] File name
    too long` out of z.extractall() - the exact pattern the presets bug hit."""
    target = tmp_path / "out"
    with pytest.raises(SystemExit) as exc:
        extract(stack, target, [LONG + ".cfg", "index.html"])
    assert "Unsafe zip path" in str(exc.value)
    assert list(target.iterdir()) == [], "a refused archive must write nothing"


def test_an_over_long_member_name_deep_in_a_tree_is_refused(stack, tmp_path):
    target = tmp_path / "out"
    with pytest.raises(SystemExit):
        extract(stack, target, ["sub/dir/" + "C" * 300 + ".js", "index.html"])
    assert list(target.iterdir()) == []


def test_a_member_name_at_the_filesystem_limit_still_extracts(stack, tmp_path):
    target = tmp_path / "out"
    extract(stack, target, [MAX_NAME, "sub/index.html"])
    assert (target / MAX_NAME).is_file()
    assert (target / "sub" / "index.html").is_file()


class _RawEntry:
    """A zip central-directory entry exactly as recorded on the wire.

    zipfile.ZipInfo sanitises names (it truncates at a NUL and strips the
    directory), so it cannot be used to model a hostile archive; _safe_extract()
    only ever reads `.filename`, which is what a malicious writer sets.
    """

    def __init__(self, name):
        self.filename = name


class FakeArchive:
    """Stand-in for a ZipFile whose central directory carries an embedded NUL.

    os.path.realpath() - used by the containment check - raises ValueError for
    such a name, so the name must be audited before it reaches the filesystem.
    """

    def __init__(self, names):
        self._names = names

    def infolist(self):
        return [_RawEntry(name) for name in self._names]

    def extractall(self, path):
        raise AssertionError("a refused archive must never be extracted")


def test_a_nul_in_a_member_name_is_refused_before_the_filesystem(stack, tmp_path):
    target = tmp_path / "out"
    target.mkdir(parents=True)
    with pytest.raises(ValueError):            # what the guard prevents
        os.path.realpath("index\x00.html")
    with pytest.raises(SystemExit) as exc:
        stack.installer._safe_extract(FakeArchive(["index\x00.html"]), target)
    assert "Unsafe zip path" in str(exc.value)
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("pin", [
    pytest.param(LONG, id="400-chars"),
    pytest.param("x" * 5000, id="5000-chars"),
    pytest.param("\x00v1.0", id="nul-byte"),
    pytest.param("v1.0\x01", id="control-byte"),
    pytest.param("v1.0 branch", id="space"),
])
def test_an_over_long_or_control_character_pin_is_a_user_error(stack, pin):
    dest = stack.installer.DESTS["klipper"]
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "CANARY.txt").write_text("IRREPLACEABLE-PINNED-SOURCE\n")
    with pytest.raises(SystemExit) as exc:
        stack.installer._validate_ref("klipper", pin)
    assert "Invalid klipper pin" in str(exc.value)
    assert (dest / "CANARY.txt").exists()


def test_checkout_with_an_over_long_pin_never_reaches_git(stack, monkeypatch):
    dest = stack.installer.DESTS["klipper"]
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "CANARY.txt").write_text("IRREPLACEABLE-PINNED-SOURCE\n")
    calls = []
    monkeypatch.setattr(stack.installer, "_run",
                        lambda cmd, **kw: calls.append(cmd) or cp(0))
    with pytest.raises(SystemExit):
        stack.installer.checkout("klipper", LONG)
    assert calls == []
    assert (dest / "CANARY.txt").read_text().startswith("IRREPLACEABLE")
    assert not (dest.parent / "klipper.new").exists()


def test_fetch_webui_with_an_over_long_tag_never_reaches_the_network(stack,
                                                                    monkeypatch):
    dest = stack.paths.WEB_MAINSAIL
    dest.mkdir(parents=True)
    (dest / "index.html").write_text("the UI that works\n")

    def boom(*a, **k):
        raise AssertionError("an invalid tag must not be fetched")

    monkeypatch.setattr(stack.installer, "_download", boom)
    with pytest.raises(SystemExit) as exc:
        stack.installer.fetch_mainsail(LONG)
    assert "Invalid mainsail pin" in str(exc.value)
    assert (dest / "index.html").read_text() == "the UI that works\n"


def test_a_legitimate_long_tag_still_provisions(stack, monkeypatch):
    """The bound is NAME_MAX, not something tighter: a 200-char ref is fine."""
    ref = "v" + "9" * 199
    dest = stack.installer.DESTS["klipper"]

    def fake_run(cmd, **kw):
        argv = [str(c) for c in cmd]
        if "clone" in argv:
            stage = Path(argv[-1])
            (stage / "klippy").mkdir(parents=True, exist_ok=True)
            (stage / "klippy" / "klippy.py").write_text("#upstream\n")
        if "rev-parse HEAD" in " ".join(argv):
            return cp(0, (SHA + "\n").encode())
        return cp(0)

    monkeypatch.setattr(stack.installer, "_run", fake_run)
    assert stack.installer.checkout("klipper", ref) == SHA
    assert (dest / "klippy" / "klippy.py").exists()
