"""LIVE tier: the kill sequence on the bench board behind this install.

Deselected by default (pytest.ini: ``-m "not live and not hardware"``) and
refusing without KLIPPERFORMAC_ALLOW_LIVE=1, exactly like `klipperformac
powerloss-test` itself, which is the command this test runs through the module
API so a failed check comes back as a named assertion instead of a grep.

What it proves on real hardware, and nothing else can:
  * klipper SIGKILLed mid-print really does leave the journal readable and the
    session marker open (the crash is visible, not swallowed by process._load()
    clearing run/state.json),
  * Moonraker SIGKILLed mid-print does the same, and klipper outlives it,
  * the stack rebuilds to `klippy ready` both times, with no orphan of OURS and
    no stack port held by anybody else,
  * a graceful `down` after all that writes the clean marker.

The offline twin of this exact sequence is test_kill_sequence_machine.py in this
folder, which is what runs in the gate: same `run_kill_sequence()` source, fake
machine, no board harmed.
"""
import os

import pytest

from klipperformac import paths, powerloss, process

pytestmark = pytest.mark.live

# Anything else and the sequence has no business killing a stack: the opt-in that
# `powerloss-test` itself demands is the opt-in for this test.
SKIPPED = "KLIPPERFORMAC_ALLOW_LIVE=1 is not set (see: make test-live)"


def test_bench_kill_sequence_survives_both_sigkills(capsys):
    if os.environ.get("KLIPPERFORMAC_ALLOW_LIVE") != "1":
        pytest.skip(SKIPPED)
    state = process.running() or {}
    if not (state.get("klipper") and state.get("moonraker")):
        pytest.skip("the stack is not up: klipperformac up first")
    live = powerloss.query_stats() or {}
    if live.get("state") in ("printing", "paused"):
        pytest.skip("a real print is live ({}); this test kills mid-print"
                    .format(live.get("filename")))
    prior = dict(paths.load_settings().get("powerloss") or {})
    try:
        harness = powerloss.LiveHarness()
        ok, checks = powerloss.run_kill_sequence(harness, ticks=3)
        capsys.readouterr()          # the sequence's own report, on demand
        failed = [(name, detail) for name, passed, detail in checks
                  if not passed]
        assert ok, "kill sequence failed at: {}".format(failed)
        assert len(checks) >= 25, checks
        names = [name for name, _passed, _d in checks]
        for promised in ("journal recorded advancing byte offsets",
                         "unclean stop detected after SIGKILL klipper",
                         "journal survived SIGKILL klipper",
                         "journal survived SIGKILL moonraker",
                         "clean-stop marker written by down",
                         "stack healthy at the end",
                         "no orphan service processes",
                         "no foreign process holds a stack port"):
            assert promised in names, promised
        # The markers this run wrote are real files in this install, and the
        # verdict they carry is the product: read them, do not trust the print.
        verdict = powerloss.last_stop(stack_up=True)
        assert verdict["state"] == "running", verdict
        assert powerloss.read_records(), "the journal is empty on a machine " \
                                         "that just printed"
    finally:
        # Leave the user's own settings and gcodes/ as they were found: the test
        # enabled the journal to prove it works, it did not ask to keep it on.
        try:
            powerloss.save_settings(**{key: value for key, value in prior.items()
                                       if key in powerloss.DEFAULTS}
                                   if prior else {"journal": False})
        except OSError:
            pass
        try:
            (paths.DATA / "gcodes" / powerloss.TEST_GCODE_NAME).unlink()
        except OSError:
            pass


def test_live_rest_evidence_is_what_the_module_claims():
    """The read-only half: the module's citations about what upstream serves must
    hold against the running Moonraker, or `status` is describing a fiction."""
    if os.environ.get("KLIPPERFORMAC_ALLOW_LIVE") != "1":
        pytest.skip(SKIPPED)
    stats = powerloss.query_stats()
    assert stats is not None, "/printer/objects/query?print_stats&... did not answer"
    for key in ("state", "file_position", "file_size", "filename", "progress"):
        assert key in stats, key
    assert stats["file_position"] >= 0 and stats["file_size"] >= 0
    # upstream's one durable signal, answered live rather than cited from memory
    answer = powerloss._http_get("/server/history/list?limit=25")
    assert isinstance(((answer or {}).get("result") or {}).get("jobs"), list)
    # and the namespace the facts claimed was readable is still 403: the module
    # says so, so the claim is re-proved instead of inherited.
    forbidden = powerloss._http_get(
        "/server/database/item?namespace=database&key=unsafe_shutdowns")
    assert forbidden is None or "error" in forbidden, forbidden
