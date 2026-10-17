"""`flash build` (preview only), `flash build-rp2040`, and `flash doctor`.

The flash spec argues that building is feasible on macOS (defconfig -> .config ->
`make olddefconfig` -> `make out/klipper.uf2` with the brew cross compiler, all
inside the pristine checkout whose .config and out/ are gitignored). This round
PREVIEWS it and never compiles, because shipping a build wrapper without having
run it against a real arm-none-eabi-gcc would be the kind of unverified promise
AGENTS.md forbids. Both halves are asserted on the same property: no child
process is started, and no upstream byte changes.
"""
import argparse
import os

import pytest

# Upstream spells board configs `<name>.cfg` inside klipper/config/ (verified in
# the pinned checkout: 228 `.cfg` files and no `.defconfig` extension at all -
# "defconfig" is what Klipper calls the file, not its suffix), so the tests use
# the names a human would actually copy out of `klipperformac presets --list`.
DEFCONFIG = "generic-bigtreetech-skr-pico-v1.0.cfg"


def _checkout(fstack, defconfigs=(DEFCONFIG,
                                 "generic-bigtreetech-manta-m5p.cfg")):
    """A throwaway upstream-shaped tree: config/*.cfg, lib/pico-sdk,
    lib/elf2uf2, scripts/flash_usb.py, and the .gitignore upstream has."""
    root = fstack.paths.KLIPPER
    (root / "config").mkdir(parents=True, exist_ok=True)
    for name in defconfigs:
        (root / "config" / name).write_text("CONFIG_MCU=...\n")
    (root / "lib" / "pico-sdk" / "boot").mkdir(parents=True, exist_ok=True)
    (root / "lib" / "pico-sdk" / "boot" / "uf2.h").write_text("#define x 1\n")
    (root / "lib" / "elf2uf2").mkdir(parents=True, exist_ok=True)
    (root / "lib" / "elf2uf2" / "main.cpp").write_text("// upstream\n")
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "flash_usb.py").write_text("# upstream\n")
    return root


def _venv(fstack):
    """Plant the stack venv's python so the pyserial probe is reached (the probe
    itself is stubbed: the answer comes from the Recorder, not a child)."""
    py = fstack.paths.VENV_PY
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("#!/bin/sh\n")
    return py


def _embedded_toolchain(fstack):
    """A complete local ARM toolchain tree under ~/.klipperformac/toolchains."""
    root = fstack.flash._toolchains_root() / "gcc-arm-embedded-test" / "Payload"
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "arm-none-eabi" / "include").mkdir(parents=True, exist_ok=True)
    for name in ("arm-none-eabi-gcc", "arm-none-eabi-objcopy",
                 "arm-none-eabi-readelf"):
        (root / "bin" / name).write_text("#!/bin/sh\n")
    (root / "arm-none-eabi" / "include" / "string.h").write_text("/* newlib */\n")
    return root


def _tree(root):
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): (p.stat().st_mtime_ns,
                                       p.stat().st_size)
            for p in sorted(root.rglob("*"))}


def test_build_plan_prints_upstreams_own_make_sequence(fstack):
    _checkout(fstack)
    ok, lines = fstack.flash.build_plan(DEFCONFIG, chip="rp2040", jobs=8)
    assert ok, lines
    text = "\n".join(lines)
    assert "cp {}".format(fstack.paths.KLIPPER / "config" / DEFCONFIG) in text
    assert "olddefconfig" in text and "Makefile:114-115" in text
    assert "make -C {} -j8 out/klipper.uf2".format(fstack.paths.KLIPPER) in text
    assert "CROSS_PREFIX=arm-none-eabi-" in text
    assert "would build" in text.lower() or "NOT run this round" in text


def test_build_plan_never_sets_cpath(fstack):
    """The macOS shim headers are for the host chelper only; firmware compiles
    against real arm-none-eabi headers. Inheriting CPATH here would build a
    board image out of stubs - which is the exact failure mode that produces a
    board that boots and never answers."""
    _checkout(fstack)
    ok, lines = fstack.flash.build_plan(DEFCONFIG, chip="stm32f407")
    text = "\n".join(lines)
    assert ok, lines
    assert "CPATH is deliberately NOT inherited" in text
    assert "out/klipper.bin" in text, "an STM32 target has no .uf2"


