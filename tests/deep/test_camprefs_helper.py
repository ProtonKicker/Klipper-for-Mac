"""Deep battery: helper-binary lookup (cam.helper_path / _needs_copy /
cam._repo_mac_dir) with completely fabricated paths.

Nothing is ever executed: cam.subprocess is replaced by a blocker, the real
/Applications bundle is swapped for tmp_path fakes, and every path that the
lookup may create lives inside the throwaway home from the `stack` fixture.
"""
import os
import subprocess
import time

import pytest
from hypothesis import HealthCheck, given, settings as hst, strategies as st

CAM_NAME = "KlipperCam"


class NoExec(BaseException):
    """Raised instead of ever launching a process from this battery."""


class BlockedSubprocess:
    DEVNULL = getattr(subprocess, "DEVNULL", -3)
    TimeoutExpired = subprocess.TimeoutExpired

    @staticmethod
    def run(*a, **k):
        raise NoExec("subprocess.run blocked: " + repr(a))

    @staticmethod
    def Popen(*a, **k):
        raise NoExec("subprocess.Popen blocked: " + repr(a))


@pytest.fixture(autouse=True)
def _hermetic(stack, tmp_path, monkeypatch):
    """No env leakage from the machine, no real bundle, no exec, no socket."""
    monkeypatch.delenv("KLIPPERFORMAC_CAM_BIN", raising=False)
    monkeypatch.delenv("KLIPPERFORMAC_REPO_DIR", raising=False)
    monkeypatch.setattr(stack.cam, "CAM_BIN_ENVS", ("KLIPPERFORMAC_CAM_BIN",))
    monkeypatch.setattr(stack.cam, "REPO_DIR_ENVS", ("KLIPPERFORMAC_REPO_DIR",))
    monkeypatch.setattr(stack.cam, "GUI_BUNDLES", ())
    monkeypatch.setattr(stack.cam, "subprocess", BlockedSubprocess)
    monkeypatch.setattr(stack.cam, "_port_free", lambda port: True)
    return stack


def bundle_at(stack, tmp_path, name="Klipper for Mac.app", body=b"BINARY"):
    b = tmp_path / name / "Contents" / "Resources" / CAM_NAME
    b.parent.mkdir(parents=True, exist_ok=True)
    b.write_bytes(body)
    return b


# ---------------------------------------------------------------- env var


def test_env_var_wins_when_it_is_a_real_file(stack, tmp_path, monkeypatch):
    helper = tmp_path / "mycam"
    helper.write_bytes(b"X")
    monkeypatch.setenv("KLIPPERFORMAC_CAM_BIN", str(helper))
    stack.paths.CAM_BIN.parent.mkdir(parents=True, exist_ok=True)
    stack.paths.CAM_BIN.write_bytes(b"installed")
    assert stack.cam.helper_path() == helper
    assert stack.paths.CAM_BIN.read_bytes() == b"installed"


@pytest.mark.parametrize("target", ["missing", "directory", "empty", "spaces",
                                   "newline", "huge", "relative"])
def test_env_var_pointing_at_anything_but_a_file_is_ignored(stack, tmp_path,
                                                            monkeypatch, target):
    d = tmp_path / "a directory"
    d.mkdir()
    values = {
        "missing": str(tmp_path / "nope"),
        "directory": str(d),
        "empty": "",
        "spaces": "   ",
        "newline": "cam\nname",
        "huge": str(tmp_path / ("x" * 5000)),
        "relative": "not-an-absolute-helper",
    }
    monkeypatch.setenv("KLIPPERFORMAC_CAM_BIN", values[target])
    got = stack.cam.helper_path()
    assert got is None, "must fall through to nothing, got %r" % (got,)


def test_env_var_expands_a_leading_tilde(stack, tmp_path, monkeypatch):
    fake_home = tmp_path / "userhome"
    helper = fake_home / "bin/CAM"
    helper.parent.mkdir(parents=True)
    helper.write_bytes(b"X")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("KLIPPERFORMAC_CAM_BIN", "~/bin/CAM")
    assert stack.cam.helper_path() == helper


