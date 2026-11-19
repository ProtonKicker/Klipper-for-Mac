"""login on / off / status and uninstall's teardown, against a recorded launchctl.

launchd is the one part of this feature that is not ours to own, so every claim
here is about the *asking*: which command line we hand launchctl, in which order,
and what we say when the answer is no. The order is not ceremony - a bootout
before the unlink is what lets the supervisor exit 0 on a SIGTERM it understands,
and a bootout before `rmtree` is what stops a respawned job with no interpreter.

Nothing in this file can create a real login item: the agent folder is relocated
by the fixture and `_launchctl` - the single place the binary is ever exec'd - is
a table of answers.
"""
import plistlib


def write_foreign(agent):
    """Somebody else's plist sitting at our filename."""
    path = agent.launchd.plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = agent.launchd.job_dict()
    data["Label"] = "com.other.backup.agent"
    path.write_bytes(plistlib.dumps(data))
    return path


# ---------------------------------------------------------------- on --------
def test_login_on_writes_the_file_and_then_bootstraps_that_exact_path(agent):
    agent.make_setup_home()
    ok, lines = agent.launchd.login_on()
    assert ok, lines
    path = agent.launchd.plist_path()
    assert path.is_file() and agent.launchd.is_ours(path)
    assert agent.ctl.calls[0] == ["bootstrap", agent.launchd.domain(), str(path)]
    assert agent.launchd.want_state()["wanted by"] == "login on"
    assert any("Login start: ON" in line for line in lines), lines
    assert any("camera is never started automatically" in line for line in lines)


def test_login_on_refuses_before_setup_and_asks_launchctl_nothing(agent):
    ok, lines = agent.launchd.login_on()
    assert not ok and "setup" in " ".join(lines)
    assert agent.ctl.calls == [], "no bootstrap for an install that cannot run"
    assert not agent.launchd.plist_path().exists()
    assert agent.launchd.want_state() is None


def test_login_on_dry_run_writes_no_file_and_calls_no_launchctl(agent):
    agent.make_setup_home()
    ok, lines = agent.launchd.login_on(tick=7, dry_run=True)
    assert ok and agent.ctl.calls == []
    assert not agent.launchd.plist_path().exists()
    assert agent.launchd.want_state() is None
    assert any("--tick 7" in line or "supervise" in line for line in lines), lines


def test_an_already_loaded_job_is_booted_out_and_bootstrapped_again(agent):
    """launchd keeps the ProgramArguments it loaded: re-pointing the file without
    a reload would leave the old folders running, which is the opposite of what
    `login on` a second time means."""
    agent.make_setup_home()
    agent.ctl.replies["bootstrap"] = [
        (1, "", "Bootstrap failed: 5: Already loaded"), (0, "", "")]
    ok, _lines = agent.launchd.login_on()
    assert ok
    assert agent.ctl.verbs() == ["bootstrap", "bootout", "bootstrap", "print"]


def test_a_bootout_of_a_foreign_job_is_never_offered_by_that_path(agent):
    """The reload above only ever boots out OUR label: the target string carries
    it, so a name collision on the machine cannot unload somebody else."""
    agent.make_setup_home()
    agent.ctl.replies["bootstrap"] = [(1, "", "Bootstrap failed: 5: Already loaded"),
                                      (0, "", "")]
    agent.launchd.login_on()
    bootout = [call for call in agent.ctl.calls if call[0] == "bootout"][0]
    assert bootout[1] == "{}/{}".format(agent.launchd.domain(),
                                        agent.launchd.label())
    assert "com.other" not in " ".join(bootout)


def test_a_failed_bootstrap_keeps_the_agent_and_prints_the_exact_retry(agent):
    agent.make_setup_home()
    agent.ctl.replies["bootstrap"] = (3, "", "Operation not permitted")
    ok, lines = agent.launchd.login_on()
    assert not ok
    assert agent.launchd.plist_path().is_file(), "the file is the durable half"
    joined = " ".join(lines)
    assert "launchctl bootstrap {} {}".format(agent.launchd.domain(),
                                              agent.launchd.plist_path()) in joined