def test_build_plan_applies_kconfig_overrides_as_a_printed_step(fstack):
    _checkout(fstack)
    ok, lines = fstack.flash.build_plan(
        DEFCONFIG, overrides=[("CONFIG_REBOOT", "y")])
    text = "\n".join(lines)
    assert ok, lines
    assert "rewrite .config: CONFIG_REBOOT=y" in text
    assert text.index("CONFIG_REBOOT=y") < text.index("olddefconfig"), \
        "the override has to be applied before kconfiglib expands it"


def test_build_never_runs_a_child_process(fstack, uf2bytes):
    """The promise of this round: --dry-run, no make, no brew, no compiler. The
    Recorder is the proof - if any argv had been run, the test would see it."""
    _checkout(fstack)
    rec = fstack.use_recorder()
    fstack.tools()
    ok, lines = fstack.flash.build_plan(DEFCONFIG, chip="rp2040")
    assert ok, "\n".join(lines)
    assert rec.commands() == []


def test_building_says_it_is_not_implemented(fstack):
    """An honest statement beats a silent stub: the reason (needs a real run +
    a verified toolchain) is in the output."""
    _checkout(fstack)
    text = "\n".join(fstack.flash.build_plan(DEFCONFIG)[1])
    assert "will not compile yet" in text
    assert "libc_nano.a" in text, "the known brew-toolchain risk is named"


def test_build_refuses_without_a_checkout(fstack):
    ok, lines = fstack.flash.build_plan(DEFCONFIG)
    assert not ok
    assert "klipperformac setup" in "\n".join(lines)


def test_build_refuses_a_defconfig_that_is_not_upstream(fstack):
    _checkout(fstack)
    ok, lines = fstack.flash.build_plan("my-custom-board.cfg")
    assert not ok
    assert "is not a board config in" in "\n".join(lines)


def test_build_refuses_a_defconfig_that_is_a_path_escape(fstack):
    """--defconfig becomes a filename under the checkout: '/../../etc/passwd'
    must not be resolvable, or the build preview reads outside klipper."""
    _checkout(fstack)
    ok, lines = fstack.flash.build_plan("../../../../etc/passwd")
    assert not ok
    assert "is not a board-config name" in "\n".join(lines)


def test_build_refuses_an_unknown_chip(fstack):
    _checkout(fstack)
    ok, lines = fstack.flash.build_plan(DEFCONFIG, chip="sam3x")
    assert not ok and "this round previews builds for" in "\n".join(lines)


def test_build_without_a_defconfig_lists_where_they_live(fstack):
    _checkout(fstack)
    ok, lines = fstack.flash.build_plan(None)
    assert not ok
    text = "\n".join(lines)
    assert "No --defconfig given" in text
    assert DEFCONFIG in text


def test_build_never_touches_the_checkout(fstack):
    _checkout(fstack)
    before = _tree(fstack.paths.KLIPPER)
    fstack.flash.build_plan(DEFCONFIG, chip="rp2040", jobs=4)
    assert _tree(fstack.paths.KLIPPER) == before
    assert not (fstack.paths.KLIPPER / ".config").exists()
    assert not (fstack.paths.KLIPPER / "out").exists()


def test_the_cli_build_command_is_preview_only(fstack, capsys):
    """Exit 0 for a preview that printed a plan, and zero children spawned -
    `make` is never reached."""
    _checkout(fstack)
    rec = fstack.use_recorder()
    fstack.cli.main(["flash", "build", "--defconfig", DEFCONFIG,
                     "--chip", "rp2040"])
    out = capsys.readouterr().out
    assert "NOT run this round" in out
    assert "make -C" in out
    assert rec.commands() == []


