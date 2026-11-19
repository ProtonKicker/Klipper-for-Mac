"""process.py's two marker calls: the seam where the feature becomes true.

The unclean-stop verdict is only honest if the LIFECYCLE writes the markers, and
that wiring is exactly what a future refactor silently drops - `powerloss` works,
`status` works, and every crash reads as "no record". So these tests run the real
`stop()` / `kill_all()` / `start()` with pids, `ps` and signals faked, and assert
the files powerloss then reports.

They also pin the ordering that makes the verdict truthful: process.stop() hands
in the liveness it read BEFORE signalling anything (asking afterwards would find
every service DOWN and turn every graceful `down` into a reported crash).
"""
import json

import pytest
from conftest import BOOT, write_state

from klipperformac import process

MARKERS = process.MARKERS


@pytest.fixture
def proc(pl, monkeypatch):
    """The real process module on the throwaway install, with sysctl pinned and
    no external command: no ps, no lsof, no signal, no sleep."""
    monkeypatch.setattr(process, "_boot_time", lambda: BOOT)
    monkeypatch.setattr(pl.powerloss, "_boot_time", lambda: BOOT)
    monkeypatch.setattr(process, "_scan_service_pids",
                        lambda with_caffeinate=False: [])
    monkeypatch.setattr(process, "reap_stale_caffeinate", lambda: [])
    monkeypatch.setattr(process, "_listener_pids", lambda port: [])
    monkeypatch.setattr(process.time, "sleep", lambda _s: None)
    return pl


def fake_ps(monkeypatch, recorded):
    """Answer `_live_cmds` from a table built off the service names (state.json
    also carries boot_time/started, which are not processes), and forget a pid the
    moment it is signalled - which is what lets stop() converge without a real
    process table."""
    commands = {str(recorded[name]): MARKERS[name] + " --real"
                for name in process.SERVICES if recorded.get(name)}
    gone = set()

    def _live_cmds(pids):
        return {str(pid): commands[str(pid)] for pid in pids
                if str(pid) in commands and str(pid) not in gone}

    sent = []

    def _signal(pid, sig):
        sent.append((pid, sig))
        gone.add(str(pid))
        return True

    monkeypatch.setattr(process, "_live_cmds", _live_cmds)
    monkeypatch.setattr(process, "_signal", _signal)
    return sent


# ------------------------------------------------------------------- down -----
def test_down_on_a_live_stack_leaves_a_clean_marker(proc, monkeypatch):
    pids = write_state(proc)
    fake_ps(monkeypatch, pids)
    assert process.powerloss.note_session_start() is True
    assert process.stop(timeout=1.0) is True
    verdict = process.powerloss.last_stop(stack_up=False)
    assert verdict["state"] == "clean"
    assert verdict["reason"] == "down"
    assert not proc.paths.STATE.exists()          # the stack record is cleared
    assert (proc.paths.POWERLOSS / "stop.json").exists()
    assert not (proc.paths.POWERLOSS / "session.json").exists()


def test_down_after_klipper_died_is_recorded_unclean(proc, monkeypatch):
    """The half-dead case facts B4 warns about: state.json still names a klipper
    that is gone. A marker that said "clean" here would erase the crash."""
    pids = write_state(proc)
    sent = fake_ps(monkeypatch, pids)
    process.powerloss.note_session_start()
    # klipper is already dead before `down` runs: ps does not list it.
    gone = {str(pids["klipper"])}
    live = process._live_cmds

    def _klipper_already_dead(values):
        return {key: value for key, value in live(values).items()
                if key not in gone}

    monkeypatch.setattr(process, "_live_cmds", _klipper_already_dead)
    assert process.stop(timeout=1.0) is True
    verdict = process.powerloss.last_stop(stack_up=False)
    assert verdict["state"] == "unclean"
    assert "klipper DOWN" in verdict["reason"]
    assert sent  # it still tore the rest of the stack down


def test_down_on_a_never_tracked_stack_writes_no_marker(proc, monkeypatch):
    """No state.json, nothing stopped: `down` twice must not manufacture a
    history of a session that never ran in this invocation."""
    fake_ps(monkeypatch, {})
    assert process.stop(timeout=1.0) is False
    assert process.powerloss.last_stop() is None
    assert not proc.paths.POWERLOSS.exists()


