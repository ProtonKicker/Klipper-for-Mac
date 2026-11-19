"""DFU: the one transport where our command line has to match upstream's byte
for byte, because dfu-util is doing the writing to flash.

The flag sets are taken from klipper's own scripts/flash_usb.py - `call_dfuutil`
builds ["dfu-util"] + ["-d", "," + vidpid] + extra + ["-D", binfile], and the
per-chip `extra` is ["-R", "-a", "2"] for an F103 with the stm32duino bootloader
and ["-R", "-a", "0", "-s", "<application address>:leave"] for F4/F0 (the
address is CONFIG_FLASH_APPLICATION_ADDRESS, src/stm32/Kconfig:323-329). The
tool itself is never installed here: it is detected, and a missing dfu-util is
one line and exit 1.
"""
import pytest

BIN = "klipper.bin"


def _bin(fstack, uf2bytes, name=BIN, **kw):
    return fstack.artifacts(name=name, blob=uf2bytes.bin_image(**kw))


def test_the_f103_stm32duino_argv_matches_upstream(fstack, uf2bytes):
    path = _bin(fstack, uf2bytes, msp=0x2000F000, reset=0x08008101)
    fstack.tools("dfu-util")
    ok, argv = fstack.flash.dfu_plan(path, chip="stm32f103")
    assert ok, argv
    assert argv == ["dfu-util", "-d", ",1eaf:0003", "-a", "2", "-R",
                    "-D", path]


def test_the_f407_argv_carries_the_application_address_leave(fstack, uf2bytes):
    """flash_stm32f4 builds `start = "0x%x:leave" % options.start` and passes
    ["-R", "-a", "0", "-s", start]: the `:leave` is what leaves the chip in its
    bootloader instead of resetting into a half-written app."""
    path = _bin(fstack, uf2bytes, msp=0x20010000, reset=0x08010101)
    fstack.tools("dfu-util")
    ok, argv = fstack.flash.dfu_plan(path, chip="stm32f407")
    assert ok, argv
    assert argv == ["dfu-util", "-d", ",0483:df11", "-a", "0",
                    "-s", "0x8004000:leave", "-R", "-D", path]


def test_explicit_flags_beat_the_preset(fstack, uf2bytes):
    path = _bin(fstack, uf2bytes)
    fstack.tools("dfu-util")
    ok, argv = fstack.flash.dfu_plan(path, chip="stm32f407",
                                    vidpid="1209:beba", alt="1",
                                    address="0x08020000", leave=False)
    assert ok, argv
    assert argv == ["dfu-util", "-d", ",1209:beba", "-a", "1",
                    "-s", "0x08020000", "-R", "-D", path]


@pytest.mark.parametrize("vidpid", ["0483df11", "gggg:hhhh", "483:df1",
                                    "0483:df11:extra", ""])
def test_a_vidpid_is_four_hex_digits_a_colon_and_four_more(fstack, uf2bytes,
                                                           vidpid):
    """Upstream's own guard is `re.compile(r"^[a-fA-F0-9]{4}:[a-fA-F0-9]{4}$")`
    before it will call dfu-util at all; anything else is a typo that would
    target the wrong device on the bus."""
    fstack.tools("dfu-util")
    ok, why = fstack.flash.dfu_plan(_bin(fstack, uf2bytes), vidpid=vidpid)
    assert not ok
    expect = "Nothing to target" if not vidpid else "must look like 0483:df11"
    assert expect in why


@pytest.mark.parametrize("address", ["8004000", "0xZZZZ", "-1", "0x123456789"])
def test_a_dfu_address_is_hex(fstack, uf2bytes, address):
    fstack.tools("dfu-util")
    ok, why = fstack.flash.dfu_plan(_bin(fstack, uf2bytes), vidpid="0483:df11",
                                   address=address)
    assert not ok and "hex address" in why


def test_a_dfu_address_may_be_upper_case_hex(fstack, uf2bytes):
    """dfu-util takes whatever base a human wrote, and `0X08004000` is a valid
    spelling; refusing it would be a made-up rule."""
    fstack.tools("dfu-util")
    ok, argv = fstack.flash.dfu_plan(_bin(fstack, uf2bytes),
                                     vidpid="0483:df11",
                                     address="0X08004000")
    assert ok, argv
    assert "0x08004000" in argv


