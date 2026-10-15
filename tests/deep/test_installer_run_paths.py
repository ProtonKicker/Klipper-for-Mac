"""Deep tests: installer `_run` seams and every subprocess-facing helper.

Scope: klipperformac/installer.py -- `_run`, `find_python`, `check_prereqs`,
`create_venv`, `install_requirements`, `test_libsodium`, `test_chelper_build`,
`checkout`, `_head_sha`, `verify_pristine`, `_exclude_build_artifacts`.

Offline contract: an autouse fixture makes `urllib.request.urlopen` explode and
neutralises the developer's own git config, so nothing can reach the network or
GitHub. `_run` is replaced by a recorder in the tests that care about argv; the
few tests that do run a subprocess run `sys.executable -c ...` or `git init`
inside pytest's tmp_path -- never a service, never a serial port, never
~/.klipperformac.

`xfail(strict=True)` marks encode the expected-robust behaviour of product bugs
that are still present; everything else passes today.
"""
import ast
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from klipperformac import compat as compat_mod

SHA = "461c4e3722c3a897fba1c6b3f0780a5315043842"
HAS_GIT = shutil.which("git") is not None
needs_git = pytest.mark.skipif(not HAS_GIT, reason="git is not installed")


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """No socket, no user git config, no ambient KLIPPERFORMAC_PYTHON."""
    def _boom(*a, **k):
        raise AssertionError("installer reached the network")
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.delenv("KLIPPERFORMAC_PYTHON", raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    fake_home = tmp_path / "git-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


@pytest.fixture(autouse=True)
def no_real_setup(stack, offline):
    """setup() would run git, pip and real downloads: never in a deep test."""
    def _forbidden(*a, **kw):
        raise AssertionError(
            "klipperformac.installer.setup() must never run for real")
    stack.installer.setup = _forbidden
    yield


def cp(rc=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


def write_exe(path, body, mode=0o755):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    os.chmod(str(path), mode)
    return path


class RunRecorder:
    """Stands in for installer._run: records argv, replies from a script.

    `script` is a list of (substring, result) pairs consumed in order, where a
    result is a CompletedProcess, an exception instance, or a returncode int.
    """

    def __init__(self, script=None, default_rc=0, default_stdout=b"",
                 default_stderr=b""):
        self.script = list(script or [])
        self.calls = []
        self.default_rc = default_rc
        self.default_stdout = default_stdout
        self.default_stderr = default_stderr

    def __call__(self, cmd, **kw):
        argv = [str(c) for c in cmd]
        self.calls.append((argv, kw))
        joined = " ".join(argv)
        for i, (pattern, result) in enumerate(self.script):
            if pattern in joined:
                del self.script[i]
                if isinstance(result, BaseException):
                    raise result
                if isinstance(result, subprocess.CompletedProcess):
                    res = subprocess.CompletedProcess(argv, result.returncode,
                                                      result.stdout,
                                                      result.stderr)
                else:
                    res = cp(int(result), self.default_stdout,
                             self.default_stderr)
                if res.returncode and kw.get("check"):
                    raise subprocess.CalledProcessError(
                        res.returncode, argv, res.stdout, res.stderr)
                return res
        res = cp(self.default_rc, self.default_stdout, self.default_stderr)
        if self.default_rc and kw.get("check"):
            raise subprocess.CalledProcessError(self.default_rc, argv)
        return res

    def argv(self, index=-1):
        return self.calls[index][0]

    def joined(self, index=-1):
        return " ".join(self.argv(index))

    def find(self, needle):
        return [cmd for cmd, _ in self.calls if needle in " ".join(cmd)]


@pytest.fixture
def rec(stack, monkeypatch):
    """installer._run replaced by a recorder; the recorder is returned."""
    r = RunRecorder()
    monkeypatch.setattr(stack.installer, "_run", r)
    return r


def git(repo, *args, **kw):
    return subprocess.run(["git", "-C", str(repo)] + list(args),
                          capture_output=True, check=True, **kw)


def init_repo(path, tracked="README"):
    """A real, local, offline git repo with one committed file."""
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (Path(path) / tracked).write_text("upstream content\n")
    git(path, "add", tracked)
    git(path, "-c", "user.email=k@l", "-c", "user.name=k", "commit", "-qm",
        "initial")
    return Path(path)


# --------------------------------------------------------------------------
# _run itself
# --------------------------------------------------------------------------
def test_run_is_a_plain_argv_exec_with_no_shell(stack):
    """Shell metacharacters must be data, never commands."""
    res = stack.installer._run(
        [sys.executable, "-c", "import sys;sys.stdout.write(sys.argv[1])",
         "a; rm -rf /tmp/klipperformac-should-not-exist | $(touch x)"],
        capture_output=True)
    assert res.returncode == 0
    assert b"rm -rf" in res.stdout
    assert not Path("/tmp/klipperformac-should-not-exist").exists()


def test_run_propagates_the_child_exit_code_when_check_is_on(stack):
    with pytest.raises(subprocess.CalledProcessError) as exc:
        stack.installer._run([sys.executable, "-c", "raise SystemExit(3)"],
                             check=True)
    assert exc.value.returncode == 3


def test_run_without_check_returns_the_failed_result(stack):
    res = stack.installer._run([sys.executable, "-c", "raise SystemExit(4)"])
    assert res.returncode == 4


def test_run_reports_a_missing_executable_as_oserror(stack):
    with pytest.raises(OSError):
        stack.installer._run(["klipperformac-definitely-not-on-path", "x"])


def test_run_captures_stdout_and_stderr_separately(stack):
    res = stack.installer._run(
        [sys.executable, "-c",
         "import sys; sys.stdout.write('OUT'); sys.stderr.write('ERR')"],
        capture_output=True)
    assert res.stdout == b"OUT" and res.stderr == b"ERR"


def test_run_forwards_env_and_cwd(stack, tmp_path):
    res = stack.installer._run(
        [sys.executable, "-c", "import os;print(os.environ['KFM_PROBE'])"],
        capture_output=True, env={"KFM_PROBE": "yes", "PATH": "/usr/bin"},
        cwd=str(tmp_path))
    assert b"yes" in res.stdout


def test_run_is_the_only_subprocess_gate_installer_uses(stack):
    """Every child process must go through _run, or no fake can cover it."""
    tree = ast.parse(Path(stack.installer.__file__).read_text())
    gates = {}
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call)):
            f = call.func
            if (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                    and f.value.id == "subprocess"):
                gates.setdefault(fn.name, []).append(f.attr)
    assert sorted(gates) == ["_run"], (
        "installer bypasses _run in %r -- untestable offline" % gates)


