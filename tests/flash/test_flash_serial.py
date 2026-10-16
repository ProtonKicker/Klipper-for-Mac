"""The STM32 serial (boot0) transport, and the argv it is allowed to run.

Two facts drive this file:

  * the task's contract is "run upstream klipper's own flasher through
    ~/.klipperformac/venv/bin/python as a SUBPROCESS - upstream-CITING
    execution, not modification", and the venv is where pyserial already lives
    (installer.py installs upstream's scripts/klippy-requirements.txt into it);
  * CODE WINS: this checkout has no scripts/flash_stm32.py. It ships
    flash_usb.py, whose `translate_serial_to_tty` reads /sys, so it cannot
    address a macOS /dev/cu port - and upstream itself documents the macOS-side
    equivalent as `make serialflash` -> `stm32flash -w <bin> -v -g 0 <port>`
    (src/stm32/Makefile:109-111, docs/Bootloaders.md:307).

So `--tool auto` prefers a real upstream script when one can do the job and
otherwise falls back to stm32flash, and every branch is proven by the exact
argv, with nothing ever executed.
"""

import pytest

PORT = "/dev/cu.usbmodem14101"


def _upstream_script(fstack, rel="scripts/flash_stm32.py"):
    """Plant an upstream flasher in the throwaway klipper tree (read-only
    target: nothing here executes it)."""
    where = fstack.paths.KLIPPER / rel
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text("#!/usr/bin/env python3\n# upstream, untouched\n")
    return where


def _venv(fstack):
    """Plant the stack venv python so serial_plan believes it is there."""
    py = fstack.paths.VENV_PY
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("#!/bin/sh\n")
    return py


def _bin(fstack, uf2bytes):
    return fstack.artifacts(name="klipper.bin",
                            blob=uf2bytes.bin_image(msp=0x20010000,
                                                    reset=0x08008101))


def test_the_argv_is_venv_python_plus_the_upstream_script(fstack, uf2bytes):
    """The exact argv: [venv python, script, -t <chip>, -d <port>, <bin>], the
    interface upstream's own flash_usb.py documents (`-t <type> -d <device>
    <klipper.bin>`)."""
    script = _upstream_script(fstack)
    venv = _venv(fstack)
    path = _bin(fstack, uf2bytes)
    ok, argv = fstack.flash.serial_plan(path, PORT, chip="stm32f407",
                                       tool="upstream")
    assert ok, argv
    assert argv == [str(venv), str(script), "-t", "stm32f407", "-d", PORT,
                    path]


def test_the_script_is_verified_to_exist_before_any_call(fstack, uf2bytes):
    """No venv, no script, no subprocess: the check is the point, so a
    half-installed machine gets a message instead of an ENOENT."""
    _venv(fstack)
    assert fstack.flash.upstream_flash_script() is None
    ok, why = fstack.flash.serial_plan(_bin(fstack, uf2bytes), PORT,
                                      tool="upstream")
    assert not ok
    assert "no upstream flasher script" in why


def test_a_venv_without_the_script_or_stm32flash_is_one_clean_refusal(
        fstack, uf2bytes, capsys):
    fstack.down()
    fstack.port_present(True)
    fstack.tools()  # nothing installed
    rec = fstack.use_recorder()
    path = _bin(fstack, uf2bytes)
    with pytest.raises(SystemExit) as exc:
        fstack.cli.main(["flash", "serial", "--bin", path, "--port", PORT])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "No usable STM32 serial flasher" in out
    assert "brew install stm32flash" in out
    assert "flash_usb.py" in out and "Linux" in out
    assert rec.commands() == []


def test_this_checkout_ships_flash_usb_not_flash_stm32(fstack):
    """The candidate list is what pins the real upstream layout: flash_usb.py
    exists in the pristine checkout, flash_stm32.py does not - and the code
    asks the filesystem instead of assuming either."""
    _upstream_script(fstack, rel="scripts/flash_usb.py")
    script = fstack.flash.upstream_flash_script()
    assert script is not None
    assert script.name == "flash_usb.py"
    assert str(script).startswith(str(fstack.paths.KLIPPER))


def test_flash_stm32_wins_when_both_exist(fstack):
    """Order matters: the more specific name is the first candidate."""
    _upstream_script(fstack, rel="scripts/flash_usb.py")
    specific = _upstream_script(fstack)
    assert fstack.flash.upstream_flash_script() == specific


