"""Which pids ``klipperformac/process.py`` decides to signal, and with what.

Everything is offline and no real process is ever touched:

* ``sysctl`` / ``ps`` / ``Popen`` are faked *inside* the module under test,
* ``os.kill`` / ``os.waitpid`` are faked too -- the tests only assert the
  ``(pid, signal)`` pairs the module **would** send,
* ``run/state.json`` is always forged under the throwaway ``tmp_path`` home
  (the real ``~/.klipperformac`` is never opened), and
* a virtual clock means no test really sleeps.

The fake ``os.kill`` mirrors the exception surface measured on this Mac: a
non-int pid raises ``TypeError`` (not ``OSError``), an absurdly large int raises
``OverflowError``, a foreign pid raises ``PermissionError`` and an
already-dead pid raises ``ProcessLookupError``.
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
MAXPID = 99999          # measured: `ps -p 100000` -> "ps: process id too large"
DEFAULT_HOME = "/Users/peter/.klipperformac"


class Clock:
    """Virtual clock; nothing under test ever really waits."""

    def __init__(self, start=1000000.0):
        self.t = start

    def time(self):
        return self.t

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds

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
    """Answers only the sysctl/ps argvs process.py builds, with the macOS
    semantics measured by hand: one unparseable or out-of-range pid token
    aborts the *whole* ``ps -p`` query with empty stdout, while a merely-dead
    pid simply contributes no row."""

    DEVNULL = subprocess.DEVNULL

    def __init__(self):
        self.rows = {}        # {pid str: {"state", "cmd", "policy"}}
        self.scan = b""       # canned `ps -eo pid=,command=` output
        self.sysctl = SYSCTL_OK
        self.sysctl_rc = 0
        self.calls = []
        self.popens = []
        self._next_pid = 40000

    # -- spawning is never allowed to be real ------------------------------
    def Popen(self, cmd, **kwargs):
        self.popens.append(([str(c) for c in cmd], kwargs))
        self._next_pid += 1
        proc = FakeProc(self._next_pid)
        self.add(proc.pid, " ".join(str(c) for c in cmd))
        return proc

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
                    # measured: "ps: Invalid process id: abc" / "ps: process id
                    # too large: 100000", and stdout is EMPTY for the batch.
                    return Completed(b"", 1, b"ps: bad pid token\n")
            lines = ["%6s %-3s %s" % (t, self.rows[t]["state"], self.rows[t]["cmd"])
                     for t in tokens if t in self.rows]
            return Completed(("\n".join(lines) + "\n").encode() if lines else b"",
                             0 if lines else 1)
        if argv[0] == "ps":
            return Completed(self.scan, 0)
        return Completed(b"", 1)

    def ps_calls(self):
        return [c for c in self.calls if c[0] == "ps"]

    def pid_queries(self):
        return [c for c in self.calls if c[0] == "ps" and "-p" in c]


class FakeOS:
    """Records the signals process.py would send; emulates the real errno
    surface so "OSError is swallowed" can be told apart from "it crashes"."""

    def __init__(self, on_kill=None):
        self.signals = []
        self.waited = []
        self.foreign = set()      # pids owned by another user -> EPERM
        self.dead = set()         # pids that do not exist -> ESRCH
        self._on_kill = on_kill

    def kill(self, pid, sig):
        if not isinstance(pid, int):
            raise TypeError("an integer is required (got type %s)"
                            % type(pid).__name__)
        if pid > 2 ** 31 - 1:
            raise OverflowError("Python int too large to convert to C int")
        self.signals.append((pid, sig))
        if pid in self.dead:
            raise ProcessLookupError(3, "No such process")
        if pid in self.foreign:
            raise PermissionError(1, "Operation not permitted")
        if self._on_kill:
            self._on_kill(pid, sig)

    def waitpid(self, pid, options):
        if not isinstance(pid, int):
            raise TypeError("an integer is required")
        self.waited.append(pid)
        raise ChildProcessError(10, "No child processes")

    def __getattr__(self, name):
        return getattr(os, name)


def argv_at(home, name, web_port=8080, fluidd_port=8081, moonraker_port=7125):
    """The argv process.start() spawns for `name` in an install rooted at
    `home` (data at <home>/data, which is the default layout)."""
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


def ps_eo_text(*pairs):
    """Fabricate `ps -eo pid=,command=` text from (pid, command) pairs."""
    return "".join("%6d %s\n" % (pid, cmd) for pid, cmd in pairs).encode()


@pytest.fixture
def proc(stack, monkeypatch):
    """process.py with sysctl/ps/Popen/time/os.kill faked.

    ``alive()`` forges the process table with command lines for *this* throwaway
    home (what start() would really spawn here); ``scan_with()`` forges a
    ``ps -eo`` listing of this install's own strays, because
    _scan_service_pids() only recognises command lines carrying *this*
    install's paths -- DEFAULT_HOME rows in the batteries below are a foreign
    install's, and are expected to be left alone.
    """
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
        """Each service's pid is live in the table carrying its own marker."""
        for name, pid in pid_for.items():
            ps.add(pid, " ".join(argv_at(str(stack.home), name)))
        return pid_for

    def scan_with(*names, **kw):
        first = kw.get("first_pid", 700)
        lines = ["%6d %s" % (first + offset, cmdline_at(str(stack.home), name))
                 for offset, name in enumerate(names)]
        ps.scan = ("\n".join(lines) + "\n").encode()
        return [first + offset for offset in range(len(names))]

    def reset():
        """Drop every forged table; needed between hypothesis examples, which
        all share one fixture instance."""
        ps.rows.clear()
        ps.scan = b""
        ps.calls.clear()
        ps.popens.clear()
        system.signals.clear()
        system.waited.clear()
        system.foreign.clear()
        system.dead.clear()
        stack.process._boot_cache[:] = [0.0, None]
        stack.paths.STATE.unlink(missing_ok=True)

    return types.SimpleNamespace(p=stack.process, paths=stack.paths, ps=ps,
                                 system=system, clock=clock, home=stack.home,
                                 data=stack.data, write_state=write_state,
                                 alive=alive, scan_with=scan_with,
                                 argv_at=argv_at, reset=reset)