# --------------------------------------------------------------------------
# find_python / check_prereqs
# --------------------------------------------------------------------------
def test_find_python_uses_klipperformac_python_first(stack, monkeypatch,
                                                     tmp_path):
    chosen = write_exe(tmp_path / "chosen-python",
                       '#!/bin/sh\n'
                       'case "$*" in\n'
                       '  *sys.executable*) echo /opt/chosen/python;;\n'
                       '  *) echo 3.12;;\n'
                       'esac\n')
    monkeypatch.setenv("KLIPPERFORMAC_PYTHON", str(chosen))
    monkeypatch.setattr(stack.installer, "PY_CANDIDATES", [sys.executable])
    assert stack.installer.find_python() == "/opt/chosen/python"


@settings(max_examples=24, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.tuples(st.integers(3, 4), st.integers(0, 40)))
def test_find_python_accepts_exactly_at_or_above_min_py(stack, monkeypatch,
                                                         pair):
    """Property: >= MIN_PY accepts, < MIN_PY declines; never a crash."""
    major, minor = pair
    version = "%d.%d" % pair
    r = RunRecorder(
        script=[("sys.executable", cp(0, b"/opt/first/python\n"))],
        default_stdout=(version + "\n").encode())
    monkeypatch.setattr(stack.installer, "_run", r)
    monkeypatch.setattr(stack.installer, "PY_CANDIDATES", ["python3.12"])
    monkeypatch.setattr(stack.installer.shutil, "which", lambda n: "/x/" + n)
    monkeypatch.setenv("KLIPPERFORMAC_PYTHON", "/opt/first/python")
    got = stack.installer.find_python()
    if (major, minor) >= stack.installer.MIN_PY:
        assert got == "/opt/first/python"
    else:
        assert got is None


def test_find_python_returns_none_when_no_candidate_is_new_enough(
        stack, monkeypatch):
    monkeypatch.setattr(stack.installer, "PY_CANDIDATES", ["python3.9"])
    monkeypatch.setattr(stack.installer.shutil, "which",
                        lambda name: "/usr/bin/" + name)
    r = RunRecorder(default_stdout=b"3.9\n")
    monkeypatch.setattr(stack.installer, "_run", r)
    assert stack.installer.find_python() is None


def test_find_python_survives_a_candidate_that_prints_nothing(
        stack, monkeypatch):
    """An empty version banner must be skipped, not compared as a tuple()."""
    monkeypatch.setattr(stack.installer, "PY_CANDIDATES",
                        ["python3.12", "python3.13"])
    monkeypatch.setattr(stack.installer.shutil, "which",
                        lambda name: "/usr/bin/" + name)
    r = RunRecorder(script=[
        ("python3.12", cp(0, b"")),
        ("python3.13", cp(0, b"3.13\n")),
        ("sys.executable", cp(0, b"/usr/bin/python3.13\n")),
    ])
    monkeypatch.setattr(stack.installer, "_run", r)
    assert stack.installer.find_python() == "/usr/bin/python3.13"


def test_find_python_skips_a_candidate_that_raises(stack, monkeypatch):
    monkeypatch.setattr(stack.installer, "PY_CANDIDATES",
                        ["python3.12", "python3.13"])
    monkeypatch.setattr(stack.installer.shutil, "which",
                        lambda name: "/usr/bin/" + name)
    r = RunRecorder(script=[
        ("python3.12", FileNotFoundError(2, "No such file")),
        ("python3.13", cp(0, b"3.13\n")),
        ("sys.executable", cp(0, b"/usr/bin/python3.13\n")),
    ])
    monkeypatch.setattr(stack.installer, "_run", r)
    assert stack.installer.find_python() == "/usr/bin/python3.13"


