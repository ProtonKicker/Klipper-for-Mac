"""The resident loop's moving parts: sweeps, the intent file, the flock, the
signal path and the boot boundary - all against recorded fakes.

Where test_decide_table.py proves the policy is right, this file proves the
machinery around it is: the lock that keeps a background supervisor from
trampling a human's own command, the want file that decides every sweep, the
log that has to say what happened, and the exit path launchd reads as "the user
asked for this" rather than "it crashed".
"""
import fcntl
import signal
import time


def prepare(agent, wanted=True, running=None, start=True, rest=True):
    """An installed agent, an intent file, and process/REST fakes recorded."""
    agent.installed_plist()
    if wanted:
        agent.launchd.want_set("test")
    else:
        agent.launchd.want_clear("test")
    agent.fake_process(running=running, start=start)
    agent.fake_rest(rest)
    return agent


def log_text(agent):
    path = agent.launchd.supervisor_log_path()
    return path.read_text() if path.is_file() else ""


FULL = ("klipper", "moonraker", "web_mainsail", "web_fluidd", "caffeinate")


# ------------------------------------------------------- the intent file ----
def test_no_want_file_means_no_action_however_dead_the_stack(agent):
    """The whole point: a user's `down` outranks login forever. Without this
    row, autostart would be a machine that disagrees with the person standing at
    it, and the printer would heat up behind them."""
    prepare(agent, wanted=False, running=None)
    outcome, actions = agent.launchd.sweep(now=time.time())
    assert outcome == "held down" and actions == []
    assert agent.calls["start"] == 0 and agent.calls["stop"] == 0


def test_the_want_file_is_written_by_a_user_command_and_never_by_a_sweep(agent):
    prepare(agent)
    assert agent.launchd.want_state()["wanted by"] == "test"
    agent.launchd.sweep(now=time.time())
    assert agent.launchd.want_state()["wanted by"] == "test", \
        "a sweep must not re-date or rewrite the user's own record"
    assert agent.launchd.want_clear("test")
    assert agent.launchd.want_state() is None
    assert not agent.launchd.want_path().exists()
    assert agent.launchd.want_clear("test"), "down twice is still down"


def test_an_unparsable_want_record_reads_as_down(agent):
    """A junk file must never be read as licence to start a printer."""
    prepare(agent)
    agent.launchd.want_path().write_text("\x00\x01 not a record \x02")
    assert agent.launchd.want_state() is None
    outcome, _actions = agent.launchd.sweep(now=time.time())
    assert outcome == "held down"
    assert agent.calls["start"] == 0


def test_a_want_path_that_is_a_directory_does_not_crash_anything(agent):
    prepare(agent)
    agent.launchd.want_path().unlink()
    agent.launchd.want_path().mkdir()
    assert agent.launchd.want_state() is None
    assert agent.launchd.sweep(now=time.time())[0] == "held down"
    assert agent.launchd.want_clear("test")
    assert not agent.launchd.want_path().exists()


# ----------------------------------------------------------- sweep bodies ---
def test_a_cold_start_is_exactly_one_start_and_an_owed_check(agent):
    now = time.time()
    prepare(agent, running=None)
    outcome, actions = agent.launchd.sweep(now=now)
    assert outcome == "acted" and actions == [("start", None)]
    assert agent.calls["start"] == 1 and agent.calls["stop"] == 0
    assert agent.launchd.read_sup_state()["pending_start"] == now
    assert "stack started" in log_text(agent)


def test_a_stack_we_started_is_left_alone_while_it_loads(agent):
    """klippy takes seconds to read printer.cfg; a sweep that re-acted during
    that window would be a second start on the same serial port."""
    now = time.time()
    full = agent.svc(*FULL)
    prepare(agent, running=[None, full, full], rest=False)
    assert agent.launchd.sweep(now=now)[0] == "acted"    # the cold start
    outcome, _actions = agent.launchd.sweep(now=now + 2)
    assert outcome == "starting"
    assert agent.calls["start"] == 1 and agent.calls["stop"] == 0


def test_the_api_is_what_closes_the_loop_after_a_start(agent):
    """REST is the proof, not the pids: pending_start is cleared and the log
    says ready, so the next sweep owes nothing and stays quiet."""
    now = time.time()
    full = agent.svc(*FULL)
    prepare(agent, running=[None, full, full, full], rest=True)
    agent.launchd.sweep(now=now)
    assert agent.launchd.read_sup_state()["pending_start"] == now
    assert agent.launchd.sweep(now=now + 20)[0] == "up"
    assert agent.launchd.read_sup_state()["pending_start"] == 0.0
    assert "stack ready" in log_text(agent)
    assert agent.launchd.sweep(now=now + 30)[0] == "up"
    assert agent.calls["start"] == 1, "a ready stack is not re-started"


