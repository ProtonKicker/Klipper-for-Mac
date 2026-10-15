"""Deep tests: `klipperformac selftest` and `klipperformac logs` failure paths.

Covered (the parts the first suite missed):
  * cmd_selftest when the venv interpreter is missing / not executable / a
    directory, and when the selftest child process fails.
  * cmd_logs when the log file is missing, when the service name is hostile,
    when the tail invocation fails, and what argv tail actually receives.

Everything runs against the throwaway home+data from the `stack` fixture.
No live stack, no network, no ~/.klipperformac, no /dev/cu.*.

The bugs this battery found are fixed in ``klipperformac/cli.py``; the tests
that encoded them (``xfail(strict=True)``, "BUG DEEP: ...") keep their
assertions and now pass.
"""
import argparse
import os

import pytest


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
class FakeSubprocess:
    """Stands in for the `subprocess` module attribute inside cli.py."""

    def __init__(self, returncode=0):
        self.returncode = returncode
        self.calls = []

    def call(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs))
        return self.returncode

    # cmd_logs only uses call(); anything else would be a surprise.
    def __getattr__(self, name):
        raise AssertionError(
            "cli touched subprocess.%s (cmd_logs must only use call())" % name)


def write_exe(path, body, mode=0o755):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    os.chmod(str(path), mode)
    return path


def log_args(**kw):
    base = dict(service="klipper", lines=40, follow=False)
    base.update(kw)
    return argparse.Namespace(**base)


# --------------------------------------------------------------------------
# cmd_selftest: the interpreter is not there / not runnable
# --------------------------------------------------------------------------
def test_selftest_missing_venv_python_is_a_clean_error(stack, capfd):
    """`klipperformac selftest` before setup must explain, not crash."""
    assert not stack.paths.VENV_PY.exists()
    with pytest.raises(SystemExit) as exc:
        stack.cli.cmd_selftest(argparse.Namespace())
    assert exc.value.code != 0
    assert "Traceback" not in capfd.readouterr().err


def test_selftest_nonexecutable_venv_python_is_a_clean_error(stack):
    write_exe(stack.paths.VENV_PY, "#!/bin/sh\nexit 0\n", mode=0o644)
    assert not os.access(str(stack.paths.VENV_PY), os.X_OK)
    with pytest.raises(SystemExit) as exc:
        stack.cli.cmd_selftest(argparse.Namespace())
    assert exc.value.code != 0


def test_selftest_venv_python_is_a_directory_clean_error(stack):
    stack.paths.VENV_PY.mkdir(parents=True)
    with pytest.raises(SystemExit) as exc:
        stack.cli.cmd_selftest(argparse.Namespace())
    assert exc.value.code != 0


def test_selftest_runs_the_bundled_module_with_repo_on_path(stack, monkeypatch):
    """Good behaviour: the child is `python -m klipperformac.tests.selftest`
    with PYTHONPATH set to the repo root and the sandbox env forwarded."""
    fake = FakeSubprocess(returncode=0)
    monkeypatch.setattr(stack.cli, "subprocess", fake)
    stack.cli.cmd_selftest(argparse.Namespace())
    assert len(fake.calls) == 1
    cmd, kwargs = fake.calls[0]
    assert cmd == [str(stack.paths.VENV_PY), "-m", "klipperformac.tests.selftest"]
    repo_root = os.path.dirname(os.path.dirname(
        os.path.abspath(stack.cli.__file__)))
    assert kwargs["env"]["PYTHONPATH"] == repo_root
    # the caller's environment (throwaway KLIPPERFORMAC_HOME/DATA) must be kept
    assert kwargs["env"]["KLIPPERFORMAC_HOME"] == str(stack.home)


def test_selftest_success_does_not_raise(stack):
    """A green child process must not make the CLI report failure."""
    write_exe(stack.paths.VENV_PY, "#!/bin/sh\necho selftest ok\nexit 0\n")
    stack.cli.cmd_selftest(argparse.Namespace())


def test_selftest_propagates_child_failure(stack):
    write_exe(stack.paths.VENV_PY,
              "#!/bin/sh\necho selftest failing >&2\nexit 1\n")
    with pytest.raises(SystemExit) as exc:
        stack.cli.cmd_selftest(argparse.Namespace())
    assert exc.value.code != 0


