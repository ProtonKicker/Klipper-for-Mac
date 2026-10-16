"""The read-only telemetry behind ``klipperformac timing`` / doctor.

Everything here is parsed offline from fixture text: fake ``ps``/``pmset``/
``ioreg`` answers and a fabricated klippy.log under tmp_path. The numbers are the
ones the "Real-time scheduling" research pinned on this Mac (srtt <= 3 ms, min
half-RTT 64-84 us, rto floored at MIN_RTO 0.025 -
~/.klipperformac/klipper/klippy/chelper/serialqueue.c:107 - bytes_retransmit 9,
0 "Timer too close" across 6 logs, klippy at ps pri 31), so a parser that drifts
from them fails here instead of lying to a user mid-print.

The trap this suite exists for: ``pmset -g therm`` streams and never exits on its
own. It must always be read with a timeout, and its buffered output must still be
parsed when the timeout fires.
"""
import json
import subprocess
import types

import pytest


class Completed:
    def __init__(self, stdout=b"", code=0):
        self.stdout = stdout
        self.returncode = code
        self.stderr = b""


class FakeSystem:
    """Answers the reads timing makes, and records how it was asked.

    A value that is an Exception is raised instead of returned, which is how a
    test says "this binary is not here" (OSError) or "this command hung"
    (TimeoutExpired, the real class)."""

    DEVNULL = subprocess.DEVNULL
    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def run(self, cmd, *args, **kwargs):
        argv = " ".join(str(c) for c in cmd)
        self.calls.append((argv, kwargs))
        for needle, value in self.answers.items():
            if needle in argv:
                if isinstance(value, Exception):
                    raise value
                return Completed(value if isinstance(value, bytes)
                                 else value.encode())
        return Completed(b"")

    def timeout_for(self, needle):
        for argv, kwargs in self.calls:
            if needle in argv:
                return kwargs.get("timeout")
        return None


STAT_A = ("Stats 237052.5: gcodein=0 mcu: mcu_awake=0.000 mcu_task_avg=0.000002 "
          "mcu_task_stddev=0.000002 bytes_write=1435 bytes_read=6599 "
          "bytes_retransmit=9 bytes_invalid=0 send_seq=195 receive_seq=195 "
          "retransmit_seq=2 srtt=0.001 rttvar=0.000 rto=0.025 ready_bytes=0 "
          "upcoming_bytes=0 freq=12000531 print_time=1000.000 buffer_time=0.500 "
          "print_stall=0 sysload=2.60 cputime=2.124")
# Two MCUs on one line: the worse srtt, the busiest sysload and the worst
# mcu_task_stddev of any of them are the answer.
STAT_B = ("Stats 237053.5: gcodein=0 mcu: mcu_task_stddev=0.000004 "
          "bytes_retransmit=9 srtt=0.003 rto=0.025 print_time=1005.000 "
          "buffer_time=9.750 print_stall=0 sysload=6.68 mcu2: "
          "mcu_task_stddev=0.00012 bytes_retransmit=9 srtt=0.002 rto=0.025 "
          "print_time=1005.000 buffer_time=9.750 print_stall=0 sysload=6.68")
# print_time did not advance: a parked toolhead always reads buffer_time=0.000.
STAT_C = ("Stats 237054.5: gcodein=0 mcu: mcu_task_stddev=0.000002 "
          "bytes_retransmit=9 srtt=0.000 rto=0.025 print_time=1005.000 "
          "buffer_time=0.000 print_stall=0 sysload=2.60")
CLOCKSYNC_1 = ("clocksync state: mcu_freq=12000000 last_clock=43927853 "
               "clock_est=(201389.303 17629626 12000706.474) "
               "min_half_rtt=0.000084 min_rtt_time=201389.525")
CLOCKSYNC_2 = ("clocksync state: mcu_freq=12000000 last_clock=43929853 "
               "clock_est=(201399.303 17629926 12000706.474) "
               "min_half_rtt=0.000064 min_rtt_time=201399.525")

AC = '"ExternalConnected" = Yes\n'
BATTERY = '"ExternalConnected" = No\n'
CUSTOM = ("Battery Power:\n"
          " lowpowermode         1\n"
          " sleep                1\n"
          " disksleep            10\n"
          "AC Power:\n"
          " lowpowermode         0\n"
          " sleep                0\n")