def test_env_var_tilde_is_not_expanded_in_the_middle(stack, monkeypatch,
                                                     tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setenv("KLIPPERFORMAC_CAM_BIN", "nowhere/~/CAM")
    assert stack.cam.helper_path() is None


# ------------------------------------------------------------ installed bin


def test_current_installed_copy_wins_and_the_bundle_is_untouched(stack, tmp_path,
                                                                monkeypatch):
    inside = bundle_at(stack, tmp_path, body=b"same bytes")
    monkeypatch.setattr(stack.cam, "GUI_BUNDLES", (str(inside),))
    stack.paths.CAM_BIN.parent.mkdir(parents=True, exist_ok=True)
    stack.paths.CAM_BIN.write_bytes(b"same bytes")
    os.utime(stack.paths.CAM_BIN, (inside.stat().st_atime,
                                   inside.stat().st_mtime))
    assert stack.cam.helper_path() == stack.paths.CAM_BIN
    assert stack.paths.CAM_BIN.read_bytes() == b"same bytes"
    assert inside.read_bytes() == b"same bytes"


def test_missing_everywhere_returns_none(stack):
    assert stack.cam.helper_path() is None


def test_helper_is_copied_out_of_the_bundle_once(stack, tmp_path, monkeypatch):
    inside = bundle_at(stack, tmp_path, body=b"APPLE-SILICON-BINARY")
    monkeypatch.setattr(stack.cam, "GUI_BUNDLES", (str(inside),))
    got = stack.cam.helper_path()
    assert got == stack.paths.CAM_BIN
    assert stack.paths.CAM_BIN.read_bytes() == b"APPLE-SILICON-BINARY"
    assert stack.paths.CAM_BIN.stat().st_mode & 0o111, "must be executable"
    first = stack.paths.CAM_BIN.stat().st_mtime_ns
    assert stack.cam.helper_path() == stack.paths.CAM_BIN
    assert stack.paths.CAM_BIN.stat().st_mtime_ns == first, \
        "second lookup must not re-copy"
    assert inside.read_bytes() == b"APPLE-SILICON-BINARY", "bundle untouched"


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: cam.helper_path() returns any existing "
    "~/.klipperformac/bin/KlipperCam without ever comparing it to the app "
    "bundle, so _needs_copy() is dead code and a newer helper shipped by an "
    "updated 'Klipper for Mac.app' is never installed - the user keeps "
    "streaming on the old binary until the file is deleted by hand"))
def test_a_newer_bundle_helper_replaces_the_installed_copy(stack, tmp_path,
                                                           monkeypatch):
    inside = bundle_at(stack, tmp_path, body=b"OLD")
    monkeypatch.setattr(stack.cam, "GUI_BUNDLES", (str(inside),))
    assert stack.cam.helper_path() == stack.paths.CAM_BIN
    assert stack.paths.CAM_BIN.read_bytes() == b"OLD"
    time.sleep(0.01)
    inside.write_bytes(b"NEWER-AND-BIGGER")
    os.utime(inside, None)
    assert stack.cam.helper_path() == stack.paths.CAM_BIN
    assert stack.paths.CAM_BIN.read_bytes() == b"NEWER-AND-BIGGER"


def test_second_bundle_candidate_is_used_when_the_first_is_gone(stack, tmp_path,
                                                                monkeypatch):
    gone = tmp_path / "no-app.app/Contents/Resources/KlipperCam"
    inside = bundle_at(stack, tmp_path, name="Second.app")
    monkeypatch.setattr(stack.cam, "GUI_BUNDLES", (str(gone), str(inside)))
    assert stack.cam.helper_path() == stack.paths.CAM_BIN
    assert stack.paths.CAM_BIN.read_bytes() == inside.read_bytes()


def test_tilde_bundle_candidate_expands(stack, tmp_path, monkeypatch):
    fake_home = tmp_path / "userhome"
    inside = bundle_at(stack, fake_home / "Applications")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(stack.cam, "GUI_BUNDLES",
                        ("~/Applications/Klipper for Mac.app/Contents/Resources/"
                         + CAM_NAME,))
    assert stack.cam.helper_path() == stack.paths.CAM_BIN
    assert stack.paths.CAM_BIN.read_bytes() == inside.read_bytes()


@pytest.mark.parametrize("many", [1, 20, 500])
def test_long_candidate_lists_are_scanned_without_trouble(stack, tmp_path,
                                                          monkeypatch, many):
    cands = [str(tmp_path / ("ghost%d" % i)) for i in range(many)]
    inside = bundle_at(stack, tmp_path)
    monkeypatch.setattr(stack.cam, "GUI_BUNDLES", tuple(cands + [str(inside)]))
    assert stack.cam.helper_path() == stack.paths.CAM_BIN
    assert stack.paths.CAM_BIN.read_bytes() == inside.read_bytes()


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod is a no-op for root")
def test_read_only_bin_dir_falls_back_to_the_bundle(stack, tmp_path,
                                                    monkeypatch):
    inside = bundle_at(stack, tmp_path)
    monkeypatch.setattr(stack.cam, "GUI_BUNDLES", (str(inside),))
    stack.paths.CAM_BIN.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(stack.paths.CAM_BIN.parent, 0o500)
    try:
        assert stack.cam.helper_path() == inside
    finally:
        os.chmod(stack.paths.CAM_BIN.parent, 0o755)


