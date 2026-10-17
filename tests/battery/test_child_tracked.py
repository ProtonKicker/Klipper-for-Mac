"""A real tracked child: started, written down, and reaped.

The rest of this folder proves the decisions; this file proves the process. It
spawns the guard for real (a throwaway app home, an interpreter wrapper that
carries the exact argv `is_guard_cmd()` recognises, and a Moonraker port that
nothing listens on, so a test can never reach the developer's stack) and then
asks the only question that matters about a detached child: can `down` and
`killall` still turn it off?

Wall-clock by nature (a spawn, a 0.6 s settle, a TERM), so it is marked slow.
Nothing here touches ~/.klipperformac or a live printer.
"""
import json
import os
import signal
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.slow

# A port nothing serves on: the child's REST reads fail, which is exactly the
# offline-safe state for a spawned guard.
DEAD_PORT = "17125"

# `paths.VENV_PY` on a real install is a symlink to a Mach-O python, and that
# is what makes the ledger's identity rule work: `ps` then reports the path the
# stack passed (`<app home>/venv/bin/python -m klipperformac.batteryguard run
# ...`), measured against the live stack on this Mac. A shell script with a `#!`
# would NOT: the kernel rewrites argv to the resolved interpreter, so the
# throwaway "venv" here is a symlink too, not a wrapper.


@pytest.fixture
def child(guard, monkeypatch):
    """A throwaway install with a working `venv/bin/python` for the guard."""
    bg, p = guard.bg, guard.process
    monkeypatch.setenv("KLIPPERFORMAC_MOONRAKER_PORT", DEAD_PORT)
    monkeypatch.setenv("KLIPPERFORMAC_BATTERY_PERCENT", "95")
    monkeypatch.setenv("KLIPPERFORMAC_POWER_SOURCE", "ac")
    monkeypatch.delenv("KLIPPERFORMAC_BATTERY_FILE", raising=False)
    paths = guard.paths
    paths.VENV_PY.parent.mkdir(parents=True, exist_ok=True)
    if paths.VENV_PY.exists() or paths.VENV_PY.is_symlink():
        paths.VENV_PY.unlink()
    os.symlink(sys.executable, paths.VENV_PY)
    # the reloaded module in this test process still holds the fixture's port;
    # the child re-reads its own environment, which is what this is about
    monkeypatch.setattr(bg, "read_power",
                        lambda env=None: {"percent": 95, "source": "ac",
                                          "charging": True, "remaining": None,
                                          "engine": "env", "raw": ""})
    guard.child = child_state = {"pids": []}

    def track(pid):
        child_state["pids"].append(pid)
        return pid

    original = bg._alive

    def alive(pid):
        track(pid)
        return original(pid)

    monkeypatch.setattr(bg, "_alive", alive)
    # Every test here ends with the child gone, whatever the assertions did:
    # a stray watchdog from a test would be the invisible process AGENTS.md
    # forbids, and it would outlive the tmp folder it writes into.
    yield guard
    for pid in set(child_state["pids"]):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    bg.process.clear_guard_record()


def wait_dead(pid, limit=5.0):
    deadline = time.time() + limit
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.1)
    return False


def test_start_spawns_a_tracked_child_and_stop_reaps_it(child):
    bg, p = child.bg, child.process
    ok, lines = bg.start()
    assert ok is True, lines
    pids = p.live_guard_pids()
    assert len(pids) == 1, pids
    pid = pids[0]
    # written down, and readable back as "on"
    doc = json.loads(bg.ledger_path().read_text())
    assert doc["pids"] == [pid]
    assert doc["app_home"] == str(child.paths.APP_HOME)
    state = bg.running()
    assert state is not None and state["pid"] == pid
    os.kill(pid, 0)                      # really running

    ok, lines = bg.stop(timeout=3.0)
    assert ok is True, lines
    assert wait_dead(pid), "the guard survived `off`"
    assert not bg.ledger_path().exists()
    assert not bg.episode_path().exists()
    assert bg.running() is None
    assert p.live_guard_pids() == []


def test_down_and_killall_reap_a_guard_whose_ledger_was_deleted(child):
    """The hand-deleted ledger is the case `killall` exists for: a watchdog
    nobody recorded still has to be turnable off."""
    bg, p = child.bg, child.process
    ok, lines = bg.start()
    assert ok is True, lines
    pid = p.live_guard_pids()[0]
    bg.ledger_path().unlink()
    assert p.live_guard_pids() == []        # nothing recorded...
    killed = bg.kill_orphans()              # ...but the argv scan finds it
    assert [k[1] for k in killed] == [pid]
    assert wait_dead(pid)
    assert bg.running() is None
    assert bg.kill_orphans() == []          # idempotent


