"""The artifact validator: the gate that decides whether a file may be written
into a chip.

Real 512-byte sectors are assembled here and broken one header field at a time,
because that is the only way to prove each field is checked. The RP2040 boot ROM
silently skips any block whose magics or family id do not match (upstream
`lib/rp2040_flash/main.c:52-59`), so a lax validator does not crash - it leaves a
board that kept its old firmware while the user is told to power-cycle it.
"""
import pytest


def test_a_real_uf2_image_passes(fstack, uf2bytes):
    ok, why = fstack.flash.verify_uf2(
        fstack.artifacts(blob=uf2bytes.uf2_image(blocks=12)))
    assert ok, why
    assert "12 UF2 sector(s)" in why


def test_rp2040_chip_requires_the_rp2040_family_id(fstack, uf2bytes):
    """`flash uf2` passes chip=rp2040 for the RPI-RP2 volume: a Pi Pico
    CircuitPython .uf2 and an SKR Pico Klipper .uf2 are both valid containers,
    but only one of them is the printer's firmware."""
    blob = uf2bytes.uf2_image(blocks=8)
    path = fstack.artifacts(blob=blob)
    assert fstack.flash.verify_uf2(path, chip="rp2040")[0]
    wrong = fstack.artifacts(name="other.uf2",
                             blob=uf2bytes.uf2_image(blocks=8, family=0))
    ok, why = fstack.flash.verify_uf2(wrong, chip="rp2040")
    assert not ok
    assert "not rp2040 firmware" in why


def test_rp2350_family_ids_are_accepted_for_rp2350(fstack, uf2bytes):
    path = fstack.artifacts(blob=uf2bytes.uf2_image(
        blocks=8, family=uf2bytes.RP2350_FAMILY_ID))
    assert fstack.flash.verify_uf2(path, chip="rp2350")[0]
    ok, why = fstack.flash.verify_uf2(path, chip="rp2040")
    assert not ok and "not rp2040 firmware" in why


def test_missing_family_id_flag_is_rejected_for_rp2040(fstack, uf2bytes):
    path = fstack.artifacts(blob=uf2bytes.uf2_image(blocks=8, flags=0))
    ok, why = fstack.flash.verify_uf2(path, chip="rp2040")
    assert not ok
    assert "no family-id flag" in why


@pytest.mark.parametrize("field,expect", [
    ("magic0", "is not a UF2 block"),
    ("magic1", "is not a UF2 block"),
    ("magic_end", "is not a UF2 block"),
    ("payload_size", "is not a UF2 block"),
    ("target", "not 4-byte aligned"),
])
def test_every_header_field_is_actually_checked(fstack, uf2bytes, field, expect):
    """Exactly one field of sector 1 is wrong; the refusal has to say so."""
    broken = {"magic0": uf2bytes.UF2_MAGIC_START0 + 1,
              "magic1": uf2bytes.UF2_MAGIC_START1 + 1,
              "magic_end": uf2bytes.UF2_MAGIC_END ^ 0xFF,
              "payload_size": uf2bytes.UF2_PAYLOAD_AREA + 1,
              "target": 0x10000001}[field]
    blob = uf2bytes.replace_sector(uf2bytes.uf2_image(blocks=8), 1,
                                   **{field: broken})
    ok, why = fstack.flash.verify_uf2(fstack.artifacts(blob=blob))
    assert not ok
    assert "sector 2" in why, why
    assert expect in why


def test_the_magics_are_upstream_values_not_a_typo(fstack):
    """The constants are asserted against the copies in the test module (which
    come from pico-sdk/boot/uf2.h): if someone edits one in flash.py, or upstream
    changes its header, this fails instead of every .uf2 silently refusing."""
    f = fstack.flash
    assert f._UF2_MAGIC_START0 == fstack.uf2b.MAGIC0
    assert f._UF2_MAGIC_START1 == fstack.uf2b.MAGIC1
    assert f._UF2_MAGIC_END == fstack.uf2b.MAGICEND
    assert f.UF2_BLOCK_SIZE == 512
    assert f._RP2040_FAMILY_ID == fstack.uf2b.FAMILY_RP2040
    assert f._UF2_FLAG_FAMILY_ID_PRESENT == fstack.uf2b.FLAG_FAMILY
    assert f._UF2_FLAG_FILE_CONTAINER == fstack.uf2b.FLAG_CONTAINER