def test_find_python_ignores_a_non_numeric_version_banner(stack, monkeypatch):
    monkeypatch.setattr(stack.installer, "PY_CANDIDATES", ["python3"])
    monkeypatch.setattr(stack.installer.shutil, "which", lambda n: "/x/" + n)
    r = RunRecorder(default_stdout=b"Python 3.12 (default)\n")
    monkeypatch.setattr(stack.installer, "_run", r)
    assert stack.installer.find_python() is None


def test_find_python_ignores_a_path_with_a_spaced_directory(
        stack, monkeypatch, tmp_path):
    """A Homebrew-ish path must survive both probes unchanged."""
    real = write_exe(tmp_path / "with space" / "python 3.12",
                     '#!/bin/sh\n'
                     'case "$*" in\n'
                     '  *sys.executable*) printf "%s\\n" "$0";;\n'
                     '  *) echo 3.12;;\n'
                     'esac\n')
    monkeypatch.setenv("KLIPPERFORMAC_PYTHON", str(real))
    monkeypatch.setattr(stack.installer, "PY_CANDIDATES", [])
    got = stack.installer.find_python()
    assert got == str(real), got


def test_check_prereqs_reports_missing_git_and_gcc(stack, monkeypatch):
    monkeypatch.setattr(stack.installer.shutil, "which", lambda n: None)
    monkeypatch.setattr(stack.installer, "find_python", lambda: None)
    problems = stack.installer.check_prereqs(strict=False)
    assert len(problems) == 3
    assert any("xcode-select" in p for p in problems)
    assert any("brew install python" in p for p in problems)
    with pytest.raises(SystemExit):
        stack.installer.check_prereqs(strict=True)


def test_check_prereqs_is_quiet_when_the_toolchain_is_there(stack, monkeypatch):
    monkeypatch.setattr(stack.installer.shutil, "which",
                        lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(stack.installer, "find_python", lambda: "/py/3.12")
    assert stack.installer.check_prereqs(strict=False) == []
    assert stack.installer.check_prereqs(strict=True) == []


def test_check_prereqs_non_strict_returns_problems_without_raising(
        stack, monkeypatch):
    monkeypatch.setattr(stack.installer.shutil, "which", lambda n: None)
    monkeypatch.setattr(stack.installer, "find_python", lambda: None)
    assert stack.installer.check_prereqs(strict=False)
    assert len(stack.installer.check_prereqs(strict=False)) == 3


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: find_python() decides 'is this interpreter usable' from the "
    "first probe and then returns the *second* probe's stdout verbatim "
    "(installer.py:65-66). When `python -c 'import sys; print(sys.executable)'` "
    "fails it returns the empty string -- not None -- so check_prereqs() "
    "(which tests `is None`) calls that a usable interpreter and create_venv() "
    "tries to exec '' and dies with a raw PermissionError instead of the "
    "documented 'No suitable Python found' SystemExit."))
def test_interpreter_that_answers_the_version_probe_but_not_the_executable_one(
        stack, monkeypatch, tmp_path):
    lying = write_exe(tmp_path / "lying-python",
                      '#!/bin/sh\n'
                      'case "$*" in\n'
                      '  *sys.executable*) exit 1;;\n'
                      '  *) echo 3.12;;\n'
                      'esac\n')
    monkeypatch.setenv("KLIPPERFORMAC_PYTHON", str(lying))
    monkeypatch.setattr(stack.installer, "PY_CANDIDATES", [])
    assert stack.installer.find_python() is None
    assert stack.installer.check_prereqs(strict=False) != []


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: same root cause -- create_venv() only guards against "
    "`find_python() is None`, so an '' interpreter slips through "
    "(installer.py:144-147) and `python -m venv` is invoked as ['', '-m', "
    "'venv', ...]: with the real subprocess layer that is a PermissionError "
    "traceback, and with any wrapper that swallows it setup continues and "
    "ensure_runtime_shims() is pointed at a venv that was never created. Either "
    "way the documented 'No suitable Python found' SystemExit never fires."))
def test_create_venv_with_an_unresolvable_interpreter_is_a_user_error(
        stack, monkeypatch):
    monkeypatch.setattr(stack.installer, "find_python", lambda: "")
    monkeypatch.setattr(stack.installer, "_run", RunRecorder())
    with pytest.raises(SystemExit):
        stack.installer.create_venv()


# --------------------------------------------------------------------------
# create_venv
# --------------------------------------------------------------------------
def test_create_venv_targets_only_the_configured_app_home(stack, monkeypatch,
                                                           rec):
    monkeypatch.setattr(stack.installer, "find_python", lambda: "/py/3.12")
    monkeypatch.setattr(stack.installer, "ensure_runtime_shims",
                        lambda p: False)
    stack.installer.create_venv()
    venv_calls = rec.find("-m venv")
    assert venv_calls, "venv was never created"
    argv = venv_calls[0]
    assert argv[0] == "/py/3.12"
    assert argv[1:3] == ["-m", "venv"]
    assert argv[3] == str(stack.paths.APP_HOME / "venv")
    assert Path(argv[3]).is_relative_to(stack.paths.APP_HOME)
    assert rec.calls[0][1].get("check") is True


def test_create_venv_is_a_no_op_when_the_venv_python_already_exists(
        stack, monkeypatch, rec):
    write_exe(stack.paths.VENV_PY, '#!/bin/sh\nexit 0\n')
    monkeypatch.setattr(stack.installer, "ensure_runtime_shims",
                        lambda p: True)
    stack.installer.create_venv()
    assert rec.find("-m venv") == [], "an existing venv must not be recreated"