def test_a_second_child_bails_instead_of_double_pausing(child):
    """Two guards on one printer would pause each other's resumes. The single
    instance check is on the ledger, re-validated by argv, so a recycled pid
    cannot block a legitimate start either."""
    bg, p = child.bg, child.process
    ok, lines = bg.start()
    assert ok is True, lines
    pid = p.live_guard_pids()[0]
    code = bg.main(["run", "--interval", "0.05", "--polls", "1"])
    assert code == 0                      # it refused to be a second guard
    assert p.live_guard_pids() == [pid]   # and the first is untouched
    log = bg.log_path().read_text()
    assert "already running" in log
    ok, _lines = bg.start()
    assert ok is True                     # "already on", not a second child
    assert p.live_guard_pids() == [pid]
    assert len(p.live_guard_pids()) == 1


def test_the_real_child_loop_polls_records_and_dry_runs(child):
    """The loop as it actually runs, in its own process: battery injected to
    12 % on battery through the env hook, --dry-run, two quick polls, and a
    Moonraker port nothing listens on. It must record what it saw, say what it
    would do, and post nothing - and exit 0 by itself."""
    bg = child.bg
    bg.episode_path().unlink(missing_ok=True)
    repo_root = os.path.dirname(os.path.dirname(
        os.path.abspath(bg.__file__)))
    env = dict(os.environ)
    env["PYTHONPATH"] = repo_root
    env["KLIPPERFORMAC_BATTERY_PERCENT"] = "12"
    env["KLIPPERFORMAC_POWER_SOURCE"] = "battery"
    proc = subprocess.Popen(
        [sys.executable, "-m", "klipperformac.batteryguard", "run",
         "--interval", "0.05", "--polls", "2", "--dry-run"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child.child["pids"].append(proc.pid)
    assert proc.wait(timeout=30) == 0

    episode = json.loads(bg.episode_path().read_text())
    assert episode["percent"] == 12
    assert episode["source"] == "battery"
    assert episode["armed"] is False          # it decided to pause
    assert episode["guardPaused"] is True
    assert episode["polls"] == 2
    log = bg.log_path().read_text()
    assert "would pause" in log and "would notify" in log
    assert "guard started" in log
    # and the dry run really sent nothing anywhere
    assert child.rec["post"] == []


def test_main_argument_surface(child):
    bg = child.bg
    assert bg.main(["teleport"]) == 2
    assert bg.main(["run", "--interval", "x"]) == 2
    assert bg.main(["run", "--polls", "x"]) == 2


def test_poll_interval_floor_and_its_one_exception(child, monkeypatch):
    """The floor is 2 s: a watchdog that hammers pmset burns the power it is
    trying to save. Going below it needs KLIPPERFORMAC_HOME to name a throwaway
    app home - that is, to be a test - so a stray env var cannot do it."""
    bg = child.bg
    cfg = bg.normalise({"pollSec": 30})
    assert bg.poll_interval(cfg, environ={}) == 30.0
    assert bg.poll_interval(cfg, environ={"KLIPPERFORMAC_BATTERY_POLL":
                                         "0.05"}) == 2.0
    assert bg.poll_interval(cfg, environ={
        "KLIPPERFORMAC_BATTERY_POLL": "0.05",
        "KLIPPERFORMAC_HOME": str(child.home)}) == 0.05
    assert bg.poll_interval(cfg, environ={
        "KLIPPERFORMAC_BATTERY_POLL": "not a number"}) == 30.0
    assert bg.poll_interval(cfg, environ={
        "KLIPPERFORMAC_BATTERY_POLL": "nan"}) == 30.0
    assert bg.poll_interval(cfg, environ={
        "KLIPPERFORMAC_BATTERY_POLL": "-5"}) == 30.0
    # a config below the floor is floored (normalise() would repair it to 30;
    # poll_interval() does not depend on that, because the child can be handed
    # a cfg straight from --interval)
    assert bg.poll_interval({"pollSec": 1}, environ={}) == 2.0


def test_a_stubborn_child_is_killed_not_left(child):
    """A guard that ignores TERM (here: a wrapper that traps it) is escalated to
    KILL, and the ledger is cleared anyway. `down` must never leave a watchdog
    that fires later."""
    p = child.process
    proc = subprocess.Popen(
        [sys.executable, "-c", "import signal, time;"
         "signal.signal(signal.SIGTERM, lambda *a: None); time.sleep(60)"],
        start_new_session=True)
    child.child["pids"].append(proc.pid)
    os.kill(proc.pid, 0)
    assert p.reap_guard([proc.pid], timeout=0.3) == [proc.pid]
    assert wait_dead(proc.pid)
    # reaped, not a zombie: `ps` has forgotten it and our own table has too
    try:
        os.waitpid(proc.pid, os.WNOHANG)
        raise AssertionError("the child was left for the test to reap")
    except ChildProcessError:
        pass
