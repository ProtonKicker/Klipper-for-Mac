"""The bounded print journal: JSONL that has to survive being cut off.

Two properties matter more than the format: it must contain the ONLY thing
upstream never persists on this Mac - virtual_sdcard's `file_position`, the byte
offset M26 wants (virtual_sdcard.py:220-225) - and it must be readable after an
abrupt death, which means a torn last line is expected, not exceptional. The size
cap is the third: a resident poller on a printing machine would otherwise grow a
file forever.
"""
import json
import os

import pytest
from conftest import rest, stats_payload, write_state
from hypothesis import HealthCheck, given, settings, strategies as st


def test_the_journal_is_off_until_the_user_turns_it_on(pl):
    """AGENTS.md 2/5: nothing that writes to disk during a print is enabled
    behind the user's back, and "off" here is proven by the GET never happening."""
    calls = []
    pl.powerloss._http_get = lambda path, timeout=3: calls.append(path)
    assert pl.powerloss.journal_enabled() is False
    assert pl.powerloss.journal_tick() is None
    assert calls == []
    assert not pl.paths.POWERLOSS.exists()


def test_snapshot_force_writes_even_with_the_setting_off(pl):
    """`powerloss snapshot` is a record the user asked for by name: consent is in
    the typing, and it is one GET plus one line."""
    pl.powerloss._http_get = lambda path, timeout=3: stats_payload()
    record = pl.powerloss.journal_tick(force=True)
    assert record["file_position"] == 1024
    assert len(pl.powerloss.read_records()) == 1


@pytest.mark.parametrize("state", ["printing", "paused"])
def test_an_active_print_lands_in_the_journal(pl, state):
    pl.powerloss.save_settings(journal=True)
    pl.powerloss._http_get = lambda path, timeout=3: stats_payload(
        state=state, position=777)
    record = pl.powerloss.journal_tick()
    assert record["state"] == state and record["file_position"] == 777
    assert record["filename"] == "cube.gcode"
    assert record["progress"] == pytest.approx(777 / 300000.0)


@pytest.mark.parametrize("stats,expect", [
    (stats_payload(state="standby", filename=""), None),
    (stats_payload(state="complete"), None),
    (stats_payload(filename=""), None),
    (None, None),
])
def test_an_idle_printer_is_never_journalled(pl, monkeypatch, stats, expect):
    """One record per poll of a machine printing nothing would fill the cap with
    noise and bury the seam - `standby` has no offset worth keeping."""
    monkeypatch.setattr(pl.powerloss, "query_stats", lambda timeout=3: stats)
    assert pl.powerloss.record_for(
        pl.powerloss.query_stats()) is expect
    assert pl.powerloss.last_record() is None


def test_query_stats_reads_the_offset_from_virtual_sdcard(pl):
    """There is NO /printer/print_stats REST route (404, verified live): the data
    comes from the object query, and file_position lives in virtual_sdcard, not in
    print_stats. Pinned here so nobody "simplifies" the URL back to a 404."""
    seen = []

    def _get(path, timeout=3):
        seen.append(path)
        return stats_payload(position=9000, size=30000)

    pl.powerloss._http_get = _get
    stats = pl.powerloss.query_stats()
    assert stats["file_position"] == 9000 and stats["file_size"] == 30000
    assert stats["state"] == "printing" and stats["current_layer"] == 3
    assert seen == ["/printer/objects/query?print_stats&virtual_sdcard&toolhead"]


def test_query_stats_answer_of_junk_is_none(pl):
    for answer in (None, [], {"result": None}, {"result": {"status": []}},
                   {"error": "404"}):
        pl.powerloss._http_get = lambda path, timeout=3, a=answer: a
        assert pl.powerloss.query_stats() is None


def test_junk_status_values_coerce_instead_of_reaching_json(pl):
    """Moonraker answers numbers; a None/str/bool in a field is no evidence, and
    allow_nan=False means a float('nan') would raise on the way out."""
    payload = stats_payload()
    status = payload["result"]["status"]
    status["virtual_sdcard"]["file_position"] = "not a number"
    status["virtual_sdcard"]["progress"] = float("nan")
    status["print_stats"]["print_duration"] = True
    status["print_stats"]["filename"] = None
    status["toolhead"]["position"] = "home"
    pl.powerloss._http_get = lambda path, timeout=3: payload
    stats = pl.powerloss.query_stats()
    assert stats["file_position"] == 0 and stats["progress"] is None
    assert stats["print_duration"] is None and stats["filename"] == ""
    assert stats["position"] is None
    assert pl.powerloss.append(pl.powerloss.record_for(
        dict(stats, filename="cube.gcode", state="printing"))) is not None