def test_create_venv_without_any_python_raises_system_exit(stack, monkeypatch):
    monkeypatch.setattr(stack.installer, "find_python", lambda: None)
    with pytest.raises(SystemExit) as exc:
        stack.installer.create_venv()
    assert "No suitable Python" in str(exc.value)


def test_create_venv_installs_the_runtime_shims(stack, monkeypatch, rec):
    seen = []
    monkeypatch.setattr(stack.installer, "find_python", lambda: "/py/3.12")
    monkeypatch.setattr(stack.installer, "ensure_runtime_shims",
                        lambda p: seen.append(p))
    stack.installer.create_venv()
    assert seen == [stack.paths.VENV_PY]


def test_create_venv_does_not_run_pip_or_the_network(stack, monkeypatch, rec):
    monkeypatch.setattr(stack.installer, "find_python", lambda: "/py/3.12")
    monkeypatch.setattr(stack.installer, "ensure_runtime_shims", lambda p: False)
    stack.installer.create_venv()
    assert not rec.find("pip")
    assert not rec.find("https://")


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: create_venv() runs `python -m venv` with check=True and no "
    "error handling (installer.py:147), so the ordinary failures -- a full "
    "disk, a read-only home, a venv module that cannot bootstrap -- escape as "
    "subprocess.CalledProcessError and `klipperformac setup` shows a traceback "
    "with no hint about the fix."))
def test_venv_creation_failure_is_a_user_error(stack, monkeypatch):
    monkeypatch.setattr(stack.installer, "find_python", lambda: "/py/3.12")
    r = RunRecorder(default_rc=1, default_stderr=b"ensurepip failed")
    monkeypatch.setattr(stack.installer, "_run", r)
    with pytest.raises(SystemExit):
        stack.installer.create_venv()


# --------------------------------------------------------------------------
# install_requirements
# --------------------------------------------------------------------------
def _mk_req(stack, comp, rel):
    p = stack.installer.DESTS[comp] / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("jinja2\n")
    return p


def test_install_requirements_uses_the_venv_interpreter_not_the_console_script(
        stack, rec):
    _mk_req(stack, "klipper", "scripts/klippy-requirements.txt")
    _mk_req(stack, "moonraker", "scripts/moonraker-requirements.txt")
    stack.installer.install_requirements()
    pip_calls = rec.find("-m pip")
    assert len(pip_calls) == 2
    for argv in pip_calls:
        assert argv[0] == str(stack.paths.VENV_PY)
        assert argv[1:3] == ["-m", "pip"]
        assert "--quiet" in argv and "--upgrade" in argv and "-r" in argv
    assert rec.calls[0][1].get("check") is True


def test_install_requirements_falls_back_to_the_legacy_path(stack, rec):
    _mk_req(stack, "klipper", "klippy/requirements.txt")
    _mk_req(stack, "moonraker", "requirements.txt")
    stack.installer.install_requirements()
    pip_calls = rec.find("-m pip")
    assert len(pip_calls) == 2
    assert any(argv[-1].endswith("klippy/requirements.txt") for argv in pip_calls)
    assert any(argv[-1].endswith("/moonraker/requirements.txt")
               for argv in pip_calls)


def test_install_requirements_installs_each_component_only_once(stack, rec):
    """Both candidate paths exist -> exactly one pip run per component."""
    _mk_req(stack, "klipper", "scripts/klippy-requirements.txt")
    _mk_req(stack, "klipper", "klippy/requirements.txt")
    _mk_req(stack, "moonraker", "scripts/moonraker-requirements.txt")
    _mk_req(stack, "moonraker", "requirements.txt")
    stack.installer.install_requirements()
    assert len(rec.find("-m pip")) == 2


def test_install_requirements_never_reaches_the_network_directly(stack, rec):
    """pip is delegated to; installer.py itself must not open a URL here."""
    _mk_req(stack, "klipper", "scripts/klippy-requirements.txt")
    _mk_req(stack, "moonraker", "scripts/moonraker-requirements.txt")
    stack.installer.install_requirements()
    assert rec.find("-m pip")
    assert not any("https://" in " ".join(cmd) for cmd, _ in rec.calls)
    assert not any("urlopen" in " ".join(cmd) for cmd, _ in rec.calls)


def test_install_requirements_without_a_requirements_file(stack, rec):
    with pytest.raises(SystemExit) as exc:
        stack.installer.install_requirements()
    assert "requirements file" in str(exc.value)


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: install_requirements() lets pip's CalledProcessError escape "
    "(installer.py:164-167). A resolver conflict aborts setup with a raw "
    "traceback *after* the ref markers were written and the venv created, so "
    "the half-installed state looks complete to the next `doctor` run."))
def test_pip_failure_is_a_user_error(stack, monkeypatch):
    _mk_req(stack, "klipper", "scripts/klippy-requirements.txt")
    _mk_req(stack, "moonraker", "scripts/moonraker-requirements.txt")
    r = RunRecorder(default_rc=1, default_stderr=b"ERROR: resolution impossible")
    monkeypatch.setattr(stack.installer, "_run", r)
    with pytest.raises(SystemExit):
        stack.installer.install_requirements()


