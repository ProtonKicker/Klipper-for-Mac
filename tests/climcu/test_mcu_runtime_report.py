"""Per-MCU runtime: what doctor and selftest ask klippy, and how they degrade.

`klipper state: ready` is the printer as a whole. Upstream builds one object per
MCU - 'mcu' for the timing master and 'mcu <name>' for each '[mcu <name>]'
(klippy/mcu.py:1246-1253) - so the honest question is per section:
`GET /printer/objects/query?mcu%20tool=mcu_version`. Three answers, and only the
middle one is a failure:

  ok       klippy loaded it (and says which firmware)
  missing  klippy has no such object: cable 2 is not talking
  unknown  the stack did not answer in 3 s - never red, never a hang

Read-only: one GET with a timeout, no restart, no flash, no write.
"""
import pytest


@pytest.fixture
def probe(cstack, monkeypatch):
    """Feed `_moonraker_get` canned answers and record the URLs it was asked."""
    seen = []

    def install(responses):
        def fake_get(path, timeout=3):
            seen.append((path, timeout))
            return responses.get(path)
        monkeypatch.setattr(cstack.cli, "_moonraker_get", fake_get)
        seen.clear()
        return seen
    return install


def _answer(obj, version):
    return {"result": {"eventtime": 1.0,
                       "status": {obj: {"mcu_version": version}}}}


def test_one_query_per_section_and_it_is_the_klipper_object_name(
        cstack, attached, printer_cfg, facts, probe):
    printer_cfg(facts.CFG_MULTI_SET)
    attached([facts.MASTER, facts.TOOL_NODE], [])
    seen = probe({"/printer/objects/query?mcu=mcu_version":
                  _answer("mcu", "461c4e3"),
                  "/printer/objects/query?mcu%20tool=mcu_version":
                  _answer("mcu tool", "abc1234")})
    lines = cstack.cli._mcu_report_lines()
    assert [path for path, _t in seen] == [
        "/printer/objects/query?mcu=mcu_version",
        "/printer/objects/query?mcu%20tool=mcu_version"]
    assert all(timeout == 3.0 for _p, timeout in seen), "never a long hang"
    assert [tag for tag, _text, verdict in lines] == ["[ok] ", "[ok] "]
    assert "461c4e3" in lines[0][1] and "abc1234" in lines[1][1]
    assert "[mcu tool]" in lines[1][1]


def test_a_section_klipper_never_loaded_is_said_out_loud(
        cstack, attached, printer_cfg, facts, probe):
    """Moonraker answers 200 with a null value for an object that does not
    exist: that IS 'this board is not connected', not a timeout."""
    printer_cfg(facts.CFG_MULTI_SET)
    attached([facts.MASTER, facts.TOOL_NODE], [])
    probe({"/printer/objects/query?mcu=mcu_version": _answer("mcu", "461c4e3"),
           "/printer/objects/query?mcu%20tool=mcu_version":
           _answer("mcu tool", None)})
    lines = cstack.cli._mcu_report_lines()
    assert [tag for tag, _t, _v in lines] == ["[ok] ", "[!] "]
    assert lines[1][2] == "missing"
    assert "klippy did not load" in lines[1][1]


def test_a_silent_stack_is_unknown_never_red(cstack, attached, printer_cfg,
                                            facts, probe):
    printer_cfg(facts.CFG_MULTI_SET)
    attached([facts.MASTER], [])
    probe({})                          # every GET returns None
    lines = cstack.cli._mcu_report_lines()
    assert [tag for tag, _t, _v in lines] == ["[i]  ", "[i]  "]
    assert all(verdict == "unknown" for _tag, _text, verdict in lines)
    assert all("runtime unknown" in text for _tag, text, _v in lines)


def test_a_config_with_no_sections_asks_nothing(cstack, attached, printer_cfg,
                                               probe):
    printer_cfg("[printer]\nspeed: 50\n")
    attached([], [])
    seen = probe({})
    assert cstack.cli._mcu_report_lines() == []
    assert seen == []


