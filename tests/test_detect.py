"""detect.py: serial port classification, scan, [mcu] serial writes."""


def test_describe_labels(stack):
    d = stack.detect
    assert "FTDI" in d._describe("/dev/cu.usbserial-ABCD")
    assert "CDC" in d._describe("/dev/cu.usbmodem14101")
    assert "CH340" in d._describe("/dev/cu.wchusbserial123")
    assert "CP210x" in d._describe("/dev/cu.SLAB_USBtoUART")
    assert d._describe("/dev/cu.other") == ""


def test_scan_dedupes_sorted(stack, monkeypatch):
    def fake_glob(pat):
        if "usbserial" in pat:
            return ["/dev/cu.usbserial-1"]
        if "usbmodem" in pat:
            return ["/dev/cu.usbmodem-2"]
        return []

    # detect calls glob.glob(); swap the module attribute for a stub.
    import types

    monkeypatch.setattr(stack.detect, "glob", types.SimpleNamespace(glob=fake_glob))
    ports = stack.detect.scan()
    assert [p["path"] for p in ports] == ["/dev/cu.usbmodem-2", "/dev/cu.usbserial-1"]
    assert ports[0]["description"]


def test_write_serial_replaces_only_mcu(stack):
    stack.paths.PRINTER_CFG.write_text(
        "[mcu]\nserial: /old\nbaud: 250000\n[bltouch]\nserial: /must-not-touch\n"
    )
    assert stack.detect.write_serial(stack.paths.PRINTER_CFG, "/dev/cu.new") is True
    text = stack.paths.PRINTER_CFG.read_text()
    assert "[mcu]" in text and "serial: /dev/cu.new" in text
    assert "serial: /must-not-touch" in text  # other sections untouched


def test_write_serial_adds_line_when_missing(stack):
    stack.paths.PRINTER_CFG.write_text("[mcu]\nbaud: 250000\n")
    assert stack.detect.write_serial(stack.paths.PRINTER_CFG, "/dev/cu.new") is True
    assert "serial: /dev/cu.new" in stack.paths.PRINTER_CFG.read_text()


def test_write_serial_refuses_without_mcu(stack):
    stack.paths.PRINTER_CFG.write_text("[temperature_sensor x]\nsensor_type: ntc100k\n")
    assert stack.detect.write_serial(stack.paths.PRINTER_CFG, "/dev/cu.new") is False
    assert "serial" not in stack.paths.PRINTER_CFG.read_text()


def test_write_serial_missing_file(stack):
    assert stack.detect.write_serial(stack.paths.PRINTER_CFG, "/dev/cu.new") is False


def test_sort_key_variants(stack):
    v = stack.versions
    assert v._sort_key("v1.2.3") == (1, 2, 3)
    assert v._sort_key("2") == (2, 0, 0)
    assert v._sort_key("v1.2.3-rc1") == (
        -1,
        -1,
        -1,
    )  # non-numeric prerelease segment -> malformed/ineligible
    assert v._sort_key("v1.10") > v._sort_key("v1.9.9")