def test_the_cli_build_command_fails_on_a_bad_defconfig(fstack):
    _checkout(fstack)
    with pytest.raises(SystemExit) as exc:
        fstack.cli.main(["flash", "build", "--defconfig", "nope.cfg"])
    assert exc.value.code == 1


def test_toolchain_status_reports_the_transports_without_installing(fstack):
    _checkout(fstack)
    rec = fstack.use_recorder()
    fstack.tools("dfu-util", "diskutil")
    st = fstack.flash.toolchain_status()
    assert st["found"]["dfu-util"] == "/usr/local/bin/dfu-util"
    assert "stm32flash" in st["missing"]
    assert st["brew"] is None
    assert st["upstream_flash_script"].endswith("scripts/flash_usb.py")
    assert st["uf2_header"] is True
    assert st["elf2uf2_source"] is True
    assert st["volume_flow"] is True
    assert st["flashable"] is True
    for argv in rec.commands():
        assert "install" not in " ".join(argv)


def test_the_venv_pyserial_probe_runs_in_the_stack_venv(fstack):
    """The probe is how `flash serial --tool upstream` knows pyserial is there:
    it asks the venv's own interpreter, because pyserial is that venv's
    dependency (upstream's klippy-requirements.txt), not a package we add."""
    venv = fstack.paths.VENV_PY
    venv.parent.mkdir(parents=True, exist_ok=True)
    venv.write_text("#!/bin/sh\n")
    rec = fstack.use_recorder({"python": (0, "3.5\n")})
    assert fstack.flash.venv_pyserial() == "3.5"
    argv = rec.commands()[0]
    assert argv[0] == str(venv)
    assert "import serial" in argv[2]


def test_a_venv_without_pyserial_reports_none_not_an_error(fstack):
    venv = fstack.paths.VENV_PY
    venv.parent.mkdir(parents=True, exist_ok=True)
    venv.write_text("#!/bin/sh\n")
    fstack.use_recorder({"python": (1, "", "ModuleNotFoundError")})
    assert fstack.flash.venv_pyserial() is None


def test_no_venv_means_no_probe(fstack):
    rec = fstack.use_recorder()
    assert fstack.flash.venv_pyserial() is None
    assert rec.commands() == []


def test_doctor_lines_cover_every_transport_and_the_venv(fstack):
    _checkout(fstack)
    _venv(fstack)
    _embedded_toolchain(fstack)
    fstack.tools("dfu-util", "diskutil")
    fstack.use_recorder({"python": (0, "3.5")})
    text = "\n".join(fstack.flash.doctor_lines())
    assert "[ok] UF2 volume flow" in text
    assert "[ok] dfu-util:" in text
    assert "brew install stm32flash" in text
    assert "bossac" in text and "not implemented yet" in text
    assert "cross compiler" in text
    assert "embedded toolchain" in text
    assert "pyserial (venv):" in text and "3.5" in text
    assert "`flash build-rp2040` is the macOS build path" in text


def test_toolchain_status_reports_a_complete_local_embedded_toolchain(fstack):
    _checkout(fstack)
    root = _embedded_toolchain(fstack)
    st = fstack.flash.toolchain_status()
    assert st["embedded_toolchain"] == str(root)
    if st["cached_embedded_pkg"] is not None:
        assert st["cached_embedded_pkg"].endswith(".pkg")


def test_build_rp2040_plan_uses_seeded_usb_config_and_local_toolchain(fstack):
    _checkout(fstack)
    root = _embedded_toolchain(fstack)
    ok, lines = fstack.flash.build_rp2040_plan(
        chip="rp2040", bootloader="none", jobs=8,
        overrides=[("CONFIG_REBOOT", "y")])
    text = "\n".join(lines)
    assert ok, lines
    assert "seeded rp2040 USB config" in text
    assert "CONFIG_MACH_RP2040=y" in text
    assert "CONFIG_RPXXXX_USB=y" in text
    assert "CONFIG_RPXXXX_FLASH_START_0100=y" in text
    assert "CONFIG_REBOOT=y" in text
    assert str(root / "bin") in text
    assert "toolchain source: local" in text
    assert "out/lib/elf2uf2/elf2uf2" in text