def test_a_moonraker_answer_that_is_nonsense_is_unknown(
        cstack, attached, printer_cfg, facts, probe):
    printer_cfg(facts.CFG_SINGLE)
    attached([facts.MASTER], [])
    probe({"/printer/objects/query?mcu=mcu_version": {"result": {}}})
    lines = cstack.cli._mcu_report_lines()
    assert lines[0][2] == "unknown"


def test_selftest_prints_the_sections_and_still_defers_to_the_child(
        cstack, attached, printer_cfg, facts, monkeypatch, capsys):
    """`selftest`'s exit code is the AGENTS.md gate and belongs to the child
    process; the per-MCU lines are the same ones doctor prints."""
    import argparse
    printer_cfg(facts.CFG_MULTI_SET)
    attached([facts.MASTER, facts.TOOL_NODE], [])
    monkeypatch.setattr(cstack.cli.subprocess, "call", lambda *a, **k: 0)
    monkeypatch.setattr(cstack.cli, "_moonraker_get",
                        lambda path, timeout=3: _answer("mcu", "461c4e3")
                        if path.endswith("?mcu=mcu_version") else None)
    cstack.cli.cmd_selftest(argparse.Namespace())
    out = capsys.readouterr().out
    assert "[ok] mcu [mcu]: loaded by klippy (firmware 461c4e3)" in out
    assert "runtime unknown" in out


def test_selftest_exit_code_is_the_childs(cstack, attached, printer_cfg,
                                         facts, monkeypatch, capsys):
    import argparse
    printer_cfg(facts.CFG_SINGLE)
    attached([facts.MASTER], [])
    monkeypatch.setattr(cstack.cli.subprocess, "call", lambda *a, **k: 3)
    monkeypatch.setattr(cstack.cli, "_moonraker_get",
                        lambda path, timeout=3: None)
    with pytest.raises(SystemExit) as exc:
        cstack.cli.cmd_selftest(argparse.Namespace())
    assert exc.value.code == 3


def test_doctor_goes_red_only_for_a_missing_section_when_klipper_is_ready(
        cstack):
    """The rule behind the gate: 'unknown' never turns doctor red (AGENTS.md 4
    gates install correctness, not hardware attendance), and a section klippy
    did not load while reporting `ready` always does."""
    missing = [("[!] ", "mcu [mcu tool]: klippy did not load this", "missing")]
    unknown = [("[i]  ", "mcu [mcu tool]: runtime unknown", "unknown")]
    ok = [("[ok] ", "mcu [mcu]: loaded by klippy", "ok")]
    assert cstack.cli._mcu_lines_fail(missing, "ready") is True
    assert cstack.cli._mcu_lines_fail(missing, "startup") is False
    assert cstack.cli._mcu_lines_fail(unknown, "ready") is False
    assert cstack.cli._mcu_lines_fail(ok, "ready") is False
    assert cstack.cli._mcu_lines_fail([], "ready") is False
    assert cstack.cli._mcu_lines_fail(None, "ready") is False


def test_the_report_helper_is_the_same_one_selftest_prints(
        cstack, attached, printer_cfg, facts, probe, monkeypatch, capsys):
    """One helper, two commands: the lines `doctor` prints are the ones
    `selftest` prints, so the two can never disagree about one Mac."""
    import argparse
    printer_cfg(facts.CFG_MULTI_SET)
    attached([facts.MASTER, facts.TOOL_NODE], [])
    probe({"/printer/objects/query?mcu=mcu_version": _answer("mcu", "461c4e3"),
           "/printer/objects/query?mcu%20tool=mcu_version":
           _answer("mcu tool", None)})
    expected = cstack.cli._mcu_report_lines()
    monkeypatch.setattr(cstack.cli.subprocess, "call", lambda *a, **k: 0)
    capsys.readouterr()
    cstack.cli.cmd_selftest(argparse.Namespace())
    out = capsys.readouterr().out
    for tag, text, _verdict in expected:
        assert tag + text in out, (tag, text, out)
