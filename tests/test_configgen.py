"""configgen.py: moonraker.conf generation, LAN toggle, sd-card path sync."""


def _write_moonraker(stack, host="127.0.0.1"):
    stack.paths.MOONRAKER_CONF.write_text(
        stack.configgen.MOONRAKER_CFG.format(
            host=host,
            moonraker_port=stack.paths.MOONRAKER_PORT,
            uds_address=str(stack.paths.API_SOCKET),
            web_port=stack.paths.WEB_PORT,
            fluidd_port=stack.paths.FLUIDD_PORT,
        )
    )


def test_generated_conf_shape(stack):
    _write_moonraker(stack)
    text = stack.paths.MOONRAKER_CONF.read_text()
    assert "host: 127.0.0.1" in text
    assert "trusted_clients:" in text
    assert "file_system_observer: none" in text  # macOS: no inotify
    assert "provider: none" in text  # macOS: no systemd


def test_apply_lan_enable_disable_idempotent(stack):
    _write_moonraker(stack)
    assert stack.configgen.apply_lan(True) is True
    text = stack.paths.MOONRAKER_CONF.read_text()
    assert "host: 0.0.0.0" in text
    for cidr in stack.configgen.LAN_CIDRS:
        assert cidr in text
    # Second enable must be a no-op (returns False).
    assert stack.configgen.apply_lan(True) is False

    assert stack.configgen.apply_lan(False) is True
    text = stack.paths.MOONRAKER_CONF.read_text()
    assert "host: 127.0.0.1" in text
    for cidr in stack.configgen.LAN_CIDRS:
        assert cidr not in text
    assert "127.0.0.1" in text  # loopback trust kept
    assert stack.configgen.apply_lan(False) is False


def test_apply_lan_no_file(stack):
    assert stack.configgen.apply_lan(True) is False


def test_sync_virtual_sdcard(stack):
    stack.paths.PRINTER_CFG.write_text("[virtual_sdcard]\npath: /old/gcodes\n\n[mcu]\n")
    assert stack.configgen.sync_virtual_sdcard() is True
    assert f"path: {stack.paths.DATA / 'gcodes'}" in stack.paths.PRINTER_CFG.read_text()
    assert stack.configgen.sync_virtual_sdcard() is False


def test_sync_moonraker_conf(stack):
    _write_moonraker(stack)
    stack.paths.MOONRAKER_CONF.write_text(
        stack.paths.MOONRAKER_CONF.read_text().replace(str(stack.paths.API_SOCKET), "/stale/socket")
    )
    assert stack.configgen.sync_moonraker_conf() is True
    assert f"klippy_uds_address: {stack.paths.API_SOCKET}" in stack.paths.MOONRAKER_CONF.read_text()


def test_ensure_configs_creates_both_without_checkout(stack, monkeypatch):
    # starter_cfg_text() needs the upstream example checkout; give it a stub.
    example = stack.paths.KLIPPER / "config" / stack.configgen.STARTER_EXAMPLE
    example.parent.mkdir(parents=True, exist_ok=True)
    example.write_text("[mcu]\nserial: /old\n")

    stack.configgen.ensure_configs()

    assert stack.paths.PRINTER_CFG.exists()
    cfg = stack.paths.PRINTER_CFG.read_text()
    assert "serial: /dev/REPLACE_WITH_YOUR_PRINTER" in cfg
    assert "[virtual_sdcard]" in cfg
    assert stack.paths.MOONRAKER_CONF.exists()
    assert "host: 127.0.0.1" in stack.paths.MOONRAKER_CONF.read_text()


def test_ensure_configs_lan_template_host(stack):
    example = stack.paths.KLIPPER / "config" / stack.configgen.STARTER_EXAMPLE
    example.parent.mkdir(parents=True, exist_ok=True)
    example.write_text("[mcu]\n")
    stack.paths.save_settings({"lan": True})
    stack.configgen.ensure_configs()
    assert "host: 0.0.0.0" in stack.paths.MOONRAKER_CONF.read_text()