THERM_OK = ("2026-10-16 10:50:35 -0700\n"
           "\tCPU_Scheduler_Limit \t= 100\n"
           "\tCPU_Available_CPUs \t= 8\n"
           "\tCPU_Speed_Limit \t= 100\n")
ASSERTIONS = (
    "Listed by owning process:\n"
    "   pid 16010(caffeinate): [0x1] 01:08:21 PreventUserIdleSystemSleep "
    'named: "caffeinate command-line tool"\n'
    "   pid 999(otherapp): [0x2] 00:01:00 PreventUserIdleSystemSleep "
    'named: "somebody else"\n')
PS_QOS_ON = ("python klippy.py -a run/klippy.sock "
             "KLIPPERFORMAC_HOME=/Users/x/.klipperformac KLIPPERFORMAC_QOS=ui "
             "PATH=/usr/bin\n")


@pytest.fixture
def rt(stack, monkeypatch):
    """timing's world, forged: fake ps/pmset/ioreg, a tracked stack record and
    klippy.log files inside the throwaway install."""
    fake = FakeSystem()
    monkeypatch.setattr(stack.cli, "subprocess", fake)
    monkeypatch.setattr(stack.cli, "_klippy_state", lambda timeout=3: "ready")
    monkeypatch.setattr(stack.process, "running", lambda: {
        "web_mainsail": True, "web_fluidd": True, "moonraker": True,
        "klipper": True, "caffeinate": True})
    stack.paths.RUN.mkdir(parents=True, exist_ok=True)
    stack.paths.STATE.write_text(json.dumps({
        "boot_time": "1791932927", "klipper": 16006, "moonraker": 16007,
        "caffeinate": 16010}))
    fake.answers = {
        "ps -o pid=,ni=,pri= -p 16006": "16006  0  31\n",
        "ps eww": PS_QOS_ON,
        "pmset -g custom": CUSTOM,
        "ioreg": AC,
        "pmset -g therm": THERM_OK,
        "pmset -g assertions": ASSERTIONS,
    }
    return types.SimpleNamespace(stack=stack, cli=stack.cli, paths=stack.paths,
                                 fake=fake, monkeypatch=monkeypatch)