# --------------------------------------------------------------------------
# the parts that already behave
# --------------------------------------------------------------------------

def test_stop_without_a_state_file_signals_nothing(proc):
    assert proc.p.stop() is False
    assert proc.system.signals == []
    assert proc.ps.ps_calls() == []


def test_stop_signals_exactly_the_tracked_services_in_stop_order(proc):
    pids = {"web_mainsail": 4321, "web_fluidd": 4322, "moonraker": 4323,
            "klipper": 4324, "caffeinate": 4325}
    proc.write_state(**pids)
    proc.alive(**pids)
    assert proc.p.stop() is True
    assert [pid for pid, _ in proc.system.signals] == \
        [pids[n] for n in proc.p._STOP_ORDER]
    assert {sig for _, sig in proc.system.signals} == {signal.SIGTERM}
    assert not proc.paths.STATE.exists()


def test_a_service_that_is_already_gone_is_only_ever_sigtermed(proc):
    """Nothing may reach for SIGKILL while the plain TERM path is working."""
    proc.write_state(klipper=4321, moonraker=4322)
    proc.alive(klipper=4321)          # 4322 is not in the table: already dead
    assert proc.p.stop() is True
    assert sorted(proc.system.signals) == [(4321, signal.SIGTERM),
                                           (4322, signal.SIGTERM)]
    assert signal.SIGKILL not in [sig for _, sig in proc.system.signals]


