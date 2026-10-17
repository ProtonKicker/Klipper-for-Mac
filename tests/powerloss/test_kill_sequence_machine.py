"""The kill sequence as a state machine - offline, with the machine faked.

`klipperformac powerloss-test` SIGKILLs a printing klipper and then a printing
Moonraker on the bench board, and the integrator runs that (gated by
KLIPPERFORMAC_ALLOW_LIVE=1). What runs HERE is the same `run_kill_sequence()`
source, driven by a fake that records every contact with the machine: which pid
was killed, in what order, when the stack was rebuilt, with what `stack_up` the
verdict was asked. The markers and the journal are the REAL ones in a throwaway
data folder, so "the journal survived both kills" is a fact about this repo's
files, not about a mock.

This is the tier that catches the bugs a live run cannot show you cheaply: the
verdict asked while the stack still reads as up (silenced crash), the reconnect
never asserted, the sequence continuing after the journal stopped advancing, and
- the one every tool of this kind gets wrong - a test that ends with the board
killed and the ports held.
"""
import json

import pytest
from conftest import services_up, stats_payload, write_state

from klipperformac import powerloss


class Machine:
    """The fabricated bench: pids, liveness, what REST would say, and the log."""

    def __init__(self):
        self.pids = {"klipper": 601, "moonraker": 602, "web_mainsail": 603,
                     "web_fluidd": 604, "caffeinate": 605}
        self.dead = set()
        self.offset = 512
        self.printing = False
        self.klippy = "ready"
        self.orphans = []
        self.conflicts = []
        self.start_refused = False
        self.calls = []

    def log(self, verb, *args):
        self.calls.append((verb,) + args)
        return verb


class FakeHarness(powerloss.KillTestHarness):
    """Every machine contact, replaced by a recorder. Nothing here spawns,
    signals, binds or sleeps: the sequence's only real code is powerloss itself."""

    def __init__(self, ns, machine):
        self.ns = ns
        self.pl = ns.powerloss
        self.machine = machine

    # ------------------------------------------------------------------ setup --
    def enable_journal(self):
        self.machine.log("enable_journal")
        return self.pl.save_settings(journal=True)

    def write_test_gcode(self):
        folder = self.ns.paths.DATA / "gcodes"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / powerloss.TEST_GCODE_NAME).write_text(
            self.pl.test_gcode_text())
        self.machine.log("write_test_gcode")
        return powerloss.TEST_GCODE_NAME

    # ------------------------------------------------------------------ print --
    def start_print(self, name):
        self.machine.log("start_print", name)
        if self.machine.start_refused:
            return False
        self.machine.printing = True
        return True

    def wait_printing(self, timeout=40.0):
        if not self.machine.printing:
            return None
        return {"state": "printing", "file_position": self.machine.offset}

    def print_state(self):
        return "printing" if self.machine.printing else "standby"

    def journal_tick(self):
        self.machine.log("journal_tick")
        self.machine.offset += 256
        payload = stats_payload(position=self.machine.offset)
        self.pl._http_get = lambda path, timeout=3: payload
        return self.pl.journal_tick()

    def journal_records(self):
        return self.pl.read_records()

    def pause(self, seconds):
        self.machine.log("pause", seconds)

    # ------------------------------------------------------------------ death --
    def _dead_names(self):
        """Which SERVICES the killed pids were: process.running() answers per
        service name, and a fake that answered per pid would let a killed klipper
        read as alive everywhere that matters."""
        by_pid = {pid: name for name, pid in self.machine.pids.items()}
        return [by_pid.get(pid, "stray") for pid in self.machine.dead]

    def stack_pids(self):
        return dict(self.machine.pids)

    def kill(self, pid):
        self.machine.log("kill", pid)
        self.machine.dead.add(pid)
        return True

    def alive(self, pid):
        return pid not in self.machine.dead

    def wait_dead(self, pid, timeout=20.0):
        self.machine.log("wait_dead", pid)
        return not self.alive(pid)

    # ------------------------------------------------------------------ stack --
    def bring_up(self):
        self.machine.log("bring_up")
        self.stop_stack()                 # what process.start() follows in `up`
        self.machine.dead.clear()
        self.machine.printing = False
        self.machine.pids = {name: pid + 100
                             for name, pid in self.machine.pids.items()}
        write_state(self.ns, pids=self.machine.pids)
        self.pl.note_session_start()      # exactly process.start()'s marker call
        return True

    def stop_stack(self):
        self.machine.log("stop_stack")
        # process.stop() hands in the liveness it read BEFORE signalling; here it
        # is the fake machine's own truth (a killed klipper stays "DOWN").
        self.pl.note_clean_stop(state=services_up(dead=self._dead_names()))
        try:
            self.ns.paths.STATE.unlink()
        except OSError:
            pass
        return True

    def klippy_state(self, timeout=40.0):
        self.machine.log("klippy_state")
        return self.machine.klippy

    def services_up(self):
        return services_up(dead=tuple(self._dead_names()))

    def orphan_pids(self):
        return list(self.machine.orphans)

    def port_conflicts(self):
        return list(self.machine.conflicts)

    def last_stop(self, stack_up=False):
        self.machine.log("last_stop", stack_up)
        return self.pl.last_stop(stack_up)


