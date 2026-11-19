"""THE byte-golden: `klipperformac serial --json` produces these bytes, and the
SwiftUI Dashboard holds the same bytes as its own fixture.

Why bytes and not a parsed dict: a dict comparison passes when a key is renamed
on one side and re-added on the other, it passes when the producer starts
emitting `{"mcus": []}` vs `{"mcus":[]}`, and it says nothing at all about the
one thing the consumer copies into its source. So this suite freezes the
stdout of the real handler for a frozen config (`canon_multi_mcu.cfg`) and
frozen live facts (`canon_scan.json`) into `serial_json.golden`, then asserts
that the *same* bytes are what agent C's `contractFixture` literal says.

The three-way pins below are the seam: B's producer, A's reader vocabulary, and
C's decoder. Refresh deliberately, never reflexively:

    KLIPPERFORMAC_FREEZE_GOLDEN=1 PYTHONPATH=. \
        .venv-test/bin/python -m pytest tests/contract/test_json_bytes_contract.py

and then mirror the printed bytes into McuSerial.swift's contractFixture - the
mirror test is what makes forgetting that a red build instead of a blank pane.
"""
import json

import pytest

from tests.contract.conftest import (CONFLICT_ORDER, CONTRACT_KEYS,
                                     CANON_CFG, GOLDEN_PATH, IDENTITY_KEYS,
                                     swift_conflict_raw_values,
                                     swift_decoded_keys,
                                     swift_literal_block,
                                     swift_multiline_literal)


@pytest.fixture
def golden_bytes(freeze_golden):
    """The checked-in golden bytes.

    Skipped (not passed) while a run is re-recording the golden, and a hard
    failure when the file is simply missing: it is a checked artifact, so its
    absence is the contract having lost its reference, not a TODO.
    """
    if freeze_golden:
        pytest.skip("re-recording serial_json.golden this run; the producer, the "
                    "file and the Swift mirror are compared on the next run")
    if not GOLDEN_PATH.is_file():
        pytest.fail("tests/contract/serial_json.golden is missing. It is a "
                    "checked artifact of the wire contract; regenerate it with "
                    "KLIPPERFORMAC_FREEZE_GOLDEN=1 (see this file's docstring) "
                    "and review the diff like code.")
    return GOLDEN_PATH.read_text()


# --------------------------------------------------------------- the producer --
def test_the_producer_writes_exactly_the_golden_bytes(serial_json_bytes,
                                                      freeze_golden):
    """One read of printer.cfg, one ioreg-free fact set, these bytes.

    This is the freeze path, so it reads the file itself rather than going
    through the `golden_bytes` fixture (which sits the re-recording run out).
    """
    out, err = serial_json_bytes
    if freeze_golden:
        frozen = out.rstrip("\n")
        GOLDEN_PATH.write_text(frozen)
        # Print the Swift side too: the golden and C's fixture are one change,
        # and making the paste mechanical is how the mirror stays honest.
        print("\n[frozen] %s (%d bytes)\n%s\n\nPaste over "
              "KlipperMac/Sources/KlipperMac/Core/McuSerial.swift's "
              "contractFixture:\n%s\n"
              % (GOLDEN_PATH.name, len(frozen), frozen,
                 swift_literal_block(frozen)))
        return
    assert out == GOLDEN_PATH.read_text() + "\n", (
        "`serial --json` no longer produces the frozen bytes.\n  got: %s\n  "
        "want: %s" % (out, golden_bytes))
    # Read-only mode promises nothing on stderr: a GUI panel reads stdout and a
    # warning there is a bug, not a diagnostic.
    assert err == "", err


def test_the_golden_is_one_json_document_and_nothing_else(serial_json_bytes,
                                                          golden_bytes):
    out, _err = serial_json_bytes
    assert out.count("\n") == 1 and out.endswith("\n")
    assert json.loads(out) == json.loads(golden_bytes)
    assert set(json.loads(out)) == {"mcus", "single"}