def test_signal_errors_never_escape_the_signal_helper(proc):
    """ESRCH/EPERM are tolerable; they must not abort a shutdown.

    Read together with test_kill_all_reports_only_the_processes_it_actually_
    killed, which is the other half of the same contract: kill_all() used to
    return every pid it *attempted*, and this battery pinned ("moonraker",
    4322) for a pid os.kill refused with EPERM. A refused signal is not a
    killed process -- `klipperformac killall`/`uninstall` would tell the user a
    stranger's pid is down while it keeps holding the ports -- so the honest
    report is the empty list, and the shutdown still has to run to completion.
    """
    proc.write_state(klipper=4321)
    proc.system.dead.add(4321)                    # os.kill -> ProcessLookupError
    assert proc.p.stop() is True                  # no raise, state cleared

    proc.write_state(moonraker=4322)
    proc.ps.add(4322, cmdline_at(str(proc.home), "moonraker"))
    proc.system.foreign.add(4322)                 # os.kill -> PermissionError
    assert proc.p.kill_all() == []                # nothing claimed as killed
    assert (4322, signal.SIGKILL) in proc.system.signals   # it was attempted
    assert not proc.paths.STATE.exists()          # and the run finished


def test_state_keys_that_are_not_services_are_never_signalled(proc):
    proc.write_state(klipper=4321, cam_pid=4399, evil="moonraker")
    proc.alive(klipper=4321)
    proc.ps.add(4399, cmdline_at(DEFAULT_HOME, "moonraker"))
    proc.p.stop()
    assert [pid for pid, _ in proc.system.signals] == [4321]


def test_kill_all_combines_tracked_and_scanned_strays(proc):
    proc.write_state(klipper=4321)
    proc.alive(klipper=4321)
    strays = proc.scan_with("web_mainsail", "moonraker")
    killed = proc.p.kill_all()
    assert killed == sorted([("klipper", 4321)] +
                            [("stray", pid) for pid in strays])
    assert {pid for pid, _ in proc.system.signals} == set([4321] + strays)
    assert {sig for _, sig in proc.system.signals} == {signal.SIGKILL}
    assert not proc.paths.STATE.exists()


def test_kill_all_still_clears_state_when_something_ignores_sigkill(proc):
    proc.write_state(klipper=4321)
    proc.ps.add(4321, cmdline_at(DEFAULT_HOME, "klipper"), policy="immune")
    assert proc.p.kill_all() == [("klipper", 4321)]
    assert not proc.paths.STATE.exists()


def test_kill_all_with_nothing_to_do_is_a_no_op(proc):
    assert proc.p.kill_all() == []
    assert proc.system.signals == []
    assert not proc.paths.STATE.exists()


def test_a_pid_listed_by_state_and_scan_is_signalled_once(proc):
    """The tracked and scanned views of the same process must not double-tap."""
    proc.write_state(klipper=4321)
    proc.alive(klipper=4321)
    proc.ps.scan = ps_eo_text((4321, cmdline_at(DEFAULT_HOME, "klipper")),
                              (4321, cmdline_at(DEFAULT_HOME, "klipper")))
    assert proc.p.kill_all() == [("klipper", 4321)]
    assert proc.system.signals == [(4321, signal.SIGKILL)]


@settings(max_examples=30, database=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.dictionaries(st.sampled_from(["klipper", "moonraker", "web_mainsail",
                                        "web_fluidd", "caffeinate"]),
                       st.integers(min_value=100, max_value=MAXPID),
                       max_size=5))
def test_only_pids_listed_in_the_state_file_are_ever_signalled(proc, pids):
    """stop() must never signal a pid it invented, nor an exotic signal."""
    proc.reset()
    proc.write_state(**pids)
    for name, pid in pids.items():
        proc.ps.add(pid, " ".join(proc.argv_at(str(proc.home), name)))
    proc.p.stop()
    assert {pid for pid, _ in proc.system.signals} <= set(pids.values())
    assert {sig for _, sig in proc.system.signals} <= {signal.SIGTERM,
                                                       signal.SIGKILL}


# --------------------------------------------------------------------------
# BUGS
# --------------------------------------------------------------------------

def test_stop_escalates_a_service_that_ignores_sigterm_to_sigkill(proc):
    """A hung klipper that traps SIGTERM must be killed once the timeout is up.

    The same escalation exists and works in _reap_orphans()
    (process.py:170-171); stop()'s version of it is unreachable, so `down`
    waits out the timeout and then forgets about the processes entirely.
    """
    proc.write_state(klipper=4321)
    proc.ps.add(4321, cmdline_at(DEFAULT_HOME, "klipper"), policy="kill")
    proc.p.stop(timeout=1.0)
    assert (4321, signal.SIGKILL) in proc.system.signals, (
        "a service that survives SIGTERM must be escalated to SIGKILL, got %r"
        % proc.system.signals)