def test_processes_up_but_silent_for_a_full_window_is_a_wedged_start(agent):
    """Only a start WE ordered owes this check: a stack the user brought up
    themselves is never restarted over a slow API (see `status`)."""
    now = time.time()
    full = agent.svc(*FULL)
    prepare(agent, running=[None, full, full, full], rest=False)
    agent.launchd.sweep(now=now)
    assert agent.launchd.sweep(now=now + 25)[0] == "starting"
    assert agent.calls["start"] == 1
    outcome, actions = agent.launchd.sweep(now=now + agent.launchd.START_TIMEOUT + 1)
    assert outcome == "acted" and actions == [("core_restart", None)]
    assert agent.calls["start"] == 2 and agent.calls["stop"] == 1


def test_a_stack_the_user_started_is_never_restarted_for_a_silent_api(agent):
    """The other half of that rule: no pending_start, no opinion."""
    now = time.time()
    prepare(agent, running=agent.svc(*FULL), rest=False)
    for offset in (0, 30, 120, 300):
        assert agent.launchd.sweep(now=now + offset)[0] == "up"
    assert agent.calls["start"] == 0 and agent.calls["stop"] == 0


def test_only_the_web_proxies_dying_is_a_repair_never_a_restart(agent):
    """The free path cli.py already uses: the running klipper and its print are
    untouched, the two proxies and the sleep guard come back."""
    prepare(agent, running=agent.svc("klipper", "moonraker"))
    outcome, actions = agent.launchd.sweep(now=time.time())
    assert outcome == "acted"
    assert actions == [("repair", ["web_mainsail", "web_fluidd", "caffeinate"])]
    assert agent.calls["repair"] == [["caffeinate", "web_fluidd", "web_mainsail"]]
    assert agent.calls["stop"] == 0 and agent.calls["start"] == 0


def test_moonraker_down_with_klipper_up_says_so_and_touches_nothing(agent):
    prepare(agent, running=agent.svc("klipper", "web_mainsail", "web_fluidd",
                                     "caffeinate"))
    outcome, actions = agent.launchd.sweep(now=time.time())
    assert outcome == "acted" and actions == [("report_moonraker", None)]
    assert agent.calls["stop"] == 0 and agent.calls["start"] == 0
    text = log_text(agent)
    assert "NOT restarting" in text and "a print may be running" in text


def test_klipper_down_restarts_within_budget_and_latches_beyond_it(agent):
    """The crash loop an unplugged board would otherwise cause is finite, and
    the log line names the repair instead of going quiet."""
    dead_core = agent.svc("moonraker", "web_mainsail", "web_fluidd", "caffeinate")
    now = time.time()
    prepare(agent, running=dead_core)
    for n in range(agent.launchd.MAX_KLIPPER_RESTARTS):
        outcome, actions = agent.launchd.sweep(now=now + n)
        assert actions == [("core_restart", None)], (n, actions)
    assert agent.calls["stop"] == agent.launchd.MAX_KLIPPER_RESTARTS
    assert not agent.launchd.latched()
    outcome, actions = agent.launchd.sweep(now=now + 5)
    assert actions == [("latch", None)] and agent.launchd.latched()
    assert agent.calls["stop"] == agent.launchd.MAX_KLIPPER_RESTARTS, \
        "the latch stops the trying, not just the logging"
    assert "NOT" in log_text(agent)


def test_a_refused_start_is_charged_to_the_budget_too(agent):
    """A stack that cannot start (port held by a squatter) is exactly the loop
    ThrottleInterval was added for: it must run out."""
    dead_core = agent.svc("moonraker", "web_mainsail", "web_fluidd", "caffeinate")
    now = time.time()
    prepare(agent, running=dead_core, start=False)
    for _n in range(agent.launchd.MAX_KLIPPER_RESTARTS):
        agent.launchd.sweep(now=now)
    assert agent.launchd.sweep(now=now)[1] == [("latch", None)]
    assert len(agent.launchd.read_sup_state()["restarts"]) == \
        agent.launchd.MAX_KLIPPER_RESTARTS