def test_the_golden_row_order_is_master_first_then_by_name(golden_bytes):
    """klipper's own priority, spelled out: the timing master, then the
    secondaries by name. A GUI that keys rows by name does not care, but a
    diff of this file does, and so does anyone reading `mcus[0]`."""
    rows = json.loads(golden_bytes)["mcus"]
    names = [row["mcu"] for row in rows]
    assert names[0] == "mcu"
    assert names[1:] == sorted(names[1:])


def test_the_golden_exercises_the_whole_contract(golden_bytes):
    """Not a toy payload: one document must carry every state the GUI branches on."""
    rows = json.loads(golden_bytes)["mcus"]
    by_name = {row["mcu"]: row for row in rows}
    assert len(rows) == 7, "the canonical rig is seven sections"
    assert json.loads(golden_bytes)["single"] is False
    # every conflict code, in the order of precedence the producer ranks by
    assert {row["conflict"] for row in rows} == {
        None, "duplicate-port", "missing-port", "bad-section-name"}
    # identity: a live board, an unplugged board bound by marker, and nothing
    present = [row for row in rows if (row["identity"] or {}).get("present")]
    bound = [row for row in rows
             if row["identity"] and row["identity"]["present"] is False]
    absent = [row for row in rows if row["identity"] is None]
    assert present and bound and absent, (present, bound, absent)
    # a CAN section: configured, no /dev node claimed, no conflict
    can = by_name["rpi"]
    assert can["serial"] == "112233445566" and can["configured"] is True
    assert can["claimed"] is False and can["conflict"] is None
    # the badly named header outranks its own dead port (cause before symptom)
    board = by_name[" board"]
    assert board["conflict"] == "bad-section-name", board
    assert board["serial"] == "/dev/cu.usbmodem14198", board
    # the marker-bound board keeps an identity the GUI can show while the board
    # itself is away, and says so instead of guessing a node for it
    tool = by_name["tool"]
    assert tool["identity"]["present"] is False and tool["serial"] is None
    assert tool["conflict"] == "missing-port", tool
    # a shared node is named on BOTH rows, not just on the loser of the race
    shared = {row["mcu"] for row in rows if row["conflict"] == "duplicate-port"}
    assert shared == {"analyser", "scanner"}, shared
    # and the whitespace name survives as what klipper registered, not as a
    # pretty label: mcus.py derives it by stripping exactly the four 'mcu '
    assert " board" in by_name, sorted(by_name)


def test_seven_keys_per_row_and_four_per_identity(golden_bytes):
    for row in json.loads(golden_bytes)["mcus"]:
        assert tuple(row) == CONTRACT_KEYS, row
        if row["identity"] is not None:
            assert tuple(row["identity"]) == IDENTITY_KEYS, row


def test_a_one_board_config_is_single_and_the_contract_is_still_valid(run_serial_json):
    """The fallback C draws as the old picker: `single` true, one row, and the
    same seven keys. Pinned here because the GUI branches on exactly this."""
    out, err = run_serial_json(text=("""[mcu]
serial: /dev/cu.usbmodem14101
baud: 250000

[stepper_x]
step_pin: P2.1
"""))
    assert err == ""
    data = json.loads(out)
    assert data["single"] is True
    assert [tuple(row) for row in data["mcus"]] == [CONTRACT_KEYS]
    assert data["mcus"][0]["mcu"] == "mcu"
    assert data["mcus"][0]["conflict"] is None


def test_read_only_never_exits_nonzero_and_never_tracebacks(
        offline_machine, run_serial_json):
    """`main(["serial", "--json"])` must simply return - no SystemExit - because
    the GUI runs it on every poll tick and a non-zero exit reads as a broken
    printer."""
    assert offline_machine.cli.main(["serial", "--json"]) is None
    for text in ("", "[mcu\n", "[MCU]\nserial: /dev/x\n", "\x00\xff\n",
                 "[mcu]\nserial: /dev/x\nserial: /dev/x\n"):
        out, err = run_serial_json(text=text)
        assert json.loads(out)["mcus"] is not None, text
        assert "Traceback" not in err and "Traceback" not in out, (text, err)


