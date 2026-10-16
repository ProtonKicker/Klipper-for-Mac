"""The CLI surface of `klipperformac flash`: the subparser wiring in cli.py, the
thin cmd_flash dispatch, and the argument errors a human actually hits.

cli.py owns the flags and nothing else - flash.dispatch() owns what they mean -
so this file is where "the two halves agree" gets pinned, including the promise
that adding `flash` did not disturb any existing command (cli.py is 2150 lines
that other work also depends on).
"""
import argparse
import types

import pytest


def _capture_dispatch(fstack, monkeypatch):
    """Return (calls, patch): flash.dispatch is replaced by a recorder so a test
    can assert exactly what argparse handed the module."""
    calls = []

    def fake(args):
        calls.append(args)
        return 0

    monkeypatch.setattr(fstack.flash, "dispatch", fake)
    return calls


def test_flash_is_a_registered_subcommand(fstack, monkeypatch):
    """handlers["flash"] is cmd_flash: without the entry the subparser builds
    and then does nothing at all."""
    seen = []
    monkeypatch.setattr(fstack.cli, "cmd_flash",
                        lambda args: seen.append(args.action))
    fstack.cli.main(["flash", "detect"])
    assert seen == ["detect"]


def test_cmd_flash_is_thin_and_delegates_to_dispatch(fstack, monkeypatch):
    """The one thing cmd_flash may do is hand the namespace to
    flash.dispatch(args) and honour its exit code."""
    calls = _capture_dispatch(fstack, monkeypatch)
    fstack.cli.main(["flash", "doctor"])
    assert len(calls) == 1 and calls[0].action == "doctor"


def test_cmd_flash_propagates_a_nonzero_exit(fstack, monkeypatch):
    monkeypatch.setattr(fstack.flash, "dispatch", lambda args: 1)
    with pytest.raises(SystemExit) as exc:
        fstack.cli.main(["flash", "uf2", "--bin", "x.uf2"])
    assert exc.value.code == 1


def test_cmd_flash_does_not_exit_on_success(fstack, monkeypatch):
    """`flash detect` returning 0 must not raise SystemExit: the TUI and the
    GUI both call main() and read stdout."""
    _capture_dispatch(fstack, monkeypatch)
    fstack.cli.main(["flash", "detect", "--json"])


@pytest.mark.parametrize("argv,expect", [
    (["flash", "detect"], ("action", "detect")),
    (["flash", "detect", "--json"], ("json", True)),
    (["flash", "doctor"], ("action", "doctor")),
    (["flash", "status"], ("action", "status")),
    (["flash", "build", "--defconfig", "generic-bigtreetech-skr-pico-v1.0"
      ".cfg", "--chip", "rp2040", "--jobs", "8",
      "--set", "CONFIG_REBOOT=y", "--dry-run"], ("defconfig",
                                                 "generic-bigtreetech-skr-pico"
                                                 "-v1.0.cfg")),
    (["flash", "uf2", "--bin", "/tmp/k.uf2", "--volume", "RPI-RP2",
      "--timeout", "90", "--dry-run"], ("volume", "RPI-RP2")),
    (["flash", "uf2", "/tmp/k.uf2"], ("path", "/tmp/k.uf2")),
    (["flash", "dfu", "--bin", "/tmp/k.bin", "--chip", "stm32f407",
      "--vidpid", "0483:df11", "--alt", "0", "--address", "0x8004000",
      "--leave", "--dry-run"], ("leave", True)),
    (["flash", "serial", "--bin", "/tmp/k.bin", "--port", "/dev/cu.usbserial0",
      "--chip", "stm32f103", "--tool", "stm32flash", "--dry-run"],
     ("tool", "stm32flash")),
])
def test_the_flags_the_spec_names_all_exist(fstack, monkeypatch, argv, expect):
    """The GUI (wave 4) drives exactly these; the test is that argparse accepts
    each one and hands the right value down."""
    calls = _capture_dispatch(fstack, monkeypatch)
    fstack.cli.main(argv)
    assert len(calls) == 1
    name, value = expect
    assert getattr(calls[0], name) == value


@pytest.mark.parametrize("action,extra", [
    ("uf2", ["--bin", "/tmp/k.uf2"]),
    ("dfu", ["--bin", "/tmp/k.bin", "--vidpid", "0483:df11"]),
    ("serial", ["--bin", "/tmp/k.bin", "--port", "/dev/cu.usbserial0"]),
    ("build", ["--defconfig", "generic-bigtreetech-skr-pico-v1.0.cfg"]),
])
def test_every_writing_action_has_a_dry_run(fstack, monkeypatch, action, extra):
    """Nothing that can write to a chip may be ambiguous about whether it did."""
    calls = _capture_dispatch(fstack, monkeypatch)
    fstack.cli.main(["flash", action] + extra + ["--dry-run"])
    assert calls[0].dry_run is True


def test_detect_has_no_dry_run_because_it_writes_nothing(fstack):
    with pytest.raises(SystemExit) as exc:
        fstack.cli.main(["flash", "detect", "--dry-run"])
    assert exc.value.code == 2