def test_selftest_propagates_child_signal(stack):
    write_exe(stack.paths.VENV_PY, "#!/bin/sh\nkill -9 $$\n")
    with pytest.raises(SystemExit) as exc:
        stack.cli.cmd_selftest(argparse.Namespace())
    assert exc.value.code != 0


def test_selftest_child_env_points_only_at_the_sandbox(stack, monkeypatch):
    """The selftest child inherits our env: it must be handed the throwaway
    home/data and never a path that could reach the live stack's dirs."""
    fake = FakeSubprocess(returncode=0)
    monkeypatch.setattr(stack.cli, "subprocess", fake)
    stack.cli.cmd_selftest(argparse.Namespace())
    (_, kwargs), = fake.calls
    env = kwargs["env"]
    assert env["KLIPPERFORMAC_HOME"] == str(stack.home)
    assert env["KLIPPERFORMAC_DATA"] == str(stack.data)
    assert "/.klipperformac" not in env["KLIPPERFORMAC_HOME"]
    assert stack.paths.APP_HOME == stack.home


# --------------------------------------------------------------------------
# cmd_logs: missing logs, hostile inputs, exit status
# --------------------------------------------------------------------------
def test_logs_missing_klipper_log_is_clean(stack, capfd):
    with pytest.raises(SystemExit) as exc:
        stack.cli.cmd_logs(log_args(service="klipper"))
    assert exc.value.code == 1
    err = capfd.readouterr().err
    assert err.startswith("No log yet: ")
    assert str(stack.paths.KLIPPY_LOG) in err
    assert "Traceback" not in err


def test_logs_missing_moonraker_log_is_clean(stack, capfd):
    with pytest.raises(SystemExit) as exc:
        stack.cli.cmd_logs(log_args(service="moonraker"))
    assert exc.value.code == 1
    assert str(stack.paths.MOONRAKER_LOG) in capfd.readouterr().err


def test_logs_service_name_cannot_escape_the_logs_dir(stack, capfd, tmp_path):
    """Only the two known services resolve; a traversal-looking name must never
    read an arbitrary file."""
    elsewhere = tmp_path / "secret.log"
    elsewhere.write_text("TOP-SECRET\n")
    with pytest.raises(SystemExit) as exc:
        stack.cli.cmd_logs(log_args(service=".."))
    assert exc.value.code == 1
    out = capfd.readouterr()
    assert "TOP-SECRET" not in out.out + out.err
    assert str(stack.paths.MOONRAKER_LOG) in out.err


def test_logs_when_log_is_a_directory_is_clean(stack, monkeypatch):
    """A directory where the log belongs is not 'a log': must not exec tail."""
    stack.paths.LOGS.mkdir(parents=True, exist_ok=True)
    stack.paths.KLIPPY_LOG.mkdir(parents=True, exist_ok=True)
    fake = FakeSubprocess(returncode=1)
    monkeypatch.setattr(stack.cli, "subprocess", fake)
    with pytest.raises(SystemExit) as exc:
        stack.cli.cmd_logs(log_args())
    assert fake.calls == []
    assert exc.value.code != 0


def test_logs_missing_log_never_spawns_a_child(stack, monkeypatch):
    """Guardrail for the ordering: the missing-file check must run before any
    subprocess is spawned."""
    fake = FakeSubprocess(returncode=0)
    monkeypatch.setattr(stack.cli, "subprocess", fake)
    with pytest.raises(SystemExit):
        stack.cli.cmd_logs(log_args(service="moonraker"))
    assert fake.calls == []


def test_logs_propagates_tail_failure(stack, monkeypatch):
    stack.paths.LOGS.mkdir(parents=True, exist_ok=True)
    stack.paths.KLIPPY_LOG.write_text("hello\n")
    monkeypatch.setattr(stack.cli, "subprocess", FakeSubprocess(returncode=1))
    with pytest.raises(SystemExit) as exc:
        stack.cli.cmd_logs(log_args())
    assert exc.value.code != 0