# --------------------------------------------------------------------------
# test_libsodium / test_chelper_build
# --------------------------------------------------------------------------
def test_libsodium_missing_is_a_runtime_error_with_the_brew_hint(stack, rec):
    rec.script = [("import libnacl", cp(1, b"", b"ModuleNotFoundError"))]
    with pytest.raises(RuntimeError) as exc:
        stack.installer.test_libsodium()
    assert "brew install libsodium" in str(exc.value)


def test_libsodium_present_is_silent(stack, rec):
    rec.script = [("import libnacl", cp(0))]
    assert stack.installer.test_libsodium() is None


def test_libsodium_kills_a_missing_venv_python_quietly_or_loudly(stack,
                                                                  monkeypatch):
    """Documented behaviour check: no venv -> RuntimeError, not SystemExit."""
    r = RunRecorder(default_rc=127, default_stderr=b"no such file")
    monkeypatch.setattr(stack.installer, "_run", r)
    with pytest.raises(RuntimeError):
        stack.installer.test_libsodium()


def test_chelper_build_failure_reports_the_stderr_tail(stack, rec, monkeypatch):
    monkeypatch.setattr(stack.installer, "invalidate_stale_build",
                        lambda d: False)
    stderr = b"noise " * 4000 + b"\nclang: error: you need a shim\n"
    rec.script = [("import chelper", cp(2, b"", stderr))]
    with pytest.raises(RuntimeError) as exc:
        stack.installer.test_chelper_build()
    msg = str(exc.value)
    assert "c_helper.so build FAILED" in msg
    assert msg.endswith("clang: error: you need a shim\n")
    assert len(msg) < 2000, "40KB of build noise must not be dumped"


def test_chelper_build_uses_the_compat_cpath_env(stack, rec, monkeypatch):
    monkeypatch.setattr(stack.installer, "invalidate_stale_build",
                        lambda d: False)
    stack.installer.test_chelper_build()
    argv, kw = rec.calls[-1]
    assert argv[0] == str(stack.paths.VENV_PY)
    assert "chelper" in " ".join(argv)
    assert str(compat_mod.INCLUDE) in kw["env"]["CPATH"]
    assert kw["env"]["CPATH"].startswith(str(compat_mod.INCLUDE))


def test_chelper_build_builds_from_the_configured_klipper_checkout(
        stack, rec, monkeypatch):
    monkeypatch.setattr(stack.installer, "invalidate_stale_build",
                        lambda d: False)
    stack.installer.test_chelper_build()
    script = rec.argv()[-1]
    assert str(stack.paths.KLIPPER / "klippy") in script
    assert "sys.path.insert" in script


def test_chelper_build_forces_a_rebuild_of_the_stale_so(stack, monkeypatch):
    seen = []
    monkeypatch.setattr(stack.installer, "invalidate_stale_build",
                        lambda d: seen.append(d) or True)
    monkeypatch.setattr(stack.installer, "_run", RunRecorder())
    stack.installer.test_chelper_build()
    assert seen == [stack.paths.KLIPPER]


# --------------------------------------------------------------------------
# checkout
# --------------------------------------------------------------------------
def test_checkout_refuses_to_replace_a_symlinked_component(stack, rec, tmp_path):
    dest = stack.installer.DESTS["klipper"]
    victim = tmp_path / "innocent-real-dir"
    victim.mkdir()
    (victim / "keepme.txt").write_text("precious")
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(str(victim), str(dest))
    with pytest.raises(SystemExit) as exc:
        stack.installer.checkout("klipper", SHA)
    assert "symlink" in str(exc.value)
    assert (victim / "keepme.txt").read_text() == "precious"
    assert rec.calls == [], "no git may run against a symlinked destination"


def test_checkout_skips_git_when_the_marker_matches(stack, rec):
    dest = stack.installer.DESTS["klipper"]
    dest.mkdir(parents=True)
    stack.installer._ref_marker("klipper").write_text(SHA + "\n")
    rec.default_stdout = (SHA + "\n").encode()
    assert stack.installer.checkout("klipper", SHA) == SHA
    assert rec.calls == [(["git", "-C", str(dest), "rev-parse", "HEAD"],
                          {"capture_output": True})]


def test_checkout_refetches_when_the_marker_exists_but_the_dir_is_gone(
        stack, rec):
    """A deleted checkout with a stale marker must be restored, not trusted."""
    stack.installer._ref_marker("klipper").write_text(SHA + "\n")
    rec.default_stdout = (SHA + "\n").encode()
    stack.installer.checkout("klipper", SHA)
    assert rec.find("fetch --quiet --depth 1 origin " + SHA)


def test_checkout_refetches_when_the_pin_moved(stack, rec):
    dest = stack.installer.DESTS["klipper"]
    dest.mkdir(parents=True)
    (dest / "old.py").write_text("previous pin\n")
    stack.installer._ref_marker("klipper").write_text(SHA + "\n")
    rec.default_stdout = (SHA + "\n").encode()
    stack.installer.checkout("klipper", "0" * 40)
    assert rec.find("fetch"), "a moved pin must be re-fetched"
    assert not (dest / "old.py").exists(), "the stale tree must be replaced"


