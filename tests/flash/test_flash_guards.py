"""The refusals: the file, the volume and the running stack.

Flashing is the one command in this tool that can leave a printer board with no
firmware, so every way in has to say no first: a path that is not what it looks
like, a volume that is not a bootloader, and a klippy that is holding the port.
Each refusal here is checked for the three things that matter to a human: it
exits non-zero, it prints one explanation plus the fix, and it wrote nothing.
"""
import os

import pytest


def test_a_plain_firmware_path_is_accepted(fstack):
    path = fstack.artifacts()
    ok, resolved = fstack.flash.check_bin_path(path)
    assert ok
    assert os.path.isfile(resolved)


def test_dotdot_traversal_is_refused(fstack):
    """What flash prints and what flash opens must be the same string. A '..'
    makes the printed command line a lie about which file went to the board."""
    ok, why = fstack.flash.check_bin_path(
        str(fstack.paths.APP_HOME / ".." / ".." / "etc" / "passwd"))
    assert not ok
    assert "'..' segments are not allowed" in why


@pytest.mark.parametrize("bad", [
    "", "   ", "firm\nware.uf2", "firm\x00ware.uf2", "firm\tware.uf2",
    "firm\rware.uf2",
])
def test_blank_and_control_character_paths_are_refused(fstack, bad):
    ok, why = fstack.flash.check_bin_path(bad)
    assert not ok
    assert "No firmware file" in why or "control characters" in why


def test_a_missing_file_is_refused_without_a_traceback(fstack):
    ok, why = fstack.flash.check_bin_path(
        str(fstack.paths.APP_HOME / "gone" / "klipper.uf2"))
    assert not ok and "No such firmware file" in why


def test_a_directory_is_refused(fstack):
    ok, why = fstack.flash.check_bin_path(str(fstack.paths.APP_HOME))
    assert not ok and "not a regular file" in why


def test_a_device_node_is_refused(fstack):
    """/dev/zero exists and is readable and is not firmware."""
    ok, why = fstack.flash.check_bin_path("/dev/zero")
    assert not ok and "not a regular file" in why


def test_a_dangling_symlink_is_refused(fstack):
    where = fstack.paths.APP_HOME / "firmware"
    where.mkdir(parents=True, exist_ok=True)
    link = where / "dangling.uf2"
    try:
        os.symlink(str(where / "nowhere.uf2"), str(link))
    except OSError:  # a filesystem that refuses symlinks: skip, never fail
        pytest.skip("symlinks unavailable here")
    ok, why = fstack.flash.check_bin_path(str(link))
    assert not ok and "No such firmware file" in why


def test_a_name_that_only_looks_like_traversal_is_fine(fstack):
    """`..` inside one component is not a traversal - the guard is the path
    component, not the substring, or `klipper-1.1..defconfig.uf2` would be
    unflashable."""
    path = fstack.artifacts(name="a..b.uf2")
    ok, resolved = fstack.flash.check_bin_path(path)
    assert ok and resolved.endswith("a..b.uf2")


@pytest.mark.parametrize("name,expect", [
    ("Macintosh HD", "system volume"),
    ("Macintosh HD - Data", "system volume"),
    ("System", "system volume"),
    ("../Volumes/Other", "not usable"),
    ("RPI-RP2/../", "not usable"),
    ("", "No volume name"),
    ("RPI RPI2/" + "x" * 70, "too long"),
])
def test_volume_names_are_one_safe_component(fstack, name, expect):
    ok, why = fstack.flash.safe_volume_name(name)
    assert not ok
    assert expect in why


def test_a_normal_volume_name_is_accepted(fstack):
    for name in ("RPI-RP2", "RP2350", "FLY_DUE", "My Board"):
        ok, value = fstack.flash.safe_volume_name(name)
        assert ok and value == name


