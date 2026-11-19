"""cli.py: command handlers against a throwaway stack (no network, no stack)."""

import os
import types

import pytest

from klipperformac import cli


def _args(**kw):
    return types.SimpleNamespace(**kw)


def test_status_reports_stopped(stack, capsys, monkeypatch):
    # keyless offline: check_updates must degrade, never block the net.
    def boom(url):
        raise OSError("offline in tests")

    monkeypatch.setattr(stack.versions, "_get_json", boom)
    cli.cmd_status(_args())
    out = capsys.readouterr().out
    assert "stopped" in out


def test_status_full_output_when_tracked(stack, capsys, monkeypatch):
    import json

    def boom(url):
        raise OSError("offline in tests")

    monkeypatch.setattr(stack.versions, "_get_json", boom)
    # A state file whose pids don't exist => every service "DOWN".
    stack.paths.RUN.mkdir(parents=True)
    bt = stack.process._boot_time()
    data = {"boot_time": bt}
    for i, name in enumerate(stack.process.SERVICES):
        data[name] = 999_999 + i
    stack.paths.STATE.write_text(json.dumps(data))
    cli.cmd_status(_args())
    out = capsys.readouterr().out
    assert "url" in out
    assert "moonraker" in out
    assert "DOWN" in out


def test_data_reports_folder(stack, capsys):
    cli.cmd_data(_args(set=None))
    out = capsys.readouterr().out
    assert "Data folder:" in out
    assert str(stack.data) in out


def test_relocate_refuses_home_and_root(stack):
    r = cli.relocate_data(os.path.expanduser("~"))
    assert "Refusing" in r
    r = cli.relocate_data("/")
    assert "Refusing" in r


def test_relocate_moves_contents(stack, capsys):
    (stack.paths.DATA / "config").mkdir(parents=True, exist_ok=True)
    (stack.paths.DATA / "gcodes").mkdir(exist_ok=True)
    (stack.paths.DATA / "gcodes" / "cube.gcode").write_text("M117 hi\n")
    new = stack.home / "moved"
    result = cli.relocate_data(str(new))
    assert (new / "gcodes" / "cube.gcode").exists()
    assert stack.paths.load_settings()["data"].endswith("moved")
    assert "moved" in result


def test_ui_rejects_bad_name(stack):
    with pytest.raises(SystemExit):
        cli.cmd_ui(_args(name="notavalidui", no_open=True))


def test_ui_sets_pin(stack, capsys):
    cli.cmd_ui(_args(name="mainsail", no_open=True))
    assert stack.versions.load_pins()["ui"] == "mainsail"


def test_presets_save_use_flow(stack, capsys):
    stack.paths.CONFIG.mkdir(parents=True, exist_ok=True)
    stack.paths.PRINTER_CFG.write_text("[mcu]\nserial: /a\n")
    cli.cmd_presets(
        _args(
            save="bench",
            use=None,
            import_=None,
            import_name=None,
            library=None,
            use_library=None,
            history=None,
            restore=None,
            snapshot=None,
            status=None,
            auto_serial=None,
        )
    )
    assert (stack.paths.PRESETS / "bench.cfg").exists()

    cli.cmd_presets(
        _args(
            save=None,
            use="bench",
            import_=None,
            import_name=None,
            library=None,
            use_library=None,
            history=None,
            restore=None,
            snapshot=None,
            status=None,
            auto_serial=None,
        )
    )
    assert stack.paths.CONFIG.joinpath("printer.cfg.bak").exists()
    out = capsys.readouterr().out
    assert "applied" in out


def test_presets_use_missing_dies(stack):
    with pytest.raises(SystemExit):
        cli.cmd_presets(
            _args(
                save=None,
                use="nope",
                import_=None,
                import_name=None,
                library=None,
                use_library=None,
                history=None,
                restore=None,
                snapshot=None,
                status=None,
                auto_serial=None,
            )
        )


def test_lan_toggle_persists_and_restart_hint(stack, capsys):
    stack.paths.save_settings({"lan": False})
    cli.cmd_lan(_args(state="on"))
    out = capsys.readouterr().out
    assert "OPEN" in out
    assert stack.paths.lan_enabled() is True


def test_lan_off_closed(stack, capsys):
    stack.paths.save_settings({"lan": True})
    cli.cmd_lan(_args(state="off"))
    out = capsys.readouterr().out
    assert "closed" in out
    assert stack.paths.lan_enabled() is False