@pytest.fixture
def rig(pl):
    """The throwaway install as `klipperformac up` leaves it: a stack record, a
    session marker that records it, and the fake harness wired to both."""
    write_state(pl)
    pl.powerloss.note_session_start()
    return FakeHarness(pl, Machine())


@pytest.fixture
def run(rig):
    ok, checks = powerloss.run_kill_sequence(rig, log=lambda _t: None)
    return ok, checks, rig


# ------------------------------------------------------------------- happy --
def test_the_live_sequence_passes_offline(run):
    ok, checks, _harness = run
    failed = [name for name, passed, _detail in checks if not passed]
    assert failed == []
    assert ok is True
    names = [name for name, _passed, _d in checks]
    for promised in ("journal recorded advancing byte offsets",
                     "unclean stop detected after SIGKILL klipper",
                     "journal survived SIGKILL klipper",
                     "journal survived SIGKILL moonraker",
                     "clean-stop marker written by down",
                     "stack healthy at the end",
                     "no orphan service processes",
                     "no foreign process holds a stack port"):
        assert promised in names, promised


def test_kills_happen_in_order_and_each_is_followed_by_a_rebuild(run):
    _ok, _checks, harness = run
    calls = [call[0] for call in harness.machine.calls]
    assert calls.index("kill") < calls.index("bring_up")
    assert calls.count("kill") == 2, calls
    assert calls.count("bring_up") == 3            # after each kill, then at the end
    assert "bring_up" in calls[-4:], calls[-4:]    # the machine is left RUNNING
    killed = [call for call in harness.machine.calls if call[0] == "kill"]
    assert killed[0][1] == 601 and killed[1][1] == 702  # klipper, then moonraker


def test_the_verdict_is_asked_as_if_nothing_is_running(run):
    """The bug this pins: asking `last_stop` while the stack still reads as up
    silences the crash (powerloss.last_stop's `stack_up` rule). The ONE call made
    with the stack up is the precondition check, which must therefore read
    "running"; every judgement after the first kill is made as if nothing runs,
    because that is when the marker becomes evidence."""
    _ok, _checks, harness = run
    asked = [call for call in harness.machine.calls if call[0] == "last_stop"]
    assert asked[0][1] is True
    assert all(call[1] is False for call in asked[1:]), asked


def test_the_journal_and_markers_that_survive_are_this_repos_files(run):
    _ok, _checks, harness = run
    folder = harness.ns.paths.POWERLOSS
    session = json.loads((folder / "session.json").read_text())
    assert session["stack"]["klipper"]            # the stack is up at the end
    verdict = json.loads((folder / "stop.json").read_text())
    assert verdict["clean"] is True               # the last close was `down`
    records = harness.journal_records()
    assert len(records) >= 4                    # 3 ticks, then the second print
    assert [record["file_position"] for record in records] == sorted(
        record["file_position"] for record in records)
    for record in records:
        assert record["filename"] == "cube.gcode"


def test_the_moonraker_kill_is_followed_by_a_clean_marker_stop(rig):
    """The sequence asserts BOTH states: unclean after each SIGKILL, clean only
    after the graceful `down` - which is the whole feature."""
    seen = []
    original = rig.last_stop

    def spy(stack_up=False):
        record = original(stack_up)
        seen.append(None if record is None else record["state"])
        return record

    rig.last_stop = spy
    ok, _checks = powerloss.run_kill_sequence(rig, log=lambda _t: None)
    assert ok is True
    assert "unclean" in seen and "clean" in seen
    assert seen.count("unclean") >= 2


def test_nothing_prints_and_no_port_is_held_at_the_end(run):
    ok, checks, harness = run
    assert harness.print_state() == "standby"
    assert harness.port_conflicts() == []
    assert harness.orphan_pids() == []
    assert ok


