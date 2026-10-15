"""The reads behind ``klipperformac/process.py``'s decisions: ``ps -p``,
``sysctl -n kern.boottime`` and ``running()``.

``running()`` is the single answer every surface trusts -- ``status`` prints it,
``up``/``restart`` branch on it, and ``doctor`` warns from it -- so it must
report exactly the processes this install started, and it must degrade to
"unknown" rather than to a destructive answer whenever the evidence is thin.

Fully offline: ``ps``/``sysctl``/``os.kill`` are faked inside the module under
test with the semantics measured on this Mac (see FakePs.run), the process
table is fabricated, ``run/state.json`` always lives under pytest's
``tmp_path``, and no real process is ever signalled.
"""
import json
import os
import subprocess
import types

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

BOOT = "1791932927"
SYSCTL_OK = ("{ sec = %s, usec = 560431 } Tue Oct 13 16:08:47 2026\n"
             % BOOT).encode()
MAXPID = 99999          # measured: `ps -p 100000` -> "ps: process id too large"


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
    """sysctl + ps, matching the behaviour measured on macOS:

    * ``ps -p 1,abc`` / ``ps -p 1,100000`` print NOTHING at all (the whole
      batch fails on a single unusable token),
    * ``ps -p 1,99999`` prints the rows for pids that exist and nothing for
      the ones that do not,
    * a zombie is printed with a ``Z`` in the state column.
    """

    DEVNULL = subprocess.DEVNULL

    def __init__(self):
        self.rows = {}
        self.scan = b""
        self.sysctl = SYSCTL_OK
        self.sysctl_rc = 0
        self.sysctl_error = None       # e.g. FileNotFoundError (no sysctl)
        self.ps_error = None           # e.g. ps missing from PATH
        self.calls = []

    def Popen(self, cmd, **kwargs):
        raise AssertionError("offline harness must never spawn a process")

    def add(self, pid, cmd, state="Ss"):
        self.rows[str(pid)] = {"state": state, "cmd": cmd}
        return pid

    def drop(self, pid):
        self.rows.pop(str(pid), None)

    def handle_signal(self, pid, sig):
        self.drop(pid)

    def run(self, cmd, *args, **kwargs):
        argv = [str(c) for c in cmd]
        self.calls.append(argv)
        if argv[0] == "sysctl":
            if self.sysctl_error:
                raise self.sysctl_error
            if self.sysctl_rc and kwargs.get("check"):
                raise subprocess.CalledProcessError(self.sysctl_rc, argv)
            return Completed(self.sysctl, self.sysctl_rc)
        if argv[0] == "ps" and self.ps_error:
            raise self.ps_error
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

    def pid_queries(self):
        return [c for c in self.calls if c[0] == "ps" and "-p" in c]

    def sysctl_calls(self):
        return [c for c in self.calls if c[0] == "sysctl"]


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
def reads(stack, monkeypatch):
    """process.py bound to the throwaway home with ps/sysctl/os.kill faked."""
    ps = FakePs()
    system = FakeOS(on_kill=ps.handle_signal)
    clock = Clock()
    monkeypatch.setattr(stack.process, "subprocess", ps)
    monkeypatch.setattr(stack.process, "time", clock)
    monkeypatch.setattr(stack.process, "os", system)
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

    def raw_state(doc):
        stack.paths.STATE.write_text(json.dumps(doc))
        return doc

    def live_cmds(*pids):
        return stack.process._live_cmds(list(pids))

    real_run = ps.run

    def set_ps_stdout(raw):
        """Answer the next `ps -p` query with exactly these bytes, whatever
        the requested pids were -- the way a truncated or localised ps would."""
        data = raw.encode("utf-8", "surrogatepass") if isinstance(raw, str) else raw

        def run(cmd, *a, **kw):
            argv = [str(c) for c in cmd]
            ps.calls.append(argv)
            if argv[0] == "sysctl":
                return real_run(cmd, *a, **kw)
            if argv[0] == "ps" and "-p" in argv:
                return Completed(data, 0)
            return Completed(ps.scan, 0)

        ps.run = run

    def restore_ps():
        ps.run = real_run

    return types.SimpleNamespace(p=stack.process, paths=stack.paths, ps=ps,
                                 system=system, clock=clock, home=stack.home,
                                 write_state=write_state, alive=alive,
                                 raw_state=raw_state, live=live_cmds,
                                 set_ps_stdout=set_ps_stdout,
                                 restore_ps=restore_ps)