def test_a_user_command_reopens_the_latch(agent):
    """`up` is what clears the giving-up: the board is plugged back in, and the
    intent write resets the budget."""
    agent.launchd._write_sup_state({"restarts": [time.time()] * 5,
                                    "latched": True})
    assert agent.launchd.latched()
    agent.launchd.want_set("up")
    assert not agent.launchd.latched()
    assert agent.launchd.read_sup_state()["restarts"] == []


def test_a_latch_never_survives_a_reboot(agent):
    """M2 of the research test plan, offline: yesterday's crash loop must not
    be why nothing starts today. The ledger is keyed on kern.boottime."""
    agent.launchd._write_sup_state({"restarts": [time.time()] * 5,
                                    "latched": True})
    assert agent.launchd.latched()
    agent.boot["value"] = "a-later-boot"
    assert agent.launchd.read_sup_state() == {}
    assert not agent.launchd.latched()


def test_a_ledger_from_another_install_is_not_ours(agent):
    path = agent.launchd.sup_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"restarts": [1.0], "latched": true, '
                    '"app_home": "/Users/other/.klipperformac", '
                    '"boot_time": "test-boot"}')
    assert agent.launchd.read_sup_state() == {}
    assert agent.launchd.latched() is False


def test_a_corrupt_ledger_reads_as_no_history_and_still_acting(agent):
    """Losing the count must not lose the supervision: the budget simply starts
    over, which is the behaviour of a fresh install."""
    agent.launchd.sup_state_path().parent.mkdir(parents=True, exist_ok=True)
    agent.launchd.sup_state_path().write_text("{not json")
    assert agent.launchd.read_sup_state() == {}
    prepare(agent, running=agent.svc("moonraker", "web_mainsail", "web_fluidd",
                                     "caffeinate"))
    outcome, actions = agent.launchd.sweep(now=time.time())
    assert outcome == "acted" and actions == [("core_restart", None)]


# ------------------------------------------------------------ the lock ------
def test_a_busy_command_lock_makes_the_sweep_do_nothing(agent):
    """The supervisor never blocks a human and never barges in behind one: a
    `down` in progress is exactly the moment a tick must be skipped."""
    prepare(agent, running=None)
    agent.paths.RUN.mkdir(parents=True, exist_ok=True)
    blocker = open(agent.paths.CLI_LOCK, "a+")
    try:
        fcntl.flock(blocker, fcntl.LOCK_EX | fcntl.LOCK_NB)
        outcome, actions = agent.launchd.sweep(now=time.time())
        assert outcome == "busy" and actions == []
        assert agent.calls["start"] == 0
    finally:
        fcntl.flock(blocker, fcntl.LOCK_UN)
        blocker.close()
    assert agent.launchd.sweep(now=time.time())[0] == "acted"
    assert agent.calls["start"] == 1


def test_two_supervisors_cannot_run_at_once(agent):
    """The wave-2 flock pattern, applied to the supervisor's own identity:
    launchd serialises its own job, but `login on` kickstarts, a second install
    can bootstrap, and a human can always type `klipperformac supervise`."""
    first, why = agent.launchd.single_instance()
    assert first is not None, why
    second, why = agent.launchd.single_instance()
    assert second is None and "already holds" in why
    agent.launchd._unlock(first)
    third, why = agent.launchd.single_instance()
    assert third is not None, why
    agent.launchd._unlock(third)


def test_the_loop_refuses_to_be_the_second_one_and_says_so(agent):
    prepare(agent, running=None)
    holder, why = agent.launchd.single_instance()
    assert holder is not None, why
    try:
        assert agent.launchd.run(tick=0.05, sweeps=1, handlers=False) == 0
    finally:
        agent.launchd._unlock(holder)
    assert agent.calls["start"] == 0, "the second supervisor did nothing at all"
    assert "already holds" in log_text(agent)


def test_the_loop_sweeps_the_requested_number_of_times_and_exits_zero(agent):
    prepare(agent, running=agent.svc(*FULL), rest=True)
    slept = []
    code = agent.launchd.run(tick=5, sweeps=3, handlers=False,
                             sleeper=lambda total, *a: slept.append(total))
    assert code == 0
    assert slept == [5.0, 5.0], "no sleep after the last sweep"
    assert agent.calls["start"] == 0 and agent.calls["stop"] == 0
    assert "supervisor start pid=" in log_text(agent)
    assert "supervisor exiting after 3 sweep(s) (up)" in log_text(agent)