def test_build_rp2040_firmware_runs_make_and_caches_the_artifact(fstack,
                                                                 uf2bytes):
    _checkout(fstack)
    _embedded_toolchain(fstack)

    def reply(argv):
        if argv[:3] == ["make", "-C", str(fstack.paths.KLIPPER)] and \
                argv[-1] == "out/klipper.uf2":
            out = fstack.paths.KLIPPER / "out"
            out.mkdir(parents=True, exist_ok=True)
            (out / "klipper.uf2").write_bytes(uf2bytes.uf2_image())
        return (0, "", "")

    rec = fstack.use_recorder({"make": reply})
    ok, lines = fstack.flash.build_rp2040_firmware(chip="rp2040", jobs=4)
    text = "\n".join(lines)
    assert ok, lines
    assert rec.commands() == [
        ["make", "-C", str(fstack.paths.KLIPPER), "clean"],
        ["make", "-C", str(fstack.paths.KLIPPER), "olddefconfig"],
        ["make", "-C", str(fstack.paths.KLIPPER), "out/lib/elf2uf2/elf2uf2"],
        ["make", "-C", str(fstack.paths.KLIPPER), "-j4", "out/klipper.uf2"],
    ]
    assert "Cached " in text
    saved = sorted((fstack.paths.APP_HOME / "firmware").glob("klipper-rp2040-*.uf2"))
    assert saved, "the built uf2 should be copied into the firmware cache"
    assert not (fstack.paths.KLIPPER / ".config").exists(), \
        "the seeded config should be cleaned up after the build"


def test_build_rp2040_firmware_refuses_without_toolchain(fstack):
    _checkout(fstack)
    fstack.flash.cached_embedded_pkg = lambda: None
    ok, lines = fstack.flash.build_rp2040_firmware(chip="rp2040")
    assert not ok
    assert "no complete ARM embedded toolchain" in "\n".join(lines)


def test_doctor_names_the_exact_brew_formulae_for_what_is_missing(fstack,
                                                                  capsys):
    """AGENTS.md: a missing dependency is reported, never auto-installed. The
    line has to be copy-pasteable."""
    _checkout(fstack)
    fstack.tools("diskutil")
    fstack.use_recorder()
    code = fstack.flash.dispatch(argparse.Namespace(
        action="doctor"))
    out = capsys.readouterr().out
    assert "[!] dfu-util:" in out and "brew install dfu-util" in out
    assert "[!] stm32flash:" in out and "brew install stm32flash" in out
    assert code == 0, "UF2 needs no toolchain, so doctor is not a failure"


def test_doctor_fails_only_when_the_uf2_transport_is_broken(fstack, capsys):
    """/Volumes gone (or diskutil missing) means the primary path is dead, and
    that is worth a non-zero exit; a missing brew formula is not."""
    _checkout(fstack)
    fstack.tools("diskutil")
    os.rmdir(str(fstack.volumes))
    code = fstack.flash.dispatch(argparse.Namespace(
        action="doctor"))
    assert code == 1
    assert "[!] UF2 volume flow" in capsys.readouterr().out


def test_doctor_reports_a_missing_checkout_as_a_problem(fstack, capsys):
    fstack.tools("dfu-util", "diskutil")
    fstack.use_recorder()
    code = fstack.flash.dispatch(argparse.Namespace(
        action="status"))
    out = capsys.readouterr().out
    assert "No upstream klipper checkout" in out
    assert "klipperformac setup" in out
    assert code == 0


def test_status_is_the_same_report_as_doctor(fstack, capsys):
    """The spec named the report `status`; the brief named it `doctor`. They are
    one function, so a user cannot get two different answers."""
    _checkout(fstack)
    fstack.use_recorder()
    for action in ("doctor", "status"):
        assert fstack.flash.dispatch(argparse.Namespace(action=action)) == 0
        assert "[ok] UF2 volume flow" in capsys.readouterr().out