def test_records_are_one_json_object_per_line_and_fsynced(pl, monkeypatch):
    """fsync before returning is the whole point: a buffer a power cut takes away
    is a journal that lied about the seam."""
    syncs = []
    monkeypatch.setattr(os, "fsync", lambda fd: syncs.append(fd))
    pl.powerloss.save_settings(journal=True)
    pl.powerloss._http_get = lambda path, timeout=3: stats_payload(position=1)
    pl.powerloss.journal_tick()
    pl.powerloss._http_get = lambda path, timeout=3: stats_payload(position=2)
    pl.powerloss.journal_tick()
    lines = pl.paths.POWERLOSS.joinpath("journal.jsonl").read_text().splitlines()
    assert len(lines) == 2 and all(json.loads(line)["file_position"] for line in lines)
    assert len(syncs) == 2  # exactly one fsync per appended snapshot


def test_a_torn_last_line_is_skipped_not_fatal(pl):
    """kill -9 and a dead 12V rail leave half a line: the reader must still hand
    back every complete record, because that is the resume point."""
    pl.paths.POWERLOSS.mkdir(parents=True)
    good = json.dumps({"file_position": 4096, "filename": "cube.gcode",
                       "state": "printing", "epoch": 1.0})
    (pl.paths.POWERLOSS / "journal.jsonl").write_text(
        good + "\n" + good.replace("4096", "5000") + "\n" + '{"file_posi')
    records = pl.powerloss.read_records()
    assert [record["file_position"] for record in records] == [4096, 5000]
    assert pl.powerloss.last_record()["file_position"] == 5000


@pytest.mark.parametrize("junk", ["", "\n\n", "{}", "[1,2]", '{"a": 1}\nnope',
                                  "x" * 5000])
def test_a_journal_of_rubbish_is_empty_not_a_crash(pl, junk):
    pl.paths.POWERLOSS.mkdir(parents=True)
    (pl.paths.POWERLOSS / "journal.jsonl").write_text(junk)
    assert isinstance(pl.powerloss.read_records(), list)
    assert pl.powerloss.last_record() is None
    assert pl.powerloss.journal_size() > 0 or junk == ""


def test_the_cap_rotates_to_the_newest_records_atomically(pl):
    """Size-capped AND atomic: the rotation is tmp+fsync+replace, so a death in
    the middle of it leaves the old file intact, and the newest seam always
    survives - that record is the one a resume reads."""
    cfg = {"journal": True, "intervalSec": 5.0, "maxBytes": 600, "keepRecords": 3}
    for index in range(40):
        record = {"epoch": float(index), "file_position": index * 100,
                  "filename": "cube.gcode", "state": "printing",
                  "file_size": 300000}
        pl.powerloss.append(record)
    assert pl.powerloss.cap_size(cfg) is True
    records = pl.powerloss.read_records()
    assert len(records) == cfg["keepRecords"] == 3
    assert records[-1]["file_position"] == 3900
    assert records[0]["file_position"] == 3700
    assert pl.powerloss.journal_size() <= 600 + 200
    assert not (pl.paths.POWERLOSS / "journal.jsonl.tmp").exists()


def test_rotation_keeps_a_readable_seam_when_the_cap_is_tiny(pl):
    cfg = {"journal": True, "intervalSec": 5.0, "maxBytes": 128,
           "keepRecords": 2}
    for index in range(20):
        pl.powerloss.append({"epoch": float(index), "file_position": index,
                             "filename": "cube.gcode", "state": "printing",
                             "file_size": 300000})
        pl.powerloss.cap_size(cfg)
    assert pl.powerloss.journal_size() <= 400
    records = pl.powerloss.read_records()
    assert records and records[-1]["file_position"] == 19


def test_append_returns_none_when_the_folder_refuses(pl):
    """`up` and the watcher must survive a read-only data folder: the answer is
    None (nothing journalled, doctor says so), not a traceback."""
    pl.paths.DATA.mkdir(parents=True, exist_ok=True)
    (pl.paths.POWERLOSS).write_text("in the way")
    assert pl.powerloss.append({"a": 1}) is None
    assert pl.powerloss.cap_size() is False
    assert pl.powerloss.writable() is False