def test_bin_dir_blocked_by_a_regular_file_falls_back(stack, tmp_path,
                                                      monkeypatch):
    inside = bundle_at(stack, tmp_path)
    monkeypatch.setattr(stack.cam, "GUI_BUNDLES", (str(inside),))
    stack.paths.CAM_BIN.parent.parent.mkdir(parents=True, exist_ok=True)
    stack.paths.CAM_BIN.parent.write_text("I am a file, not a folder")
    assert stack.cam.helper_path() == inside


def test_bundle_that_is_a_directory_is_skipped(stack, tmp_path, monkeypatch):
    d = tmp_path / "folder.app"
    d.mkdir()
    monkeypatch.setattr(stack.cam, "GUI_BUNDLES", (str(d),))
    assert stack.cam.helper_path() is None


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: when ~/.klipperformac/bin/KlipperCam exists as a DIRECTORY, "
    "helper_path() copies the bundle binary *into* that folder (shutil.copy2 "
    "silently retargets), chmods the folder, and returns the folder path as "
    "'the helper' - every later exec then fails and doctor blames the camera "
    "permission instead of the broken install"))
def test_cam_bin_as_a_directory_must_not_be_reported_as_the_helper(
        stack, tmp_path, monkeypatch):
    inside = bundle_at(stack, tmp_path, body=b"BINARY")
    monkeypatch.setattr(stack.cam, "GUI_BUNDLES", (str(inside),))
    stack.paths.CAM_BIN.mkdir(parents=True)
    got = stack.cam.helper_path()
    assert got is None or got.is_file(), "helper_path returned %r" % (got,)
    assert list(stack.paths.CAM_BIN.iterdir()) == [], \
        "the bundle binary must not be dumped into the folder"


# ---------------------------------------------------------------- _needs_copy


@pytest.mark.parametrize("dest_kind, dest_age, expect", [
    ("missing", 0, True),
    ("same-size, same mtime", 0, False),
    ("same-size, dest newer", -50, False),
    ("same-size, dest older", 50, True),
    ("different-size, dest newer", -50, True),
])
def test_needs_copy_table(stack, tmp_path, dest_kind, dest_age, expect):
    src = tmp_path / "src"
    src.write_bytes(b"12345")
    now = time.time()
    os.utime(src, (now, now))
    dest = tmp_path / "dest"
    if dest_kind != "missing":
        dest.write_bytes(b"12345" if "different" not in dest_kind else b"1")
        os.utime(dest, (now - dest_age, now - dest_age))
    assert stack.cam._needs_copy(src, dest) is expect


def test_needs_copy_ignores_content_of_an_up_to_date_dest(stack, tmp_path):
    """Documented limitation, not a bug: freshness is stat-based, so a
    same-size bundle binary that is not newer than the copy is never re-read.
    (asserted so a future size+mtime/hash comparison change is noticed)"""
    src = tmp_path / "src"
    src.write_bytes(b"AAAAA")
    dest = tmp_path / "dest"
    dest.write_bytes(b"BBBBB")
    now = time.time()
    os.utime(src, (now, now))
    os.utime(dest, (now, now))
    assert stack.cam._needs_copy(src, dest) is False


def test_needs_copy_when_dest_is_a_directory(stack, tmp_path):
    src = tmp_path / "src"
    src.write_bytes(b"x")
    dest = tmp_path / "dirdest"
    dest.mkdir()
    assert stack.cam._needs_copy(src, dest) is True


def test_needs_copy_when_src_is_gone(stack, tmp_path):
    dest = tmp_path / "dest"
    dest.write_bytes(b"x")
    assert stack.cam._needs_copy(tmp_path / "ghost", dest) is True


# ------------------------------------------------------------ repo lookup


def test_repo_dir_env_pointing_at_the_repo_root(stack, tmp_path, monkeypatch):
    mac = tmp_path / "repo/KlipperMac"
    mac.mkdir(parents=True)
    (mac / "Package.swift").write_text("// swift\n")
    monkeypatch.setenv("KLIPPERFORMAC_REPO_DIR", str(tmp_path / "repo"))
    assert stack.cam._repo_mac_dir() == mac


def test_repo_dir_env_pointing_at_klippersmac_dir(stack, tmp_path, monkeypatch):
    mac = tmp_path / "repo/KlipperMac"
    mac.mkdir(parents=True)
    (mac / "Package.swift").write_text("// swift\n")
    monkeypatch.setenv("KLIPPERFORMAC_REPO_DIR", str(mac))
    assert stack.cam._repo_mac_dir() == mac


def test_repo_dir_env_with_package_swift_directly(stack, tmp_path, monkeypatch):
    root = tmp_path / "flat"
    root.mkdir()
    (root / "Package.swift").write_text("//\n")
    monkeypatch.setenv("KLIPPERFORMAC_REPO_DIR", str(root))
    assert stack.cam._repo_mac_dir() == root