def test_a_size_that_is_not_a_multiple_of_512_is_truncated(fstack, uf2bytes):
    path = fstack.artifacts(blob=uf2bytes.uf2_image(blocks=8) + b"\x00" * 100)
    ok, why = fstack.flash.verify_uf2(path)
    assert not ok
    assert "whole number of 512-byte UF2 sectors" in why


def test_block_count_disagreement_is_rejected(fstack, uf2bytes):
    """Sector 3 claims a different total: two images concatenated, which a ROM
    would half-write."""
    blob = uf2bytes.replace_sector(uf2bytes.uf2_image(blocks=8), 3,
                                   num_blocks=9)
    ok, why = fstack.flash.verify_uf2(fstack.artifacts(blob=blob))
    assert not ok
    assert "disagrees about the block count" in why


def test_a_missing_final_sector_is_rejected(fstack, uf2bytes):
    """The headers promise 10 blocks and only 9 arrived (and 9 sectors is
    already past the size floor, so this is the completeness check answering,
    not the size check)."""
    blob = b"".join(uf2bytes.uf2_sector(payload=b"K" * 16, block_no=i,
                                        num_blocks=10) for i in range(9))
    ok, why = fstack.flash.verify_uf2(fstack.artifacts(blob=blob))
    assert not ok
    assert "the image is incomplete" in why


def test_block_no_out_of_range_is_rejected(fstack, uf2bytes):
    blob = uf2bytes.replace_sector(uf2bytes.uf2_image(blocks=8), 2,
                                   block_no=99)
    ok, why = fstack.flash.verify_uf2(fstack.artifacts(blob=blob))
    assert not ok
    assert "out of 8 blocks" in why


def test_a_repeated_block_no_is_rejected(fstack, uf2bytes):
    """Two sectors claiming to be block 3: an interleaved pair of images, where
    the ROM would write one over the other."""
    blob = uf2bytes.replace_sector(uf2bytes.uf2_image(blocks=8), 4, block_no=3)
    ok, why = fstack.flash.verify_uf2(fstack.artifacts(blob=blob))
    assert not ok
    assert "repeats block_no 3" in why


def test_mixed_family_ids_are_rejected_even_without_a_chip(fstack, uf2bytes):
    """One file, two target chips: the RP2040 ROM writes the blocks it recognises
    and silently skips the rest, which is a half-flashed board."""
    blob = uf2bytes.replace_sector(uf2bytes.uf2_image(blocks=8), 5,
                                   family=uf2bytes.RP2350_FAMILY_ID)
    ok, why = fstack.flash.verify_uf2(fstack.artifacts(blob=blob))
    assert not ok
    assert "cannot be firmware for two chips" in why


def test_a_data_sector_without_the_family_flag_is_rejected(fstack, uf2bytes):
    """flags without UF2_FLAG_FAMILY_ID_PRESENT means the ROM reads that last
    header word as a file size, not a family: the block is skipped."""
    blob = uf2bytes.replace_sector(uf2bytes.uf2_image(blocks=8), 1, flags=0)
    ok, why = fstack.flash.verify_uf2(fstack.artifacts(blob=blob))
    assert not ok
    assert "no family-id flag set" in why


def test_under_the_size_floor_is_refused(fstack, uf2bytes):
    """Two valid sectors (1024 bytes) is a container, not firmware."""
    path = fstack.artifacts(blob=uf2bytes.uf2_image(blocks=2))
    ok, why = fstack.flash.verify_uf2(path)
    assert not ok
    assert "1024 bytes" in why and "floor" in why