def test_watch_stops_when_the_stack_stops_answering(pl, monkeypatch):
    """The loop is the journal's heartbeat; it must not spin forever against a
    dead Moonraker, and with the setting off it must refuse to write a byte -
    not even one GET."""
    sleeps = []
    monkeypatch.setattr(pl.powerloss.time, "sleep",
                        lambda seconds: sleeps.append(seconds))
    calls = rest(pl, stats_payload(position=10), stats_payload(position=20),
                 None)
    assert pl.powerloss.watch(log=lambda _text: None) == 0
    assert calls == []
    pl.powerloss.save_settings(journal=True)
    assert pl.powerloss.watch(log=lambda _text: None) == 2
    assert [record["file_position"] for record in pl.powerloss.read_records()] \
        == [10, 20]
    assert sleeps == [pl.powerloss.DEFAULTS["intervalSec"]] * 2


def test_watch_honours_sweeps_and_interval(pl, monkeypatch):
    """`--sweeps N` is how a one-shot run ends: the budget is spent BEFORE the
    sleep, so the last poll never waits for nothing."""
    sleeps = []
    monkeypatch.setattr(pl.powerloss.time, "sleep",
                        lambda seconds: sleeps.append(seconds))
    rest(pl, stats_payload(position=1))
    pl.powerloss.save_settings(journal=True, intervalSec=90)
    assert pl.powerloss.watch(sweeps=2, log=lambda _text: None) == 2
    assert sleeps == [90.0]


def test_watch_interval_is_clamped(pl, monkeypatch):
    """A poll period of 1e9 s would journal nothing until next year and a negative
    one would spin: the same bounds the settings coercion uses apply to the flag."""
    sleeps = []
    monkeypatch.setattr(pl.powerloss.time, "sleep",
                        lambda seconds: sleeps.append(seconds))
    rest(pl, stats_payload(position=1))
    pl.powerloss.save_settings(journal=True)
    pl.powerloss.watch(interval=1e9, sweeps=2, log=lambda _text: None)
    pl.powerloss.watch(interval=-5, sweeps=2, log=lambda _text: None)
    assert sleeps == [pl.powerloss.MAX_INTERVAL, pl.powerloss.MIN_INTERVAL]


def test_watch_refuses_to_run_with_the_journal_off(pl, monkeypatch):
    monkeypatch.setattr(pl.powerloss.time, "sleep", lambda _s: None)
    calls = rest(pl, stats_payload(position=1))
    printed = []
    assert pl.powerloss.watch(log=printed.append) == 0
    assert calls == [] and any("powerloss on" in line for line in printed)


@given(junk=st.one_of(st.none(), st.booleans(), st.text(max_size=4),
                      st.integers(), st.floats(), st.lists(st.integers()),
                      st.dictionaries(st.text(max_size=3), st.integers())))