@pytest.mark.parametrize("value", ["", "   ", "/definitely/not/here",
                                  "relative-nope", "/"])
def test_repo_dir_env_garbage_falls_back_to_the_checkout(stack, monkeypatch,
                                                         tmp_path, value):
    monkeypatch.setenv("KLIPPERFORMAC_REPO_DIR", value)
    got = stack.cam._repo_mac_dir()
    assert got is not None and (got / "Package.swift").is_file()


def test_repo_dir_env_expands_tilde(stack, tmp_path, monkeypatch):
    mac = tmp_path / "userhome/repo/KlipperMac"
    mac.mkdir(parents=True)
    (mac / "Package.swift").write_text("//\n")
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setenv("KLIPPERFORMAC_REPO_DIR", "~/repo")
    assert stack.cam._repo_mac_dir() == mac


# ------------------------------------------------- prefs/doctor around it


def test_doctor_without_any_helper_is_all_red_but_sane(stack):
    out = stack.cam.doctor()
    assert len(out) == 6
    ok_helper, msg = out[0]
    assert ok_helper is False and "cam build" in msg
    assert out[1] == (True, "camera permission: not checkable without the helper")


def test_permission_of_a_missing_helper_is_unknown(stack):
    assert stack.cam.permission() == ("unknown", False)


@pytest.mark.parametrize("code, word, usable", [
    (0, "authorized", True), (2, "denied", False), (3, "restricted", False),
    (4, "not determined", False), (7, "unknown", False), (-1, "unknown", False),
    (255, "unknown", False),
])
def test_permission_maps_helper_exit_codes(stack, monkeypatch, code, word,
                                           usable):
    stack.paths.CAM_BIN.parent.mkdir(parents=True, exist_ok=True)
    stack.paths.CAM_BIN.write_bytes(b"\xcf\xfa\xed\xfe")

    class FakeCompleted:
        returncode = code

    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return FakeCompleted()

    monkeypatch.setattr(stack.cam, "subprocess",
                        type("SP", (BlockedSubprocess,),
                             {"run": staticmethod(fake_run)}))
    assert stack.cam.permission() == (word, usable)
    assert calls[0][0] == [str(stack.paths.CAM_BIN), "--check-permission"]
    assert calls[0][1]["timeout"] == 30


def test_permission_of_an_unanswerable_helper(stack, monkeypatch):
    """An exec failure (not an exit code) is 'unknown', never a crash."""
    stack.paths.CAM_BIN.parent.mkdir(parents=True, exist_ok=True)
    stack.paths.CAM_BIN.write_bytes(b"not executable")

    class OSErrorSubprocess(BlockedSubprocess):
        @staticmethod
        def run(*a, **k):
            raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(stack.cam, "subprocess", OSErrorSubprocess)
    assert stack.cam.permission() == ("unknown", False)
    assert (False, "camera permission: unknown (the helper did not answer)") \
        in stack.cam.doctor()


@hst(max_examples=25, deadline=None,
     suppress_health_check=[HealthCheck.too_slow,
                            HealthCheck.function_scoped_fixture])
@given(body=st.lists(st.sampled_from(["missing", "file", "dir", "symlinkish",
                                      "empty", "huge-name"]), max_size=6))
def test_helper_path_never_raises_for_any_candidate_layout(body, stack,
                                                           tmp_path, monkeypatch):
    base = tmp_path / "cases"
    base.mkdir(exist_ok=True)
    cands = []
    for i, kind in enumerate(body):
        p = base / ("%s-%d" % (kind, i))
        if kind == "missing":
            pass
        elif kind == "file":
            p.write_bytes(b"B")
        elif kind == "dir":
            p.mkdir(exist_ok=True)
        elif kind == "empty":
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"")
        elif kind == "huge-name":
            p = base / ("n" * 200)
            p.write_bytes(b"B")
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"B")
        cands.append(str(p))
    monkeypatch.setattr(stack.cam, "GUI_BUNDLES", tuple(cands))
    got = stack.cam.helper_path()
    assert got is None or isinstance(got, type(stack.paths.CAM_BIN))
    if got is not None:
        assert str(got).startswith(str(base)) or got == stack.paths.CAM_BIN


def test_doctor_survives_a_directory_named_like_the_helper(stack, monkeypatch):
    stack.paths.CAM_BIN.parent.mkdir(parents=True, exist_ok=True)
    stack.paths.CAM_BIN.mkdir()
    monkeypatch.setattr(stack.cam, "GUI_BUNDLES", ())
    out = stack.cam.doctor()
    assert len(out) == 6 and all(isinstance(m, str) for ok, m in out)
