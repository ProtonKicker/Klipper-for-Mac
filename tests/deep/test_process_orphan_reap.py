"""``_scan_service_pids()`` / ``_reap_orphans()`` in ``klipperformac/process.py``.

The orphan reaper is what lets ``klipperformac up`` recover after a crash, a
reboot or a lost state file: it reads a full ``ps -eo pid=,command=`` listing
and SIGTERMs -- then SIGKILLs -- whatever it believes is a klipperformac
service that no live state file tracks. Since it acts on *untracked* pids it
has to be exactly right about identity: too narrow and a squatted port blocks
every later start, too wide and it kills the user's editor.

Offline: ``ps``/``sysctl``/``os.kill`` are faked inside the module under test,
the process table is fabricated text, and no real process is ever signalled --
the tests assert which ``(pid, signal)`` pairs the module WOULD send. Nothing
here opens anything outside pytest's ``tmp_path``.
"""
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
# The default install layout. _scan_service_pids() recognises command lines by
# this literal string (process.py:146), so a `ps -eo` row written in this shape
# is what a real user's machine would produce.
DEFAULT_HOME = "/Users/peter/.klipperformac"
OTHER_HOME = "/Users/guest/.klipperformac"


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
    """`sysctl -n kern.boottime`, `ps -p ... -o pid=,state=,command=` and
    `ps -eo pid=,command=`, with the macOS semantics measured by hand: an
    unparseable or out-of-range pid token empties the whole -p query."""

    DEVNULL = subprocess.DEVNULL

    def __init__(self):
        self.rows = {}
        self.scan = b""
        self.sysctl = SYSCTL_OK
        self.sysctl_rc = 0
        self.calls = []
        self.popens = []
        self._next_pid = 40000

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
        if argv[0] == "sysctl":
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

    def ps_calls(self):
        return [c for c in self.calls if c[0] == "ps"]

    def scan_calls(self):
        return [c for c in self.calls
                if c[0] == "ps" and "-p" not in c and "-eo" in c]


class FakeOS:
    def __init__(self, on_kill=None):
        self.signals = []
        self.foreign = set()
        self.dead = set()
        self._on_kill = on_kill

    def kill(self, pid, sig):
        if not isinstance(pid, int):
            raise TypeError("an integer is required (got type %s)"
                            % type(pid).__name__)
        self.signals.append((pid, sig))
        if pid in self.dead:
            raise ProcessLookupError(3, "No such process")
        if pid in self.foreign:
            raise PermissionError(1, "Operation not permitted")
        if self._on_kill:
            self._on_kill(pid, sig)

    def waitpid(self, pid, options):
        raise ChildProcessError(10, "No child processes")

    def __getattr__(self, name):
        return getattr(os, name)


def argv_at(home, name, web_port=8080, fluidd_port=8081, moonraker_port=7125):
    """The argv process.start() spawns for `name` in an install at `home`."""
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


def ps_eo(*pairs):
    """Fabricate `ps -eo pid=,command=` text from (pid, command) pairs."""
    return "".join("%6d %s\n" % (pid, cmd) for pid, cmd in pairs).encode()


@pytest.fixture
def scan(stack, monkeypatch):
    """process.py with a fabricated process table; signals are recorded only."""
    ps = FakePs()
    system = FakeOS(on_kill=ps.handle_signal)
    clock = Clock()
    monkeypatch.setattr(stack.process, "subprocess", ps)
    monkeypatch.setattr(stack.process, "time", clock)
    monkeypatch.setattr(stack.process, "os", system)
    stack.process._boot_cache[:] = [0.0, None]
    stack.paths.RUN.mkdir(parents=True, exist_ok=True)
    return types.SimpleNamespace(p=stack.process, paths=stack.paths, ps=ps,
                                 system=system, clock=clock, home=stack.home)


# --------------------------------------------------------------------------
# identity rules that already hold
# --------------------------------------------------------------------------

def test_untracked_services_of_a_default_install_are_found(scan):
    want = {}
    rows = []
    for offset, name in enumerate(("klipper", "moonraker", "web_mainsail",
                                   "web_fluidd")):
        pid = 7000 + offset
        want[pid] = name
        rows.append((pid, cmdline_at(DEFAULT_HOME, name)))
    scan.ps.scan = ps_eo(*rows)
    assert set(scan.p._scan_service_pids()) == set(want)