def test_logs_tail_argv_is_a_shell_free_list(stack, monkeypatch):
    """Hostile data-folder names (quotes/spaces/$/backticks) must reach tail as
    one argv element and never through a shell."""
    nasty = "na'sty \"dir\" $HOME `id` ;rm\\t;/"
    stack.paths.set_data(str(stack.data / nasty))
    stack.paths.LOGS.mkdir(parents=True, exist_ok=True)
    stack.paths.KLIPPY_LOG.write_text("one\ntwo\n")
    fake = FakeSubprocess(returncode=0)
    monkeypatch.setattr(stack.cli, "subprocess", fake)
    stack.cli.cmd_logs(log_args(lines=7))
    (cmd, kwargs), = fake.calls
    assert kwargs == {}
    assert cmd[0] == "tail"
    assert cmd[1] == "-n" and cmd[2] == "7"
    assert cmd[3] == str(stack.paths.KLIPPY_LOG)
    assert len(cmd) == 4
    assert "shell" not in kwargs and not any(" " == c for c in cmd)


def test_logs_follow_adds_f_without_blocking_the_cli(stack, monkeypatch):
    fake = FakeSubprocess(returncode=0)
    monkeypatch.setattr(stack.cli, "subprocess", fake)
    stack.paths.LOGS.mkdir(parents=True, exist_ok=True)
    stack.paths.KLIPPY_LOG.write_text("x\n")
    stack.cli.cmd_logs(log_args(follow=True))
    (cmd, _), = fake.calls
    assert cmd[:3] == ["tail", "-f", "-n"]


def test_logs_default_service_is_klipper(stack, monkeypatch):
    """`klipperformac logs` with no service argument must tail klippy.log.

    The default lives in the argparse subparser (`default="klipper"`), not in
    cmd_logs, so the namespace has to come from the real parser: cmd_logs never
    sees service=None in the product (the TUI passes an explicit service too).
    """
    fake = FakeSubprocess(returncode=0)
    monkeypatch.setattr(stack.cli, "subprocess", fake)
    stack.paths.LOGS.mkdir(parents=True, exist_ok=True)
    stack.paths.KLIPPY_LOG.write_text("klippy line\n")
    stack.paths.MOONRAKER_LOG.write_text("moonraker line\n")
    stack.cli.main(["logs"])
    (cmd, _), = fake.calls
    assert cmd[-1] == str(stack.paths.KLIPPY_LOG)
    assert cmd[-1] != str(stack.paths.MOONRAKER_LOG)


def test_logs_negative_line_count_does_not_run_unbounded(stack, monkeypatch):
    """A negative count is nonsense: reject it, or hand it to tail — but never
    accept it silently as 'show everything'. Here we only pin that the CLI does
    not crash with a Python traceback."""
    fake = FakeSubprocess(returncode=0)
    monkeypatch.setattr(stack.cli, "subprocess", fake)
    stack.paths.LOGS.mkdir(parents=True, exist_ok=True)
    stack.paths.KLIPPY_LOG.write_text("x\n")
    try:
        stack.cli.cmd_logs(log_args(lines=-1))
    except SystemExit as exc:            # a clean rejection is fine
        assert exc.value.code != 0
    else:
        assert len(fake.calls) <= 1      # ...and so is delegating to tail


def test_selftest_module_target_exists(stack, monkeypatch):
    """`-m klipperformac.tests.selftest` must resolve inside the repo root the
    CLI puts on PYTHONPATH (guards against a rename of the test module).

    The root under test is the one cmd_selftest actually hands to the child,
    so derive it from that argv/env instead of re-counting dirname()s here.
    """
    fake = FakeSubprocess(returncode=0)
    monkeypatch.setattr(stack.cli, "subprocess", fake)
    stack.cli.cmd_selftest(argparse.Namespace())
    (cmd, kwargs), = fake.calls
    assert cmd[2] == "klipperformac.tests.selftest"
    repo_root = kwargs["env"]["PYTHONPATH"]
    target = os.path.join(repo_root, "klipperformac", "tests", "selftest.py")
    assert os.path.isfile(target), target


def test_selftest_never_opens_a_socket_in_the_cli_process(stack, monkeypatch):
    """The connection checks belong to the child process. The CLI itself must
    not touch any port (the live stack owns 7125/8080/8081)."""
    import socket as socket_module

    def guarded(*args, **kwargs):
        raise AssertionError("cmd_selftest opened a socket in the CLI process")

    monkeypatch.setattr(socket_module, "socket", guarded)
    monkeypatch.setattr(stack.cli, "subprocess", FakeSubprocess(returncode=0))
    stack.cli.cmd_selftest(argparse.Namespace())