# --------------------------------------------------------------------------
# _live_cmds: one ps call, only what ps actually reported
# --------------------------------------------------------------------------

def test_live_cmds_reports_only_pids_ps_answered_for(reads):
    reads.alive(klipper=4321, moonraker=4322)
    result = reads.live(4321, 4322, 4323)
    assert set(result) == {"4321", "4322"}
    assert result["4321"] == cmdline_at(str(reads.home), "klipper")
    assert len(reads.ps.pid_queries()) == 1, "one batched ps call, not one per pid"


def test_live_cmds_never_asks_ps_about_nothing(reads):
    assert reads.live(0, None, "", [], {}, False, 0.0) == {}
    assert reads.live() == {}
    assert reads.ps.calls == []


@pytest.mark.parametrize("state", ["Z", "Z+", "Zs"])
def test_zombies_are_not_alive(reads, state):
    """A SIGKILLed-but-never-reaped child still shows in ps; it is gone."""
    reads.ps.add(4321, cmdline_at(str(reads.home), "klipper"), state=state)
    assert reads.live(4321) == {}


@pytest.mark.parametrize("junk", [
    "", "   ", "\n\n\n", "7", "7 Ss", "pid command", "not-a-table at all",
    "\x00\x01\x02", "   7   Ss", "Ss /bin/sh", "7 X", "-1 Ss /bin/sh",
    "7 Ss \t\t\t", "٠١٢ Ss moonraker", "7 Ss\N{SNOWMAN}",
])
def test_garbage_ps_text_reads_as_no_process_at_all(reads, junk):
    """ps can be truncated, localised, or replaced; none of that may raise, and
    nothing ps did not report for *our* pid may be reported as our pid."""
    reads.set_ps_stdout(junk.encode("utf-8", "surrogatepass"))
    reads.alive(klipper=4321)                     # the real row, if ps lies
    try:
        assert reads.live(4321).get("4321") is None
        reads.write_state(klipper=4321)
        assert reads.p.running() == {name: False for name in reads.p.SERVICES}
    finally:
        reads.restore_ps()


def test_a_ps_that_cannot_be_executed_reads_as_unknown(reads):
    for error in (FileNotFoundError(2, "No such file or directory", "ps"),
                  PermissionError(13, "Operation not permitted", "ps"),
                  OSError(12, "Cannot allocate memory", "ps")):
        reads.ps.ps_error = error
        assert reads.live(4321) == {}
        reads.ps.ps_error = None
    assert reads.p.running() is None       # no state file in this test


def test_unicode_and_very_long_command_lines_are_parsed(reads):
    long_cmd = cmdline_at(str(reads.home), "moonraker") + " " + ("x" * 400000)
    reads.ps.add(5001, long_cmd)
    reads.ps.add(5002, cmdline_at(str(reads.home), "klipper") + " \N{SNOWMAN}")
    result = reads.live(5001, 5002)
    assert result["5001"].startswith(cmdline_at(str(reads.home), "moonraker"))
    assert len(result["5001"]) > 400000
    assert "klippy.py" in result["5002"]


def test_padding_and_internal_spaces_are_survived(reads):
    """The proxy marker needs the space after the port to stay put."""
    cmd = " ".join(argv_at(str(reads.home), "web_mainsail"))
    reads.set_ps_stdout("  6001    Ss+    %s\n" % cmd)
    try:
        result = reads.live(6001)
    finally:
        reads.restore_ps()
    assert result["6001"] == cmd
    assert "klipperformac.proxy %d " % reads.paths.WEB_PORT in result["6001"]


