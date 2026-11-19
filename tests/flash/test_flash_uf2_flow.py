"""The UF2 flow end to end, against a fake /Volumes.

This is the SKR Pico path and the only place the tool writes anything. The
sequence under test is the RP2040's own: BOOTSEL mounts a mass-storage volume,
the .uf2 is copied onto it, the boot ROM consumes the file, resets the chip and
unmounts the volume - so "the volume disappeared" IS the success signal and
`diskutil eject` is only the fallback for macOS's delayed writes. Every step runs
on the deterministic clock and a Recorder, so no test waits, writes or ejects
anything real.
"""
import os



def test_no_bootloader_volume_prints_the_instructions_and_then_waits(fstack):
    """The documented user step (press BOOTSEL + plug USB) is the only way in.
    The flow says it verbatim, then watches /Volumes for the timeout instead of
    either hanging forever or demanding the user retype the command."""
    fstack.down()
    fake = fstack.fake_clock()
    ok, lines = fstack.flash.flash_uf2(fstack.artifacts(), timeout=10,
                                       sleep=fake.sleep)
    assert not ok
    text = "\n".join(lines)
    assert "No UF2 bootloader volume is mounted right now." in text
    assert "Hold the BOOTSEL button." in text
    assert "Replug USB. Release BOOTSEL." in text
    assert "Rerun: klipperformac flash uf2" in text
    assert "Waiting up to 10 s for it to appear" in text
    assert "timed out after 10 s. Nothing was written." in text


def test_a_wait_that_never_mounts_times_out_loudly(fstack, uf2bytes):
    """Bounded wait: 5 s of fake clock (10 polls of 0.5 s), then the same
    instructions plus what /Volumes actually holds, so a differently named
    board is not a mystery."""
    fstack.down()
    fake = fstack.fake_clock()
    rec = fstack.use_recorder()
    (fstack.volumes / "SANDISK").mkdir()
    path = fstack.artifacts(blob=uf2bytes.uf2_image(blocks=8))
    ok, lines = fstack.flash.flash_uf2(path, volume="RPI-RP2", timeout=5,
                                       sleep=fake.sleep)
    assert not ok
    text = "\n".join(lines)
    assert "Waiting for /Volumes/RPI-RP2 (up to 5 s)" in text
    assert "timed out after 5 s" in text
    assert "SANDISK" in text, "the listing must show what is mounted"
    assert fake.slept == 10
    assert rec.commands() == [], "a timed-out wait must run no diskutil"
    assert not (fstack.volumes / "RPI-RP2").exists()


def test_the_volume_can_appear_late_and_the_flow_continues(fstack, uf2bytes):
    """BOOTSEL is a human holding a button: the mount may land on poll 3, and
    the board may reset on poll 5 - the wait has to survive both."""
    fstack.down()
    fake = fstack.fake_clock()
    mount = fstack.volumes / "RPI-RP2"

    def step(count):
        if count == 3:
            fstack.make_volume("RPI-RP2")
        elif count == 5:
            for child in sorted(mount.iterdir()):
                child.unlink()
            mount.rmdir()

    fake.on_sleep = step
    rec = fstack.use_recorder()
    path = fstack.artifacts(blob=uf2bytes.uf2_image(blocks=8))
    ok, lines = fstack.flash.flash_uf2(path, timeout=30, sleep=fake.sleep)
    assert ok, "\n".join(lines)
    assert "  /Volumes/RPI-RP2 appeared." in lines
    assert not rec.ran("diskutil")


def test_a_successful_flash_ends_with_the_volume_gone(fstack, uf2bytes):
    """The copy happens, the board resets itself, the volume disappears: no
    diskutil call is needed and the tool says so."""
    fstack.down()
    mount = fstack.make_volume("RPI-RP2")
    fake = fstack.fake_clock()

    def unmount(count):
        if count == 2:
            for child in sorted(mount.iterdir()):
                child.unlink()
            mount.rmdir()

    fake.on_sleep = unmount
    rec = fstack.use_recorder()
    path = fstack.artifacts(blob=uf2bytes.uf2_image(blocks=9))
    ok, lines = fstack.flash.flash_uf2(path, sleep=fake.sleep)
    assert ok, "\n".join(lines)
    text = "\n".join(lines)
    assert "Board reset into Klipper: /Volumes/RPI-RP2 is gone." in text
    assert "klipperformac up" in text
    assert not rec.ran("diskutil"), "cp alone resets the RP2040; ejecting " \
        "is the fallback for a volume that will not go away"


