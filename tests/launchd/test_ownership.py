"""Who does this agent file belong to, and what happens when it does not parse.

`login` writes into ~/Library/LaunchAgents - a folder that belongs to the user's
whole account, shared with every other app's background items - so the rule the
rest of these tests lean on is: this tool only overwrites or deletes a file it
can *prove* it wrote. A label we did not mint, an argv we did not build, or an
app home that is not ours means hands off, and the user gets the reason.

The one deliberate exception is a file at our exact path that is not a property
list at all: the name is our label, so the only author of a broken file there is
our own earlier write (a crash, a disk full, a hand edit). That case is
recovered by keeping the broken bytes next to it and writing a fresh agent -
otherwise `login on` would be permanently stuck.
"""
import plistlib


def write_raw(agent, payload=None, raw=None):
    """Put bytes at the agent path the way any other author would: by hand."""
    path = agent.launchd.plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is None:
        raw = plistlib.dumps(payload)
    path.write_bytes(raw)
    return path


def foreign(agent, **changes):
    """A payload that looks like ours and then is not."""
    data = agent.launchd.job_dict()
    data.update(changes)
    return data


def test_is_ours_is_true_only_for_the_file_we_wrote(agent):
    path = agent.installed_plist()
    assert agent.launchd.is_ours(path)
    assert agent.launchd.installed()
    assert agent.launchd.read_plist(path) == agent.launchd.job_dict()


def test_a_foreign_label_is_never_touched(agent):
    write_raw(agent, foreign(agent, Label="com.somebody.else.supervisor"))
    assert not agent.launchd.installed()
    ok, lines = agent.launchd.install_plist()
    assert not ok and "not this install" in " ".join(lines)
    ok, lines = agent.launchd.remove_plist()
    assert not ok and "not this install" in " ".join(lines)
    assert agent.launchd.plist_path().read_bytes(), "the file must still be there"


def test_our_label_but_a_foreign_program_is_not_ours(agent):
    args = list(agent.launchd.job_dict()["ProgramArguments"])
    write_raw(agent, foreign(agent, ProgramArguments=["/opt/other/bin/tool"] + args[1:]))
    assert not agent.launchd.installed()
    assert "different install" in str(agent.launchd.plist_stale())


def test_our_label_but_no_supervise_in_the_argv_is_not_ours(agent):
    """A hypothetical `login` agent that runs the stack directly (the forbidden
    fork-and-exit shape) is not ours either: this tool only owns its own job."""
    args = [str(a) for a in agent.launchd.job_dict()["ProgramArguments"]]
    write_raw(agent, foreign(agent, ProgramArguments=[args[0], "up"]))
    assert not agent.launchd.installed()


def test_our_argv_but_another_installs_home_is_not_ours(agent):
    env = dict(agent.launchd.job_dict()["EnvironmentVariables"])
    env["KLIPPERFORMAC_HOME"] = str(agent.home.parent / "other-klipperformac")
    write_raw(agent, foreign(agent, EnvironmentVariables=env))
    assert not agent.launchd.installed()
    ok, _lines = agent.launchd.install_plist()
    assert not ok


def test_a_moved_data_folder_is_ours_but_stale(agent):
    """`data --set` then `login on` must rewrite, not refuse: the agent is
    ours, it just names a folder that no longer exists."""
    agent.installed_plist()
    before = agent.launchd.read_plist()
    data = agent.paths.DATA
    moved = agent.home / "newdata"
    agent.paths.set_data(moved)
    try:
        assert agent.launchd.installed(), "still ours..."
        stale = agent.launchd.plist_stale()
        assert stale and "data folder" in stale
        ok, _lines = agent.launchd.install_plist()
        assert ok
        after = agent.launchd.read_plist()
        assert after["EnvironmentVariables"]["KLIPPERFORMAC_DATA"] == str(moved)
        assert after["StandardOutPath"].startswith(str(moved))
        assert after["Label"] == before["Label"]
        assert not agent.launchd.plist_stale()
    finally:
        agent.paths.set_data(data)


def test_a_program_that_vanished_is_reported_stale(agent):
    """The everyday breakage this wave cannot prevent: the app folder is moved
    or reinstalled and the login item still exec's a path that is gone. launchd
    will fail the job in silence, so `login status` has to say it."""
    data = agent.launchd.job_dict()
    missing = str(agent.home / "bin" / "klipperformac")
    args = [str(a) for a in data["ProgramArguments"]]
    args[0] = missing
    data["ProgramArguments"] = args
    write_raw(agent, data)
    assert agent.launchd.installed(), "it is still recognisably ours"
    stale = agent.launchd.plist_stale()
    assert stale and missing in stale and "gone" in stale, stale
    lines = agent.launchd.status_lines()
    assert any("stale" in line for line in lines), lines


def test_no_agent_means_off_not_stale(agent):
    assert agent.launchd.plist_stale() is None
    assert not agent.launchd.installed()
    ok, lines = agent.launchd.remove_plist()
    assert ok and any("No LaunchAgent" in line for line in lines), lines


def test_a_directory_where_the_agent_should_be_is_refused(agent):
    path = agent.launchd.plist_path()
    path.mkdir(parents=True)
    ok, lines = agent.launchd.install_plist()
    assert not ok and "folder" in " ".join(lines)
    assert agent.launchd.read_plist(path) == {}
    assert "a folder, not a property list" in str(agent.launchd.plist_stale())


def test_a_malformed_agent_is_kept_and_replaced(agent):
    """Recovery, not deletion: the bytes are preserved, so a user can still see
    what their login item said before this tool rewrote it."""
    path = write_raw(agent, raw=b'<?xml version="1.0"?>\n<plist><dict><key>Lab')
    assert not agent.launchd.installed()
    assert agent.launchd.read_plist(path) == {}
    ok, lines = agent.launchd.install_plist()
    assert ok, lines
    assert any("not a readable plist" in line for line in lines), lines
    assert agent.launchd.is_ours(path)
    backups = [p for p in path.parent.iterdir() if ".corrupt-" in p.name]
    assert len(backups) == 1, [p.name for p in path.parent.iterdir()]
    assert backups[0].read_bytes() == b'<?xml version="1.0"?>\n<plist><dict><key>Lab'


def test_a_dry_run_of_the_same_recovery_moves_nothing(agent):
    raw = b"not a plist at all"
    path = write_raw(agent, raw=raw)
    ok, lines = agent.launchd.install_plist(dry_run=True)
    assert ok and any("Would keep the unreadable file" in line for line in lines)
    assert path.read_bytes() == raw
    assert not any(".corrupt-" in p.name for p in path.parent.iterdir())


def test_a_plist_whose_root_is_not_a_dictionary_reads_as_foreign(agent):
    write_raw(agent, raw=plistlib.dumps(["com.klipperformac.supervisor"]))
    assert agent.launchd.read_plist() == {}
    ok, lines = agent.launchd.install_plist()
    assert ok, lines
    assert agent.launchd.installed()


def test_two_broken_agents_keep_two_pieces_of_evidence(agent):
    """A timestamp-only backup name would overwrite the first file inside the
    same second, and a user comparing the two is the only witness left."""
    path = write_raw(agent, raw=b"garbage-one")
    agent.launchd.install_plist()
    write_raw(agent, raw=b"garbage-two")
    ok, _lines = agent.launchd.install_plist()
    assert ok and agent.launchd.installed()
    backups = {p.name: p.read_bytes() for p in path.parent.iterdir()
               if ".corrupt-" in p.name}
    assert len(backups) == 2, backups
    assert sorted(backups.values()) == [b"garbage-one", b"garbage-two"]
