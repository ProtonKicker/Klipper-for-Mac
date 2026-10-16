"""The CLI surface of `klipperformac backup`: the subparser wiring in cli.py, the
thin cmd_backup dispatch, and the error UX of every --from value a human (or a
fuzzer) can type.

cli.py owns the flags and nothing else - backup.dispatch() owns what they mean -
so this file is where "the two halves agree" gets pinned: the action choices, the
dest names, the exit codes, and the rule that a bad value is one clean line on
stderr rather than an interpreter traceback.
"""
import argparse
import sys

import pytest

GOOD = [("data/config/", "dir", b""),
        ("data/config/printer.cfg", "reg", "[printer]\nmax_velocity: 300\n"),
        ("data/config/moonraker.conf", "reg", "[server]\nhost: 127.0.0.1\n")]


def test_backup_is_a_registered_subcommand(bstack, monkeypatch):
    """`handlers["backup"] is cmd_backup` is the whole wiring: without it the
    subparser builds and then does nothing at all."""
    seen = []
    monkeypatch.setattr(bstack.cli, "cmd_backup",
                        lambda args: seen.append(args.action))
    bstack.cli.main(["backup", "list"])
    assert seen == ["list"]


def test_cmd_backup_is_thin_and_delegates_to_dispatch(bstack, monkeypatch):
    """The one thing cmd_backup may do is hand the namespace to backup.dispatch:
    no logic here, so the module can be tested without argparse."""
    seen = []
    monkeypatch.setattr(bstack.backup, "dispatch",
                        lambda args: seen.append(args))
    bstack.cli.main(["backup", "list"])
    assert len(seen) == 1 and seen[0].action == "list"


@pytest.mark.parametrize("action", ["create", "list", "verify", "restore",
                                    "prune"])
def test_every_action_reaches_the_module(bstack, monkeypatch, action):
    called = []
    monkeypatch.setattr(bstack.backup, "dispatch",
                        lambda args: called.append(args.action))
    bstack.cli.main(["backup", action])
    assert called == [action]


def test_the_action_is_required(bstack):
    with pytest.raises(SystemExit) as exc:
        bstack.cli.main(["backup"])
    assert exc.value.code == 2  # argparse usage error, not a crash


def test_an_unknown_action_is_a_usage_error(bstack):
    with pytest.raises(SystemExit) as exc:
        bstack.cli.main(["backup", "delete-everything"])
    assert exc.value.code == 2


def test_dispatch_refuses_an_action_argparse_did_not_see(bstack):
    """The guard the TUI/GUI need: a namespace built by hand, not by argparse."""
    with pytest.raises(SystemExit):
        bstack.backup.dispatch(argparse.Namespace(action="wipe"))
    with pytest.raises(SystemExit):
        bstack.backup.dispatch(argparse.Namespace(action=None))


def test_help_mentions_the_five_actions(bstack):
    out = []
    real = sys.stdout

    class Capture:
        def write(self, text):
            out.append(text)

        def flush(self):
            pass
    sys.stdout = Capture()
    try:
        with pytest.raises(SystemExit) as exc:
            bstack.cli.main(["backup", "--help"])
    finally:
        sys.stdout = real
    assert exc.value.code == 0
    text = "".join(out)
    for word in ("create", "list", "verify", "restore", "prune", "--from"):
        assert word in text


def test_top_level_help_lists_backup(bstack, capsys):
    with pytest.raises(SystemExit):
        bstack.cli.main(["--help"])
    assert "backup" in capsys.readouterr().out


# --------------------------------------------------------------- exit codes --

def test_verify_exits_zero_on_a_good_backup(bstack):
    bstack.backup.create()
    bstack.cli.main(["backup", "verify", "--from", "latest"])


def test_verify_exits_one_on_a_bad_one_and_says_why(bstack, tools, capsys):
    tools.write_backup(bstack.paths.DATA,
                       "klipperformac-backup-20260101-000000.tar.gz",
                       tools.build([("data/config/", "dir", b""),
                                    ("data/config/printer.cfg", "reg",
                                     "no section header\n"),
                                    ("data/config/moonraker.conf", "reg",
                                     "[server]\n")]))
    with pytest.raises(SystemExit) as exc:
        bstack.cli.main(["backup", "verify", "--from",
                        "klipperformac-backup-20260101-000000.tar.gz"])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "[ok]" in out and "[!]" in out
    assert "does NOT restore" in out