def test_the_copied_file_is_the_whole_image_byte_for_byte(fstack, uf2bytes):
    """What actually reached the board: the copy is a full flush of the file the
    user asked for (macOS buffers writes to a USB mass-storage device, and the
    boot ROM reads the moment the host closes it - which is why the flow
    fsyncs)."""
    fstack.down()
    mount = fstack.make_volume("RPI-RP2")
    blob = uf2bytes.uf2_image(blocks=8, payload=b"FLASHDATA" * 12)
    seen = {}

    def eject(argv):
        seen["argv"] = argv
        seen["bytes"] = (mount / "new.uf2").read_bytes()
        return (0, "", "ejected")

    fstack.use_recorder({"diskutil": eject})
    ok, lines = fstack.flash.flash_uf2(
        fstack.artifacts(blob=blob), sleep=fstack.fake_clock().sleep)
    assert not ok, "the volume never went away, so the flow must not claim a " \
        "successful flash"
    assert seen["bytes"] == blob
    assert seen["argv"] == ["diskutil", "eject", str(mount)]
    assert any("did not take" in line for line in lines)


def test_the_eject_fallback_runs_diskutil_with_one_exact_argv(fstack,
                                                             uf2bytes):
    """macOS can keep the mount after the ROM has taken the file, so the flow
    ejects and re-polls. The argv is pinned: `diskutil eject <mount>` and
    nothing more invasive - no unmountDisk, no erase."""
    fstack.down()
    mount = fstack.make_volume("RPI-RP2")
    fake = fstack.fake_clock()

    def eject(argv):
        assert argv == ["diskutil", "eject", str(mount)]
        for child in sorted(mount.iterdir()):
            child.unlink()
        mount.rmdir()
        return (0, "Unmount of disk4 succeeded", "")

    rec = fstack.use_recorder({"diskutil": eject})
    path = fstack.artifacts(blob=uf2bytes.uf2_image(blocks=8))
    ok, lines = fstack.flash.flash_uf2(path, sleep=fake.sleep)
    assert ok, "\n".join(lines)
    text = "\n".join(lines)
    assert "Unmount of disk4 succeeded" in text
    assert "ejecting so the buffered writes reach the chip" in text
    assert "Board reset into Klipper" in text
    assert rec.ran("diskutil")


def test_a_failed_eject_is_reported_with_the_tool_tail(fstack, uf2bytes):
    fstack.down()
    fstack.make_volume("RPI-RP2")
    rec = fstack.use_recorder({"diskutil": (1, "", "Unable to unmount disk")})
    ok, lines = fstack.flash.flash_uf2(
        fstack.artifacts(blob=uf2bytes.uf2_image(blocks=8)),
        sleep=fstack.fake_clock().sleep)
    assert not ok
    text = "\n".join(lines)
    assert "diskutil eject failed (exit 1)" in text
    assert "Unable to unmount disk" in text
    assert "Eject it by hand" in text or "Eject /Volumes/RPI-RP2 by hand" in \
        text
    assert rec.ran("diskutil")


def test_a_missing_diskutil_is_reported_not_crashed(fstack, uf2bytes):
    fstack.down()
    fstack.make_volume("RPI-RP2")
    rec = fstack.use_recorder({"diskutil": (None, "", "command not found")})
    ok, lines = fstack.flash.flash_uf2(
        fstack.artifacts(blob=uf2bytes.uf2_image(blocks=8)),
        sleep=fstack.fake_clock().sleep)
    assert not ok
    assert any("diskutil could not run" in line for line in lines)
    assert rec.ran("diskutil")


def test_a_write_error_is_one_line_not_a_traceback(fstack, uf2bytes):
    """Volume present but not writable (a read-only mount, an odd permission
    set, a full disk): the user gets the OS reason and no interpreter dump."""
    fstack.down()
    mount = fstack.make_volume("RPI-RP2")
    os.chmod(str(mount), 0o500)
    try:
        ok, lines = fstack.flash.flash_uf2(
            fstack.artifacts(blob=uf2bytes.uf2_image(blocks=8)),
            sleep=fstack.fake_clock().sleep)
    finally:
        os.chmod(str(mount), 0o755)
    assert not ok
    assert any("Could not write to /Volumes/RPI-RP2" in line for line in lines)
    assert not any("Traceback" in line for line in lines)