def test_a_stop_that_refused_to_die_leaves_the_session_open(proc, monkeypatch):
    """stop() returns False and keeps state.json for inspection; the session
    marker must stay OPEN so the next read still calls that death unclean."""
    pids = write_state(proc)
    sent = fake_ps(monkeypatch, pids)

    def stubborn(pid, sig):
        sent.append((pid, sig))
        return True                                # alive forever: never forgets

    monkeypatch.setattr(process, "_signal", stubborn)
    process.powerloss.note_session_start()
    assert process.stop(timeout=0.1) is False
    assert (proc.paths.POWERLOSS / "session.json").exists()
    assert not (proc.paths.POWERLOSS / "stop.json").exists()
    assert process.powerloss.last_stop(stack_up=False)["state"] == "unclean"


# ----------------------------------------------------------------- killall -----
def test_killall_is_recorded_as_a_forced_unclean_stop(proc, monkeypatch):
    pids = write_state(proc)
    fake_ps(monkeypatch, pids)
    process.powerloss.note_session_start()
    killed = process.kill_all()
    assert [name for name, _pid in killed]
    verdict = process.powerloss.last_stop(stack_up=False)
    assert verdict["state"] == "unclean"
    assert verdict["forced"] is True
    assert verdict["reason"] == "killall"


def test_killall_with_nothing_to_kill_writes_no_marker(proc, monkeypatch):
    fake_ps(monkeypatch, {})
    assert process.kill_all() == []
    assert process.powerloss.last_stop() is None


# --------------------------------------------------------------------- up -----
@pytest.fixture
def startable(proc, monkeypatch):
    """Everything start() needs except its own logic: an install whose venv
    python exists, whose compat shims and ports are already settled, and whose
    children are fabricated."""
    proc.paths.VENV_PY.parent.mkdir(parents=True, exist_ok=True)
    proc.paths.VENV_PY.write_text("")
    monkeypatch.setattr(process, "invalidate_stale_build", lambda _p: None)
    monkeypatch.setattr(process, "ensure_runtime_shims", lambda _p: None)
    monkeypatch.setattr(process, "_reap_orphans", lambda: None)
    monkeypatch.setattr(process, "port_conflicts", lambda: [])
    monkeypatch.setattr(process.shaper, "interpreter_for_klipper",
                        lambda: (proc.paths.VENV_PY, None))
    spawned = []

    class FakeProc:
        def __init__(self, argv):
            self.argv = argv
            self.pid = 20000 + len(spawned)

    def _popen(argv, **kwargs):
        proc = FakeProc(argv)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(process, "_popen", _popen)
    return proc, spawned


def test_start_opens_the_session_marker_with_this_boot_and_those_pids(startable):
    proc, spawned = startable
    assert process.start() is True
    marker = json.loads((proc.paths.POWERLOSS / "session.json").read_text())
    assert marker["boot_time"] == BOOT
    assert marker["stack"]["klipper"] == spawned[0].pid
    assert marker["started"] and marker["started_epoch"]
    assert proc.paths.STATE.exists()               # the stack record too


def test_start_sweeps_the_crash_before_it_opens_its_own_session(startable):
    """The order matters: `up` is where a previous death becomes history. If the
    new marker were written first, the swept verdict would describe THIS session
    and the crash would be gone from the record."""
    proc, _spawned = startable
    write_state(proc)
    process.powerloss.note_session_start()
    first = json.loads(
        (proc.paths.POWERLOSS / "session.json").read_text())["started_epoch"]
    assert process.start() is True
    verdict = json.loads(
        (proc.paths.POWERLOSS / "stop.json").read_text())
    assert verdict["clean"] is False
    assert verdict["started_epoch"] == first
    marker = json.loads(
        (proc.paths.POWERLOSS / "session.json").read_text())
    assert marker["started_epoch"] != first


def test_a_stack_that_cannot_be_persisted_never_opens_a_marker(startable):
    """start() refuses to spawn when the stack record cannot be written; the
    session marker must not claim a stack that does not exist."""
    proc, _spawned = startable
    paths = proc.paths
    paths.RUN.mkdir(parents=True, exist_ok=True)
    paths.RUN.chmod(0o500)                         # readable, nothing creatable
    try:
        assert process.start() is False
    finally:
        paths.RUN.chmod(0o700)
    assert not (paths.POWERLOSS / "session.json").exists()


def test_a_full_cycle_up_down_up_reports_the_last_stop_only(proc, monkeypatch,
                                                            startable):
    """`down` then `up` on a healthy machine: the verdict is clean, and after the
    second start there is nothing to report at all (the open marker IS the
    running session)."""
    proc, _spawned = startable
    assert process.start() is True
    pids = json.loads(proc.paths.STATE.read_text())
    fake_ps(monkeypatch, pids)
    assert process.stop(timeout=1.0) is True
    assert process.powerloss.last_stop(stack_up=False)["state"] == "clean"
    assert process.start() is True
    assert process.powerloss.verdict_lines(stack_up=True) == []