def test_auto_refuses_the_linux_only_script_and_uses_stm32flash(
        fstack, uf2bytes):
    """flash_usb.py is present but resolves the port through /sys, so `auto`
    does not run something that cannot work here; it runs the command upstream's
    Makefile runs instead, byte for byte: `stm32flash -w <bin> -v -g 0 <port>`."""
    _upstream_script(fstack, rel="scripts/flash_usb.py")
    _venv(fstack)
    path = _bin(fstack, uf2bytes)
    fstack.tools("stm32flash")
    ok, argv = fstack.flash.serial_plan(path, PORT, chip="stm32f103",
                                       tool="auto")
    assert ok, argv
    assert argv == ["/usr/local/bin/stm32flash", "-w", path, "-v", "-g", "0",
                    PORT]


def test_explicit_tool_upstream_overrides_the_sys_verdict(fstack, uf2bytes):
    """--tool upstream is the user saying "run the upstream script anyway" (a
    board addressed by vidpid, or a future checkout that does have a macOS-side
    script): the argv still comes out through the stack venv python."""
    script = _upstream_script(fstack, rel="scripts/flash_usb.py")
    venv = _venv(fstack)
    path = _bin(fstack, uf2bytes)
    ok, argv = fstack.flash.serial_plan(path, PORT, chip="stm32f407",
                                       tool="upstream")
    assert ok, argv
    assert argv[:2] == [str(venv), str(script)]


def test_dry_run_prints_the_command_and_runs_nothing(fstack, uf2bytes):
    fstack.down()
    fstack.port_present(True)
    fstack.tools("stm32flash")
    rec = fstack.use_recorder()
    path = _bin(fstack, uf2bytes)
    ok, lines = fstack.flash.flash_serial(path, PORT, dry_run=True)
    assert ok
    text = "\n".join(lines)
    assert "Dry run - would run:" in text
    assert "stm32flash -w {} -v -g 0 {}".format(path, PORT) in text
    assert "BOOT0=1" in text, "the physical wiring step is part of the answer"
    assert rec.commands() == []


def test_a_real_run_propagates_stm32flash_output(fstack, uf2bytes):
    fstack.down()
    fstack.port_present(True)
    fstack.tools("stm32flash")
    rec = fstack.use_recorder({"stm32flash": (
        0, "Trying baud rate 115200\nJumping to RAM code\nBye.")})
    ok, lines = fstack.flash.flash_serial(_bin(fstack, uf2bytes), PORT)
    assert ok, "\n".join(lines)
    assert any("Jumping to RAM code" in line for line in lines)
    assert rec.ran("stm32flash")


def test_a_nonzero_exit_is_reported_as_a_failure(fstack, uf2bytes):
    """A boot0 board that never answered is the common case; the last lines of
    stm32flash are the diagnosis, and the command must not claim success."""
    fstack.down()
    fstack.port_present(True)
    fstack.tools("stm32flash")
    fstack.use_recorder({"stm32flash": (
        4, "", "Timeout while waiting for acknowledge\nCould not open port")})
    ok, lines = fstack.flash.flash_serial(_bin(fstack, uf2bytes), PORT)
    assert not ok
    text = "\n".join(lines)
    assert "stm32flash failed (exit 4)" in text
    assert "Timeout while waiting for acknowledge" in text


def test_an_absent_port_is_named_with_the_list_of_ports(fstack, uf2bytes,
                                                        monkeypatch):
    fstack.down()
    fstack.port_present(False)
    monkeypatch.setattr(fstack.detect, "scan",
                        lambda: [{"path": "/dev/cu.usbmodem14201",
                                  "description": "USB-CDC"}])
    rec = fstack.use_recorder()
    ok, lines = fstack.flash.flash_serial(_bin(fstack, uf2bytes), PORT)
    assert not ok
    assert any("/dev/cu.usbmodem14101 does not exist" in line for line in lines)
    assert any("/dev/cu.usbmodem14201" in line for line in lines)
    assert rec.commands() == []