@settings(max_examples=50, database=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(st.text(max_size=80), max_size=8))
def test_live_cmds_never_raises_on_any_ps_output(reads, lines):
    """Whatever ps prints, the read is a dict of str -> str and never a crash."""

    reads.set_ps_stdout("\n".join(lines) + "\n")
    try:
        result = reads.p._live_cmds([4321, 4322])
    finally:
        reads.restore_ps()
    assert isinstance(result, dict)
    assert all(isinstance(k, str) and isinstance(v, str)
               for k, v in result.items())


@settings(max_examples=40, database=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.dictionaries(st.integers(min_value=1, max_value=MAXPID),
                       st.sampled_from(["Ss", "R+", "U", "Z+", "T"]),
                       max_size=5))
def test_live_cmds_answers_exactly_for_the_live_subset(reads, rows):
    pids = sorted(set(rows))[:5]
    reads.ps.rows.clear()
    for pid, state in rows.items():
        reads.ps.add(pid, cmdline_at(str(reads.home), "klipper"), state=state)
    result = reads.p._live_cmds(pids)
    expect = {str(pid) for pid, state in rows.items()
              if "Z" not in state and pid in pids}
    assert set(result) == expect


# --------------------------------------------------------------------------
# kern.boottime: parsing and the 5 second memo
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expect", [
    (SYSCTL_OK, BOOT),
    (b"{ sec = 1700000000, usec = 1 }", "1700000000"),
    (b"sec = 12, usec = 34\n", "12"),
    (b"{ sec = 111, usec = 1 } { sec = 222, usec = 2 }", "111"),
    (b"kern.boottime: { sec = 4242, usec = 0 } Wed Jan  1 00:00:00 2025", "4242"),
    (b"", None),
    (b"no numbers here", None),
    (b"seconds = 1700000000", None),
    (b"sec = \xc4\xb1", None),
])
def test_boot_time_parsing(reads, raw, expect):
    reads.ps.sysctl = raw
    reads.p._boot_cache[:] = [0.0, None]
    assert reads.p._boot_time() == expect


def test_boot_time_is_memoised_for_five_seconds(reads):
    for _ in range(5):
        reads.p._boot_time()
        reads.clock.t += 1.0
    assert len(reads.ps.sysctl_calls()) == 1
    reads.clock.t += 5.0
    reads.p._boot_time()
    assert len(reads.ps.sysctl_calls()) == 2


@pytest.mark.parametrize("error", [
    FileNotFoundError(2, "No such file or directory", "sysctl"),
    PermissionError(13, "Operation not permitted", "sysctl"),
    OSError(12, "Cannot allocate memory", "sysctl"),
])
def test_a_sysctl_that_cannot_run_is_answered_with_unknown(reads, error):
    """_boot_time() must not raise: doctor/status call it on every repaint."""
    reads.ps.sysctl_error = error
    reads.p._boot_cache[:] = [0.0, None]
    assert reads.p._boot_time() is None


def test_a_nonzero_sysctl_exit_is_answered_with_unknown(reads):
    reads.ps.sysctl_rc = 1
    reads.p._boot_cache[:] = [0.0, None]
    assert reads.p._boot_time() is None


def test_running_is_none_without_a_state_file(reads):
    assert reads.p.running() is None
    assert reads.ps.pid_queries() == []


def test_running_requires_the_marker_on_that_exact_pid(reads):
    reads.write_state(klipper=4321, moonraker=4322)
    reads.alive(klipper=4321)
    # 4322 exists, is ours to see, but is emphatically not moonraker
    reads.ps.add(4322, "/Applications/Calculator.app/Contents/MacOS/Calculator")
    state = reads.p.running()
    assert state == {"klipper": True, "moonraker": False,
                     "web_mainsail": False, "web_fluidd": False,
                     "caffeinate": False}


