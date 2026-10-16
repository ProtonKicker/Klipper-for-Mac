"""``start()`` / ``stop()`` as a transaction in ``klipperformac/process.py``.

``start()`` is the only place that decides what a stack *is*: five children, the
argvs they are spawned with, and the ``run/state.json`` that every later read
(status, down, doctor, the SwiftUI app) judges those children against. So the
promises worth pinning are: every spawned command line must still be
recognisable on the next read; nothing may be spawned when a stack is already
up; the children must outlive the terminal; and LAN binding stays opt-in.

Offline: ``Popen``/``ps``/``sysctl``/``os.kill`` are faked inside the module
under test. The fabricated ``Popen`` only records argv + kwargs and hands back a
fake pid, so no process is spawned, nothing binds a port, and no real device is
opened. All files live under pytest's ``tmp_path``.
"""
import json
import os
import signal
import subprocess
import types

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

BOOT = "1791932927"
SYSCTL_OK = ("{ sec = %s, usec = 560431 } Tue Oct 13 16:08:47 2026\n"
             % BOOT).encode()
MAXPID = 99999
# A *foreign* install's layout: _scan_service_pids() only recognises command
# lines carrying this install's own paths, so a stray of the stack under test is
# forged at txn.home, and a DEFAULT_HOME row must be left alone.
DEFAULT_HOME = "/Users/peter/.klipperformac"


class Clock:
    def __init__(self, start=1000000.0):
        self.t = start
        self.slept = 0.0

    def time(self):
        return self.t

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        self.slept += seconds

    def strftime(self, fmt):
        return "2026-10-15T00:00:00"


class Completed:
    def __init__(self, stdout=b"", code=0, stderr=b""):
        self.stdout = stdout
        self.returncode = code
        self.stderr = stderr


class FakeProc:
    def __init__(self, pid):
        self.pid = pid


class FakePs:
    """sysctl + ps + Popen. Popen records the argv/kwargs it *would* have been
    given and also publishes that command line in the ps table, so a
    start()-then-read round trip exercises the real marker strings."""

    DEVNULL = subprocess.DEVNULL

    def __init__(self):
        self.rows = {}
        self.scan = b""
        self.sysctl = SYSCTL_OK
        self.sysctl_rc = 0
        self.calls = []
        self.spawned = []
        self._next_pid = 40000

    def add(self, pid, cmd, state="Ss", policy="term"):
        self.rows[str(pid)] = {"state": state, "cmd": cmd, "policy": policy}
        return pid

    def drop(self, pid):
        self.rows.pop(str(pid), None)

    def handle_signal(self, pid, sig):
        row = self.rows.get(str(pid))
        if row is None:
            return
        if sig == signal.SIGTERM and row["policy"] == "term":
            self.drop(pid)
        elif sig == signal.SIGKILL and row["policy"] in ("term", "kill"):
            self.drop(pid)

    def Popen(self, cmd, **kwargs):
        argv = [str(c) for c in cmd]
        self.calls.append(["spawn"] + argv)
        self.spawned.append({"argv": argv, "kwargs": kwargs})
        self._next_pid += 1
        proc = FakeProc(self._next_pid)
        self.spawned[-1]["pid"] = proc.pid
        self.add(proc.pid, " ".join(argv))
        return proc

    def run(self, cmd, *args, **kwargs):
        argv = [str(c) for c in cmd]
        self.calls.append(argv)
        if argv[0].endswith("sysctl"):
            if self.sysctl_rc and kwargs.get("check"):
                raise subprocess.CalledProcessError(self.sysctl_rc, argv)
            return Completed(self.sysctl, self.sysctl_rc)
        if argv[0] == "ps" and "-p" in argv:
            tokens = argv[argv.index("-p") + 1].split(",")
            for tok in tokens:
                if not tok.isdigit() or int(tok) > MAXPID:
                    return Completed(b"", 1, b"ps: bad pid token\n")
            lines = ["%6s %-3s %s" % (t, self.rows[t]["state"], self.rows[t]["cmd"])
                     for t in tokens if t in self.rows]
            return Completed(("\n".join(lines) + "\n").encode() if lines else b"",
                             0 if lines else 1)
        if argv[0] == "ps":
            return Completed(self.scan, 0)
        return Completed(b"", 1)

    def index_of(self, prefix):
        for i, call in enumerate(self.calls):
            if call[0] == prefix:
                return i
        return -1

    def spawn_argv(self, needle):
        return [rec for rec in self.spawned if any(needle in a for a in rec["argv"])]


