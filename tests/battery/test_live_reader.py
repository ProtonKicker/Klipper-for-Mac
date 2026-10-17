"""Live tier (KLIPPERFORMAC_ALLOW_LIVE=1, see pytest.ini): the battery reader
against the real Mac.

The whole offline suite judges pmset/ioreg through captured fixture strings and
injected env hooks; that fidelity is exactly what needs a periodic check on
live macOS output, because a parser that silently stops matching reality means
a watchdog reading "no battery" while a print dies at 4 %. One test, one read,
read-only: no socket, no process signal, nothing sent to the printer (the
folder's autouse no_network trap stays armed, and pmset touches no socket).
"""
import pytest

pytestmark = pytest.mark.live


def test_live_power_reader_reads_this_mac_for_real(stack, monkeypatch):
    """`read_power()` with every injection env cleared: the shipped chain
    (/usr/bin/pmset, then /usr/sbin/ioreg as the fallback) must return a
    reading that passes the same _clean_percent gate that protects a print.
    A Mac that truly has no battery makes pmset say "present: no" - that is
    parse_pmset's offline-proven None, so this bench (a MacBook) must read a
    percent."""
    bg = stack.batteryguard
    for var in ("KLIPPERFORMAC_BATTERY_FILE", "KLIPPERFORMAC_BATTERY_PERCENT",
                "KLIPPERFORMAC_POWER_SOURCE", "KLIPPERFORMAC_BATTERY_POLL"):
        monkeypatch.delenv(var, raising=False)
    reading = bg.read_power()
    assert reading is not None, ("this Mac told pmset/ioreg's parsers nothing "
                                 "about a battery")
    assert reading["engine"] in ("pmset", "ioreg"), reading
    assert reading["source"] in ("battery", "ac"), reading
    assert bg.clean_percent(reading["percent"]) is not None, reading
    # the live reading is judged like any other: evaluate() eats it without
    # raising and without any destructive surprise on a healthy Mac
    actions, _ep = bg.evaluate(reading, bg.new_episode(), bg.normalise({}))
    assert all(isinstance(a, str) for a in actions)
