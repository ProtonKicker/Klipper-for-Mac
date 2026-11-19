"""The integration the backup module deliberately left dangling.

`backup.doctor_lines()` shipped as a tested-but-uncalled helper: its docstring
says the two lines that make it real belong to "whoever owns cli.py's doctor",
and in this wave the scheduling slice was editing `cmd_doctor()` at the same
time. So the wiring is pinned here, by the integrator, against the promise the
helper makes:

    doctor prints the backup answer, and a missing backup is ADVISORY.

That second half is the judgement call, and it is the reason this file exists.
`backup.doctor_lines()` returns `(False, "no backups yet - ...")` on an install
that has a printer.cfg and has never been backed up. If that flipped doctor's
exit code, `klipperformac doctor` would fail on a perfectly healthy install (and
`make release-check` runs doctor as a gate, AGENTS.md 4). So the line is printed
with its honest [!] tag and never touches `ok` - the same way the on-battery
line in `_doctor_scheduling_lines()` warns without failing the install, and the
same way the camera lines only bite when a camera is actually configured.
"""
import argparse

import pytest

PRINTER_CFG = """\
[printer]
kinematics: cartesian
max_velocity: 300
"""


@pytest.fixture
def quiet_doctor(bstack, monkeypatch):
    """`cmd_doctor` with everything except the backup line stubbed out.

    Mirrors the scheduling slice's own doctor fixture (tests/rt/test_rt_timing_cli.py)
    rather than inventing a second shape: no prereq checks, no chelper build, a
    fake pristine upstream, no processes, no telemetry, no camera.
    """
    monkeypatch.setattr(bstack.cli.installer, "check_prereqs",
                        lambda strict=False, **kw: [])
    monkeypatch.setattr(bstack.cli.installer, "verify_pristine", lambda: [])
    monkeypatch.setattr(bstack.paths, "KLIPPER", bstack.home / "klipper")
    (bstack.home / "klipper").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bstack.paths, "VENV_PY", bstack.home / "no-python")
    monkeypatch.setattr(bstack.process, "running", lambda: None)
    monkeypatch.setattr(bstack.shaper, "readiness",
                        lambda: {"needed": False, "healthy": True,
                                 "detail": "stubbed"})
    # No telemetry: doctor handles a scheduling report that cannot be read, and
    # this file is about the backup line, not about ps/pmset/ioreg.
    def explode():
        raise OSError(2, "stubbed: no telemetry in this test")
    monkeypatch.setattr(bstack.cli, "_timing_report", explode)
    monkeypatch.setattr(bstack.cam, "running", lambda: None)
    monkeypatch.setattr(bstack.cam, "doctor", lambda: [])
    monkeypatch.setattr(bstack.configgen, "webcam_state",
                        lambda *a, **kw: (False, ""))
    return bstack


def run_doctor(doctor, capsys):
    with pytest.raises(SystemExit) as stop:      # cmd_doctor always exits
        doctor.cli.cmd_doctor(argparse.Namespace())
    return stop.value.code, capsys.readouterr().out


def test_the_backup_module_is_the_one_doctor_calls(quiet_doctor):
    """`from . import backup` inside cmd_doctor must land on the SAME reloaded
    module the fixtures bind to the throwaway home - otherwise doctor would read
    the developer's real ~/.klipperformac/data/backups from inside a test."""
    import klipperformac.backup as live
    assert live is quiet_doctor.backup
    assert str(live.backups_dir()).startswith(str(quiet_doctor.paths.DATA))


def test_doctor_prints_the_backup_line(quiet_doctor, capsys):
    code, out = run_doctor(quiet_doctor, capsys)
    assert "backups" in out, out


def test_a_config_and_no_backups_is_a_warning_that_does_not_fail_the_install(
        quiet_doctor, capsys):
    """The whole point of the wiring: the user sees the nudge, doctor stays green."""
    assert quiet_doctor.paths.PRINTER_CFG.is_file()
    code, out = run_doctor(quiet_doctor, capsys)
    assert code == 0, out
    assert "[!] no backups yet" in out
    assert "klipperformac backup create" in out


def test_a_fresh_backup_reads_ok(quiet_doctor, capsys):
    quiet_doctor.backup.create()
    code, out = run_doctor(quiet_doctor, capsys)
    assert code == 0, out
    assert "[ok] backups: 1 archive(s)" in out


def test_a_stale_backup_warns_without_failing_the_install(quiet_doctor, capsys):
    quiet_doctor.backup.create()
    # The name is the timestamp (a copied folder's mtime lies), so renaming is
    # exactly how a year-old backup looks to doctor.
    where = quiet_doctor.backup.backups_dir()
    for p in list(where.iterdir()):
        if p.name.endswith(".tar.gz"):
            p.rename(where / "klipperformac-backup-20000101-000000.tar.gz")
    code, out = run_doctor(quiet_doctor, capsys)
    assert code == 0, out
    assert "[!] newest backup is" in out


def test_an_unreadable_backup_folder_still_prints_everything_else(
        quiet_doctor, capsys, monkeypatch):
    """A helper that raises must cost doctor one line, not the report."""
    def boom():
        raise OSError(13, "permission denied")
    monkeypatch.setattr(quiet_doctor.backup, "doctor_lines", boom)
    code, out = run_doctor(quiet_doctor, capsys)
    assert code == 0, out
    assert "backups: not reported" in out
    assert "host scheduling posture: not measured" in out


def test_backup_lines_come_last_and_do_not_swallow_the_others(quiet_doctor,
                                                              capsys):
    """Ordering matters for a human reading the report: the backup line is the
    tail, and it does not disturb the lines above it."""
    quiet_doctor.backup.create()
    code, out = run_doctor(quiet_doctor, capsys)
    lines = [line for line in out.splitlines() if line]
    assert any("shaper" in line for line in lines)
    assert lines[-1].startswith(("[ok] backups:", "[!] backups:",
                                "[!] newest backup", "[!] no backups"))