class FakeOS:
    def __init__(self, on_kill=None):
        self.signals = []
        self._on_kill = on_kill

    def kill(self, pid, sig):
        if not isinstance(pid, int):
            raise TypeError("an integer is required (got type %s)"
                            % type(pid).__name__)
        self.signals.append((pid, sig))
        if self._on_kill:
            self._on_kill(pid, sig)

    def waitpid(self, pid, options):
        raise ChildProcessError(10, "No child processes")

    def __getattr__(self, name):
        return getattr(os, name)


class FakeJSON:
    """json module stand-in whose dump() fails half-way (disk full, SIGKILL)."""

    def __init__(self, error):
        self._error = error

    def dump(self, *args, **kwargs):
        raise self._error

    def load(self, *args, **kwargs):
        return json.load(*args, **kwargs)

    def dumps(self, *args, **kwargs):
        return json.dumps(*args, **kwargs)

    def loads(self, *args, **kwargs):
        return json.loads(*args, **kwargs)


def argv_at(home, name, web_port=8080, fluidd_port=8081, moonraker_port=7125):
    data = home + "/data"
    py = home + "/venv/bin/python"
    if name == "klipper":
        return [py, home + "/klipper/klippy/klippy.py", "-a",
                data + "/run/klippy.sock", "-l", data + "/logs/klippy.log",
                data + "/config/printer.cfg"]
    if name == "moonraker":
        return [py, "-m", "moonraker", "-d", data, "-c",
                data + "/config/moonraker.conf", "-l",
                data + "/logs/moonraker.log"]
    if name in ("web_mainsail", "web_fluidd"):
        port = str(web_port) if name == "web_mainsail" else str(fluidd_port)
        root = data + "/web/mainsail" if name == "web_mainsail" \
            else data + "/web/fluidd"
        return [py, "-m", "klipperformac.proxy", port, root,
                str(moonraker_port), "127.0.0.1"]
    return ["caffeinate", "-dis"]


def cmdline_at(home, name, **kw):
    return " ".join(argv_at(home, name, **kw))


@pytest.fixture
def txn(stack, monkeypatch):
    ps = FakePs()
    system = FakeOS(on_kill=ps.handle_signal)
    clock = Clock()
    monkeypatch.setattr(stack.process, "subprocess", ps)
    monkeypatch.setattr(stack.process, "time", clock)
    monkeypatch.setattr(stack.process, "os", system)
    monkeypatch.setattr(stack.process, "invalidate_stale_build", lambda d: False)
    monkeypatch.setattr(stack.process, "ensure_runtime_shims", lambda p: False)
    stack.process._boot_cache[:] = [0.0, None]
    stack.paths.RUN.mkdir(parents=True, exist_ok=True)

    def write_state(**services):
        doc = {"boot_time": BOOT, "started": "2026-10-15T00:00:00"}
        doc.update(services)
        stack.paths.STATE.write_text(json.dumps(doc))
        return doc

    def alive(**pid_for):
        for name, pid in pid_for.items():
            ps.add(pid, cmdline_at(str(stack.home), name))
        return pid_for

    return types.SimpleNamespace(p=stack.process, paths=stack.paths, ps=ps,
                                 system=system, clock=clock, home=stack.home,
                                 data=stack.data, write_state=write_state,
                                 alive=alive, monkeypatch=monkeypatch)


# --------------------------------------------------------------------------
# what start() spawns and what it writes down
# --------------------------------------------------------------------------