def test_the_reaper_never_signals_the_process_running_it(scan):
    """`ps -eo` always contains the CLI itself; self-signalling would abort the
    very command that is reaping."""
    scan.ps.scan = ps_eo((os.getpid(), cmdline_at(DEFAULT_HOME, "klipper")),
                         (7101, cmdline_at(DEFAULT_HOME, "moonraker")))
    assert scan.p._scan_service_pids() == [7101]


@pytest.mark.parametrize("cmd", [
    "/System/Library/CoreServices/Finder.app/Contents/MacOS/Finder",
    "/usr/sbin/cfprefsd daemon",
    "/bin/zsh -l",
    DEFAULT_HOME + "/venv/bin/python -m klipperformac status",
    DEFAULT_HOME + "/venv/bin/python -m klipperformac doctor",
    "/Applications/PrinterThing.app/Contents/MacOS/klipper-imitator",
])
def test_unrelated_processes_are_not_reported(scan, cmd):
    """Noise on a real machine must never end up in a kill list."""
    scan.ps.scan = ps_eo((7200, cmd))
    assert scan.p._scan_service_pids() == []
    scan.p._reap_orphans()
    assert scan.system.signals == []


@pytest.mark.parametrize("cmd", [
    "caffeinate -dis",
    "caffeinate -i",
    "/usr/bin/caffeinate -t 3600",
    DEFAULT_HOME + "/bin/caffeinate --help",
])
def test_someone_elses_caffeinate_is_never_a_service(scan, cmd):
    """process.py deliberately skips the caffeinate marker in the scan because
    the name "may belong to other tools"; only OUR exact invocation counts."""
    scan.ps.scan = ps_eo((7300, cmd))
    assert scan.p._scan_service_pids() == []
    assert scan.p._scan_service_pids(with_caffeinate=True) == \
        ([7300] if cmd == "caffeinate -dis" else [])


def test_reap_does_nothing_at_all_when_no_strays_are_found(scan):
    scan.ps.scan = ps_eo((7400, "/sbin/launchd"))
    scan.p._reap_orphans()
    assert scan.system.signals == []
    assert scan.clock.slept == 0.0
    assert [c for c in scan.ps.calls if c[0] == "ps" and "-p" in c] == []


def test_reap_escalates_stubborn_strays_to_sigkill(scan):
    strays = [7501, 7502]
    scan.ps.scan = ps_eo((7501, cmdline_at(DEFAULT_HOME, "moonraker")),
                         (7502, cmdline_at(DEFAULT_HOME, "klipper")))
    scan.ps.add(7501, cmdline_at(DEFAULT_HOME, "moonraker"), policy="term")
    scan.ps.add(7502, cmdline_at(DEFAULT_HOME, "klipper"), policy="kill")
    scan.p._reap_orphans()
    assert (7501, signal.SIGTERM) in scan.system.signals
    assert (7502, signal.SIGTERM) in scan.system.signals
    assert (7502, signal.SIGKILL) in scan.system.signals, (
        "_reap_orphans is the reference implementation of the escalation; it "
        "does reach SIGKILL")
    assert (7501, signal.SIGKILL) not in scan.system.signals


def test_reap_does_not_wait_forever_on_a_pid_that_vanishes(scan):
    """A stray that dies between the scan and the liveness check must be left
    alone rather than SIGKILLed (SIGKILL to a recycled pid is a live hazard)."""
    scan.ps.scan = ps_eo((7601, cmdline_at(DEFAULT_HOME, "web_mainsail")))
    scan.p._reap_orphans()                       # not in the -p table at all
    assert scan.system.signals == [(7601, signal.SIGTERM)]
    assert scan.clock.slept == pytest.approx(0.2)


@settings(max_examples=40, database=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(st.tuples(st.integers(min_value=-5, max_value=100000),
                          st.text(alphabet=st.characters(
                              exclude_characters="\n\r"), max_size=60)),
                max_size=12))
def test_the_scan_never_invents_a_pid_it_cannot_justify(scan, rows):
    """No `ps -eo` text of any shape may make the reaper signal a pid that is
    not on the printed list, make it signal this very process, or raise."""
    scan.ps.scan = ps_eo(*rows)
    scan.system.signals.clear()
    printed = {pid for pid, _ in rows}
    found = scan.p._scan_service_pids()
    assert os.getpid() not in found
    assert all(isinstance(pid, int) for pid in found)
    assert set(found) <= printed
    scan.p._reap_orphans()
    assert {pid for pid, _ in scan.system.signals} <= printed


# --------------------------------------------------------------------------
# BUGS
# --------------------------------------------------------------------------

