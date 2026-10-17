"""The CLI surface: what a human (and the GUI) can type, and how `up`/`down`
now feed the supervisor's one input.

Two promises live here rather than in launchd.py:

  * the parser is the guard rail - a typo is an argparse exit 2 with usage, not
    a traceback and not a silently ignored argument;
  * the intent file is written by *user commands only*, in the right order:
    `down` clears the want BEFORE it stops the stack, so the supervisor can never
    catch a window where the stack is down and the user still wants it up.
"""
import json


def spy(agent, name, order, tag=None):
    """Wrap a module function so a call is recorded, then really happens."""
    module, _, attr = name.rpartition(".")
    target = getattr(agent, module) if module else agent.launchd
    real = getattr(target, attr)
    label = tag or attr

    def wrapper(*args, **kwargs):
        order.append(label)
        return real(*args, **kwargs)

    agent.monkey.setattr(target, attr, wrapper)
    return wrapper


def run_cli(agent, argv):
    """cli.main(argv) -> (exit code or None, leaked exception or None)."""
    try:
        agent.cli.main(argv)
    except SystemExit as e:
        return e.code, None
    except BaseException as e:
        return None, e
    return 0, None


def prepare_up_path(agent):
    """Everything `up` touches, without a stack, a socket or a browser."""
    cli = agent.cli
    agent.make_setup_home()
    agent.installed_plist()
    agent.monkey.setattr(cli.configgen, "sync_data_paths", lambda: False)
    agent.monkey.setattr(cli, "_wait_ready", lambda timeout=35: True)
    agent.monkey.setattr(cli, "_power_warning", lambda: None)
    agent.monkey.setattr(cli.shaper, "interpreter_for_klipper",
                         lambda: (agent.paths.VENV_PY, ""))
    agent.monkey.setattr(cli.process, "running", lambda: None)
    agent.monkey.setattr(cli.process, "start", lambda: True)
    agent.monkey.setattr(cli.webbrowser, "open", lambda url: None)


def prepare_down_path(agent):
    cli = agent.cli
    agent.monkey.setattr(cli.process, "stop", lambda timeout=6.0: True)
    agent.monkey.setattr(cli.process, "our_listeners", lambda: [])
    agent.monkey.setattr(cli.batteryguard, "stop", lambda: (False, []))
    agent.monkey.setattr(cli.cam, "stop", lambda: (False, []))


class Clock:
    def __init__(self, start=1000.0):
        self.now = start

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


# ------------------------------------------------------------- the parser ---
def test_login_takes_only_on_off_or_status(agent, capsys):
    for good in ("on", "off", "status"):
        code, exc = run_cli(agent, ["login", good])
        assert exc is None, (good, exc)
    code, exc = run_cli(agent, ["login", "enable"])
    assert exc is None and code == 2, (code, exc)
    assert "invalid choice" in capsys.readouterr().err
    code, exc = run_cli(agent, ["login"])
    assert exc is None and code == 2, "the action is not optional"


def test_login_flags_are_the_documented_ones(agent, capsys):
    code, exc = run_cli(agent, ["login", "status", "--json"])
    assert exc is None and code == 0, (code, exc)
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) >= {"label", "domain", "plist", "installed", "stale",
                            "job_loaded", "job_state", "job_pid",
                            "disabled_in_settings", "stack_intent", "latched",
                            "throttle_interval", "supervisor_log",
                            "klipper_restarts_in_window"}
    assert payload["domain"] == agent.launchd.domain()
    code, exc = run_cli(agent, ["login", "status", "--wat"])
    assert exc is None and code == 2


def test_supervise_is_a_command_of_its_own(agent):
    """The job the LaunchAgent runs is a real subcommand, so `login on` can be
    checked by hand - and so a test can drive the loop with --sweeps."""
    agent.installed_plist()
    agent.launchd.want_set("test")
    agent.fake_process(running=None)
    agent.monkey.setattr(agent.launchd, "single_instance",
                         lambda lock_path=None: (object(), None))
    agent.monkey.setattr(agent.launchd, "_unlock", lambda handle: None)
    code, exc = run_cli(agent, ["supervise", "--tick", "junk"])
    assert exc is None and code == 2, (code, exc)   # argparse, not a traceback
    code, exc = run_cli(agent, ["supervise", "--tick", "5", "--sweeps", "1"])
    assert exc is None and code == 0, (code, exc)
    assert agent.calls["start"] == 1, "the sweep ran through the real CLI path"


# -------------------------------------------------- the intent file wiring --
def test_up_records_the_intent_after_a_successful_start(agent):
    prepare_up_path(agent)
    order = []
    spy(agent, "launchd.want_set", order, tag="want_set")
    agent.launchd.want_clear("test")
    assert run_cli(agent, ["up"])[1] is None
    assert order == ["want_set"]
    assert agent.launchd.want_state()["wanted by"] == "up"


def test_a_start_that_failed_does_not_claim_an_intent(agent):
    """A want file the stack cannot satisfy is a background loop that starts a
    failing printer over and over, and blames the user for wanting it."""
    prepare_up_path(agent)
    agent.monkey.setattr(agent.cli.process, "start", lambda: False)
    agent.launchd.want_clear("test")
    code, exc = run_cli(agent, ["up"])
    assert exc is None and code == 1
    assert agent.launchd.want_state() is None