def test_checkout_force_redoes_the_clone(stack, rec):
    dest = stack.installer.DESTS["klipper"]
    dest.mkdir(parents=True)
    stack.installer._ref_marker("klipper").write_text(SHA + "\n")
    rec.default_stdout = (SHA + "\n").encode()
    stack.installer.checkout("klipper", SHA, force=True)
    assert rec.find("fetch --quiet --depth 1 origin " + SHA)
    assert rec.find("clone") == [], "a raw SHA must never use --branch"


def test_checkout_branch_path_for_a_tag_ref(stack, rec):
    rec.default_stdout = b"deadbeef\n"
    sha = stack.installer.checkout("moonraker", "v0.11.0")
    joined = [rec.joined(i) for i in range(len(rec.calls))]
    assert any("clone --quiet --depth 1 --branch v0.11.0" in j for j in joined)
    assert all("fetch" not in j for j in joined)
    assert sha == "deadbeef"
    assert stack.installer._ref_marker("moonraker").read_text() == "v0.11.0\n"


def test_checkout_writes_the_marker_only_after_a_successful_fetch(stack, rec):
    marker = stack.installer._ref_marker("klipper")
    rec.default_stdout = b"deadbeef\n"
    stack.installer.checkout("klipper", "v1.2.3")
    assert marker.read_text() == "v1.2.3\n"


def test_checkout_never_passes_a_shell_or_a_destination_outside_app_home(
        stack, rec):
    rec.default_stdout = b"beef\n"
    stack.installer.checkout("klipper", SHA)
    for argv, kw in rec.calls:
        assert kw.get("shell") is not True
        assert str(stack.paths.APP_HOME) in " ".join(argv), argv
    assert str(Path.home() / ".klipperformac") not in " ".join(
        " ".join(c) for c, _ in rec.calls)


def test_checkout_rejects_an_unknown_component(stack, rec):
    with pytest.raises(KeyError):
        stack.installer.checkout("bambu", "v1")
    assert rec.calls == []


@pytest.mark.parametrize("ref", [
    pytest.param(SHA.upper(), id="uppercase-sha"),
    pytest.param("0" * 39 + "A", id="mixed-case-sha"),
])
@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: checkout() recognises the raw-SHA fast path with "
    "re.fullmatch(r'[0-9a-f]{40}', ref) (installer.py:111), so a valid but "
    "upper-case commit id -- the way GitHub's UI prints it and the way a user "
    "copies it into lockfile.json -- is treated as a branch name: "
    "`git clone --branch 461C4E...` fails and, because the old checkout was "
    "already rmtree'd, setup() destroys a working install."))
def test_full_commit_sha_is_recognised_case_insensitively(stack, rec, ref):
    stack.installer.checkout("klipper", ref)
    assert rec.find("clone") == [], "an upper-case SHA must not use --branch"
    assert rec.find("fetch --quiet --depth 1 origin " + ref.lower())


@pytest.mark.parametrize("ref", [
    pytest.param(None, id="json-null"),
    pytest.param(42, id="json-number"),
    pytest.param(["v1"], id="json-list"),
    pytest.param({"tag": "v1"}, id="json-object"),
    pytest.param(True, id="json-bool"),
])
@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: checkout() takes the pin straight out of lockfile.json with no "
    "type check; a non-string value raises TypeError inside re.fullmatch "
    "(installer.py:111) -- after the pristine checkout has been deleted -- so "
    "`klipperformac setup`/`update --apply` crashes with a traceback and, per "
    "the rmtree bug below, leaves no Klipper at all."))
def test_non_string_pin_is_rejected_as_a_user_error(stack, rec, ref):
    with pytest.raises(SystemExit):
        stack.installer.checkout("klipper", ref)
    assert rec.calls == []


@pytest.mark.parametrize("ref", [
    pytest.param("", id="empty-string"),
    pytest.param("   ", id="whitespace-only"),
    pytest.param("\n", id="newline-only"),
])
@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: a blank ref in lockfile.json is handed to git as `--branch ''` "
    "(installer.py:120) and the destination is deleted before the ref is "
    "validated, so a bogus pin costs the user their working pristine checkout. "
    "The ref must be validated (non-empty, no control characters, a legal git "
    "refname) before anything is removed."))
def test_blank_pin_is_rejected_before_any_destructive_step(stack, rec, ref):
    dest = stack.installer.DESTS["klipper"]
    dest.mkdir(parents=True)
    (dest / "keepme.py").write_text("essential")
    with pytest.raises(SystemExit):
        stack.installer.checkout("klipper", ref)
    assert rec.calls == []
    assert (dest / "keepme.py").exists()


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: checkout() deletes the existing component checkout up front "
    "(installer.py:108-109) and only then tries to re-create it, with no "
    "rollback. Any fetch failure -- airplane mode, a yanked tag, GitHub "
    "unreachable, git exiting non-zero -- leaves ~/.klipperformac/klipper "
    "gone: a user who ran `update --apply` without network loses a printer "
    "that worked, and setup() then aborts on a raw CalledProcessError."))