def write_log(rt, *lines):
    rt.paths.LOGS.mkdir(parents=True, exist_ok=True)
    rt.paths.KLIPPY_LOG.write_text("\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# the log parsers
# --------------------------------------------------------------------------

def test_host_budget_reads_the_pinned_numbers(rt):
    write_log(rt, STAT_A, CLOCKSYNC_1, STAT_B, STAT_C, CLOCKSYNC_2)
    budget = rt.cli._host_budget()
    assert budget["srtt_max"] == pytest.approx(0.003)
    assert budget["rto_min"] == pytest.approx(0.025)
    assert budget["bytes_retransmit_max"] == 9.0
    assert budget["mcu_task_stddev_max"] == pytest.approx(0.00012)
    assert budget["sysload_max"] == pytest.approx(6.68)
    assert budget["print_stall_max"] == 0.0
    assert budget["buffer_time_max"] == pytest.approx(9.750)
    assert budget["print_time_max"] == pytest.approx(1005.0)
    # the LAST clocksync line is this Mac's 64 us reading, not the first
    assert budget["min_half_rtt"] == pytest.approx(0.000064)
    assert budget["stats_lines"] == 3
    assert budget["truncated"] is False
    assert budget["counts"] == {"timer_too_close": 0, "missed_scheduling": 0,
                                "rescheduled_timer": 0, "stepper_too_far": 0,
                                "lost_communication": 0}
    report = rt.cli._timing_report()
    assert report["problems"] == []


def test_starvation_errors_are_counted_by_name(rt):
    """The five strings upstream itself labels "the host computer is overloaded"
    (klippy/extras/error_mcu.py:34-46) plus mcu.py:906's serial dropout."""
    write_log(rt,
              "Transition to shutdown state: Timer too close",
              STAT_A,
              "Timer too close",
              "MCU 'mcu' shutdown: Missed scheduling of next horizontal_move",
              "Rescheduled timer in the past",
              "Stepper too far in past",
              "Lost communication with MCU 'mcu'")
    counts = rt.cli._host_budget()["counts"]
    assert counts["timer_too_close"] == 2
    assert counts["missed_scheduling"] == 1
    assert counts["rescheduled_timer"] == 1
    assert counts["stepper_too_far"] == 1
    assert counts["lost_communication"] == 1


def test_timer_too_close_is_the_one_fatal_error_string(rt):
    write_log(rt, "Transition to shutdown state: Timer too close")
    report = rt.cli._timing_report()
    assert report["ok"] is False
    assert any("Timer too close" in p for p in report["problems"])


def test_lost_communication_alone_is_explained_not_fatal(rt):
    """A USB dropout is not host starvation; saying so is the difference between
    a useful line and sending the user to fix their scheduler for a bad cable."""
    write_log(rt, "Lost communication with MCU 'mcu'")
    report = rt.cli._timing_report()
    assert report["problems"] == []
    tags = [text for tag, text in rt.cli._timing_lines(report)
            if "usually the USB link" in text]
    assert tags and tags[0].startswith("lost_communication")


def test_buffer_time_is_only_counted_while_the_print_advanced(rt):
    """The number the research wants is the high-water mark of a *moving* print;
    every idle second reads buffer_time=0.000 and would wash it out."""
    write_log(rt, STAT_C, STAT_C.replace("buffer_time=0.000",
                                         "buffer_time=4.000"))
    budget = rt.cli._host_budget()
    assert budget["print_time_max"] == pytest.approx(1005.0)
    assert budget["buffer_time_max"] == pytest.approx(0.0)


def test_a_huge_log_is_cut_short_and_says_so(rt):
    """telemetry that costs a minute is telemetry nobody runs during a print."""
    rt.paths.LOGS.mkdir(parents=True, exist_ok=True)
    rt.paths.KLIPPY_LOG.write_text(STAT_A + "\n" + ("x" * 400 + "\n") * 40)
    assert rt.cli._host_budget(budget=200)["truncated"] is True
    assert rt.cli._host_budget()["truncated"] is False


# --------------------------------------------------------------------------
# the ps priority verdict
# --------------------------------------------------------------------------

@pytest.mark.parametrize("pri,expected", [
    (31, "normal"),        # what an unthrottled process reads on this Mac
    (35, "normal"),
    (25, "throttled"),     # UTILITY-class QoS
    (20, "throttled"),
    (19, "background"),    # App-Nap territory: a print does not survive this
    (4, "background"),
    (None, "unknown"),
])
def test_pri_classifier(rt, pri, expected):
    assert rt.cli._pri_class(pri) == expected


def test_sched_posture_reads_nice_and_pri(rt):
    assert rt.cli._sched_posture(16006) == (0, 31)
    rt.fake.answers["ps -o pid=,ni=,pri= -p 16006"] = ""  # ps did not answer
    assert rt.cli._sched_posture(16006) == (None, None)


def test_a_recycled_or_junk_pid_is_never_asked_about(rt):
    """state.json is plain JSON the GUI or a torn write may have left behind;
    ps must never be handed 0 (a process group) or a 9-digit number."""
    stack_paths = rt.paths
    for junk in (0, -1, True, "16006", 10 ** 7, None):
        stack_paths.STATE.write_text(json.dumps({"klipper": junk,
                                                 "caffeinate": 16010}))
        assert rt.cli._tracked_pid("klipper") is None


def test_qos_env_state_is_honest_about_not_knowing(rt):
    assert rt.cli._qos_env_state(16006) == "on"
    rt.fake.answers["ps eww"] = ("python klippy.py -l klippy.log HOME=/Users/x "
                                 "PATH=/usr/bin\n")
    assert rt.cli._qos_env_state(16006) == "off"    # env shown, switch absent
    rt.fake.answers["ps eww"] = "/usr/bin/caffeinate -dis\n"
    assert rt.cli._qos_env_state(16006) == "unknown"  # env not shown at all
    rt.fake.answers["ps eww"] = OSError(2, "no ps")
    assert rt.cli._qos_env_state(16006) == "unknown"


# --------------------------------------------------------------------------
# power: the trap in `pmset -g therm`, and the assertion ledger
# --------------------------------------------------------------------------

def test_therm_is_always_read_with_a_timeout(rt):
    """A plain subprocess.run of `pmset -g therm` HANGS: it streams and never
    exits by itself (research finding). Without the cap, doctor never returns -
    and when it does fire, the buffered output still holds the numbers."""
    rt.fake.answers["pmset -g therm"] = subprocess.TimeoutExpired(
        ["pmset", "-g", "therm"], 2,
        output=b"CPU_Speed_Limit \t= 87\nCPU_Scheduler_Limit \t= 100\n")
    report = rt.cli._timing_report()
    assert rt.fake.timeout_for("pmset -g therm") == pytest.approx(2.0)
    assert report["power"]["cpu_speed_limit"] == 87
    capped = [tag for tag, text in rt.cli._timing_lines(report)
              if "capping the CPU" in text]
    assert capped == ["[!]"]


def test_thermal_fields_when_the_command_simply_says_nothing(rt):
    rt.fake.answers["pmset -g therm"] = (
        "Note: No thermal warning level has been recorded\n"
        "Note: No performance warning level has been recorded\n")
    assert rt.cli._timing_report()["power"]["cpu_speed_limit"] is None


def test_battery_settings_come_from_the_battery_block(rt):
    report = rt.cli._timing_report()
    assert report["power"]["battery_lowpowermode"] == 1
    assert report["power"]["battery_sleep"] == 1
    assert report["power"]["external_connected"] is True


def test_caffeinate_assertion_must_be_ours(rt):
    """Somebody else's sleep guard is not ours and must not read as protection
    - process.py keeps its own caffeinate ledger for the same reason."""
    assert rt.cli._caffeinate_assert_held([16010]) is True
    assert rt.cli._caffeinate_assert_held([999]) is False
    assert rt.cli._caffeinate_assert_held([]) is None
    rt.fake.answers["pmset -g assertions"] = OSError(2, "no pmset")
    assert rt.cli._caffeinate_assert_held([16010]) is None


def test_throttled_klippy_and_a_lost_guard_are_problems(rt):
    rt.fake.answers["ps -o pid=,ni=,pri= -p 16006"] = "16006  0  4\n"
    rt.fake.answers["pmset -g assertions"] = (
        "Listed by owning process:\n"
        '   pid 311(WindowServer): UserIsActive named: "x"\n')
    report = rt.cli._timing_report()
    assert report["ok"] is False
    assert report["klipper"]["class"] == "background"
    assert report["power"]["caffeinate_assert_held"] is False
    text = " | ".join(report["problems"])
    assert "priority 4" in text
    assert "PreventUserIdleSystemSleep" in text
    # battery is only fatal with a print at stake: _klippy_state says "ready"
    assert "battery" not in text


def test_battery_becomes_fatal_only_while_printing(rt):
    rt.fake.answers["ioreg"] = BATTERY
    assert rt.cli._timing_report()["ok"] is True   # idle: a warning, not a fault
    rt.monkeypatch.setattr(rt.cli, "_klippy_state", lambda timeout=3: "printing")
    report = rt.cli._timing_report()
    assert any("on battery" in p for p in report["problems"])
    doctor = " ".join(text for tag, text in
                      rt.cli._doctor_scheduling_lines(report))
    assert "lid-close still kills the print" in doctor


def test_a_healthy_machine_has_nothing_to_fix(rt):
    write_log(rt, STAT_A, CLOCKSYNC_1)
    report = rt.cli._timing_report()
    assert report["problems"] == []
    assert report["ok"] is True
    assert report["klipper"] == {
        "pid": 16006, "running": True, "nice": 0, "pri": 31,
        "class": "normal", "qos_env": "on"}
    texts = [text for _, text in rt.cli._timing_lines(report)]
    assert any("as good as macOS gets without root" in t for t in texts)
    assert any("queue_step" in t for t in texts)


def test_doctor_scheduling_lines_are_all_green_here(rt):
    write_log(rt, STAT_A)
    report = rt.cli._timing_report()
    assert {tag for tag, _ in rt.cli._doctor_scheduling_lines(report)} == {"[ok]"}


def test_a_stopped_stack_is_never_a_problem(rt):
    rt.monkeypatch.setattr(rt.stack.process, "running", lambda: None)
    report = rt.cli._timing_report()
    assert report["ok"] is True
    assert report["klipper"] == {"pid": None, "running": False, "nice": None,
                                 "pri": None, "class": "unknown",
                                 "qos_env": "unknown"}


def test_linux_only_option_is_named_and_fatal(rt):
    rt.paths.CONFIG.mkdir(parents=True, exist_ok=True)
    rt.paths.PRINTER_CFG.write_text(
        "[printer]\nkinematics: cartesian\nmax_velocity: 200\n"
        "rtpriority: 1\n\n[mcu]\nserial: /dev/cu.usbmodem1\n")
    report = rt.cli._timing_report()
    assert report["linux_only_options"] == ["[printer] rtpriority"]
    assert any("configfile.py:440" in text for _, text
               in rt.cli._timing_lines(report))
    assert report["ok"] is False