def test_up_refuses_a_half_stopped_core_stack(agent, capsys):
    prepare_up_path(agent)
    agent.monkey.setattr(agent.cli.process, "running", lambda: {
        "klipper": False, "moonraker": True, "web_mainsail": True,
        "web_fluidd": True, "caffeinate": True})
    code, exc = run_cli(agent, ["up"])
    assert exc is None and code == 1
    out = capsys.readouterr().err
    assert "half stopped" in out.lower()
    assert "klipperformac restart" in out


def test_up_repairs_ui_only_and_records_repair_intent(agent, capsys):
    prepare_up_path(agent)
    repaired = []
    agent.monkey.setattr(agent.cli.process, "running", lambda: {
        "klipper": True, "moonraker": True, "web_mainsail": False,
        "web_fluidd": True, "caffeinate": True})
    agent.monkey.setattr(agent.cli.process, "repair",
                         lambda state=None: repaired.append(dict(state or {})) or
                         ["web_mainsail"])
    agent.launchd.want_clear("test")
    code, exc = run_cli(agent, ["up"])
    assert exc is None and code == 0
    out = capsys.readouterr().out
    assert "Partially stopped" in out
    assert repaired and repaired[0]["web_mainsail"] is False
    assert agent.launchd.want_state()["wanted by"] == "up (repair)"


def test_up_when_fully_running_refreshes_the_plain_intent(agent, capsys):
    prepare_up_path(agent)
    agent.monkey.setattr(agent.cli.process, "running", lambda: {
        "klipper": True, "moonraker": True, "web_mainsail": True,
        "web_fluidd": True, "caffeinate": True})
    agent.launchd.want_clear("test")
    code, exc = run_cli(agent, ["up"])
    assert exc is None and code == 0
    assert "Already running." in capsys.readouterr().out
    assert agent.launchd.want_state()["wanted by"] == "up"


def test_down_clears_the_intent_before_it_stops_the_stack(agent):
    prepare_down_path(agent)
    agent.installed_plist()
    agent.launchd.want_set("up")
    order = []
    spy(agent, "launchd.want_clear", order, tag="want_clear")
    spy(agent, "process.stop", order, tag="stop")
    assert run_cli(agent, ["down"])[1] is None
    assert order == ["want_clear", "stop"], order
    assert agent.launchd.want_state() is None


def test_down_fails_when_our_ports_still_answer(agent, capsys):
    prepare_down_path(agent)
    agent.installed_plist()
    agent.launchd.want_set("up")
    agent.monkey.setattr(agent.cli.process, "our_listeners",
                         lambda: [(7125, 42), (8081, 43)])
    code, exc = run_cli(agent, ["down"])
    assert exc is None and code == 1
    out = capsys.readouterr().out
    assert "Still listening" in out
    assert "port 7125 (pid 42)" in out
    assert "klipperformac killall" in out


def test_restart_orders_guard_stop_then_stack_stop_then_up(agent):
    prepare_up_path(agent)
    order = []
    agent.monkey.setattr(agent.cli.batteryguard, "stop",
                         lambda timeout=3.0: order.append("guard_stop") or
                         (True, []))
    agent.monkey.setattr(agent.cli.process, "stop",
                         lambda timeout=6.0: order.append("stack_stop") or True)
    agent.monkey.setattr(agent.cli, "_cmd_up",
                         lambda args: order.append("up"))
    agent.monkey.setattr(agent.cli, "_guard_autostart",
                         lambda: order.append("guard_autostart"))
    code, exc = run_cli(agent, ["restart"])
    assert exc is None and code == 0
    assert order == ["guard_stop", "stack_stop", "up", "guard_autostart"]


def test_killall_clears_the_intent_too(agent):
    """`killall` is a human saying "get this off my machine", which is the same
    sentence `down` says."""
    cli = agent.cli
    agent.installed_plist()
    agent.launchd.want_set("up")
    order = []
    spy(agent, "launchd.want_clear", order, tag="want_clear")
    agent.monkey.setattr(cli.process, "kill_all", lambda: order.append("kill_all") or [])
    agent.monkey.setattr(cli.cam, "kill_orphans", lambda: [])
    agent.monkey.setattr(cli.batteryguard, "kill_orphans", lambda: [])
    assert run_cli(agent, ["killall"])[1] is None
    assert order == ["want_clear", "kill_all"], order
    assert agent.launchd.want_state() is None


def test_a_supervisor_sweep_never_writes_the_intent(agent):
    """The one-line rule that keeps launchd from fighting a user: only a command
    with a human behind it touches the want file."""
    prepare_up_path(agent)
    agent.launchd.want_clear("test")
    agent.fake_process(running=None)
    agent.launchd.sweep(now=None)
    assert agent.launchd.want_state() is None, "a sweep must not resurrect intent"