def test_running_does_not_confuse_two_services_sharing_a_pid(reads):
    """A hand-edited file may name one pid twice; only the matching service
    may be reported up."""
    reads.write_state(klipper=4321, moonraker=4321)
    reads.alive(klipper=4321)
    state = reads.p.running()
    assert state["klipper"] is True
    assert state["moonraker"] is False


def test_two_services_whose_pids_were_swapped_are_both_down(reads):
    """A state file that names each service the other's pid proves nothing;
    neither marker may vouch for the wrong process."""
    reads.write_state(klipper=4321, moonraker=4322)
    reads.ps.add(4321, cmdline_at(str(reads.home), "moonraker"))
    reads.ps.add(4322, cmdline_at(str(reads.home), "klipper"))
    state = reads.p.running()
    assert state["klipper"] is False
    assert state["moonraker"] is False


@pytest.mark.parametrize("shape", ["unreadable-dir", "symlink-to-dev-null",
                                   "empty-file", "directory"])
def test_a_state_file_that_cannot_be_read_is_answered_as_unknown(reads, shape,
                                                                 tmp_path):
    """Broken/inaccessible state must read as "nothing known", never raise."""
    if shape == "unreadable-dir":
        if os.geteuid() == 0:
            pytest.skip("root ignores directory modes")
        reads.write_state(klipper=4321)
        reads.alive(klipper=4321)
        reads.paths.RUN.chmod(0o000)
        try:
            assert reads.p.running() is None
            assert reads.p.stop() is False
        finally:
            reads.paths.RUN.chmod(0o755)
        return
    if shape == "symlink-to-dev-null":
        target = tmp_path / "null-link"
        if target.is_symlink() or target.exists():
            target.unlink()
        target.symlink_to(os.devnull)
        reads.paths.STATE.unlink(missing_ok=True)
        reads.paths.STATE.symlink_to(target)
        assert reads.p.running() is None
        assert reads.p.stop() is False
        return
    if shape == "empty-file":
        reads.paths.STATE.write_text("")
    else:
        reads.paths.STATE.mkdir(parents=True, exist_ok=True)
    assert reads.p.running() is None
    assert reads.p.stop() is False


def test_a_state_file_beyond_the_size_of_a_real_one_still_reads(reads):
    """2 MB of padding around a valid document must not stall the read."""
    doc = {"boot_time": BOOT, "klipper": 4321}
    doc.update({"filler_%d" % i: "y" * 200 for i in range(8000)})
    reads.raw_state(doc)
    reads.alive(klipper=4321)
    assert reads.p._load()["klipper"] == 4321
    assert reads.p.running()["klipper"] is True


def test_running_ignores_a_state_from_another_boot(reads):
    reads.raw_state({"boot_time": "1600000000", "klipper": 4321})
    reads.alive(klipper=4321)
    assert reads.p.running() is None


def test_running_asks_ps_once_for_the_whole_stack(reads):
    pids = {name: 4321 + i for i, name in enumerate(reads.p.SERVICES)}
    reads.write_state(**pids)
    reads.alive(**pids)
    reads.p.running()
    queries = reads.ps.pid_queries()
    assert len(queries) == 1
    assert sorted(queries[0][queries[0].index("-p") + 1].split(",")) == \
        sorted(str(p) for p in pids.values())


# --------------------------------------------------------------------------
# BUGS
# --------------------------------------------------------------------------

@pytest.mark.xfail(strict=True,
                   reason="BUG DEEP: a single pid value ps cannot use ('abc', "
                          "43.7, 999999999) aborts the whole batched `ps -p` "
                          "query, so _live_cmds() returns {} and EVERY service "
                          "reads as down (process.py:88-101); status prints the "
                          "stack as stopped, down refuses to act, and up then "
                          "clears the state and starts a second stack on top of "
                          "the live one")