@settings(deadline=None, max_examples=150,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_any_junk_in_the_settings_block_reads_as_the_defaults(pl, junk):
    """settings.json is shared with the SwiftUI app, so every value type in the
    wild is possible: no junk may make the journal unconfigured, uncapped or
    ready to write once a second for a year."""
    cfg = pl.powerloss.normalise({"journal": junk, "intervalSec": junk,
                                 "maxBytes": junk, "keepRecords": junk})
    # The one value that is not junk is the boolean True: `journal` is compared to
    # True, never coerced, exactly like `lan` in process.py.
    assert cfg["journal"] is (junk is True)
    assert pl.powerloss.MIN_INTERVAL <= cfg["intervalSec"] <= pl.powerloss.MAX_INTERVAL
    assert pl.powerloss.MIN_MAX_BYTES <= cfg["maxBytes"] <= pl.powerloss.MAX_MAX_BYTES
    assert pl.powerloss.MIN_KEEP <= cfg["keepRecords"] <= pl.powerloss.MAX_KEEP


def test_only_the_powerloss_block_is_written(pl):
    """settings.json holds the camera prefs, the LAN switch and the guard's
    thresholds: our write must land in our own key and leave the rest alone."""
    pl.paths.SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    pl.paths.SETTINGS.write_text(json.dumps(
        {"lan": False, "cam": {"device": "FaceTime"}, "data": str(pl.data)}))
    cfg = pl.powerloss.save_settings(journal=True, intervalSec=2.5)
    saved = json.loads(pl.paths.SETTINGS.read_text())
    assert saved["cam"] == {"device": "FaceTime"} and saved["lan"] is False
    assert saved["powerloss"] == cfg
    assert cfg["journal"] is True and cfg["intervalSec"] == 2.5


def test_the_only_true_enables_the_journal(pl):
    """A string "false" or "0" is not off: `lan` in process.py holds the same
    line, so the switch is compared to the boolean True, never coerced."""
    for value in ("true", 1, [True]):
        pl.powerloss.save_settings(journal=False)
        settings = pl.paths.load_settings()
        settings["powerloss"] = dict(settings["powerloss"], journal=value)
        pl.paths.save_settings(settings)
        assert pl.powerloss.journal_enabled() is False


def test_tail_lines_number_records_by_their_place_in_the_journal(pl):
    for index in range(12):
        pl.powerloss.append({"epoch": float(index), "at": "t", "file_position":
                             index * 1000, "filename": "cube.gcode",
                             "state": "printing", "file_size": 300000,
                             "progress": index / 12.0, "current_layer": index})
    lines = pl.powerloss.tail_lines(3)
    assert len(lines) == 3
    assert "#10" in lines[0] and "#12" in lines[-1]
    assert "byte 11000/300000" in lines[-1]


def test_resume_recipe_is_upstream_gcode_and_never_auto(pl):
    """M23/M26/M24/M27 are Klipper's own commands (virtual_sdcard.py:180-232):
    that is the only reason a power-loss tool can exist here without a patch, and
    the reason the recipe says "judge the seam by eye" - upstream keeps no state
    file and has no recover_at_seam, so an offset is not a proven state."""
    pl.powerloss.append({"epoch": 1.0, "at": "t", "filename": "cube.gcode",
                         "state": "printing", "file_position": 8192,
                         "file_size": 32768, "progress": 0.25})
    text = "\n".join(pl.powerloss.resume_recipe())
    assert "M23 FILE=cube.gcode" in text
    assert "M26 S=8192" in text
    assert "M24" in text and "M27" in text
    assert "25.0%" in text
    assert "never auto-resumes" in text.lower() or "not a proven" in text


def test_resume_recipe_without_a_journal_says_so(pl):
    text = "\n".join(pl.powerloss.resume_recipe())
    assert "nothing journaled yet" in text


def test_a_record_from_the_dead_session_survives_a_kill_of_the_stack(pl):
    """The end-to-end promise, with only ps and sysctl faked: klipper is SIGKILLed
    mid-print, the stack record is cleared or deleted, and the journal + verdict
    still name the byte."""
    write_state(pl)
    pl.powerloss.save_settings(journal=True)
    pl.powerloss._http_get = lambda path, timeout=3: stats_payload(position=12345)
    assert pl.powerloss.journal_tick() is not None
    pl.powerloss.note_session_start()
    pl.powerloss.note_forced_stop()          # process.kill_all()'s marker
    pl.paths.STATE.unlink()                   # what a reboot leaves nobody able to read
    verdict = pl.powerloss.last_stop(stack_up=False)
    assert verdict["state"] == "unclean"
    assert verdict["journal"]["file_position"] == 12345
    assert "12345" in "\n".join(pl.powerloss.resume_recipe())


def test_explain_mentions_the_upstream_truth(pl):
    """The lines a user reads must name what upstream does and does not have,
    because the default expectation ("Moonraker resumes it") is false here."""
    text = "\n".join(pl.powerloss.explain(query=False))
    for needle in ("history.py:250-265", "interrupted", "recover_at_seam",
                   "provider: none", "M26"):
        assert needle in text, needle


def test_moonraker_evidence_names_the_interrupted_rows(pl):
    """/server/history/list is upstream's only cross-restart signal; the row that
    says 'interrupted' is what a user gets, and it has no byte offset."""
    seen = []

    def _get(path, timeout=3):
        seen.append(path)
        return {"result": {"count": 2, "jobs": [
            {"job_id": "000009", "filename": "long-print.gcode",
             "status": "interrupted", "print_duration": 3600.0},
            {"job_id": "000010", "filename": "fine.gcode",
             "status": "completed"}]}}

    pl.powerloss._http_get = _get
    rows = pl.powerloss.interrupted_jobs()
    assert [row["filename"] for row in rows] == ["long-print.gcode"]
    assert seen[0].startswith("/server/history/list?limit=")
    lines = pl.powerloss.moonraker_lines(query=True)
    assert any("long-print.gcode" in line for line in lines)
    assert any("never position" in line for line in lines)
    # full=False is the doctor shape: one line, not the whole essay.
    assert len(pl.powerloss.moonraker_lines(query=False, full=False)) == 1