def test_over_the_size_ceiling_is_refused(fstack, uf2bytes):
    path = fstack.artifacts(blob=uf2bytes.uf2_image(blocks=8),
                            size=5 * 1024 * 1024)
    ok, why = fstack.flash.verify_uf2(path)
    assert not ok
    assert "sanity ceiling" in why


def test_a_missing_file_is_one_clean_line(fstack):
    ok, why = fstack.flash.verify_uf2(
        str(fstack.paths.APP_HOME / "nope.uf2"))
    assert not ok and "Cannot read" in why


def test_payload_total_excludes_file_container_sectors(fstack, uf2bytes):
    """UF2_FLAG_FILE_CONTAINER blocks carry index/metadata, not flash data:
    1 container + 7 real sectors is 7 x 16 payload bytes, not 8 x 16."""
    sectors = [uf2bytes.uf2_sector(payload=b"X" * 16, block_no=0, num_blocks=8,
                                   flags=fstack.uf2b.FLAG_FAMILY
                                   | fstack.uf2b.FLAG_CONTAINER)]
    sectors += [uf2bytes.uf2_sector(payload=b"Y" * 16, block_no=i,
                                    num_blocks=8) for i in range(1, 8)]
    ok, why = fstack.flash.verify_uf2(
        fstack.artifacts(blob=b"".join(sectors)))
    assert ok, why
    assert "112 bytes of payload" in why


def test_verify_artifact_dispatches_on_kind(fstack, uf2bytes):
    path = fstack.artifacts(blob=uf2bytes.uf2_image(blocks=8))
    assert fstack.flash.verify_artifact(path, "uf2")[0]
    assert not fstack.flash.verify_artifact(path, "bin")[0]
    ok, why = fstack.flash.verify_artifact(path, "hex")
    assert not ok and "Unknown firmware kind" in why


def test_artifact_kind_reads_the_extension(fstack):
    assert fstack.flash.artifact_kind("a/b/klipper.UF2") == "uf2"
    assert fstack.flash.artifact_kind("klipper.bin") == "bin"
    assert fstack.flash.artifact_kind("firmware.elf") is None


def test_the_bin_gate_checks_the_vector_table(fstack, uf2bytes):
    path = fstack.artifacts(name="klipper.bin",
                           blob=uf2bytes.bin_image(msp=0x20010000,
                                                   reset=0x08008101))
    ok, why = fstack.flash.verify_bin(path)
    assert ok, why
    assert "stack pointer 0x20010000" in why
    assert "reset vector 0x08008101" in why


@pytest.mark.parametrize("blob,expect", [
    ("uf2", "memory map"),
    ("random", "memory map"),
])
def test_the_bin_gate_rejects_a_uf2_and_noise(fstack, uf2bytes, blob, expect):
    data = (uf2bytes.uf2_image(blocks=8) if blob == "uf2" else b"\xff" * 8192)
    ok, why = fstack.flash.verify_bin(fstack.artifacts(name="x.bin", blob=data))
    assert not ok and expect in why


def test_the_bin_gate_honours_the_chip(fstack, uf2bytes):
    """An RP2040 image (XIP at 0x10000000) is not STM32F103 firmware, and
    --chip is what decides which memory map is legal."""
    path = fstack.artifacts(name="z.bin",
                           blob=uf2bytes.bin_image(msp=0x2003FFF0,
                                                   reset=0x10008101))
    assert fstack.flash.verify_bin(path, chip="rp2040")[0]
    ok, why = fstack.flash.verify_bin(path, chip="stm32f103")
    assert not ok and "Wrong chip" in why
    ok, why = fstack.flash.verify_bin(path, chip="atmega2560")
    assert not ok and "Unknown chip" in why


def test_the_bin_gate_shares_the_size_guards(fstack, uf2bytes):
    ok, why = fstack.flash.verify_bin(fstack.artifacts(
        name="small.bin", blob=uf2bytes.bin_image(size=1024)))
    assert not ok and "floor" in why
    ok, why = fstack.flash.verify_bin(fstack.artifacts(
        name="big.bin", blob=uf2bytes.bin_image(), size=5 * 1024 * 1024))
    assert not ok and "sanity ceiling" in why