def test_an_intent_write_that_fails_does_not_break_up(agent, capsys):
    """run/ is the least important folder in this tool's life: `up` still
    starts the stack, and the loss of supervision is stated, not hidden."""
    prepare_up_path(agent)

    def boom(reason="up"):
        raise OSError("read-only filesystem")

    agent.monkey.setattr(agent.launchd, "want_set", boom)
    code, exc = run_cli(agent, ["up"])
    assert exc is None and code == 0, (code, exc)
    assert "autostart intent not recorded" in capsys.readouterr().out


def test_wait_ready_reports_foreign_port_squatters(agent, capsys):
    clock = Clock()
    agent.monkey.setattr(agent.cli, "time", clock)
    agent.monkey.setattr(agent.cli, "_moonraker_get",
                         lambda path, timeout=2: None)
    agent.monkey.setattr(agent.cli.process, "our_listeners", lambda: [])
    agent.monkey.setattr(agent.cli.process, "port_conflicts",
                         lambda: [(7125, 99, "foreign moonraker")])
    assert agent.cli._wait_ready(timeout=1) is False
    out = capsys.readouterr().out
    assert "Moonraker did not answer within 1s" in out
    assert "Port 7125 is answered by pid 99 (foreign moonraker)" in out


def test_wait_ready_reports_our_own_silent_listeners(agent, capsys):
    clock = Clock()
    agent.monkey.setattr(agent.cli, "time", clock)
    agent.monkey.setattr(agent.cli, "_moonraker_get",
                         lambda path, timeout=2: None)
    agent.monkey.setattr(agent.cli.process, "port_conflicts", lambda: [])
    agent.monkey.setattr(agent.cli.process, "our_listeners",
                         lambda: [(7125, 42), (8081, 43)])
    assert agent.cli._wait_ready(timeout=1) is False
    out = capsys.readouterr().out
    assert "Our own processes are alive on port(s) 7125, 8081" in out
    assert "klipperformac restart" in out


# ------------------------------------------------------------ uninstall -----
def prepare_uninstall_path(agent):
    cli = agent.cli
    calls = []
    agent.monkey.setattr(cli.process, "kill_all",
                         lambda: calls.append("kill_all") or [])
    agent.monkey.setattr(cli.cam, "kill_orphans", lambda: [])
    agent.monkey.setattr(cli.batteryguard, "kill_orphans", lambda: [])
    # A real (throwaway) install marker, so the app folder is deletable for the
    # right reason and the only refusal left in the run is the one under test.
    (agent.paths.APP_HOME / "src").mkdir(parents=True, exist_ok=True)
    return calls


def test_uninstall_boots_the_agent_out_before_it_kills_anything(agent, capsys):
    """The order this whole slice exists for: teardown is step 0, inside the
    lock, in front of kill_all - so nothing is left that launchd can respawn
    into a tree that is being deleted."""
    calls = prepare_uninstall_path(agent)
    order = []
    spy(agent, "launchd.uninstall_teardown", order, tag="teardown")
    code, exc = run_cli(agent, ["uninstall", "--keep-data"])
    assert exc is None and code == 0, (code, exc)
    assert order == ["teardown"] and calls == ["kill_all"]
    assert "teardown ran" not in capsys.readouterr().out


def test_uninstall_prints_what_launchd_was_told(agent, capsys):
    calls = prepare_uninstall_path(agent)
    agent.monkey.setattr(agent.launchd, "uninstall_teardown",
                         lambda: (True, ["LaunchAgent unloaded from gui/501.",
                                         "Removed LaunchAgent: /tmp/x.plist"]))
    code, exc = run_cli(agent, ["uninstall", "--keep-data"])
    assert exc is None and code == 0, (code, exc)
    out = capsys.readouterr().out
    assert "LaunchAgent unloaded from gui/501." in out
    assert "Removed LaunchAgent: /tmp/x.plist" in out


def test_a_login_agent_that_could_not_be_removed_fails_the_uninstall(agent):
    """An uninstall that says "Uninstalled." while a login item nobody could
    unload is still in place would be a lie about the user's next boot."""
    prepare_uninstall_path(agent)
    agent.monkey.setattr(agent.launchd, "uninstall_teardown",
                         lambda: (False, ["[!] Could not unload the agent"]))
    code, exc = run_cli(agent, ["uninstall", "--keep-data"])
    assert exc is None and code == 1, (code, exc)


def test_an_agent_that_cannot_be_deleted_is_a_refusal_not_a_traceback(agent,
                                                                      capsys):
    """The worst case is not a foreign file but an undeletable one: a folder
    macOS has locked (or a mode nobody remembered). uninstall has to say so and
    exit nonzero, not report "Uninstalled." over a login item that survives."""
    prepare_uninstall_path(agent)
    path = agent.installed_plist()
    folder = path.parent
    folder.chmod(0o500)          # read+execute, no write: unlink is refused
    try:
        code, exc = run_cli(agent, ["uninstall", "--keep-data"])
    finally:
        folder.chmod(0o755)
    assert exc is None, "uninstall must never leak a traceback: {!r}".format(exc)
    assert code == 1, code
    out = capsys.readouterr().out
    assert "Could not remove" in out and "left in place" in out, out