def test_the_boot_volume_is_never_a_flash_target_even_when_mounted(fstack):
    """The fake /Volumes has a volume named like the boot disk: --volume must
    still refuse it, because writing a .uf2 there would put firmware on the
    disk the user booted from."""
    stack = fstack
    stack.down()
    mount = stack.make_volume("Macintosh HD")
    path = stack.artifacts()
    ok, lines = stack.flash.flash_uf2(path, volume="Macintosh HD")
    assert not ok
    assert any("system volume" in line for line in lines)
    assert not (mount / "new.uf2").exists()


def test_a_volume_that_is_not_a_bootloader_is_refused(fstack):
    """Any other mounted FAT volume (a USB stick, an SD card in a reader) looks
    writable and is not a UF2 target: the INFO_UF2.TXT marker is the proof."""
    fstack.down()
    mount = fstack.make_volume("SANDISK", bootsel=False)
    ok, lines = fstack.flash.flash_uf2(fstack.artifacts(), volume="SANDISK",
                                       sleep=fstack.fake_clock().sleep)
    assert not ok
    assert any("not a UF2 bootloader volume" in line for line in lines)
    assert not (mount / "new.uf2").exists()


@pytest.mark.parametrize("action,extra", [
    ("uf2", []),
    ("dfu", ["--chip", "stm32f407", "--address", "0x8004000"]),
    ("serial", ["--port", "/dev/cu.usbmodem14101"]),
])
def test_every_mutating_flow_refuses_while_the_stack_is_up(
        fstack, uf2bytes, action, extra, capsys):
    """The refusal is the product feature here: klippy holds /dev/cu.* with a
    flock, and a board klippy is talking to is not in its bootloader. Proven
    with a fake `process.running()` - nothing was executed either way."""
    fstack.running(klipper=True, moonraker=True)
    rec = fstack.use_recorder()
    path = fstack.artifacts(blob=uf2bytes.uf2_image(blocks=8))
    with pytest.raises(SystemExit) as exc:
        fstack.cli.main(["flash", action, "--bin", path] + extra)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Stack is running (klipper, moonraker)" in out
    assert "klipperformac down" in out
    assert "Nothing to flash." in out
    assert rec.commands() == [], "a refused flow must not run anything"


def test_only_klipper_and_moonraker_are_a_collision(fstack):
    """The web UIs and caffeinate hold no serial port: refusing for them would
    tell the user to stop things that do not matter."""
    fstack.running(klipper=False, moonraker=False, web_mainsail=True,
                   web_fluidd=True, caffeinate=True)
    assert fstack.flash.services_up() == []
    assert fstack.flash.refuse_while_running()[0]


def test_a_cancelled_wait_is_a_message_not_a_traceback(fstack, uf2bytes,
                                                       capsys):
    """Ctrl-C while waiting for BOOTSEL: tidy line, non-zero exit, nothing
    written. This is the flow a human actually interrupts."""
    fstack.down()
    clock = fstack.fake_clock()

    def cancel(_count):
        raise KeyboardInterrupt

    clock.on_sleep = cancel
    rec = fstack.use_recorder()
    path = fstack.artifacts(blob=uf2bytes.uf2_image(blocks=8))
    with pytest.raises(SystemExit) as exc:
        fstack.cli.main(["flash", "uf2", "--bin", path, "--volume", "RPI-RP2",
                         "--timeout", "30"])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "cancelled" in out
    assert "Traceback" not in out
    assert rec.commands() == []


def test_the_guarded_helper_catches_any_crash_as_one_line(fstack):
    """The spec's contract is "never raises": an unexpected error is a printed
    line, not the interpreter dump AGENTS.md forbids users to see."""
    fstack.down()

    def boom():
        raise RuntimeError("sqlite went sideways")

    ok, lines = fstack.flash.guarded(boom)
    assert not ok
    assert lines == ["Unexpected error: sqlite went sideways"]


def test_refusal_lists_the_stop_command_for_a_running_klipper_only(fstack):
    fstack.running(klipper=True, moonraker=False)
    ok, lines = fstack.flash.refuse_while_running()
    assert not ok
    assert "Stack is running (klipper)." in lines[0]