def test_no_chip_and_no_vidpid_refuses_with_the_chip_list(fstack, uf2bytes):
    fstack.tools("dfu-util")
    ok, why = fstack.flash.dfu_plan(_bin(fstack, uf2bytes))
    assert not ok
    assert "Nothing to target" in why and "--vidpid" in why
    assert "stm32f103" in why, "the chip list is the way out of this"


def test_an_rp2040_is_not_a_dfu_target_and_says_what_is(fstack, uf2bytes):
    fstack.tools("dfu-util")
    ok, why = fstack.flash.dfu_plan(_bin(fstack, uf2bytes), chip="rp2040")
    assert not ok
    assert "flash uf2" in why


def test_an_unknown_chip_lists_the_ones_that_work(fstack, uf2bytes):
    fstack.tools("dfu-util")
    ok, why = fstack.flash.dfu_plan(_bin(fstack, uf2bytes), chip="esp32")
    assert not ok and "Unknown --chip" in why


def test_a_missing_dfu_util_is_the_exact_brew_line_and_no_install(
        fstack, uf2bytes, capsys):
    """AGENTS.md forbids installing dependencies from a subcommand, and the
    flash spec says the same: detect, print `brew install dfu-util`, exit 1."""
    fstack.down()
    fstack.tools()  # nothing is installed
    rec = fstack.use_recorder()
    path = _bin(fstack, uf2bytes)
    with pytest.raises(SystemExit) as exc:
        fstack.cli.main(["flash", "dfu", "--bin", path, "--chip", "stm32f407"])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "dfu-util is not installed" in out
    assert "  brew install dfu-util" in out
    assert "never installs anything" in out
    assert "The command it would have run: dfu-util -d ,0483:df11" in out
    assert rec.commands() == [], "a missing tool must not be guessed at"


def test_dry_run_prints_the_resolved_command(fstack, uf2bytes):
    fstack.down()
    fstack.tools("dfu-util")
    rec = fstack.use_recorder()
    path = _bin(fstack, uf2bytes)
    ok, lines = fstack.flash.flash_dfu(path, chip="stm32f4", dry_run=True)
    assert ok
    text = "\n".join(lines)
    assert "Dry run - would run:" in text
    assert "dfu-util -d ,0483:df11 -a 0 -s 0x8004000:leave -R -D " + path \
        in text
    assert rec.commands() == []


def test_a_successful_dfu_run_propagates_the_tools_tail(fstack, uf2bytes):
    fstack.down()
    fstack.tools("dfu-util")
    path = _bin(fstack, uf2bytes)
    rec = fstack.use_recorder({"dfu-util": (
        0, "Found DFU: [0483]df11 ver 0000 in loc '14100000'\n"
           "Download completed\n", "")})
    ok, lines = fstack.flash.flash_dfu(path, chip="stm32f407")
    assert ok, "\n".join(lines)
    assert any("Download completed" in line for line in lines)
    argv = rec.argv_for("dfu-util")[0]
    assert argv[0] == "/usr/local/bin/dfu-util", argv
    assert str(path) in argv[-1]


def test_a_failed_dfu_run_shows_the_last_lines(fstack, uf2bytes):
    """A non-zero dfu-util means nothing was written (or only part of it): the
    tool's own words are the diagnosis, and the flow must say it failed."""
    fstack.down()
    fstack.tools("dfu-util")
    rec = fstack.use_recorder({"dfu-util": (
        1, "", "dfu_util_find_manifests: no dfu device found\n"
               "Failed to find dfu device : 0483:df11")})
    ok, lines = fstack.flash.flash_dfu(_bin(fstack, uf2bytes),
                                      chip="stm32f407")
    assert not ok
    text = "\n".join(lines)
    assert "dfu-util failed (exit 1)" in text
    assert "Failed to find dfu device" in text
    assert rec.ran("dfu-util")


def test_dfu_refuses_a_file_that_is_not_firmware_for_that_chip(fstack,
                                                              uf2bytes):
    """--chip is not decoration: it picks both the USB id and the memory map, so
    a Blue Pill image must not be aimed at an F407 target."""
    fstack.down()
    fstack.tools("dfu-util")
    rec = fstack.use_recorder()
    picoprog = fstack.artifacts(name="other.bin", blob=uf2bytes.bin_image(
        msp=0x2003FFF0, reset=0x10008101))
    ok, lines = fstack.flash.flash_dfu(picoprog, chip="stm32f407")
    assert not ok
    assert any("Wrong chip" in line for line in lines)
    assert rec.commands() == []