# ------------------------------------------------------------------- hostile --
def test_a_journal_that_stays_off_stops_the_sequence_before_any_kill(rig):
    rig.enable_journal = lambda: {"journal": False}
    ok, checks = powerloss.run_kill_sequence(rig, log=lambda _t: None)
    assert ok is False
    # Not one machine contact after the setting was read: no gcode file, no POST,
    # no kill. The gate is the first check, not a courtesy.
    assert rig.machine.calls == []
    assert checks[0][1] is False


def test_a_stack_that_opened_no_session_marker_stops_before_any_kill(rig):
    """The precondition the whole feature rests on: `up` wrote a session marker.
    Without it the kill would prove nothing about recovery, so the sequence bails
    instead of reporting an absent verdict as "unclean detected"."""
    (rig.ns.paths.POWERLOSS / "session.json").unlink()
    ok, checks = powerloss.run_kill_sequence(rig, log=lambda _t: None)
    assert ok is False
    assert any(name == "this session is tracked by an open marker" and not passed
               for name, passed, _d in checks)
    assert "kill" not in [call[0] for call in rig.machine.calls]


def test_a_print_that_never_starts_stops_the_sequence(rig):
    rig.start_print = lambda name: False
    ok, _checks = powerloss.run_kill_sequence(rig, log=lambda _t: None)
    assert ok is False
    assert "kill" not in [call[0] for call in rig.machine.calls]


def test_a_journal_that_does_not_advance_is_not_a_pass(rig):
    """A poller that journals the same offset forever is not evidence of a seam -
    and a run that accepted it would bless a broken journal and then kill a board
    for nothing."""
    rig.journal_tick = lambda: {"file_position": 10, "filename": "x.gcode",
                               "state": "printing"}
    ok, checks = powerloss.run_kill_sequence(rig, log=lambda _t: None)
    assert ok is False
    assert any(name == "journal recorded advancing byte offsets" and not passed
               for name, passed, _d in checks)
    assert "kill" not in [call[0] for call in rig.machine.calls]


def test_a_klipper_that_never_comes_back_ready_fails_the_run(rig):
    rig.machine.klippy = "startup"        # the classic bad-serial state
    ok, checks = powerloss.run_kill_sequence(rig, log=lambda _t: None)
    assert ok is False
    failed = [name for name, passed, _d in checks if not passed]
    assert "klipper reconnects to ready" in failed


def test_a_stray_service_and_a_held_port_are_both_failures(rig):
    rig.orphan_pids = lambda: [9999]
    rig.port_conflicts = lambda: [(7125, 4321, "somebody else's server")]
    ok, checks = powerloss.run_kill_sequence(rig, log=lambda _t: None)
    assert ok is False
    failed = [name for name, passed, _d in checks if not passed]
    assert "no orphan service processes" in failed
    assert "no foreign process holds a stack port" in failed


def test_a_missing_clean_marker_after_down_is_a_failure(rig):
    """If the graceful path stops writing its marker, the sequence must not read
    the resulting `None` verdict as clean."""
    real = rig.stop_stack

    def stop_without_marker():
        real()
        try:
            (rig.ns.paths.POWERLOSS / "stop.json").unlink()
        except OSError:
            pass
        return True

    rig.stop_stack = stop_without_marker
    ok, checks = powerloss.run_kill_sequence(rig, log=lambda _t: None)
    assert ok is False
    assert any(name == "clean-stop marker written by down" and not passed
               for name, passed, _d in checks)


# ------------------------------------------------------------- the test print --
def test_the_test_print_moves_no_extrudes_or_heats(pl):
    """G4-only is the promise that makes killing a live board acceptable
    (AGENTS.md 5): no motion, no heat, no filament, no homing."""
    text = powerloss.test_gcode_text()
    body = [line.strip() for line in text.splitlines() if line.strip()
            and not line.startswith(";")]
    assert all(line == "G90" or line.startswith("G4 P") for line in body), body
    assert "G28" not in text and "G1 " not in text and "M10" not in text
    assert text.count("G4 P500") == powerloss.TEST_GCODE_DWELLS


def test_the_test_print_is_named_as_a_test_file(pl):
    assert powerloss.TEST_GCODE_NAME.endswith(".gcode")
    assert "powerloss-test" in powerloss.TEST_GCODE_NAME


def test_live_harness_refuses_without_a_process_module(pl):
    """Constructing the live harness without the module that owns the pids must
    fail before any POST or signal, not half-way through a print."""
    original = powerloss._process
    powerloss._process = lambda: None
    try:
        with pytest.raises(RuntimeError):
            powerloss.LiveHarness()
    finally:
        powerloss._process = original