def test_an_action_is_required(fstack):
    with pytest.raises(SystemExit) as exc:
        fstack.cli.main(["flash"])
    assert exc.value.code == 2  # argparse usage error, not a crash


def test_an_unknown_action_is_a_usage_error(fstack):
    """Anything that could brick a board must not be reachable by a typo:
    `flash erase-all` never even reaches the module."""
    with pytest.raises(SystemExit) as exc:
        fstack.cli.main(["flash", "erase-all"])
    assert exc.value.code == 2


def test_a_flag_without_its_value_is_a_usage_error(fstack):
    with pytest.raises(SystemExit) as exc:
        fstack.cli.main(["flash", "uf2", "--bin"])
    assert exc.value.code == 2


def test_serial_requires_a_port(fstack):
    """The STM32 serial bootloader is per-port; guessing it would flash the
    wrong board."""
    with pytest.raises(SystemExit) as exc:
        fstack.cli.main(["flash", "serial", "--bin", "/tmp/k.bin"])
    assert exc.value.code == 2


def test_the_dfu_chip_is_a_closed_list(fstack):
    with pytest.raises(SystemExit) as exc:
        fstack.cli.main(["flash", "dfu", "--bin", "/tmp/k.bin",
                         "--chip", "esp32"])
    assert exc.value.code == 2


def test_bin_and_positional_together_is_a_contradiction(fstack, capsys):
    """The spec's positional form and the brief's --bin form both work; giving
    both is refused instead of silently preferring one."""
    rec = fstack.use_recorder()
    with pytest.raises(SystemExit) as exc:
        fstack.cli.main(["flash", "uf2", "/tmp/a.uf2", "--bin", "/tmp/b.uf2"])
    assert exc.value.code == 1
    assert "Give the firmware file once" in capsys.readouterr().out
    assert rec.commands() == []


def test_no_firmware_at_all_is_refused(fstack, capsys):
    rec = fstack.use_recorder()
    with pytest.raises(SystemExit) as exc:
        fstack.cli.main(["flash", "uf2", "--dry-run"])
    assert exc.value.code == 1
    assert "No firmware file given" in capsys.readouterr().out
    assert rec.commands() == []


def test_dispatch_without_a_parser_still_refuses_an_unknown_action(fstack,
                                                                  capsys):
    """The module can be called directly (the GUI path, future tests): an
    unrecognised action is a usage error, never a fall-through."""
    code = fstack.flash.dispatch(types.SimpleNamespace(action="laser"))
    assert code == 2
    assert "Unknown flash action" in capsys.readouterr().out


def test_dispatch_detect_without_a_json_flag_never_prints_json(fstack,
                                                              monkeypatch,
                                                              capsys):
    """argparse hands a namespace with `json`; a caller that omits the attribute
    must still get the human report instead of an AttributeError."""
    monkeypatch.setattr(fstack.detect, "all_identities",
                        lambda timeout=None: [])
    fstack.use_recorder({"ioreg": (0, b"<plist><dict/></plist>")},
                        as_bytes=True)
    code = fstack.flash.dispatch(argparse.Namespace(action="detect"))
    assert code == 0
    assert "{" not in capsys.readouterr().out


def test_the_other_subcommands_still_parse(fstack, monkeypatch):
    """`flash` was added to a cli.py other work depends on: every pre-existing
    command must still build its parser and reach its handler unchanged."""
    seen = []
    for cmd, argv, attr in (
            ("status", ["status"], "cmd_status"),
            ("serial", ["serial"], "cmd_serial"),
            ("lan", ["lan", "off"], "cmd_lan"),
            ("presets", ["presets"], "cmd_presets"),
            ("backup", ["backup", "list"], "cmd_backup"),
            ("cam", ["cam", "status"], "cmd_cam"),
            ("timing", ["timing"], "cmd_timing"),
            ("doctor", ["doctor"], "cmd_doctor"),
    ):
        monkeypatch.setattr(fstack.cli, attr,
                            lambda args, name=cmd: seen.append(name))
        fstack.cli.main(argv)
    assert seen == ["status", "serial", "lan", "presets", "backup", "cam",
                    "timing", "doctor"]


def test_flash_help_names_the_three_transports(fstack, capsys):
    with pytest.raises(SystemExit) as exc:
        fstack.cli.main(["flash", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    # Asserted as single tokens: argparse re-wraps the description at the
    # terminal width, and a test that depended on a line break would be a
    # lie about the help text rather than a check of it.
    assert "BOOTSEL" in out
    assert "dfu-util" in out
    assert "boot0" in out
    for action in ("detect", "doctor", "build", "uf2", "dfu", "serial"):
        assert action in out


def test_the_top_level_help_lists_flash(fstack, capsys):
    with pytest.raises(SystemExit) as exc:
        fstack.cli.main(["--help"])
    assert exc.value.code == 0
    assert "flash" in capsys.readouterr().out