def test_a_failed_fetch_must_not_destroy_the_existing_checkout(
        stack, monkeypatch):
    dest = stack.installer.DESTS["klipper"]
    dest.mkdir(parents=True)
    (dest / "keepme.py").write_text("essential")
    stack.installer._ref_marker("klipper").write_text("v1.2.2\n")
    seen = []

    def failing(cmd, **kw):
        argv = [str(c) for c in cmd]
        seen.append(" ".join(argv))
        if "clone" in argv or "fetch" in argv:
            raise subprocess.CalledProcessError(
                128, argv, b"", b"fatal: unable to access github.com")
        return cp(0, b"deadbeef\n")
    monkeypatch.setattr(stack.installer, "_run", failing)
    with pytest.raises(SystemExit):
        stack.installer.checkout("klipper", "v1.2.3")
    assert (dest / "keepme.py").exists(), "working checkout was destroyed"


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: checkout() returns whatever `git rev-parse HEAD` printed and "
    "_head_sha() swallows every failure into '' (installer.py:77-82, 124). "
    "setup() then persists klipper_sha='' into lockfile.json and prints "
    "'klipper @ <ref> ()' as if the install were verified, and `update` can no "
    "longer tell which commit is installed."))
def test_checkout_must_report_a_real_sha_or_fail(stack, rec):
    rec.script = [("rev-parse HEAD", cp(128, b"", b"fatal: bad revision"))]
    sha = stack.installer.checkout("klipper", "v1.2.3")
    assert sha and len(sha) == 40


def test_head_sha_of_a_missing_directory_is_empty_not_a_crash(stack):
    assert stack.installer._head_sha(stack.paths.APP_HOME / "nope") == ""


@needs_git
def test_head_sha_of_a_real_repo_matches_git(stack):
    repo = stack.paths.APP_HOME / "sha-probe"
    init_repo(repo)
    real = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, check=True)
    assert stack.installer._head_sha(repo) == real.stdout.decode().strip()


# --------------------------------------------------------------------------
# verify_pristine -- the project's core attestation
# --------------------------------------------------------------------------
@needs_git
def test_verify_pristine_detects_a_modified_tracked_file(stack):
    repo = init_repo(stack.installer.DESTS["klipper"])
    assert stack.installer.verify_pristine() == []
    (repo / "README").write_text("a = 2  # hand-edited upstream source\n")
    bad = stack.installer.verify_pristine()
    assert [c for c, _ in bad] == ["klipper"]
    assert "README" in bad[0][1]


@needs_git
def test_verify_pristine_reports_every_component(stack):
    for comp in ("klipper", "moonraker"):
        repo = init_repo(stack.installer.DESTS[comp])
        (repo / "README").write_text("tampered\n")
    assert sorted(c for c, _ in stack.installer.verify_pristine()) == [
        "klipper", "moonraker"]


@needs_git
def test_verify_pristine_ignores_a_component_with_no_checkout(stack):
    """No checkout yet is 'nothing to verify', and must not crash."""
    assert stack.installer.verify_pristine() == []


@needs_git
@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: verify_pristine() ignores git's exit status "
    "(`_run([...], capture_output=True).stdout` with no check, "
    "installer.py:135-138). When git fails -- 'detected dubious ownership', a "
    "corrupted index, a spawn error -- stdout is empty and the component is "
    "reported PRISTINE. `klipperformac doctor` then prints '[ok] upstream "
    "checkouts pristine' and setup() prints 'verified pristine' for a checkout "
    "it never inspected, which defeats the whole promise of this project."))
def test_verify_pristine_must_fail_loudly_when_git_errors(stack):
    dest = stack.installer.DESTS["klipper"]
    (dest / ".git").mkdir(parents=True)      # exists, but is not a repository
    bad = stack.installer.verify_pristine()
    assert bad and bad[0][0] == "klipper", (
        "a checkout git cannot inspect must not be reported pristine")


@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: verify_pristine(component) silently returns [] for a component "
    "name it does not know (installer.py:129-134), so a typo -- or a component "
    "renamed in a future release -- is indistinguishable from 'everything is "
    "pristine'."))
def test_verify_pristine_rejects_an_unknown_component(stack):
    with pytest.raises((SystemExit, KeyError, ValueError)):
        stack.installer.verify_pristine("klpiper")


# --------------------------------------------------------------------------
# _exclude_build_artifacts
# --------------------------------------------------------------------------
@needs_git
def test_exclude_build_artifacts_adds_every_pattern_once(stack):
    dest = init_repo(stack.installer.DESTS["klipper"])
    stack.installer._exclude_build_artifacts(dest)
    excl = dest / ".git" / "info" / "exclude"
    text = excl.read_text()
    for pattern in stack.installer.GIT_EXCLUDE:
        assert pattern in text.splitlines(), pattern
    stack.installer._exclude_build_artifacts(dest)
    assert excl.read_text() == text, "second call must be a no-op"


@needs_git
def test_untracked_c_helper_is_invisible_to_git_status(stack):
    """The whole reason .git/info/exclude exists: keep `git status` clean."""
    dest = init_repo(stack.installer.DESTS["klipper"])
    stack.installer._exclude_build_artifacts(dest)
    (dest / "klippy" / "chelper").mkdir(parents=True)
    (dest / "klippy" / "chelper" / "c_helper.so").write_bytes(b"\x7fELF")
    (dest / "klippy.log").write_text("log noise")
    (dest / "klippy" / "__pycache__").mkdir()
    (dest / "klippy" / "__pycache__" / "x.pyc").write_bytes(b"py")
    assert stack.installer.verify_pristine() == []