def test_start_spawns_each_service_exactly_once(txn):
    assert txn.p.start() is True
    assert len(txn.ps.spawned) == 5
    joined = [" ".join(rec["argv"]) for rec in txn.ps.spawned]
    for name in txn.p.SERVICES:
        assert any(txn.p.MARKERS[name] in cmd for cmd in joined), name
    assert txn.ps.spawned[-1]["argv"] == ["caffeinate", "-dis"]
    assert txn.ps.spawned[0]["argv"][1].endswith("klippy/klippy.py")


def test_start_persists_the_children_it_spawned(txn):
    txn.p.start()
    doc = json.loads(txn.paths.STATE.read_text())
    assert doc["boot_time"] == BOOT
    assert isinstance(doc["started"], str)
    assert set(doc) == set(txn.p.SERVICES) | {"boot_time", "started"}
    for rec in txn.ps.spawned:
        name = next(n for n in txn.p.SERVICES
                    if txn.p.MARKERS[n] in " ".join(rec["argv"]))
        assert doc[name] == rec["pid"], name


def test_a_stack_started_by_start_is_recognised_by_running(txn):
    """The contract that matters: what start() writes, running() must read back
    as up -- otherwise every later command mis-classifies a live stack."""
    txn.p.start()
    state = txn.p.running()
    assert state is not None
    assert state == {name: True for name in txn.p.SERVICES}


def test_children_survive_the_terminal_and_the_cli(txn):
    """start_new_session + DEVNULL: closing the Terminal window must not take
    the print down with it (process.py docstring promise)."""
    txn.p.start()
    for rec in txn.ps.spawned:
        kw = rec["kwargs"]
        assert kw["start_new_session"] is True, rec["argv"]
        for stream in ("stdin", "stdout", "stderr"):
            assert kw[stream] == subprocess.DEVNULL, rec["argv"]


def test_the_web_proxies_are_loopback_unless_lan_is_switched_on(txn):
    txn.p.start()
    proxies = [rec["argv"] for rec in txn.ps.spawned
               if "klipperformac.proxy" in " ".join(rec["argv"])]
    assert len(proxies) == 2
    for argv in proxies:
        assert argv[-1] == "127.0.0.1", argv

    txn.paths.save_settings({"lan": True})
    txn.paths.STATE.unlink(missing_ok=True)      # pretend nothing is tracked
    txn.ps.rows.clear()
    txn.ps.spawned.clear()
    txn.p.start()
    proxies = [rec["argv"] for rec in txn.ps.spawned
               if "klipperformac.proxy" in " ".join(rec["argv"])]
    assert len(proxies) == 2
    for argv in proxies:
        assert argv[-1] == "0.0.0.0", argv


def test_the_proxy_children_can_import_the_package(txn):
    """The proxies are spawned from the repo root; without PYTHONPATH they
    would die instantly and the UIs would never answer."""
    txn.p.start()
    root = os.path.dirname(os.path.dirname(os.path.abspath(
        txn.p.__file__)))
    for rec in txn.ps.spawned:
        if "klipperformac.proxy" in " ".join(rec["argv"]):
            assert rec["kwargs"]["env"]["PYTHONPATH"].startswith(root)


def test_start_creates_the_directories_it_needs(txn):
    for directory in (txn.paths.RUN, txn.paths.LOGS):
        if directory.exists():
            directory.rmdir()
    txn.p.start()
    assert txn.paths.RUN.exists()
    assert txn.paths.LOGS.exists()


def test_start_survives_a_missing_shaper_venv(txn):
    """printer.cfg asking for input shaping must never break a start."""
    txn.paths.CONFIG.mkdir(parents=True, exist_ok=True)
    txn.paths.PRINTER_CFG.write_text("[input_shaper]\nshaper_type: mzv\n")
    assert txn.p.start() is True
    assert len(txn.ps.spawned) == 5


# --------------------------------------------------------------------------
# when start() must refuse, and when it must clean up first
# --------------------------------------------------------------------------

def test_start_refuses_to_stack_a_second_instance(txn):
    pids = {"klipper": 4321, "moonraker": 4322}
    txn.write_state(**pids)
    txn.alive(**pids)
    assert txn.p.start() is False
    assert txn.ps.spawned == []


