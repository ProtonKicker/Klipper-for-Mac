"""The LaunchAgent payload, as a golden contract.

Every key here exists because a background job has to answer a question a
terminal command does not: who starts me, what may kill me, where do my logs go,
what environment do I get when there is no shell. The assertions are the
evidence table for those answers - and two of them are load-bearing for macOS
specifically: every EnvironmentVariables value must be a plain string (launchd
silently ignores anything else) and the plist must carry no secret, because it
is world-readable by design.
"""
import os
import plistlib
import stat
from pathlib import Path



def payload(agent, tick=None):
    """The plist payload, built through the module under test."""
    return agent.launchd.job_dict(tick)


def test_the_agent_lives_in_the_gui_domain_folder(agent):
    """~/Library/LaunchAgents is where a gui/<uid> agent is read from: a daemon
    plist (Library/LaunchDaemons) would need root and would run outside the
    Aqua session, i.e. no caffeinate for the console user and no camera.

    Paths only: nothing in this test may create that file for the real account,
    which is why the suite runs with the folder relocated by the fixture."""
    agent.monkey.delenv("KLIPPERFORMAC_LAUNCH_AGENTS")
    assert agent.launchd.agents_dir() == (
        Path.home() / "Library" / "LaunchAgents")
    assert agent.launchd.plist_path().parent == (
        Path.home() / "Library" / "LaunchAgents")
    agent.monkey.setenv("KLIPPERFORMAC_LAUNCH_AGENTS", str(agent.agents))
    assert agent.launchd.plist_path() == (
        agent.agents / "{}.plist".format(agent.launchd.label()))
    assert agent.launchd.domain() == "gui/{}".format(os.getuid())


def test_label_names_this_install_not_just_this_product(agent):
    """Two checkouts with different KLIPPERFORMAC_HOME must not boot out each
    other's job on the same Mac."""
    base = agent.launchd.label()
    other = agent.launchd.label(home=Path("/Users/you/.klipperformac-beta"))
    assert base != other and other.startswith("com.klipperformac.supervisor-")
    assert len(other.rsplit("-", 1)[1]) == 8
    # The default home keeps the clean label, which is what a human reads in
    # System Settings > Login Items.
    assert agent.launchd.label(home=Path("~/.klipperformac").expanduser()) == \
        "com.klipperformac.supervisor"
    agent.installed_plist()
    assert agent.launchd.installed()


def test_run_at_load_and_the_crash_policy(agent):
    """RunAtLoad = login. KeepAlive{SuccessfulExit=false} = restart me only if
    I *crashed*: our SIGTERM handler exits 0 on purpose, so `login off`, a
    logout and a bootout are never read as a crash to be undone."""
    data = payload(agent)
    assert data["Label"] == agent.launchd.label()
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["AbandonProcessGroup"] is True  # the stack outlives our bootout
    assert data["LimitLoadToSessionType"] == "Aqua"


def test_throttle_interval_is_at_least_30(agent):
    """A klipper that cannot open its serial port would otherwise be re-spawned
    as fast as launchd can fork: 30 s is both launchd's documented floor in
    practice and the smallest interval that is not a hammer."""
    data = payload(agent)
    assert data["ThrottleInterval"] >= 30
    assert data["ExitTimeOut"] >= 30  # a bootout gets a real grace, not 0


def test_the_program_is_this_installs_supervise(agent):
    data = payload(agent)
    args = [str(a) for a in data["ProgramArguments"]]
    assert "supervise" in args, args
    assert args[0] and Path(args[0]).is_absolute()
    if args[0].endswith("klipperformac"):
        assert Path(args[0]).is_file(), \
            "ProgramArguments[0] must exist or the job is marked insecure: " + args[0]
        assert "-m" not in args
    else:  # the documented fallback: a checkout with no launcher symlink
        assert args[:3] == [args[0], "-m", "klipperformac"]
        assert Path(args[0]).is_file()
    assert args[args.index("--tick") + 1] == "10"  # the default sweep


def test_tick_is_clamped_to_a_sane_floor(agent):
    assert payload(agent, tick=0.0)["ProgramArguments"][-1] == "1"
    assert payload(agent, tick=-5)["ProgramArguments"][-1] == "1"
    assert payload(agent, tick="junk")["ProgramArguments"][-1] == "10"
    assert payload(agent, tick=45)["ProgramArguments"][-1] == "45"


