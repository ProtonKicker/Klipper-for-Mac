"""Path sync helpers must tolerate hand-edited but still-valid configs."""


def test_sync_virtual_sdcard_updates_path_even_when_not_first_key(stack):
    stack.paths.PRINTER_CFG.write_text(
        "[virtual_sdcard]\n"
        "on_error_gcode: CANCEL_PRINT\n"
        "path: /Volumes/old/gcodes\n")
    assert stack.configgen.sync_virtual_sdcard() is True
    text = stack.paths.PRINTER_CFG.read_text()
    assert "on_error_gcode: CANCEL_PRINT" in text
    assert "path: " + str(stack.paths.DATA / "gcodes") in text


def test_sync_virtual_sdcard_restores_missing_path(stack):
    stack.paths.PRINTER_CFG.write_text(
        "[virtual_sdcard]\n"
        "on_error_gcode: CANCEL_PRINT\n")
    assert stack.configgen.sync_virtual_sdcard() is True
    text = stack.paths.PRINTER_CFG.read_text()
    assert "on_error_gcode: CANCEL_PRINT" in text
    assert "path: " + str(stack.paths.DATA / "gcodes") in text


def test_sync_moonraker_conf_only_rewrites_server_socket(stack):
    stack.paths.MOONRAKER_CONF.write_text(
        "[bridge]\n"
        "klippy_uds_address: /tmp/other.sock\n\n"
        "[server]\n"
        "port: 7125\n"
        "klippy_uds_address: /tmp/old-klippy.sock\n")
    assert stack.configgen.sync_moonraker_conf() is True
    text = stack.paths.MOONRAKER_CONF.read_text()
    assert "[bridge]\nklippy_uds_address: /tmp/other.sock" in text
    assert "klippy_uds_address: " + str(stack.paths.API_SOCKET) in text


def test_sync_moonraker_conf_restores_missing_server_keys(stack):
    stack.paths.MOONRAKER_CONF.write_text(
        "[authorization]\n"
        "trusted_clients:\n"
        "    127.0.0.1\n")
    assert stack.configgen.sync_moonraker_conf() is True
    text = stack.paths.MOONRAKER_CONF.read_text()
    assert "[server]" in text
    assert "host: 127.0.0.1" in text
    assert "klippy_uds_address: " + str(stack.paths.API_SOCKET) in text
