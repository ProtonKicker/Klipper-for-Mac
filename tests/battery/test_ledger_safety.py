"""The guard ledger: who we are allowed to signal, and what an unrecorded
child costs.

The watchdog is a detached process that posts to the printer, so its identity
rules are the caffeinate rules: a pid from the ledger is only ours when the
command line `ps` reports is exactly the argv this install spawns, the record
names this app home, and the boot time is this boot. Anything else is somebody
else's process and is never signalled. And a child that cannot be written down
is killed rather than left running: an untracked guard is a watchdog nobody can
turn off.

Offline: `_live_cmds`, `_signal`, `Popen` and `stop()` are faked here, and the
tests assert which pids the code WOULD signal. No real process is touched and
every file lands in pytest's tmp_path.
"""
import json
import signal

import pytest

BOOT = "1791932927"
ARGV_TAIL = "-m klipperformac.batteryguard run --interval 30"


@pytest.fixture
def ledger(guard, monkeypatch):
    """`ps` and `kill()` as two dicts a test can fill and read."""
    p = guard.process
    state = {"rows": {}, "signals": [], "boot": BOOT}
    monkeypatch.setattr(p, "_live_cmds",
                        lambda pids: {str(pid): state["rows"][str(pid)]
                                      for pid in pids
                                      if str(pid) in state["rows"]})
    monkeypatch.setattr(
        p, "_signal",
        lambda pid, sig: (state["signals"].append((pid, sig)), True)[1])
    monkeypatch.setattr(p, "_boot_time", lambda: state["boot"])
    monkeypatch.setattr(p, "os", FakeOs(state, p.os))
    guard.ledger = state
    return state


class FakeOs:
    """Records kill() and lets waitpid() be a no-op: the reap loops in
    process.py would otherwise wait on a real pid that does not exist."""

    def __init__(self, state, real):
        self.state = state
        self.real = real

    def getpid(self):
        return 1111

    def kill(self, pid, sig):
        self.state["signals"].append((pid, sig))

    def waitpid(self, pid, flags):
        return pid, 0

    def __getattr__(self, name):
        return getattr(self.real, name)


def our_argv(guard):
    return "{} {}".format(guard.home / "venv" / "bin" / "python", ARGV_TAIL)


def write_ledger(paths, pids, boot=BOOT, app_home=None):
    paths.RUN.mkdir(parents=True, exist_ok=True)
    doc = {"boot_time": boot, "app_home": str(app_home or paths.APP_HOME),
           "pids": list(pids), "argv": ["-m", "klipperformac.batteryguard",
                                       "run"]}
    paths.RUN.joinpath("batteryguard.json").write_text(json.dumps(doc))


# ------------------------------------------------------------- identity rules --

def test_is_guard_cmd_wants_the_exact_argv_shape(guard):
    p = guard.process
    home = str(guard.home)
    assert p.is_guard_cmd("{} {}".format(home + "/venv/bin/python",
                                         ARGV_TAIL)) is True
    # a different checkout's guard has the same module name and is not ours
    assert p.is_guard_cmd("/Users/guest/.klipperformac/venv/bin/python "
                          + ARGV_TAIL) is False
    # the words alone are not an identity: an editor, a grep or a log tail all
    # contain them
    assert p.is_guard_cmd("/bin/sh -c echo") is False
    assert p.is_guard_cmd("python -m klipperformac.proxy 8080 /srv 7125 "
                          "127.0.0.1") is False
    assert p.is_guard_cmd("vim -m klipperformac.batteryguard run") is False
    assert p.is_guard_cmd("tail -f logs/batteryguard.log") is False
    assert p.is_guard_cmd("") is False
    assert p.is_guard_cmd(home + "/venv/bin/python") is False


def test_a_recycled_pid_is_never_signalled(ledger, guard):
    """The ledger says 4321; `ps` says 4321 is the user's Finder. The pid is
    left alone and the guard reads as off."""
    p = guard.process
    write_ledger(guard.paths, [4321])
    ledger["rows"] = {"4321": "/System/Library/CoreServices/Finder.app/x"}
    assert p.live_guard_pids() == []
    assert ledger["signals"] == []
    assert p.stop_guard() == []
    assert ledger["signals"] == []
    # the same for `status`: a borrowed pid is not "on"
    assert guard.bg.running() is None


def test_a_foreign_boot_or_home_record_is_dead_not_live(ledger, guard):
    p, paths = guard.process, guard.paths
    ledger["rows"] = {"4321": our_argv(guard)}
    write_ledger(paths, [4321], boot="1600000000")        # another boot
    assert p.live_guard_pids() == []
    assert p.guard_record_ours() is True                  # ours, just stale

    write_ledger(paths, [4321], app_home="/Users/guest/.klipperformac")
    assert p.live_guard_pids() == []
    # ... and a record naming ANOTHER app home is not ours to delete either
    assert p.guard_record_ours() is False
    assert guard.bg.running() is None
    assert paths.RUN.joinpath("batteryguard.json").exists()