def test_stop_reports_failure_when_a_service_refuses_to_die(proc):
    proc.write_state(klipper=4321)
    proc.ps.add(4321, cmdline_at(DEFAULT_HOME, "klipper"), policy="immune")
    assert proc.p.stop(timeout=1.0) is False, (
        "stop() reported success on a live, un-stoppable stack")
    assert proc.paths.STATE.exists(), (
        "state.json must survive a failed stop so the next down/killall still "
        "knows about pid 4321")


def test_stop_revalidates_the_cmdline_before_signalling_a_tracked_pid(proc):
    """PID reuse must not turn `klipperformac down` into a kill of innocent apps.

    process.py's own docstring promises identity is "re-validated on every read
    (pid + cmdline marker + boot time)": the read behind running() does, the
    read behind stop() does not.
    """
    proc.write_state(moonraker=5150, klipper=5151)
    proc.ps.add(5151, cmdline_at(DEFAULT_HOME, "klipper"))
    # 5150 has been recycled by something emphatically not moonraker.
    proc.ps.add(5150, "/Applications/Calculator.app/Contents/MacOS/Calculator")
    proc.p.stop()
    signalled = {pid for pid, _ in proc.system.signals}
    assert 5150 not in signalled, (
        "stop() signalled a recycled pid; it must only signal processes whose "
        "command line still carries the service marker")


def test_kill_all_does_not_sigkill_a_recycled_pid(proc):
    proc.write_state(klipper=5150)
    proc.ps.add(5150,
                "/System/Library/CoreServices/Finder.app/Contents/MacOS/Finder")
    proc.p.kill_all()
    assert (5150, signal.SIGKILL) not in proc.system.signals


BAD_PID_TYPES = ["5150", "abc", 5150.0, 51.5, [5150], {"pid": 5150}, 10 ** 100,
                 " 5150"]


@pytest.mark.parametrize("pid", BAD_PID_TYPES)
def test_a_foreign_pid_type_does_not_crash_the_shutdown_paths(proc, pid):
    """A garbage pid field must read as "not our process", not as a traceback.

    state.json is plain JSON a human can edit and other writers can rewrite --
    the SwiftUI app reads it directly (KlipperMac/Sources/KlipperMac/Core/
    StackReader.swift) and any tool may restore one; _save() writing ints is
    not a guarantee about the bytes found on the next read.
    """
    proc.write_state(klipper=pid)
    raised = []
    for call in (proc.p.running, proc.p.stop, proc.p.kill_all):
        try:
            call()
        except Exception as exc:                       # noqa: BLE001 - the point
            raised.append("%s(%r)" % (type(exc).__name__, exc))
    assert raised == [], "shutdown path raised %r" % raised


IMPOSSIBLE_PIDS = [-1, -4321, True, 999999999, 2 ** 31 - 1]


@pytest.mark.parametrize("pid", IMPOSSIBLE_PIDS)
def test_a_non_positive_or_out_of_range_pid_is_never_signalled(proc, pid):
    """pid <= 0 addresses a process *group*, and absurd pids are not processes."""
    proc.write_state(klipper=pid)
    proc.p.stop()
    proc.p.kill_all()
    assert proc.system.signals == [], (
        "state.json pid %r reached os.kill; a corrupt state file must never be "
        "able to signal a process group or a system process" % (pid,))


def test_kill_all_reports_only_the_processes_it_actually_killed(proc):
    proc.write_state(moonraker=1, klipper=4321, web_fluidd=99999)
    proc.ps.add(4321, cmdline_at(DEFAULT_HOME, "klipper"))
    proc.system.foreign.add(1)                          # launchd: not ours to kill
    proc.system.dead.add(99999)                         # does not exist at all
    killed = proc.p.kill_all()
    assert killed == [("klipper", 4321)], (
        "kill_all() claimed %r; its docstring promises the list of *killed* "
        "(name, pid)" % (killed,))
