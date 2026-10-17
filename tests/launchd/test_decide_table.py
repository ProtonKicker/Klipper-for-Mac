"""The supervisor's policy as a table, with no I/O at all.

`decide()` is where every promise this feature makes is kept or broken, so it is
a pure function: (user intent, what process.running() says, the restart budget)
-> actions. Nothing here touches a pid, a file or launchctl, which is exactly
what makes the promises falsifiable:

  * down wins        - no intent file, no action, however dead the stack is.
  * never disturb a print - a service that answers is never a restart candidate,
    and klipper-up/moonraker-down is reported instead of acted on, because the
    only restart path we have also signals a live klipper.
  * a crash loop is finite - MAX_KLIPPER_RESTARTS inside RESTART_WINDOW, then a
    latch and a loud log line instead of an endless respawn.
"""
import pytest

CORE_DOWN = dict(klipper=False, moonraker=False, web_mainsail=False,
                 web_fluidd=False, caffeinate=False)


def up(*names):
    data = {name: False for name in ("klipper", "moonraker", "web_mainsail",
                                     "web_fluidd", "caffeinate")}
    for name in names:
        data[name] = True
    return data


FULLY_UP = up("klipper", "moonraker", "web_mainsail", "web_fluidd", "caffeinate")

CASES = [
    # ---- the user said down: nothing is ever started -----------------------
    pytest.param(False, None, [], id="down-and-nothing-running"),
    pytest.param(False, CORE_DOWN, [], id="down-and-everything-dead"),
    pytest.param(None, None, [], id="no-want-file-is-down"),
    pytest.param({}, FULLY_UP, [], id="empty-want-record-is-down"),
    # ---- only the free services are missing --------------------------------
    pytest.param(True, up("klipper", "moonraker", "caffeinate"), [],
                 id="proxies-down-repair-only"),
    pytest.param(True, up("klipper", "moonraker", "web_mainsail", "web_fluidd"),
                 [], id="caffeinate-down-repair-only"),
    # ---- the ones that must not be disturbed -------------------------------
    pytest.param(True, up("klipper", "web_mainsail", "web_fluidd", "caffeinate"),
                 [], id="moonraker-down-klipper-up-is-reported-not-restarted"),
    # ---- everything answers ------------------------------------------------
    pytest.param(True, FULLY_UP, [], id="all-up-nothing-to-do"),
]


@pytest.mark.parametrize("wanted,state,restarts", CASES)
def test_the_quiet_cases_produce_no_signals(launchd, wanted, state, restarts):
    """Table rows where the answer must be "do nothing to a live pid"."""
    actions = launchd.decide(wanted, state, restarts, now=1000.0)
    assert actions == [] or all(a[0] in ("repair", "report_moonraker")
                                for a in actions), actions


def test_a_cold_start_is_one_action(launchd):
    assert launchd.decide(True, None, [], 1000.0) == [("start", None)]


def test_dead_proxies_are_repaired_not_restarted(launchd):
    """repair() is the existing, free path (cli.py uses it the same way): it
    starts only what is missing and never looks at klipper or moonraker."""
    state = up("klipper", "moonraker")   # both proxies and caffeinate down
    actions = launchd.decide(True, state, [], 1000.0)
    assert actions == [("repair", ["web_mainsail", "web_fluidd", "caffeinate"])]
    assert "klipper" not in actions[0][1] and "moonraker" not in actions[0][1]


def test_moonraker_down_while_klipper_up_never_restarts(launchd):
    """The rule that protects a print: klippy is the printer, and it is up, so
    a dead web API is a thing to say out loud, not a thing to fix by stopping a
    running klipper. This is the honest half of "restarts on death"."""
    state = up("klipper", "web_mainsail", "web_fluidd", "caffeinate")
    actions = launchd.decide(True, state, [], 1000.0)
    assert actions == [("report_moonraker", None)]


def test_klipper_down_is_a_budgeted_restart(launchd):
    """klipper down means no print can be running, so the whole stack may be
    restarted - and that is the only reason this is allowed at all."""
    state = up("moonraker", "web_mainsail", "web_fluidd", "caffeinate")
    assert launchd.decide(True, state, [], 1000.0) == [("core_restart", None)]
    one = [1000.0 - 60.0]
    assert launchd.decide(True, state, one, 1000.0) == [("core_restart", None)]


def test_the_budget_latches_then_stops_trying(launchd):
    limit = launchd.MAX_KLIPPER_RESTARTS
    state = up("moonraker", "web_mainsail", "web_fluidd", "caffeinate")
    inside = [1000.0 - 30.0 * n for n in range(1, limit + 1)]
    assert len(inside) == limit
    assert launchd.decide(True, state, inside, 1000.0) == [("latch", None)]
    assert launchd.decide(True, state, inside + inside, 1000.0) == [("latch", None)]


def test_a_crash_window_that_expired_reopens_the_budget(launchd):
    state = up("moonraker", "web_mainsail", "web_fluidd", "caffeinate")
    old = [1000.0 - launchd.RESTART_WINDOW - 1.0] * launchd.MAX_KLIPPER_RESTARTS
    assert launchd.decide(True, state, old, 1000.0) == [("core_restart", None)]


def test_junk_in_the_restart_ledger_is_ignored(launchd):
    """The file is plain JSON a human or a half-finished write may have left."""
    state = up("moonraker", "web_mainsail", "web_fluidd", "caffeinate")
    junk = ["yesterday", None, {}, True, 999.5]
    assert launchd.decide(True, state, junk, 1000.0) == [("core_restart", None)]
    assert launchd.decide(True, state, None, 1000.0) == [("core_restart", None)]


def test_hypothesis_the_table_never_invents_a_signal_for_a_live_service(launchd):
    """Property sweep over the state space: for every combination of the five
    services and the intent file, no action may name a service that is up."""
    names = ["klipper", "moonraker", "web_mainsail", "web_fluidd", "caffeinate"]
    for flags in range(32):
        state = {n: bool(flags & (1 << i)) for i, n in enumerate(names)}
        for wanted in (True, False):
            for restarts in ([], [999.0, 999.5, 999.9], [1.0]):
                actions = launchd.decide(wanted, state, restarts, now=1000.0)
                for action, value in actions:
                    if action == "repair":
                        assert all(not state[n] for n in value), action
                        assert not set(value) & set(launchd.CORE_SERVICES)
                    if action == "core_restart":
                        assert not state["klipper"], \
                            "a restart was ordered while klipper was up"
                    assert action in ("start", "core_restart", "repair",
                                      "report_moonraker", "latch")