def test_a_volume_whose_root_cannot_be_read_is_never_written(fstack, uf2bytes):
    """The shape check needs to list the volume. If listing fails, the tool
    treats it as "not proven to be a bootloader" and stops - the safe direction
    when the alternative is copying firmware onto whatever is behind an
    EACCES."""
    fstack.down()
    mount = fstack.make_volume("RPI-RP2")
    os.chmod(str(mount), 0o000)
    try:
        ok, lines = fstack.flash.flash_uf2(
            fstack.artifacts(blob=uf2bytes.uf2_image(blocks=8)),
            volume="RPI-RP2", sleep=fstack.fake_clock().sleep)
    finally:
        os.chmod(str(mount), 0o755)
    assert not ok
    assert any("not a UF2 bootloader volume" in line for line in lines)


def test_dry_run_writes_nothing_and_touches_no_board(fstack, uf2bytes):
    fstack.down()
    mount = fstack.make_volume("RPI-RP2")
    rec = fstack.use_recorder()
    ok, lines = fstack.flash.flash_uf2(
        fstack.artifacts(blob=uf2bytes.uf2_image(blocks=8)), dry_run=True)
    assert ok
    text = "\n".join(lines)
    assert "Dry run" in text and "new.uf2" in text and "diskutil eject" in text
    assert os.listdir(mount) == ["INFO_UF2.TXT"]
    assert rec.commands() == []


def test_a_bad_uf2_is_refused_before_anything_is_written(fstack, uf2bytes):
    """The RPI-RP2 volume means rp2040, so a Pico Python .uf2 (right magics,
    wrong family id) is refused and the volume keeps its old contents."""
    fstack.down()
    mount = fstack.make_volume("RPI-RP2")
    blob = uf2bytes.uf2_image(blocks=8, family=0)
    ok, lines = fstack.flash.flash_uf2(fstack.artifacts(blob=blob),
                                       sleep=fstack.fake_clock().sleep)
    assert not ok
    assert any("not rp2040 firmware" in line for line in lines)
    assert os.listdir(mount) == ["INFO_UF2.TXT"]


def test_the_bootloader_volume_is_found_without_a_flag(fstack, uf2bytes):
    """The wizard behaviour: `flash uf2 --bin X` with a Pico in BOOTSEL finds
    RPI-RP2 by itself, ignoring an unrelated mounted volume."""
    fstack.down()
    fstack.make_volume("SANDISK", bootsel=False)
    mount = fstack.make_volume("RPI-RP2")
    assert fstack.flash.find_bootsel_volume() == "RPI-RP2"
    fake = fstack.fake_clock()

    def unmount(count):
        if count == 1:
            for child in sorted(mount.iterdir()):
                child.unlink()
            mount.rmdir()

    fake.on_sleep = unmount
    ok, lines = fstack.flash.flash_uf2(
        fstack.artifacts(blob=uf2bytes.uf2_image(blocks=8)), sleep=fake.sleep)
    assert ok, "\n".join(lines)


def test_a_custom_bootsel_volume_name_is_honoured(fstack, uf2bytes):
    """Vendor bootloaders pick their own volume name (a Duet exposes its own);
    --volume must accept it as long as the shape proves it is a UF2 disk."""
    fstack.down()
    mount = fstack.make_volume("DUET36")
    fake = fstack.fake_clock()

    def unmount(count):
        if count == 1:
            for child in sorted(mount.iterdir()):
                child.unlink()
            mount.rmdir()

    fake.on_sleep = unmount
    ok, lines = fstack.flash.flash_uf2(
        fstack.artifacts(blob=uf2bytes.uf2_image(blocks=8)), volume="DUET36",
        sleep=fake.sleep)
    assert ok, "\n".join(lines)
    # Not RPI-RP2, so the family id is not pinned: an RP2350 board's .uf2 is
    # valid here too.
    assert "Board reset into Klipper" in "\n".join(lines)
