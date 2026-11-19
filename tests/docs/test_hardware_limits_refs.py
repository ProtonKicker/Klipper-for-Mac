"""Regression guard for docs/macos-hardware-limits.md.

Cheap offline test: the hardware-limit verdicts (CAN bus, host GPIO/I2C/SPI,
real-time) are load-bearing promises to users, and each one is anchored to
file:line citations from the pinned upstream checkouts. This test only checks
that the document still EXISTS and still carries the citation markers for the
three verdicts — so nobody silently deletes or guts it. Re-verify the actual
line numbers against upstream when the pins move (see the doc's legend).
"""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "docs" / "macos-hardware-limits.md"
README = REPO / "README.md"

# Verdict 1 - CAN bus: runtime blocked (PF_CAN / errno 43 chain),
# flashing possible over USB-CDC via Katapult flashtool, WebSerial absent.
CAN_MARKERS = (
    "PF_CAN",
    "serialhdl.py:143-145",
    "serialqueue.c:331",
    "Errno 43",
    "flashtool.py:861",
    "navigator.serial",
)
# Verdict 2 - host GPIO/I2C/SPI: unavailable, second-USB-MCU workaround.
GPIO_MARKERS = (
    "pins.py:79-92",
    "gpiochardev",
)
# Verdict 3 - real-time: no host RT code in this checkout; MCU owns step edges.
RT_MARKERS = (
    "rtpriority",
    "stepper.c:30-36",
    "FAQ.md:126-132",
    "clocksync.py",
)

ALL_MARKERS = {
    "CAN": CAN_MARKERS,
    "GPIO": GPIO_MARKERS,
    "real-time": RT_MARKERS,
}


@pytest.mark.skipif(not DOC.exists(), reason="doc missing (fails below)")
def test_hardware_limits_doc_exists():
    assert DOC.is_file(), "docs/macos-hardware-limits.md was deleted"


def test_hardware_limits_doc_carries_verdict_citations():
    assert DOC.is_file(), "docs/macos-hardware-limits.md was deleted"
    text = DOC.read_text(encoding="utf-8")
    for verdict, markers in ALL_MARKERS.items():
        for marker in markers:
            assert marker in text, (
                "verdict %r lost its citation marker %r in %s"
                % (verdict, marker, DOC)
            )


def test_hardware_limits_doc_has_file_line_citations():
    assert DOC.is_file(), "docs/macos-hardware-limits.md was deleted"
    text = DOC.read_text(encoding="utf-8")
    # Every factual line must carry a file:line or doc citation; the doc as
    # written cites upstream by name dozens of times. A low floor still catches
    # a stripped-down rewrite.
    assert text.count(".py:") + text.count(".c:") + text.count(".md:") >= 20


def test_readme_points_at_hardware_limits_doc():
    assert README.is_file(), "README.md missing"
    readme = README.read_text(encoding="utf-8")
    assert "docs/macos-hardware-limits.md" in readme