def test_a_data_folder_under_documents_is_warned_about_not_broken(agent):
    """AGENTS.md 5: macOS privacy can deny a background job access to the user's
    Documents. The install still works - the user just has to know where to look
    when a login does not bring the stack up.

    home_dir() is patched so "under Documents" is a real string comparison
    against a folder inside tmp_path: this test must never create, or look at,
    the developer's own ~/Documents."""
    agent.make_setup_home()
    fake_home = agent.home.parent
    data = agent.paths.DATA
    agent.monkey.setattr(agent.launchd, "home_dir", lambda: fake_home)
    agent.paths.set_data(fake_home / "Documents" / "Klipper for Mac")
    try:
        ok, lines = agent.launchd.login_on()
        assert ok, lines
        assert any("inside ~/Documents" in line for line in lines), lines
        assert any("data --set" in line for line in lines), lines
    finally:
        agent.paths.set_data(data)


def test_no_launchctl_is_one_clean_line_never_a_traceback(agent):
    agent.make_setup_home()
    agent.ctl.default = (None, "", "[Errno 2] No such file or directory: launchctl")
    ok, lines = agent.launchd.login_on()
    assert not ok
    assert any("launchctl" in line for line in lines)
    status = agent.launchd.login_status()
    assert status["job_loaded"] is None and "no launchctl" in status["launchctl_detail"]
    assert agent.launchd.plist_path().is_file(), "written, just not loadable here"


# ---------------------------------------------------------------- off -------
def test_login_off_boots_out_before_it_removes_the_file(agent):
    agent.make_setup_home()
    agent.launchd.login_on()
    path = agent.launchd.plist_path()
    assert path.is_file()
    agent.ctl.calls[:] = []          # only the `off` conversation matters now
    ok, lines = agent.launchd.login_off()
    assert ok, lines
    # bootout is the whole first half of `off`, with no read-only probe in front
    # of it: the order is the promise, and bootout is idempotent anyway.
    assert agent.ctl.verbs() == ["bootout"], agent.ctl.calls
    assert not path.exists()
    assert agent.launchd.want_state() is None
    assert any("keeps running until you run: klipperformac down" in line
               for line in lines), lines


def test_booting_out_a_job_that_was_never_loaded_is_a_success(agent):
    """The idempotence login off depends on: the user may have already turned
    the item off in System Settings, rebooted, or never logged back in. "Could
    not find service" is the state they asked for, not an error to report."""
    agent.ctl.replies["bootout"] = (
        3, "", 'Could not find service "com.klipperformac.supervisor" in domain '
               'for user gui: 501')
    ok, detail = agent.launchd.bootout()
    assert ok and detail == "was not loaded"
    ok, lines = agent.launchd.login_off()
    assert ok, lines


def test_login_off_twice_is_still_off(agent):
    agent.make_setup_home()
    agent.launchd.login_on()
    assert agent.launchd.login_off()[0]
    ok, lines = agent.launchd.login_off()
    assert ok, lines
    assert agent.launchd.want_state() is None


def test_login_off_leaves_a_foreign_agent_and_says_why(agent):
    path = write_foreign(agent)
    before = path.read_bytes()
    ok, lines = agent.launchd.login_off()
    assert not ok
    assert path.read_bytes() == before, "not ours, not ours to delete"
    assert any("not this install" in line for line in lines), lines
    assert "launchctl bootout" in " ".join(lines), "the manual command is printed"


# -------------------------------------------------------------- status ------
def test_status_reports_the_job_launchd_actually_has(agent):
    agent.make_setup_home()
    agent.ctl.replies["print"] = (0, "  state = running\n  pid = 4242\n", "")
    ok, _lines = agent.launchd.login_on()
    assert ok
    status = agent.launchd.login_status()
    assert status["installed"] and status["job_loaded"] is True
    assert status["job_state"] == "running" and status["job_pid"] == 4242
    assert status["stack_intent"] == "up"
    assert status["throttle_interval"] >= 30


def test_status_names_a_login_item_macos_has_switched_off(agent):
    """The confusing real-world case: the plist is there, `launchctl print` says
    nothing, and the reason lives in System Settings. Without this line the user
    reboots twice before finding it."""
    agent.installed_plist()
    agent.ctl.replies["print"] = (
        3, "", "Could not find service \"com.klipperformac.supervisor\"")
    agent.ctl.replies["print-disabled"] = (
        0, '{\n    "%s" = true;\n}\n' % agent.launchd.label(), "")
    assert agent.launchd.domain_disabled() is True
    lines = agent.launchd.status_lines()
    assert any("not loaded" in line and "Login Items" in line for line in lines), lines


