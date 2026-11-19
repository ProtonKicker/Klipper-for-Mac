"""`rtpriority` is a Linux option, and an unknown option halts Klipper.

The research's finding, verified against this pristine checkout:
``docs/Config_Reference.md`` has no ``rtpriority`` (the [printer] section is
kinematics/max_velocity/max_accel/...), and ``klippy/configfile.py:440`` raises
``Option 'rtpriority' is not valid in section 'printer'`` for anything it has not
seen - which stops the printer. So the answer to "give klippy real-time priority"
is never "add the option"; it is "remove it, and here is what macOS does give
you" (``klipperformac timing``).

These tests guard the deny-list from both sides: our detection has to actually
fire, and nothing this tool generates or ships may ever contain one of the
options on it.
"""
import glob
import os

import pytest

from klipperformac import cli, configgen

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

RTPRIORITY_CFG = """\
# a config a Linux user copied over
[printer]
kinematics: cartesian
max_velocity: 200
max_accel: 3000
rtpriority: 80

[mcu]
serial: /dev/cu.usbserial-1410

[stepper_x]
step_pin: PF0
dir_pin: PF1
enable_pin: !PD7
microstep_pin: PE2
step_distance: .00625
endstop_pin: ^PKC2
position_min: -190
position_endstop: -190
position_max: 190
homing_speed: 50
"""


def test_the_linux_only_option_is_found():
    assert cli.linux_only_options(RTPRIORITY_CFG) == [("printer", "rtpriority")]


def test_valid_options_are_left_alone():
    """The deny-list is a list, not a heuristic: a false positive here is a
    doctor that cries wolf about a working config."""
    clean = RTPRIORITY_CFG.replace("rtpriority: 80\n", "")
    assert cli.linux_only_options(clean) == []
    for text in ("", "[printer]\n", "# rtpriority: 80\n[printer]\n",
                 "; rtpriority: 80\n", "[printer]\n  rtpriority-ish: 1\n"):
        assert cli.linux_only_options(text) == [], repr(text)


def test_the_match_is_section_option_and_case_insensitive():
    """rtpriority belongs to [printer] only, so [mcu] rtpriority is not this
    finding; the section names in the deny-list stay lowercase."""
    assert cli.linux_only_options("[PRINTER]\nRTPriority: 1\n") == \
        [("printer", "rtpriority")]
    assert cli.linux_only_options("[mcu]\nrtpriority: 1\n") == []
    assert cli.linux_only_options("[printer]\nrtpriority = 1\n") == \
        [("printer", "rtpriority")]


def test_indented_option_continuations_are_not_new_options():
    """Klipper allows a multi-line option value; the continuation belongs to the
    option above it and must not be read as a key of its own."""
    text = "[printer]\nkinematics: cartesian\n    rtpriority: 80\n"
    assert cli.linux_only_options(text) == []


def test_nothing_we_generate_or_ship_carries_a_deny_listed_option():
    """Every .cfg this repo controls, plus the text the installer puts in a
    fresh install. If a preset ever grows an rtpriority, this fails here rather
    than at a user's first FIRMWARE_RESTART."""
    texts = {
        "configgen HEADER": configgen.HEADER,
        "configgen EXTRAS": configgen.EXTRAS,
        "configgen MOONRAKER_CFG": configgen.MOONRAKER_CFG,
        "configgen WEBCAM_SECTION": configgen.WEBCAM_SECTION,
    }
    patterns = ["spec/**/*.cfg", "presets/**/*.cfg", "data/**/*.cfg",
                "klipperformac/**/*.cfg"]
    found = []
    for pattern in patterns:
        found.extend(glob.glob(os.path.join(REPO_ROOT, pattern),
                               recursive=True))
    for path in found:
        with open(path, errors="replace") as f:
            texts[path] = f.read()
    # The repo may ship no preset .cfg files yet (presets live in the user's
    # data folder); the generated templates above still have to be clean.
    for name, text in texts.items():
        for section, option in cli.LINUX_ONLY_CONFIG_OPTIONS:
            assert cli.linux_only_options(text) == [], name
            assert option not in text.lower(), name


def test_the_deny_list_only_grows_from_verified_upstream_reports():
    """A guard against the well-meant guess: every entry must exist in this
    Klipper's own docs before it can be reported to a user, so the list stays
    exactly the one option the research verified."""
    assert cli.LINUX_ONLY_CONFIG_OPTIONS == (("printer", "rtpriority"),)
    reference = os.path.join(os.path.expanduser("~"), ".klipperformac",
                             "klipper", "docs", "Config_Reference.md")
    if not os.path.exists(reference):
        pytest.skip("no upstream checkout on this machine to verify against")
    with open(reference, errors="replace") as f:
        text = f.read()
    assert "rtpriority" not in text