def test_junk_ledger_values_are_not_processes(ledger, guard):
    """pid 0 is a process GROUP, true is pid 1 (launchd), and macOS pids stop
    at 99999 - none of them may reach os.kill."""
    p = guard.process
    write_ledger(guard.paths, [0, -1, True, "4321", 1e9, None, 3.5,
                               [4321], 999999999])
    ledger["rows"] = {"4321": our_argv(guard)}
    assert p.live_guard_pids() == []
    assert p.stop_guard() == []
    assert ledger["signals"] == []


def test_the_guard_argv_is_not_a_service(guard):
    """The whole point of keeping it out of SERVICES: process.stop()/kill_all()
    must not be able to see it, so a `down` can never race the guard's own
    teardown, and `status` never shows an optional watchdog as DOWN."""
    p = guard.process
    argv = "{} {}".format(guard.paths.VENV_PY, ARGV_TAIL)
    assert p._service_kind(argv) is None
    assert "batteryguard" not in p.SERVICES
    assert "batteryguard" not in p.MARKERS


# ------------------------------------------------------------- the reap path --

def test_stop_guard_terms_then_clears(ledger, guard):
    p, paths = guard.process, guard.paths
    write_ledger(paths, [4321])
    ledger["rows"] = {"4321": our_argv(guard)}
    assert p.live_guard_pids() == [4321]
    assert p.stop_guard(timeout=0.2) == [4321]
    assert (4321, signal.SIGTERM) in ledger["signals"]
    assert not paths.RUN.joinpath("batteryguard.json").exists()


def test_stop_guard_reap_survives_a_stubborn_child(ledger, guard):
    """A child that ignores TERM is escalated to KILL and the ledger is cleared
    anyway: `down` must not leave a watchdog that fires later. The faked `ps`
    keeps listing the pid, which is how "refused to die" is expressed here."""
    p, paths = guard.process, guard.paths
    write_ledger(paths, [4321])
    ledger["rows"] = {"4321": our_argv(guard)}
    p.stop_guard(timeout=0.2)
    sigs = [sig for _pid, sig in ledger["signals"]]
    assert signal.SIGTERM in sigs
    assert signal.SIGKILL in sigs
    assert not paths.RUN.joinpath("batteryguard.json").exists()


def test_stop_and_kill_orphans_are_idempotent(guard):
    """Off twice is still off, and it leaves no state file behind: a guard that
    reports itself off while a state file survives is how `status` starts
    lying."""
    bg = guard.bg
    ok, lines = bg.stop()
    assert ok is False and "not on" in " ".join(lines)
    ok2, _lines = bg.stop()
    assert ok2 is False
    assert not bg.episode_path().exists()
    assert not bg.ledger_path().exists()
    assert bg.kill_orphans() == []
    assert bg.running() is None
    # off -> off after the setting was cleared, still nothing to find
    bg.save_battery_settings(enabled=False)
    assert bg.stop()[0] is False
    assert bg.kill_orphans() == []


def test_kill_orphans_finds_a_child_the_ledger_lost(guard, monkeypatch):
    """A hand-deleted batteryguard.json must not make the watchdog immortal:
    the machine-wide scan returns only this install's exact argv."""
    bg, p = guard.bg, guard.process
    ours = "{} {}".format(guard.home / "venv" / "bin" / "python", ARGV_TAIL)
    other = "/Users/guest/.klipperformac/venv/bin/python " + ARGV_TAIL
    grep = "/bin/sh -c grep -m klipperformac.batteryguard run logs/*"
    listing = "\n".join(["4321 " + ours, "4322 " + other, "4323 " + grep])
    calls = []
    monkeypatch.setattr(p, "_signal",
                        lambda pid, sig: (calls.append((pid, sig)), True)[1])
    monkeypatch.setattr(p.subprocess, "run",
                        lambda *a, **k: type("R", (),
                                             {"stdout": listing.encode()})())
    monkeypatch.setattr(p, "_live_cmds", lambda pids: {})
    killed = bg.kill_orphans()
    assert killed == [("batteryguard", 4321)]
    assert calls[0] == (4321, signal.SIGKILL)
    assert 4322 not in [pid for pid, _s in calls]
    assert 4323 not in [pid for pid, _s in calls]