def test_start_self_heals_a_tracked_but_dead_stack(txn):
    txn.write_state(klipper=4321, moonraker=4322)     # nothing in the table
    assert txn.p.start() is True
    assert len(txn.ps.spawned) == 5
    doc = json.loads(txn.paths.STATE.read_text())
    assert doc["klipper"] not in (4321, 4322)


def test_start_reaps_strays_before_spawning(txn):
    """Order matters: a stray proxy still holds 8080 when the fresh one binds."""
    stray_cmd = cmdline_at(str(txn.home), "web_mainsail")
    txn.ps.scan = (" 8801 %s\n" % stray_cmd).encode()
    txn.ps.add(8801, stray_cmd)
    txn.p.start()
    scan_at = txn.ps.index_of("ps")
    spawn_at = txn.ps.index_of("spawn")
    assert scan_at >= 0 and spawn_at >= 0
    assert scan_at < spawn_at, "the stray must be cleared before anything binds"
    assert (8801, signal.SIGTERM) in txn.system.signals


def test_start_then_stop_signals_only_what_start_spawned(txn):
    txn.p.start()
    spawned = {rec["pid"] for rec in txn.ps.spawned}
    txn.system.signals.clear()
    assert txn.p.stop() is True
    assert {pid for pid, _ in txn.system.signals} == spawned
    assert not txn.paths.STATE.exists()


@settings(max_examples=6, database=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.integers(min_value=1, max_value=4))
def test_start_stop_cycles_are_idempotent(txn, cycles):
    """No cycle may inherit a record, a signal or a child from the last one."""
    txn.ps.spawned.clear()
    txn.ps.rows.clear()
    txn.system.signals.clear()
    txn.paths.STATE.unlink(missing_ok=True)
    seen = []
    for _ in range(cycles):
        before = len(txn.ps.spawned)
        assert txn.p.start() is True
        assert len(txn.ps.spawned) - before == 5
        new = [rec["pid"] for rec in txn.ps.spawned[before:]]
        assert not set(new) & set(seen), "pid reused across cycles"
        seen += new
        txn.system.signals.clear()
        assert txn.p.stop() is True
        assert {pid for pid, _ in txn.system.signals} == set(new)
        assert not txn.paths.STATE.exists()


# --------------------------------------------------------------------------
# BUGS
# --------------------------------------------------------------------------

def test_start_refuses_even_when_one_state_pid_is_unusable(txn):
    txn.write_state(klipper=4321, moonraker=999999999)
    txn.alive(klipper=4321)
    txn.ps.add(999999999, cmdline_at(str(txn.home), "moonraker"))
    assert txn.p.start() is False, (
        "klipper is alive and carries our marker, yet start() double-spawned: "
        "%d children" % len(txn.ps.spawned))
    assert txn.ps.spawned == []


def test_a_non_boolean_lan_setting_does_not_expose_the_stack(txn):
    txn.paths.save_settings({"lan": "false"})
    txn.p.start()
    proxies = [rec["argv"] for rec in txn.ps.spawned
               if "klipperformac.proxy" in " ".join(rec["argv"])]
    assert len(proxies) == 2
    for argv in proxies:
        assert argv[-1] == "127.0.0.1", (
            "settings.json says lan=%r and the stack still bound the whole "
            "network: %s" % ("false", argv))


def test_a_failed_state_write_leaves_the_previous_record_intact(txn):
    txn.p._save({"klipper": FakeProc(4321), "moonraker": FakeProc(4322)})
    before = txn.paths.STATE.read_text()
    txn.p._save({"klipper": FakeProc(4321)})           # still fine
    good_json = txn.p.json
    txn.monkeypatch.setattr(txn.p, "json",
                            FakeJSON(OSError(28, "No space left on device")))
    try:
        with pytest.raises(OSError):
            txn.p._save({"klipper": FakeProc(4321), "moonraker": FakeProc(4322)})
    finally:
        txn.monkeypatch.setattr(txn.p, "json", good_json)
    assert txn.paths.STATE.read_text() == before, (
        "a state write that failed destroyed the record of a running stack")
    assert txn.p._load() is not None