def test_a_tick_below_the_floor_is_clamped_not_honoured(agent):
    """A 0.05 s sweep would be a flock fight with every `klipperformac` command
    a human types, so the floor is the loop's, not just the plist's."""
    prepare(agent, running=agent.svc(*FULL), rest=True)
    slept = []
    agent.launchd.run(tick=0.01, sweeps=2, handlers=False,
                      sleeper=lambda total, *a: slept.append(total))
    assert slept == [agent.launchd.MIN_TICK]


def test_a_sweep_that_raises_cannot_kill_the_loop(agent):
    """A supervisor that dies on one bad read is a machine with no watchdog and
    a launchd job in a crash loop; one logged line per bad sweep is the answer."""
    prepare(agent, running=None)

    def explode():
        raise RuntimeError("ps went wrong")

    agent.monkey.setattr(agent.launchd.process, "running", explode)
    assert agent.launchd.run(tick=0.05, sweeps=2, handlers=False,
                             sleeper=lambda total, *a: None) == 0
    text = log_text(agent)
    assert text.count("sweep failed: ps went wrong") == 2
    assert agent.calls["start"] == 0


def test_the_sigterm_handler_only_sets_a_flag(agent):
    """The handler is two assignments: anything slower here is a bootout that
    looks like a hang to launchd."""
    agent.launchd._STOP[0] = False
    for number in (signal.SIGTERM, signal.SIGINT):
        agent.launchd._on_signal(number, None)
        assert agent.launchd._STOP[0] is True
        agent.launchd._STOP[0] = False


def test_a_terminated_loop_exits_zero_without_touching_the_stack(agent):
    """launchd's bootout, a logout and Ctrl-C all land here. Exit 0 is the
    contract: KeepAlive{SuccessfulExit=false} must not read a deliberate stop as
    a crash, and the stack it watches keeps running (AbandonProcessGroup)."""
    prepare(agent, running=agent.svc(*FULL), rest=True)

    def _stop_during_the_first_sleep(total, *args):
        agent.launchd._STOP[0] = True

    assert agent.launchd.run(tick=30.0, handlers=False,
                             sleeper=_stop_during_the_first_sleep) == 0
    assert agent.calls["start"] == 0 and agent.calls["stop"] == 0
    assert "supervisor exiting after 1 sweep(s)" in log_text(agent)


def test_a_signalled_sleep_returns_immediately(agent):
    """30 s of tick must not become 30 s of un-responsive bootout: the sleep is
    sliced and every slice re-reads the flag."""
    agent.launchd._STOP[0] = False
    start = time.time()
    agent.launchd._sleep_slices(0.2)
    assert time.time() - start < 1.0
    agent.launchd._STOP[0] = True
    start = time.time()
    agent.launchd._sleep_slices(30.0)
    assert time.time() - start < 0.2, "the sleep ignored the stop flag"
    agent.launchd._STOP[0] = False


def test_a_sweep_with_no_agent_on_disk_ends_the_process(agent):
    """`login off` boots the job out; if a hand-deleted plist is what we wake up
    to instead, the honest answer is to stop having an opinion about the stack -
    leave it exactly as the user left it and exit."""
    agent.launchd.want_set("test")
    agent.fake_process(running=None)
    path = agent.installed_plist()
    assert agent.launchd.sweep(now=time.time())[0] == "acted"
    started = agent.calls["start"]
    path.unlink()
    assert agent.launchd.run(tick=0.05, handlers=False,
                             sleeper=lambda total, *a: None) == 0
    assert agent.calls["start"] == started, "the exit path touched nothing"
    assert "supervisor exiting after 1 sweep(s) (agent gone)" in log_text(agent)


def test_the_log_rotates_instead_of_growing_forever(agent):
    """launchd can keep this job alive for months; a print's worth of sweeps at
    10 s each is not a reason to fill the data folder with one line per tick."""
    path = agent.launchd.supervisor_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x" * (agent.launchd.LOG_ROTATE_BYTES + 10))
    agent.launchd.note("a new line")
    assert path.read_text().endswith(" a new line\n")
    assert len(path.read_text().splitlines()) == 1
    rotated = path.parent / (path.name + ".1")
    assert rotated.is_file() and rotated.stat().st_size > 1024 * 1024
    assert len(list(path.parent.iterdir())) == 2


def test_a_log_that_cannot_be_written_still_supervises(agent):
    """logs/ replaced by something unwritable is a reason to keep working, not
    to stop watching: note() swallows it and the sweep still runs."""
    prepare(agent, running=None)
    log = agent.launchd.supervisor_log_path()
    log.mkdir(parents=True)          # a folder where the log file should be
    assert agent.launchd.sweep(now=time.time())[0] == "acted"
    assert agent.calls["start"] == 1