@pytest.mark.xfail(strict=True,
                   reason="BUG DEEP: _scan_service_pids() recognises services by "
                          "the literal string '/.klipperformac/' (process.py:146) "
                          "instead of paths.APP_HOME/paths.DATA, so an install "
                          "with a relocated KLIPPERFORMAC_HOME (supported by "
                          "paths.py:10 and by the SwiftUI app's Environment.swift) "
                          "never sees its own strays: _reap_orphans() is a no-op, "
                          "so a crashed proxy/klipper keeps 7125/8080/8081 and the "
                          "serial port and every later `up` fails to bind")
def test_orphans_of_a_relocated_app_home_are_reaped(scan):
    """`klipperformac up` must clear its own leftovers wherever it is installed.

    The command lines below are exactly what this install's own start() spawns;
    the only difference from a default install is where the home lives.
    """
    scan.ps.scan = ps_eo(*[(8100 + i, cmdline_at(str(scan.home), name))
                           for i, name in enumerate(("klipper", "moonraker",
                                                     "web_mainsail"))])
    found = scan.p._scan_service_pids()
    assert sorted(found) == [8100, 8101, 8102], (
        "this install's own orphans are invisible to its own reaper: %r" % found)


@pytest.mark.xfail(strict=True,
                   reason="BUG DEEP: the same literal-string gate makes the scan "
                          "match *anyone's* ~/.klipperformac processes, so a "
                          "second install (another account, or a beta home) has "
                          "its stack SIGTERM/SIGKILLed by this one's "
                          "_reap_orphans()/kill_all() (process.py:146-153)")
def test_reaping_never_touches_a_different_install(scan):
    """Two installs on one Mac: `peter`'s reaper must leave `guest`'s stack up.

    Both command lines are genuine klipperformac services, so no marker check
    can tell them apart -- only the home prefix can, and the code compares it
    against a hardcoded string instead of its own paths.APP_HOME.
    """
    mine = [8200, 8201]
    theirs = [8202, 8203]
    scan.ps.scan = ps_eo(
        (8200, cmdline_at(str(scan.home), "klipper")),
        (8201, cmdline_at(str(scan.home), "moonraker")),
        (8202, cmdline_at(OTHER_HOME, "klipper")),
        (8203, cmdline_at(OTHER_HOME, "moonraker")))
    for pid in theirs:
        scan.ps.add(pid, cmdline_at(OTHER_HOME, "klipper"), policy="immune")
    found = scan.p._scan_service_pids()
    assert sorted(found) == mine, (
        "the scan reported %r; foreign-install pids %r must never be listed"
        % (sorted(found), theirs))
    scan.p._reap_orphans()
    assert not ({pid for pid, _ in scan.system.signals} & set(theirs)), (
        "_reap_orphans() signalled processes belonging to a different install: %r"
        % scan.system.signals)


@pytest.mark.xfail(strict=True,
                   reason="BUG DEEP: the moonraker marker is the bare word "
                          "'moonraker' (process.py:26) and the scan accepts any "
                          "command line that merely mentions a path under the "
                          "home, so `tail -f ~/.klipperformac/data/logs/"
                          "moonraker.log`, an editor on moonraker.conf or another "
                          "klipperformac command is SIGTERMed by `up` and "
                          "SIGKILLed by `killall` (process.py:146-153)")
def test_reaping_leaves_user_tools_that_mention_moonraker_alone(scan):
    """`klipperformac up` must not kill the user's terminal doing `tail -f`."""
    innocent = [
        (8300, "/usr/bin/tail -f %s/data/logs/moonraker.log" % DEFAULT_HOME),
        (8301, "/usr/bin/vim %s/data/config/moonraker.conf" % DEFAULT_HOME),
        (8302, "%s/venv/bin/python %s/bin/klipperformac logs moonraker"
         % (DEFAULT_HOME, DEFAULT_HOME)),
        (8303, "/bin/cat %s/data/web/mainsail/moonraker.js" % DEFAULT_HOME),
    ]
    scan.ps.scan = ps_eo(*innocent)
    found = scan.p._scan_service_pids()
    assert found == [], (
        "the reaper classified ordinary user commands as moonraker services: %r"
        % found)
    for pid, cmd in innocent:
        scan.ps.add(pid, cmd, policy="immune")
    scan.system.signals.clear()
    scan.p._reap_orphans()
    assert scan.system.signals == [], (
        "_reap_orphans() would signal the user's own tools: %r"
        % scan.system.signals)