def test_logs_go_into_the_data_folder(agent):
    """A background job with no terminal needs a file, and it must sit with the
    rest of this install's logs so `klipperformac logs` and the backup tool see
    it - never in /tmp, which launchd happily keeps across a reboot."""
    data = payload(agent)
    logs = str(agent.paths.LOGS)
    assert data["StandardOutPath"].startswith(logs)
    assert data["StandardErrorPath"].startswith(logs)
    assert data["StandardOutPath"].endswith(".log")
    assert data["StandardErrorPath"].endswith(".log")
    assert data["StandardOutPath"] != data["StandardErrorPath"]
    assert data["WorkingDirectory"] == str(agent.paths.DATA)


def test_environment_values_are_all_plain_strings(agent):
    """man launchd.plist(5): non-string env values are ignored, and a plist
    that loads fine but has no PATH is the worst kind of failure - the job
    starts, cannot find python, and the only evidence is an empty log."""
    env = payload(agent)["EnvironmentVariables"]
    assert set(env) == set(agent.launchd.ENV_KEYS)
    for key, value in env.items():
        assert isinstance(value, str), "{} is {!r}".format(key, value)
    path_parts = env["PATH"].split(os.pathsep)
    assert os.path.dirname(agent.launchd.interpreter()) in path_parts
    for required in ("/usr/sbin", "/usr/bin", "/bin", "/sbin"):
        assert required in path_parts, required
    assert env["KLIPPERFORMAC_HOME"] == str(agent.paths.APP_HOME)
    assert env["KLIPPERFORMAC_DATA"] == str(agent.paths.DATA)
    assert env["PYTHONPATH"] == agent.launchd.repo_root()
    assert env["HOME"]


def test_the_plist_carries_no_secret(agent):
    """An allowlist, never os.environ: a shell with a token exported in it must
    not end up in a 0644 file that the whole account (and any backup of it) can
    read."""
    agent.monkey.setenv("KLIPPERFORMAC_TEST_SECRET", "hunter2-do-not-copy")
    agent.paths.save_settings({"moonraker_api_key": "SK-really-secret",
                               "cam": {"password": "camera-pw"}})
    agent.installed_plist()
    raw = agent.launchd.plist_path().read_bytes().decode()
    for leak in ("hunter2-do-not-copy", "SK-really-secret", "camera-pw",
                 "KLIPPERFORMAC_TEST_SECRET"):
        assert leak not in raw, leak
    assert stat.S_IMODE(agent.launchd.plist_path().stat().st_mode) == 0o644


def test_a_path_full_of_nastiness_survives_byte_identically(agent):
    """Property lists escape XML, and a home folder is allowed things a shell
    is not. The plist must still name the same bytes: this is the difference
    between a job that runs and a job reading a mangled KLIPPERFORMAC_DATA."""
    nastiest = str(agent.home.parent / 'a home "with" both & <tags> ünïcode 中')
    data = agent.paths.DATA
    agent.paths.set_data(Path(nastiest) / "data folder")
    try:
        agent.installed_plist()
        raw = agent.launchd.plist_path().read_bytes()
        written = plistlib.loads(raw)
        assert written["WorkingDirectory"] == str(agent.paths.DATA)
        assert written["StandardOutPath"] == str(
            agent.paths.LOGS / agent.launchd.AGENT_OUT_NAME)
        assert written["EnvironmentVariables"]["KLIPPERFORMAC_DATA"] == \
            str(agent.paths.DATA)
        assert b"&amp;" in raw or b"&lt;" in raw, "it really is an XML escape"
        # ...and round-tripping the parsed payload back gives the same paths.
        assert plistlib.loads(plistlib.dumps(written)) == written
    finally:
        agent.paths.set_data(data)


def test_the_written_file_is_the_payload_and_is_idempotent(agent):
    before = payload(agent)
    path = agent.installed_plist()
    assert plistlib.loads(path.read_bytes()) == before
    ok, lines = agent.launchd.install_plist()
    assert ok and any("Already installed" in line for line in lines), lines
    assert path.read_bytes() == plistlib.dumps(before, fmt=plistlib.FMT_XML)


def test_reinstall_after_a_move_rewrites_the_same_path(agent):
    agent.installed_plist()
    old = str(agent.launchd.plist_path())
    data = agent.paths.DATA
    moved = agent.home / "elsewhere"
    agent.paths.set_data(moved)
    try:
        agent.make_setup_home()
        ok, _lines = agent.launchd.install_plist()
        assert ok
        assert str(agent.launchd.plist_path()) == old  # label keyed on home
        fresh = plistlib.loads(agent.launchd.plist_path().read_bytes())
        assert fresh["EnvironmentVariables"]["KLIPPERFORMAC_DATA"] == str(moved)
        assert fresh["StandardOutPath"].startswith(str(moved))
    finally:
        agent.paths.set_data(data)
