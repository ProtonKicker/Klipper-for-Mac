"""The kill sequence must refuse to start when the printer is not ready.

Both blocked live runs had a dead MCU link (USB wiggle after an accidental
unplug): POSTing print/start into that state is a guaranteed 400 and proves
nothing. The precondition now asks first and leaves the machine untouched.
"""
from klipperformac import powerloss


class NotReadyHarness(powerloss.KillTestHarness):
    def __init__(self):
        self.calls = []

    def enable_journal(self):
        self.calls.append("journal")
        return {"journal": True}

    def wait_ready(self, timeout=90.0):
        self.calls.append("ready")
        return False

    def _never(self, *a, **k):
        self.calls.append("beyond-ready")
        raise AssertionError("sequence continued past a not-ready printer")

    write_test_gcode = _never
    start_print = _never
    wait_printing = _never
    journal_tick = _never
    pause = _never
    journal_records = _never
    stack_pids = _never
    kill = _never
    restart_stack = _never
    last_stop = _never
    cancel_print = _never
    disable_journal = _never


def test_run_refuses_and_touches_nothing():
    h = NotReadyHarness()
    ok, checks = powerloss.run_kill_sequence(h, log=lambda *a: None)
    assert ok is False
    assert h.calls == ["journal", "ready"]
    names = [c[0] for c in checks]
    assert "klippy is ready before the sequence starts" in names


def test_default_harness_is_ready_for_offline_machines():
    # The base default keeps every existing offline state machine working:
    # a harness that never mentions wait_ready proceeds normally.
    assert powerloss.KillTestHarness.wait_ready(
        powerloss.KillTestHarness()) is True