@pytest.mark.parametrize("port,expect", [
    ("", "No board now matches"),
    ("usbmodem14101", "not a /dev/... serial port path"),
    ("COM3", "not a /dev/... serial port path"),
    ("/dev/", "not a /dev/... serial port path"),
    ("; rm -rf /", "not a /dev/... serial port path"),
    ("usb:1d50:614e:NOTPRESENT", "No board now matches"),
])
def test_the_port_is_a_device_path_or_an_identity_key(fstack, uf2bytes,
                                                     monkeypatch, port, expect):
    """A port argument that is neither is a typo; the only other accepted shape
    is the stable binding key, and no shell fragment is either. The identity
    layer is stubbed to "nothing matches", so this never reaches IOKit."""
    fstack.down()
    fstack.port_present(True)
    monkeypatch.setattr(fstack.detect, "find_by_identity",
                        lambda vid_pid, serial=None, timeout=None: None)
    monkeypatch.setattr(fstack.detect, "all_identities",
                        lambda timeout=None: [])
    rec = fstack.use_recorder()
    ok, lines = fstack.flash.flash_serial(_bin(fstack, uf2bytes), port)
    assert not ok
    assert any(expect in line for line in lines), "\n".join(lines)
    assert rec.commands() == []


def test_an_identity_key_is_resolved_to_the_node_that_exists_now(fstack,
                                                                 uf2bytes,
                                                                 monkeypatch):
    """The macOS-specific problem the multi-MCU layer solves: /dev/cu.* is
    renumbered on every replug, so `flash detect` prints a key and --port takes
    it back. Resolution goes through detect.find_by_identity."""
    fstack.down()
    fstack.port_present(True)
    fstack.tools("stm32flash")
    seen = {}

    def fake_find(vid_pid, serial=None, timeout=None):
        seen["vid_pid"] = vid_pid
        seen["serial"] = serial
        return "/dev/cu.usbmodem14201"

    monkeypatch.setattr(fstack.detect, "find_by_identity", fake_find)
    rec = fstack.use_recorder({"stm32flash": (0, "ok")})
    ok, lines = fstack.flash.flash_serial(
        _bin(fstack, uf2bytes), "usb:1d50:614e:5044506110A45B1C")
    assert ok, "\n".join(lines)
    assert seen == {"vid_pid": "1d50:614e", "serial": "5044506110A45B1C"}
    assert rec.argv_for("stm32flash")[0][-1] == "/dev/cu.usbmodem14201"


def test_a_key_that_matches_nothing_now_is_refused(fstack, uf2bytes,
                                                   monkeypatch):
    fstack.down()
    monkeypatch.setattr(fstack.detect, "find_by_identity",
                        lambda vid_pid, serial=None, timeout=None: None)
    monkeypatch.setattr(fstack.detect, "all_identities",
                        lambda timeout=None: [])
    ok, lines = fstack.flash.flash_serial(_bin(fstack, uf2bytes),
                                         "usb:1d50:614e:NOTPRESENT")
    assert not ok
    assert any("No board now matches" in line for line in lines)


def test_the_bin_is_validated_before_the_port_is_touched(fstack, uf2bytes):
    fstack.down()
    fstack.port_present(True)
    fstack.tools("stm32flash")
    rec = fstack.use_recorder()
    bad = fstack.artifacts(name="noise.bin", blob=b"\xff" * 8192)
    ok, lines = fstack.flash.flash_serial(bad, PORT)
    assert not ok and any("Refusing to write" in line for line in lines)
    assert rec.commands() == []


def _tree(root):
    """{relative path: mtime_ns} of a directory - a cheap "did anything change"
    fingerprint for the pristine-checkout rule."""
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): p.stat().st_mtime_ns
            for p in sorted(root.rglob("*"))}


def test_the_upstream_checkout_is_never_written_to(fstack, uf2bytes):
    """AGENTS.md rule 1 as an executable statement: run the flows that name
    upstream paths (serial plan, dry run, toolchain report) and the checkout's
    file list and mtimes must be byte-identical afterwards. Upstream is executed
    and cited here, never edited."""
    _upstream_script(fstack)
    _venv(fstack)
    fstack.down()
    fstack.port_present(True)
    fstack.tools("stm32flash")
    fstack.use_recorder()
    before = _tree(fstack.paths.KLIPPER)
    path = _bin(fstack, uf2bytes)
    assert fstack.flash.flash_serial(path, PORT, dry_run=True)[0]
    assert fstack.flash.upstream_flash_script() is not None
    fstack.flash.toolchain_status()
    fstack.flash.build_plan()
    assert _tree(fstack.paths.KLIPPER) == before
