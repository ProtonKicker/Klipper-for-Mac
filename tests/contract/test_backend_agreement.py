"""The degrade path against the reader: does `serial --json` answer the same way
with `mcus.py` present and with it missing or broken?

cli.py keeps its own line-oriented parse (`_parse_mcu_sections`) for a machine
where the reader module cannot be imported - an old install, a half-copied
upgrade, a syntax error in a file the installer never restarted. Its docstring
promises the consumer "must never be able to tell which backend answered", and
that promise is nobody's unit: A tests mcus.py, B tests the CLI with mcus stubbed
out both ways separately, C only ever sees one CLI. So the seam is pinned here.

WHAT AGREEMENT MEANS
--------------------
The two backends must produce the SAME payload for every config shape, and they
now do for every one of the 26 hostile shapes plus the canonical seven-section
rig, except a frozen delta of exactly one (see FROZEN_DELTAS): how the value of a
`serial:` carrying raw control bytes is truncated. Its conflict, its row count
and its verdict are identical, so the GUI renders the same badge either way - a
display difference in a string no device node can match, not a different answer.

If that set ever GROWS, this file fails on purpose: the degrade path is a copy of
rules the reader owns, and a rule copied wrong is how a tool ends up telling two
stories about one printer.cfg.
"""
import json

import pytest

from tests.contract.conftest import (CONFLICT_ORDER, CONTRACT_KEYS, CANON_CFG)
from tests.mcus.conftest import HOSTILE

#: label -> what still differs, and why it is tolerable rather than a bug.
FROZEN_DELTAS = {
    "binary junk": (
        "serial: holds raw control bytes (NUL, vertical tab). mcus.py hands back "
        "the whole value klipper reads; the line parser's '(\\S+)' stops at the "
        "first one. Row count, name, section, claimed and conflict are identical "
        "('missing-port' both ways: neither value can be opened), so nothing a "
        "user or the GUI acts on differs. Fixing it means reimplementing "
        "mcus.option_value() in the degrade path, which is the duplication this "
        "parse exists to avoid."),
}


def _payload(out):
    return json.loads(out)


@pytest.fixture(scope="module")
def shapes():
    """(label, config text) for every shape the matrix runs, canonical first."""
    return [("canonical seven-section rig", CANON_CFG)] + list(HOSTILE)


@pytest.mark.parametrize("label,text", HOSTILE, ids=[label for label, _ in HOSTILE])
def test_a_hostile_config_answers_the_contract_not_a_crash(
        run_serial_json, label, text):
    """The exit-0/never-traceback promise, per backend, per shape."""
    for use_mcus in (True, False):
        out, err = run_serial_json(text=text, use_mcus=use_mcus)
        assert "Traceback" not in err, (label, use_mcus, err)
        data = _payload(out)
        assert set(data) == {"mcus", "single"}
        assert isinstance(data["single"], bool)
        for row in data["mcus"]:
            assert tuple(row) == CONTRACT_KEYS, (label, use_mcus, row)
            assert row["conflict"] in set(CONFLICT_ORDER) | {None}, row


def test_the_degrade_path_agrees_with_the_reader_row_for_row(
        shapes, run_serial_json):
    """One pass over every shape, comparing the two payloads as parsed JSON.

    Compared as data, not bytes: the byte-golden belongs to the reader path (the
    one that ships) and is pinned in test_json_bytes_contract.py; what this test
    owns is "a user on a machine without mcus.py is not told a different story".
    """
    drifted = []
    for label, text in shapes:
        with_reader, _ = run_serial_json(text=text, use_mcus=True)
        without, _ = run_serial_json(text=text, use_mcus=False)
        if _payload(with_reader) != _payload(without):
            drifted.append(label)
    assert set(drifted) == set(FROZEN_DELTAS), (
        "the two backends disagree on %s; either copy the rule properly or "
        "explain the difference in FROZEN_DELTAS (and mean it)" % (drifted,))


def test_the_one_frozen_delta_changes_nothing_a_consumer_acts_on(
        run_serial_json):
    """The frozen delta is a string, not a verdict: same rows, same conflicts.

    Pinned as assertions rather than left in a comment, so a future change that
    turns it into a real disagreement (a row appearing, a conflict flipping)
    fails HERE instead of quietly joining the accepted list.
    """
    text = dict(HOSTILE)["binary junk"]
    with_reader, _ = run_serial_json(text=text, use_mcus=True)
    without, _ = run_serial_json(text=text, use_mcus=False)
    a, b = _payload(with_reader), _payload(without)
    assert [row["mcu"] for row in a["mcus"]] == \
        [row["mcu"] for row in b["mcus"]]
    assert [row["conflict"] for row in a["mcus"]] == \
        [row["conflict"] for row in b["mcus"]]
    assert [row["claimed"] for row in a["mcus"]] == \
        [row["claimed"] for row in b["mcus"]]
    assert a["single"] == b["single"]
    # ... and the only field that moves is the unusable serial itself
    assert a["mcus"][0]["serial"] != b["mcus"][0]["serial"]


def test_both_backends_report_the_canonical_rig_identically(run_serial_json):
    """The byte-golden's own config, spelled out: seven rows, three conflicts.

    If the degrade path lost the klipper-faithful name rules, '[mcu  board]'
    would come back as 'board' with a name this tool happily writes - and the
    board klipper registered is ' board', which no pin can address.
    """
    a, _ = run_serial_json(use_mcus=True)
    b, _ = run_serial_json(use_mcus=False)
    data = _payload(a)
    assert _payload(b) == data, "canonical rig drifted between backends"
    assert len(data["mcus"]) == 7
    board = [row for row in data["mcus"] if row["section"] == "[mcu  board]"][0]
    assert board["mcu"] == " board", board
    assert board["conflict"] == "bad-section-name", board


def test_the_frozen_delta_list_is_still_needed():
    """A guard against the accepted-difference list rotting.

    If a backend is ever fixed, its entry here becomes a lie about the code, so
    require every frozen label to still be a shape the matrix actually runs.
    """
    known = {label for label, _ in HOSTILE}
    assert set(FROZEN_DELTAS) <= known, set(FROZEN_DELTAS) - known