def test_unwritable_ledger_kills_the_child_it_just_started(guard, monkeypatch):
    """start() refuses rather than leaking a watchdog nobody can turn off."""
    bg, p = guard.bg, guard.process
    signalled = []
    monkeypatch.setattr(p, "_signal",
                        lambda pid, sig: (signalled.append((pid, sig)),
                                          True)[1])
    monkeypatch.setattr(p, "_live_cmds", lambda pids: {})
    monkeypatch.setattr(p, "_boot_time", lambda: BOOT)
    monkeypatch.setattr(bg, "read_power",
                        lambda env=None: {"percent": 40, "source": "battery",
                                          "charging": False, "remaining": None,
                                          "engine": "env", "raw": ""})
    made = []

    class FakeChild:
        pid = 4321

    def fake_popen(cmd, **kw):
        made.append(cmd)
        return FakeChild()

    monkeypatch.setattr(p, "_popen", fake_popen)
    monkeypatch.setattr(bg, "_alive", lambda pid: True)
    guard.paths.VENV_PY.parent.mkdir(parents=True, exist_ok=True)
    guard.paths.VENV_PY.write_text("#!/bin/sh\nexit 0\n")
    # the run folder is a regular file: the ledger cannot be written
    guard.paths.RUN.chmod(0o500)
    try:
        ok, lines = bg.start()
    finally:
        guard.paths.RUN.chmod(0o755)
    assert ok is False
    assert "untracked" in " ".join(lines)
    assert made, "the child was spawned through a fake that never saw it"
    assert signalled == [(4321, signal.SIGTERM)]
    assert not bg.ledger_path().exists()


def test_start_refuses_a_mac_with_no_battery(guard, monkeypatch):
    bg, p = guard.bg, guard.process
    monkeypatch.setattr(bg, "read_power", lambda env=None: None)
    made = []
    monkeypatch.setattr(p, "_popen",
                        lambda cmd, **kw: made.append(cmd))
    ok, lines = bg.start()
    assert ok is False
    assert "no battery" in " ".join(lines)
    assert made == []            # nothing was even attempted


def test_start_that_dies_immediately_reports_the_log(guard, monkeypatch):
    bg = guard.bg
    monkeypatch.setattr(bg, "read_power",
                        lambda env=None: {"percent": 30, "source": "battery",
                                          "charging": False, "remaining": None,
                                          "engine": "env", "raw": ""})
    monkeypatch.setattr(bg.process, "launch_guard",
                        lambda interval, log: (4321, None))
    monkeypatch.setattr(bg.process, "clear_guard_record", lambda: None)
    monkeypatch.setattr(bg, "_alive", lambda pid: False)
    monkeypatch.setattr(bg, "log_tail", lambda count=4: ["boom"])
    ok, lines = bg.start()
    assert ok is False
    assert "as soon as it started" in " ".join(lines)
    assert "boom" in " ".join(lines)


def test_launch_guard_refuses_without_a_venv(guard, monkeypatch):
    """No venv python means no child to start: the reason is named, and no
    process is spawned (AGENTS.md: the stack's own interpreter is the one the
    guard runs on, not whatever python the caller happened to use)."""
    p = guard.process
    assert not guard.paths.VENV_PY.exists()
    opened = []
    pid, err = p.launch_guard(30, open("/dev/null", "wb"))
    opened.append(pid)
    assert pid is None
    assert "venv" in err


def test_note_guard_records_and_rereads(guard, monkeypatch):
    """The ledger is the only thing `down` knows about the child, so a pid
    written down must come back out of it unchanged."""
    p, paths = guard.process, guard.paths
    monkeypatch.setattr(p, "_boot_time", lambda: BOOT)
    assert p.note_guard(4321) is True
    doc = json.loads(paths.RUN.joinpath("batteryguard.json").read_text())
    assert doc["pids"] == [4321]
    assert doc["boot_time"] == BOOT
    assert doc["app_home"] == str(paths.APP_HOME)
    assert p.note_guard(4322) is True
    assert p._recorded_guard_pids(boot_check=False) == [4321, 4322]
    # the ledger is bounded: 16 newest, never an unbounded file
    for pid in range(5000, 5030):
        p.note_guard(pid)
    doc = json.loads(paths.RUN.joinpath("batteryguard.json").read_text())
    assert len(doc["pids"]) == p._GUARD_LIMIT == 16
    assert doc["pids"][-1] == 5029
    assert 4321 not in doc["pids"]


def test_launch_guard_spawns_the_recorded_argv(guard, monkeypatch):
    """What start() spawns is exactly what is_guard_cmd() later recognises, on
    THIS install's own interpreter, in its own session, with the log attached."""
    p, paths = guard.process, guard.paths
    paths.VENV_PY.parent.mkdir(parents=True, exist_ok=True)
    paths.VENV_PY.write_text("#!/bin/sh\nexit 0\n")
    paths.VENV_PY.chmod(0o755)
    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            seen["cmd"] = cmd
            seen["kw"] = kw
            self.pid = 4321

    monkeypatch.setattr(p.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(p, "_boot_time", lambda: BOOT)
    log = open("/dev/null", "wb")
    pid, err = p.launch_guard(45, log)
    assert (pid, err) == (4321, None)
    assert seen["cmd"] == [str(paths.VENV_PY), "-m",
                           "klipperformac.batteryguard", "run",
                           "--interval", "45"]
    assert seen["kw"]["start_new_session"] is True
    assert p.is_guard_cmd(" ".join(seen["cmd"])) is True
    doc = json.loads(paths.RUN.joinpath("batteryguard.json").read_text())
    assert doc["pids"] == [4321]