def test_domain_disabled_accepts_both_reply_spellings(agent):
    for body, expected in (('"{}" => 1;'.format(agent.launchd.label()), True),
                           ('"{}" => 0;'.format(agent.launchd.label()), False),
                           ('"{}" = false;'.format(agent.launchd.label()), False),
                           ('"com.other.agent" = true;', False)):
        agent.ctl.replies["print-disabled"] = (0, body, "")
        assert agent.launchd.domain_disabled() is expected, body
    agent.ctl.replies["print-disabled"] = (1, "", "Operation not permitted")
    assert agent.launchd.domain_disabled() is None


def test_status_of_an_off_install_says_off_and_turns_nothing(agent):
    status = agent.launchd.login_status()
    assert not status["installed"] and status["stack_intent"] == "down"
    lines = agent.launchd.status_lines()
    assert lines[0].startswith("Login start: off")
    assert any("klipperformac login on" in line for line in lines)
    assert not [v for v in agent.ctl.verbs()
                if v in ("bootstrap", "bootout", "kickstart")], \
        "status may ask launchd, it may never tell it"


def test_status_reports_a_latched_supervisor(agent):
    import time
    agent.launchd._write_sup_state({"restarts": [time.time()] * 3,
                                    "latched": True})
    agent.installed_plist()
    lines = agent.launchd.status_lines()
    assert any("latched" in line for line in lines), lines
    assert agent.launchd.login_status()["latched"] is True


# ------------------------------------------------------------ uninstall -----
def test_uninstall_teardown_is_silent_when_autostart_was_never_used(agent):
    """The promise the research test plan makes: an uninstall on a machine that
    never turned this on prints nothing about launchd."""
    agent.ctl.replies["print"] = (3, "", "Could not find service")
    ok, lines = agent.launchd.uninstall_teardown()
    assert ok and lines == []


def test_uninstall_boots_out_then_removes_then_reports(agent):
    agent.make_setup_home()
    agent.launchd.login_on()
    agent.ctl.calls[:] = []
    agent.launchd._write_sup_state({"restarts": [1.0], "latched": True})
    path = agent.launchd.plist_path()
    ok, lines = agent.launchd.uninstall_teardown()
    assert ok, lines
    # print = the read-only "is it loaded?" probe, bootout = the action.
    assert agent.ctl.verbs() == ["print", "bootout"], agent.ctl.calls
    assert not path.exists()
    assert not agent.launchd.sup_state_path().exists()
    assert any("Removed LaunchAgent" in line for line in lines), lines


def test_a_bootout_that_fails_still_removes_the_file_and_prints_the_command(agent):
    """The file is the durable half: leaving it behind would re-load a supervisor
    with no interpreter on the next login. The manual bootout is the session's
    half, and it is named exactly."""
    agent.make_setup_home()
    agent.launchd.login_on()
    agent.ctl.replies["bootout"] = (1, "", "Operation not permitted")
    path = agent.launchd.plist_path()
    ok, lines = agent.launchd.uninstall_teardown()
    assert not ok and path.exists() is False
    joined = " ".join(lines)
    assert "launchctl bootout {}/{}".format(agent.launchd.domain(),
                                            agent.launchd.label()) in joined


def test_uninstall_bootouts_a_loaded_job_whose_file_a_human_deleted(agent):
    """The crash loop this ordering prevents: launchd still holds the job (so it
    can still respawn it), and only `print` would tell us. Teardown asks."""
    agent.ctl.replies["print"] = (0, "  state = running\n  pid = 99\n", "")
    ok, lines = agent.launchd.uninstall_teardown()
    assert ok, lines
    assert agent.ctl.verbs() == ["print", "bootout"], agent.ctl.calls


def test_uninstall_refuses_a_foreign_agent_and_reports_the_problem(agent):
    path = write_foreign(agent)
    before = path.read_bytes()
    ok, lines = agent.launchd.uninstall_teardown()
    assert not ok
    assert path.read_bytes() == before
    assert any("did not write" in line for line in lines), lines