def test_restore_without_a_name_is_one_clean_line(bstack, capsys):
    bstack.backup.create()
    capsys.readouterr()  # what create printed is not what this is about
    with pytest.raises(SystemExit) as exc:
        bstack.cli.main(["backup", "restore"])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "--from NAME" in captured.err
    assert captured.out == ""  # nothing else was reported, nothing was written


def test_prune_keep_is_a_number_and_argparse_enforces_it(bstack):
    for bad in ("ten", ""):
        with pytest.raises(SystemExit) as exc:
            bstack.cli.main(["backup", "prune", "--keep", bad])
        assert exc.value.code == 2


# ------------------------------------------------------------ weird --from ---

@pytest.mark.parametrize("name", [
    "~0",                                   # a user called "0" does not exist
    "~nobody",
    "~",
    "\x00evil.tar.gz",
    "a\x00b.tar.gz",
    "..",
    "../x.tar.gz",
    ".",
    "./x.tar.gz",
    "data/backups/x.tar.gz",
    "/",
    "//tmp/x.tar.gz",
    "klipperformac-backup-20260101-000000.tar.gz/",
    "x.tar.gz; rm -rf /",
    "-name.tar.gz",
    "a" * 300 + ".tar.gz",
])
def test_a_strange_from_value_is_a_clean_line_never_a_traceback(bstack, name):
    """Regression: '~0' reached Path('~0').expanduser() and escaped as
    RuntimeError('Could not determine home directory'); a NUL reached stat() and
    escaped as ValueError. A name nobody can have typed is a `No backup named`
    line, and the only exit code is 1."""
    bstack.backup.create()
    with pytest.raises(SystemExit) as exc:
        bstack.cli.main(["backup", "verify", "--from", name])
    # 1 = refused by the command, 2 = argparse never let an option-looking value
    # through as a value. Neither is a traceback, which is what is being pinned.
    assert exc.value.code in (1, 2), name


def test_latest_is_a_real_alias(bstack, tools):
    tools.write_backup(bstack.paths.DATA,
                       "klipperformac-backup-20260101-000000.tar.gz",
                       tools.build(GOOD))
    newer = tools.write_backup(
        bstack.paths.DATA, "klipperformac-backup-20991231-235959.tar.gz",
        tools.build(GOOD))
    archive = bstack.backup._resolve_source("latest")
    assert archive == newer
    assert bstack.backup._resolve_source(None) == newer


def test_flags_are_forwarded_from_the_parser(bstack, tools, monkeypatch):
    """The dest names are the contract between cli.py's parser and backup.py's
    dispatch - a typo in either is silent, so the wiring is asserted, not trusted."""
    seen = {}

    def fake_create(with_logs=False, allow_running=False):
        seen["with_logs"] = with_logs
        seen["allow_running"] = allow_running
        return bstack.paths.DATA / "backups" / "x.tar.gz"
    monkeypatch.setattr(bstack.backup, "create", fake_create)
    bstack.cli.main(["backup", "create", "--with-logs", "--allow-running"])
    assert seen == {"with_logs": True, "allow_running": True}
    seen.clear()
    bstack.cli.main(["backup", "create"])
    assert seen == {"with_logs": False, "allow_running": False}


def test_prune_flags_are_forwarded(bstack, monkeypatch):
    seen = {}

    def fake_prune(keep=0, dry_run=False):
        seen["keep"] = keep
        seen["dry_run"] = dry_run
        return []
    monkeypatch.setattr(bstack.backup, "prune", fake_prune)
    bstack.cli.main(["backup", "prune"])
    assert seen == {"keep": bstack.backup.DEFAULT_KEEP, "dry_run": False}
    bstack.cli.main(["backup", "prune", "--keep", "3", "--dry-run"])
    assert seen == {"keep": 3, "dry_run": True}


def test_from_dest_is_underscored_for_python(bstack, monkeypatch):
    """argparse cannot put a '-' in a dest, and backup.dispatch reads
    args.from_ - the both-sides spelling of the same flag."""
    seen = {}

    def fake_verify(name=None):
        seen["name"] = name
        return True, [(True, "fine")]
    monkeypatch.setattr(bstack.backup, "verify", fake_verify)
    bstack.cli.main(["backup", "verify", "--from", "abc.tar.gz"])
    assert seen["name"] == "abc.tar.gz"