def test_one_unusable_pid_does_not_make_the_whole_stack_look_dead(reads):
    """The batch query may not let one bad token speak for the good ones."""
    reads.write_state(klipper=4321, moonraker=999999999)
    reads.alive(klipper=4321, moonraker=999999999)
    state = reads.p.running()
    assert state["klipper"] is True, (
        "klipper is alive and marked as ours, yet running() says %r" % state)
    assert state["moonraker"] is False


@pytest.mark.xfail(strict=True,
                   reason="BUG DEEP: the same batch poisoning happens for a "
                          "non-numeric pid field, which is exactly what a "
                          "truncated or hand-edited state.json contains "
                          "(process.py:88-101)")
def test_a_non_numeric_pid_does_not_blind_the_read_of_the_other_services(reads):
    reads.write_state(klipper=4321, moonraker="moonraker")
    reads.alive(klipper=4321)
    assert reads.p.running()["klipper"] is True


@pytest.mark.xfail(strict=True,
                   reason="BUG DEEP: when the boot time cannot be read -- sysctl "
                          "missing from PATH, non-zero exit, a transient fork "
                          "failure -- _load() deletes run/state.json instead of "
                          "just answering 'unknown' (process.py:63-65), so a "
                          "live printing stack becomes invisible to status/down "
                          "and the next `up` reaps (kills) it mid-print")
def test_a_transient_boot_time_failure_does_not_erase_the_state_file(reads):
    reads.write_state(klipper=4321)
    reads.alive(klipper=4321)
    assert reads.p.running()["klipper"] is True
    reads.ps.sysctl_error = FileNotFoundError(2, "No such file or directory",
                                              "sysctl")
    reads.p._boot_cache[:] = [0.0, None]
    assert reads.p.running() is None          # unknown is fine ...
    reads.ps.sysctl_error = None
    reads.p._boot_cache[:] = [0.0, None]
    assert reads.paths.STATE.exists(), (
        "a single unreadable kern.boottime destroyed the record of a live stack")
    assert reads.p.running()["klipper"] is True, (
        "the state file must still describe the running stack after recovery")


@pytest.mark.xfail(strict=True,
                   reason="BUG DEEP: _boot_time() greps the unanchored pattern "
                          "'sec = (\\d+)', which also matches inside 'usec = ', so "
                          "a sysctl answer whose sec field is missing or garbled "
                          "yields the microsecond value as the boot time "
                          "(process.py:49); _load() then compares that number "
                          "against state.json, calls it a different boot and "
                          "deletes a live stack's record instead of reporting "
                          "'unknown'")
def test_boot_time_is_not_read_out_of_the_usec_field(reads):
    reads.ps.sysctl = b"kern.boottime: { sec = , usec = 123456 } ?"
    reads.p._boot_cache[:] = [0.0, None]
    assert reads.p._boot_time() is None, (
        "the usec field was parsed as if it were the boot time: %r"
        % reads.p._boot_time())


@pytest.mark.xfail(strict=True,
                   reason="BUG DEEP: _save() persists boot_time=null when "
                          "kern.boottime is unreadable at start() time "
                          "(process.py:79); _load() then rejects that document "
                          "on every later read (process.py:64) and deletes it, "
                          "so the just-started stack is permanently untracked -- "
                          "status says stopped, down says 'was not running', and "
                          "up starts a duplicate stack that cannot bind")
def test_a_state_saved_while_the_boot_time_was_unreadable_is_still_usable(reads):
    reads.ps.sysctl = b"kern.boottime: unknown\n"     # no `sec =` in the output
    reads.p._boot_cache[:] = [0.0, None]
    reads.p._save({"klipper": FakeProc(4321)})
    doc = json.loads(reads.paths.STATE.read_text())
    assert doc.get("boot_time") is not None, (
        "state.json was written with boot_time null: %r" % doc)
    reads.ps.sysctl = SYSCTL_OK
    reads.p._boot_cache[:] = [0.0, None]
    reads.alive(klipper=4321)
    assert reads.p._load() is not None