# ----------------------------------------------------------------- the mirror --
def test_the_swift_fixture_is_the_same_bytes(swift_contract_source, golden_bytes,
                                             freeze_golden):
    """C's copy of the contract, byte for byte.

    This is the test that turns "the CLI changed its JSON" into a red build in
    the app that consumes it, instead of an empty MCU pane nobody notices until
    a toolboard is unplugged. Update BOTH sides in one change.
    """
    if freeze_golden:
        pytest.skip("freezing the golden: the mirror is re-checked next run")
    literal = swift_multiline_literal(swift_contract_source, "contractFixture")
    assert literal == golden_bytes, (
        "McuSerial.swift's contractFixture has drifted from serial_json.golden.\n"
        "  Swift holds: %s\n  Producer:    %s" % (literal, golden_bytes))


def test_the_swift_fixture_is_decodable_json_with_the_contract_keys(
        swift_contract_source):
    literal = swift_multiline_literal(swift_contract_source, "contractFixture")
    data = json.loads(literal)
    assert set(data) == {"mcus", "single"}
    for row in data["mcus"]:
        assert set(row) == set(CONTRACT_KEYS), row


def test_the_conflict_vocabulary_is_one_list_three_spellings(
        offline_machine, swift_contract_source):
    """A (mcus.CONFLICT_CODES) == B (cli._MCU_CONFLICTS) == C (McuConflict).

    mcus.py owns the list and its order IS the precedence `conflict` is ranked
    by, so the CLI's tuple must match it exactly; Swift's enum raw values must
    cover the same three strings (its case order is display order, not
    precedence, so only the set is pinned there).
    """
    mcus = offline_machine.mcus
    cli = offline_machine.cli
    assert tuple(mcus.CONFLICT_CODES) == CONFLICT_ORDER
    assert tuple(cli._MCU_CONFLICTS) == CONFLICT_ORDER
    assert cli._MCU_CONFLICT_RANK == {
        code: rank for rank, code in enumerate(CONFLICT_ORDER)}
    assert set(mcus.CONFLICT_CODES) <= set(mcus.ISSUE_CODES)
    assert set(swift_conflict_raw_values(swift_contract_source)) == \
        set(CONFLICT_ORDER)


def test_the_swift_decoder_reads_only_keys_the_producer_emits(
        swift_contract_source, golden_bytes):
    """C's `entry["..."]`/`dict["..."]` lookups against B's actual field names.

    A key C reads that B does not emit decodes to nil and silently loses a
    board's port; a key B emits that C never reads is only dead weight. The
    first is the bug this pins.
    """
    keys = swift_decoded_keys(swift_contract_source)
    data = json.loads(golden_bytes)
    assert keys["root"] == {"mcus", "single"}, keys["root"]
    assert keys["entry"] == set(CONTRACT_KEYS), keys["entry"]
    assert keys["identity"] == set(IDENTITY_KEYS), keys["identity"]
    for row in data["mcus"]:
        assert set(row) == keys["entry"]
        if row["identity"] is not None:
            assert set(row["identity"]) == keys["identity"]


def test_the_contract_is_stable_across_repeated_calls(run_serial_json,
                                                      golden_bytes):
    """Same input, same bytes, twice: no dict ordering luck, no timestamp, no
    device enumeration order leaking into a GUI poll."""
    first, _ = run_serial_json()
    second, _ = run_serial_json()
    assert first == second == golden_bytes + "\n"


def test_the_golden_matches_the_documented_shape_in_both_sources(
        golden_bytes, swift_contract_source):
    """The contract is written down three times: the CLI's comment block, the
    Swift header comment, and these bytes. A reader of either file must be able
    to trust the prose, so check the prose mentions every key."""
    for key in CONTRACT_KEYS:
        assert '"%s"' % key in golden_bytes
        assert key in swift_contract_source
    assert CANON_CFG.count("[mcu") >= 7


def test_the_single_flag_is_the_section_count_not_an_attachment_fact(
        run_serial_json):
    """`single` describes printer.cfg, not the USB bus: an unplugged board is
    still a multi-MCU printer, and a GUI that hid its rows whenever a cable
    came loose would hide the problem it is there to show."""
    out, _ = run_serial_json()
    data = json.loads(out)
    assert data["single"] is False and len(data["mcus"]) == 7