@needs_git
@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: _exclude_build_artifacts() decides what is 'already there' with "
    "a raw substring test (`p not in existing`, installer.py:92) and appends "
    "with open(..., 'a'). A .git/info/exclude whose last line has no trailing "
    "newline -- a hand-edited file, or one delivered inside an archive -- "
    "glues the first pattern onto that line, so 'c_helper.so' is never "
    "excluded, `git status --porcelain` turns dirty, and setup/doctor accuse a "
    "perfectly pristine upstream checkout of being modified."))
def test_patterns_survive_a_no_trailing_newline_exclude_file(stack):
    dest = init_repo(stack.installer.DESTS["klipper"])
    excl = dest / ".git" / "info" / "exclude"
    excl.write_text("# local notes")             # no trailing newline
    stack.installer._exclude_build_artifacts(dest)
    lines = excl.read_text().splitlines()
    for pattern in stack.installer.GIT_EXCLUDE:
        assert pattern in lines, "glued into one line: %r" % lines
    (dest / "klippy.log").write_text("noise")
    assert stack.installer.verify_pristine() == []


@needs_git
@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: the substring presence test in _exclude_build_artifacts() "
    "(installer.py:92) cannot tell a live pattern from a comment that merely "
    "mentions it ('# do not commit klippy.log'), so the pattern is never "
    "appended, the artifact shows up in `git status --porcelain`, and setup/"
    "doctor report a pristine checkout as modified."))
def test_commented_mention_does_not_suppress_the_pattern(stack):
    dest = init_repo(stack.installer.DESTS["klipper"])
    excl = dest / ".git" / "info" / "exclude"
    excl.write_text("# do not commit klippy.log or __pycache__/ here\n")
    stack.installer._exclude_build_artifacts(dest)
    lines = excl.read_text().splitlines()
    assert "klippy.log" in lines and "__pycache__/" in lines
    (dest / "klippy.log").write_text("noise")
    assert stack.installer.verify_pristine() == []


@needs_git
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_exclude_build_artifacts_never_raises_on_a_locked_tree(stack):
    """setup() must not die on this cosmetic step."""
    dest = init_repo(stack.installer.DESTS["klipper"])
    excl = dest / ".git" / "info" / "exclude"
    os.chmod(str(excl), 0o000)
    os.chmod(str(excl.parent), 0o500)
    try:
        stack.installer._exclude_build_artifacts(dest)
    finally:
        os.chmod(str(excl.parent), 0o755)
        os.chmod(str(excl), 0o644)
    assert "c_helper.so" not in excl.read_text()


def test_exclude_build_artifacts_on_a_path_named_like_the_file(stack, tmp_path):
    """A directory where the exclude file should be must not crash setup."""
    repo = tmp_path / "weird"
    (repo / ".git" / "info" / "exclude").mkdir(parents=True)
    stack.installer._exclude_build_artifacts(repo)
    assert (repo / ".git" / "info" / "exclude").is_dir()


def test_exclude_build_artifacts_without_any_git_dir(stack, tmp_path):
    repo = tmp_path / "bare"
    repo.mkdir()
    stack.installer._exclude_build_artifacts(repo)
    assert (repo / ".git" / "info" / "exclude").is_file()


# --------------------------------------------------------------------------
# tripwire: every test here must run against the throwaway home
# --------------------------------------------------------------------------
def test_installer_module_constants_are_bound_to_the_throwaway_home(stack):
    assert stack.paths.APP_HOME == Path(os.environ["KLIPPERFORMAC_HOME"])
    assert stack.installer.REFS.is_relative_to(stack.paths.APP_HOME)
    for comp, dest in stack.installer.DESTS.items():
        assert dest.is_relative_to(stack.paths.APP_HOME), comp
    assert stack.paths.VENV_PY.is_relative_to(stack.paths.APP_HOME)
    live = Path(os.path.expanduser("~/.klipperformac"))
    assert stack.paths.APP_HOME != live


def test_setup_is_never_run_for_real_by_this_file(stack):
    """setup() would run git, pip and real downloads; the harness forbids it
    and the pieces are covered with fakes instead."""
    with pytest.raises(AssertionError, match="setup"):
        stack.installer.setup()


@needs_git
@pytest.mark.xfail(strict=True, reason=(
    "BUG DEEP: checkout()'s symlink guard only runs on the re-clone path "
    "(installer.py:106-107), but the early-return above it tests `dest.exists()`, "
    "which follows the link (installer.py:103-105). A ~/.klipperformac/klipper "
    "that is a symlink to a tampered tree therefore passes the 'already at the "
    "pinned ref' check: setup() skips the fetch, _head_sha() reads the symlinked "
    "repo, and verify_pristine() inspects it too -- but the tree klippy runs is "
    "not the one install.sh believes it created. The guard must apply to the "
    "cache hit as well."))
def test_a_symlinked_checkout_is_refused_even_when_the_marker_matches(
        stack, rec, tmp_path):
    victim = tmp_path / "planted"
    init_repo(victim)
    dest = stack.installer.DESTS["klipper"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(str(victim), str(dest))
    marker = stack.installer._ref_marker("klipper")
    marker.write_text(SHA + "\n")
    rec.default_stdout = (SHA + "\n").encode()
    with pytest.raises(SystemExit):
        stack.installer.checkout("klipper", SHA)
    assert victim.is_dir()
