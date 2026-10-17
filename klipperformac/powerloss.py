"""Power-loss slice: the truth about an unclean stop, plus a bounded print journal.

Upstream stays sacred (AGENTS.md 1). Nothing here reads or writes a Klipper or
Moonraker file: this module writes THREE files of our own under
``data/powerloss/`` and talks to Moonraker over its public localhost REST API,
exactly like `klipperformac gcode` and the battery guard do.

WHY IT IS OURS TO WRITE (every claim verified against the pinned checkouts on
this Mac, Klipper HEAD 461c4e3 / Moonraker v0.11.0, both `git status` clean):

  * Moonraker cannot see a restart here. Its only restart awareness is the
    [machine] provider registry (machine.py:111-123: none | systemd_cli |
    systemd_dbus | supervisord_cli), and OUR OWN configgen pins
    ``provider: none`` (klipperformac/configgen.py:47-50) because macOS has no
    systemd - the live log says "Using System Provider: none".
  * Moonraker cannot see what was printing either. job_state.py is 135 lines of
    pure RAM (last_print_stats at job_state.py:26, rewritten to "error" on
    klippy_disconnect at 35-40) and [job_queue] keeps queued_jobs in a dict, so
    a queued or active print does not survive a restart.
  * `recover_at_seam` / `auto_resume` DO NOT EXIST: 0 hits for either name over
    the whole Moonraker tree (docs included) and over Klipper's klippy/.
    Klipper keeps no state file (0 hits for state_file in klippy/*.py), and its
    only [virtual_sdcard] hook is on_error_gcode (virtual_sdcard.py:35, fired at
    292), which needs a klipper-driven shutdown: SIGKILL - or a dead 12V rail -
    never gives it the chance.
  * The ONE durable out-of-the-box signal is Moonraker's history startup sweep
    (history.py:250-265): a job row left at 'in_progress' in
    data/database/moonraker-sql.db is rewritten to 'interrupted' at the next
    startup. The row is INSERTed at print START (history.py:373-384), so it does
    survive kill -9 - but job_history has NO byte-offset column, so upstream can
    name the job and time it, never locate it.
  * The resume MECHANISM is plain upstream gcode and needs no patch: M23 selects
    the file (virtual_sdcard.py:180-188), M26 sets the byte offset (220-225,
    refused only while the work timer is live), M24 starts/resumes (214-216), the
    work handler seeks there (240-244, "Starting SD card print (position %d)")
    and M27 reports "SD printing byte pos/size" (226-232). So the OFFSET is the
    missing piece, and on this Mac the only place an offset is ever written down
    is the journal in this file.

That is the whole design: (1) an unclean-stop truth a human can act on, from a
marker a reboot cannot erase, and (2) a size-capped journal of the byte offset,
taken from the only read-only source that carries it
(``/printer/objects/query?print_stats&virtual_sdcard&toolhead`` - note there is
NO ``/printer/print_stats`` REST route: it is 404, the data is served by the
object query). Both are OFF unless the user turns them on
(``settings.json`` -> ``powerloss.journal``), and neither is auto-resume: this
tool journals and tells the truth, a human decides whether to print the seam.
"""
import json
import math
import os
import signal
import time
import urllib.parse
import urllib.request

from . import paths

# ---------------------------------------------------------------- settings --
# `journal` defaults to OFF: nothing that touches disk during a print is enabled
# behind the user's back (AGENTS.md 2/5), and the writes are only useful to
# people who want a seam to resume from.
DEFAULTS = {
    "journal": False,
    "intervalSec": 5.0,      # `powerloss watch`'s poll period
    "maxBytes": 1 << 20,     # journal size cap: rotate, never grow unbounded
    "keepRecords": 400,      # records kept when the cap is hit
}
MIN_INTERVAL = 1.0
MAX_INTERVAL = 3600.0
MIN_MAX_BYTES = 8192
MAX_MAX_BYTES = 16 << 20
MIN_KEEP = 10
MAX_KEEP = 5000
# The states worth a snapshot. `standby` (nothing printing) is never journalled:
# a journal of idle polls would fill the cap without ever helping a resume.
ACTIVE_STATES = ("printing", "paused")

STATS_QUERY = "/printer/objects/query?print_stats&virtual_sdcard&toolhead"

# A journaled snapshot may only read the marker as "stale" when it is clearly
# newer than the recorded stop; one poll's worth of clock skew is not evidence.
_STALE_SLACK = 2.0

SERVICE_KEYS = ("klipper", "moonraker", "web_mainsail", "web_fluidd",
                "caffeinate")


def settings():
    """Our settings block, coerced. Junk (a string, a bool where a number
  belongs, NaN) reads as the default, never as a crash or a 4 GB cap."""
    raw = paths.load_settings().get("powerloss")
    return normalise(raw if isinstance(raw, dict) else {})


def normalise(cfg):
    out = dict(DEFAULTS)
    out["journal"] = cfg.get("journal") is True
    out["intervalSec"] = _bounded(cfg, "intervalSec", MIN_INTERVAL, MAX_INTERVAL)
    out["maxBytes"] = int(_bounded(cfg, "maxBytes", MIN_MAX_BYTES,
                                   MAX_MAX_BYTES))
    out["keepRecords"] = int(_bounded(cfg, "keepRecords", MIN_KEEP, MAX_KEEP))
    return out


def _bounded(cfg, key, low, high):
    default = DEFAULTS[key]
    value = cfg.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if not math.isfinite(value):
        return default
    return min(high, max(low, float(value)))


def save_settings(**kwargs):
    """Merge our block into settings.json (paths.save_settings is
    write-then-replace, and the file is shared with the SwiftUI app). Returns
    the normalised block. Raises OSError, which the CLI turns into one line."""
    current = paths.load_settings()
    block = current.get("powerloss")
    merged = normalise(dict(block) if isinstance(block, dict) else {})
    merged.update({k: v for k, v in kwargs.items() if k in DEFAULTS})
    merged = normalise(merged)
    current["powerloss"] = merged
    paths.save_settings(current)
    return merged


def journal_enabled():
    return settings()["journal"]


# ------------------------------------------------------------------ files --
def folder():
    return paths.POWERLOSS


def journal_path():
    return paths.POWERLOSS / "journal.jsonl"


def session_path():
    """The OPEN-SESSION marker: "a stack session started and nothing has ever
    said it ended". Its presence with nothing running IS the unclean stop."""
    return paths.POWERLOSS / "session.json"


def stop_path():
    """The verdict of the most recently closed session, written by the graceful
    paths (`down`) and by `sweep()` when a crash/reboot is discovered."""
    return paths.POWERLOSS / "stop.json"


def _read_json(path):
    try:
        with open(path) as handle:
            data = json.load(handle)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _write_json(path, data):
    """tmp + replace, like every other file a running stack depends on: a torn
    marker is worse than an absent one, because a torn one reads as "no record".
    Returns True when the file is on disk and fsynced."""
    try:
        folder().mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w") as handle:
            json.dump(data, handle, indent=2, sort_keys=True,
                      allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except (OSError, ValueError, TypeError):
        try:
            path.with_name(path.name + ".tmp").unlink()
        except OSError:
            pass
        return False
    return True


def _unlink(path):
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _stamp():
    now = time.time()
    return now, time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))


def _process():
    """The process module on demand: process.py imports THIS module for its
    start/stop markers, so a top-level `from . import process` would be a cycle.
    Every read here still works when process is unavailable."""
    try:
        from . import process
        return process
    except Exception:
        return None


def _boot_time():
    """kern.boottime as recorded by our own reader (process._boot_time(): a
    `sysctl -n kern.boottime` subprocess, NOT psutil and not CLOCK_BOOTTIME, so
    it works whatever sitecustomize does to the interpreter). None when sysctl
    cannot answer, which the callers read as "cannot tell", never as "rebooted".
    Injectable by the tests via `boot_time`."""
    proc = _process()
    if proc is None:
        return None
    try:
        return proc._boot_time()
    except Exception:
        return None


def _stack_state():
    """process.running() - {service: bool} - or None when nothing is tracked."""
    proc = _process()
    if proc is None:
        return None
    try:
        return proc.running()
    except Exception:
        return None


# -------------------------------------------------- the unclean-stop truth --
def note_session_start():
    """Called by process.start() once the stack record is written.

    Rolls any session the last crash/reboot left open into its verdict FIRST
    (that is the moment an unclean stop becomes history), then opens this one.
    Never raises: `up` must not fail because a marker folder is read-only, and
    the missing marker is itself reported by `status`/`doctor`."""
    try:
        sweep()
    except Exception:
        pass
    epoch, iso = _stamp()
    record = {"started": iso, "started_epoch": epoch,
              "boot_time": _boot_time(), "app_home": str(paths.APP_HOME),
              "stack": _recorded_stack()}
    return _write_json(session_path(), record)


def _recorded_stack():
    """The pids state.json names, for the marker only (evidence of what a
    session consisted of; liveness is never inferred from it)."""
    try:
        with open(paths.STATE) as handle:
            state = json.load(handle)
    except Exception:
        return {}
    if not isinstance(state, dict):
        return {}
    return {name: state.get(name) for name in SERVICE_KEYS
            if isinstance(state.get(name), int)}


def note_clean_stop(reason="down", state=None):
    """Called by process.stop() with the liveness it read BEFORE signalling
    anything (`state` is that process.running() dict; None reads it here).

    "Graceful path ran" is not the same statement as "the session ended
    gracefully": if a service this session started is no longer alive when `down`
    runs, the session was already interrupted and the marker must say so, or a
    klipper that died an hour ago would be remembered as a clean stop. That is
    also why the caller hands the dict in - asking after the kills would find
    everything DOWN and turn every graceful `down` into a reported crash."""
    return _close_session(reason, clean_guess=True, state=state)


def note_forced_stop(reason="killall"):
    """Called by process.kill_all(): a SIGKILL sweep is not a graceful stop by
    definition, whatever is still alive, so the verdict is recorded as unclean
    with the user's own command as the reason."""
    return _close_session(reason, clean_guess=False)


def _close_session(reason, clean_guess, state=None):
    opened = _read_json(session_path()) or {}
    state = _stack_state() if state is None else state
    state = state if isinstance(state, dict) else {}
    tracked = opened.get("stack")
    tracked = tracked if isinstance(tracked, dict) else {}
    # A service this session started that is already gone means the session was
    # interrupted BEFORE the graceful path ran: `down` on a half-dead stack must
    # not remember a crashed klipper as a clean stop.
    dead = sorted(name for name, up in state.items()
                  if up is False and name in tracked)
    clean = bool(clean_guess) and not dead
    epoch, iso = _stamp()
    tail = last_record()
    verdict = {
        "clean": clean,
        "reason": reason if clean else (
            "{} ({} DOWN before the marker was written)".format(
                reason, ", ".join(dead)) if dead else reason),
        "forced": not clean_guess,
        "started": opened.get("started"),
        "started_epoch": opened.get("started_epoch"),
        "ended": iso,
        "ended_epoch": epoch,
        "boot_time": opened.get("boot_time") or _boot_time(),
        "stack": opened.get("stack") or {},
        "journal": _brief(tail),
    }
    wrote = _write_json(stop_path(), verdict)
    _unlink(session_path())
    return wrote


def _open_verdict(opened):
    """The verdict of an OPEN session marker, computed WITHOUT writing anything:
    what `status`/`doctor` read. `sweep()` persists exactly this shape."""
    started = opened.get("started_epoch")
    boot = opened.get("boot_time")
    now_boot = _boot_time()
    rebooted = bool(boot and now_boot and str(boot) != str(now_boot))
    epoch, iso = _stamp()
    return {"clean": False,
            "reason": "reboot with the stack running" if rebooted
                      else "no clean-stop marker",
            "forced": False, "rebooted": rebooted,
            "started": opened.get("started"), "started_epoch": started,
            "ended": iso, "ended_epoch": epoch, "boot_time": boot,
            "stack": opened.get("stack") or {},
            "journal": _brief(last_record(started_after=started))}


def sweep():
    """Persist `_open_verdict` as this install's verdict: called by start(), i.e.
    at the moment an interrupted session becomes history. A read-only command
    never calls it - `doctor`/`status` report history, they do not rewrite it.
    Returns the verdict dict, or None when nothing was open."""
    opened = _read_json(session_path())
    if opened is None:
        return None
    verdict = _open_verdict(opened)
    _write_json(stop_path(), verdict)
    return verdict


def last_stop(stack_up=False):
    """This install's verdict about the last stack session, or None when nothing
    has ever been recorded (a fresh install has no history to judge).

    `stack_up` is the CALLER's answer to "are klipper and moonraker alive right
    now" - `status`/`doctor` know that from process.running(), and this module
    deliberately does not read ps on its own. While a stack really is up, an
    open marker is only that session; it becomes evidence once nothing is up.

    Returns {"state": "running" | "clean" | "unclean", "clean", "reason",
             "started", "ended", "rebooted", "stale", "journal", "detail"}.
    """
    opened = _read_json(session_path())
    closed = _read_json(stop_path())
    if opened is not None:
        if stack_up:
            return {"state": "running", "clean": None, "reason": "running",
                    "started": opened.get("started"), "ended": None,
                    "rebooted": False, "stale": False, "journal": None,
                    "detail": "this session is still open"}
        closed_newer = closed is not None and not _is_older(closed, opened)
        return _describe(closed if closed_newer else _open_verdict(opened))
    if closed is None:
        return None
    if closed.get("clean") is not True:
        return _describe(closed)
    newest = _newest_journal_epoch()
    ended = closed.get("ended_epoch")
    if newest is not None and isinstance(ended, (int, float)) and \
            newest > ended + _STALE_SLACK:
        # The marker claims a graceful `down`, yet the journal holds snapshots
        # taken AFTER it: the marker cannot describe the session that actually
        # died, so it is stale and the stop counts as unclean.
        stale = dict(closed)
        stale.update({"clean": False, "stale": True,
                      "reason": "clean-stop marker is stale"})
        return _describe(stale)
    return _describe(closed)


def _is_older(closed, opened):
    """True when `closed` describes a session that ended BEFORE `opened` began
    (so the open marker is newer evidence than the recorded verdict)."""
    ended = closed.get("ended_epoch")
    started = opened.get("started_epoch")
    if not isinstance(ended, (int, float)) or not isinstance(started, (int, float)):
        return True
    return ended < started


def _describe(verdict):
    boot = verdict.get("boot_time")
    now_boot = _boot_time()
    rebooted = bool(verdict.get("rebooted") or
                    (boot and now_boot and str(boot) != str(now_boot)))
    state = "clean" if verdict.get("clean") is True else "unclean"
    reason = verdict.get("reason") or ("no clean-stop marker"
                                       if state == "unclean" else "down")
    if state == "clean":
        detail = "last stop was graceful (`{}`{})".format(
            reason, " at " + str(verdict.get("ended")) if verdict.get("ended")
            else "")
    else:
        detail = "no graceful `klipperformac down` closed the last session"
        if verdict.get("forced"):
            detail = "the last stack was SIGKILLed by `{}`".format(reason)
        elif rebooted:
            detail = "this Mac rebooted while the stack was running (power " \
                     "loss, forced restart, or a crash)"
        elif verdict.get("stale"):
            detail = "the clean-stop marker is stale: the journal holds " \
                     "snapshots taken after it was written"
    return {"state": state, "clean": verdict.get("clean"), "reason": reason,
            "forced": bool(verdict.get("forced")),
            "rebooted": rebooted, "stale": bool(verdict.get("stale")),
            "started": verdict.get("started"), "ended": verdict.get("ended"),
            "boot_time": boot, "journal": verdict.get("journal"),
            "detail": detail}


def verdict_lines(stack_up=False, indent="  "):
    """The unclean-stop truth as status/doctor lines. Silent when there is no
    record at all, which is what keeps `status` on a fresh install one word."""
    verdict = last_stop(stack_up)
    if verdict is None or verdict["state"] == "running":
        return []
    if verdict["state"] == "clean":
        return [indent + "powerloss    " + verdict["detail"]]
    lines = [indent + "[!] LAST STOP WAS UNCLEAN - " + verdict["detail"]]
    if verdict.get("started"):
        lines.append(indent + "    session began {} (boot {})".format(
            verdict["started"], verdict.get("boot_time") or "unknown"))
    journal = verdict.get("journal")
    if journal:
        lines.append(indent + "    last journaled position: {} at byte {} of {} "
                             "({})".format(journal.get("filename") or "?",
                                           journal.get("file_position"),
                                           journal.get("file_size"),
                                           journal.get("state")))
        lines.append(indent + "    resume it by hand: klipperformac powerloss "
                              "status   (M23/M26/M24, upstream gcode)")
    else:
        lines.append(indent + "    no journaled position (journal was off): "
                              "klipperformac powerloss on")
    return lines


# ---------------------------------------------------------------- journal --
def _num(value):
    """A finite real number, or None: JSON must stay strict (allow_nan=False)
    and a Moonraker field that is not a number is no evidence at all."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return value


def _int_or_none(value):
    number = _num(value)
    return None if number is None else int(number)


def _http_get(path, timeout=3):
    """One read-only GET on our own Moonraker; None when it does not answer.
    The offline tests replace this, so nothing in them can reach a socket."""
    url = "http://localhost:{}{}".format(paths.MOONRAKER_PORT, path)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode(errors="replace"))
    except Exception:
        return None


def _http_post(path, timeout=5):
    """A POST, used ONLY by `powerloss-test` (gated by KLIPPERFORMAC_ALLOW_LIVE):
    starting and cancelling the bench test print. Nothing else in this module
    writes to the printer."""
    url = "http://localhost:{}{}".format(paths.MOONRAKER_PORT, path)
    request = urllib.request.Request(url, method="POST", data=b"")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode(errors="replace"))
    except Exception:
        return None
    return body if isinstance(body, dict) and "result" in body else None


def query_stats(timeout=3):
    """print_stats + virtual_sdcard + toolhead as one snapshot, or None.

    Upstream serves these through the object query only - there is no
    /printer/print_stats REST route (404, verified live) - and virtual_sdcard is
    where the byte offset lives (file_position/file_size), which is the whole
    point of the journal."""
    answer = _http_get(STATS_QUERY, timeout)
    status = ((answer or {}).get("result") or {}).get("status")
    if not isinstance(status, dict):
        return None
    print_stats = status.get("print_stats")
    vsd = status.get("virtual_sdcard")
    toolhead = status.get("toolhead")
    print_stats = print_stats if isinstance(print_stats, dict) else {}
    vsd = vsd if isinstance(vsd, dict) else {}
    toolhead = toolhead if isinstance(toolhead, dict) else {}
    info = print_stats.get("info")
    info = info if isinstance(info, dict) else {}
    name = print_stats.get("filename")
    position = toolhead.get("position")
    return {
        "filename": name if isinstance(name, str) else "",
        "state": print_stats.get("state")
        if isinstance(print_stats.get("state"), str) else "",
        "file_position": _int_or_none(vsd.get("file_position")) or 0,
        "file_size": _int_or_none(vsd.get("file_size")) or 0,
        "progress": _num(vsd.get("progress")),
        "print_duration": _num(print_stats.get("print_duration")),
        "filament_used": _num(print_stats.get("filament_used")),
        "current_layer": _int_or_none(info.get("current_layer")),
        "total_layer": _int_or_none(info.get("total_layer")),
        "position": [p for p in position if _num(p) is not None]
        if isinstance(position, list) else None,
        "eventtime": _num((answer or {}).get("result", {}).get("eventtime")),
    }


def record_for(stats):
    """The journal record for one snapshot, or None when the snapshot says
    nothing worth journalling (no file, or the printer idle)."""
    if not isinstance(stats, dict):
        return None
    if not stats.get("filename") or stats.get("state") not in ACTIVE_STATES:
        return None
    epoch, iso = _stamp()
    return {"at": iso, "epoch": epoch, "boot_time": _boot_time(),
            "filename": stats["filename"], "state": stats["state"],
            "file_position": stats["file_position"],
            "file_size": stats["file_size"], "progress": stats["progress"],
            "print_duration": stats["print_duration"],
            "filament_used": stats["filament_used"],
            "current_layer": stats["current_layer"],
            "position": stats["position"]}


def append(record):
    """Append one JSONL line and fsync it before returning - a journal that a
    power cut can erase is not a journal. Returns the record, None on failure."""
    if not isinstance(record, dict):
        return None
    try:
        folder().mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True, allow_nan=False) + "\n"
        with open(journal_path(), "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    except (OSError, ValueError, TypeError):
        return None
    cap_size()
    return record


def journal_tick(force=False):
    """Poll Moonraker once and journal it if there is something to journal.

    `force` (what `powerloss snapshot` uses) writes even with the setting off: a
    record the user asked for by name is consent - one GET plus one line."""
    if not (force or journal_enabled()):
        return None
    return append(record_for(query_stats()))


def read_records(limit=None, since=None):
    """The journal as records, oldest first. A torn last line (the expected
    shape after kill -9 or a dead rail) is skipped, never fatal - and `since`
    keeps only records from the session that is being judged.

    "Record" means a snapshot: a line that parses but carries no seam (a `{}`, a
    foreign object, a hand-typed note) is dropped rather than returned, because
    every caller - rotation, the verdict, the recipe - reads one as a resume
    point, and a resume point without a filename and an offset is a lie."""
    try:
        raw = journal_path().read_text(errors="replace")
    except OSError:
        return []
    records = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue  # a torn tail is what a power cut leaves behind
        if not isinstance(record, dict) or not _is_snapshot(record):
            continue
        if since is not None and isinstance(_num(record.get("epoch")),
                                           (int, float)) and \
                record["epoch"] < since:
            continue
        records.append(record)
    return records[-limit:] if limit else records


def _is_snapshot(record):
    return bool(record.get("filename")) and _num(record.get("file_position")) \
        is not None


def last_record(started_after=None):
    records = read_records(since=started_after)
    return records[-1] if records else None


def _brief(record):
    """What a marker needs about the journal: enough to name the seam, not the
    whole file."""
    if not isinstance(record, dict):
        return None
    return {"filename": record.get("filename"),
            "file_position": record.get("file_position"),
            "file_size": record.get("file_size"),
            "state": record.get("state"), "at": record.get("at")}


def _newest_journal_epoch():
    record = last_record()
    return _num(record.get("epoch")) if record else None


def journal_size():
    try:
        return journal_path().stat().st_size
    except OSError:
        return 0


def cap_size(cfg=None):
    """Keep the file under its cap by rewriting the newest records atomically.
    Returns True when the file was rewritten."""
    cfg = settings() if cfg is None else cfg
    if journal_size() <= cfg["maxBytes"]:
        return False
    keep = read_records()[-cfg["keepRecords"]:]
    tmp = journal_path().with_name("journal.jsonl.tmp")
    try:
        folder().mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            for record in keep:
                handle.write(json.dumps(record, sort_keys=True,
                                        allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(journal_path())
    except (OSError, ValueError, TypeError):
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


def describe_record(record, index=None):
    parts = ["{}  {}".format(record.get("at", "?"), record.get("state", "?"))]
    if index is not None:
        parts.append("#{}".format(index))
    parts.append("{} @ byte {}{}".format(
        record.get("filename") or "?", record.get("file_position"),
        "/{}".format(record["file_size"]) if record.get("file_size") else ""))
    progress = _num(record.get("progress"))
    if progress is not None:
        parts.append("{:.1f}%".format(progress * 100.0))
    if record.get("current_layer"):
        parts.append("layer {}".format(record["current_layer"]))
    if record.get("position"):
        parts.append("xyz {}".format([round(float(v), 2)
                                      for v in record["position"][:3]]))
    return "  ".join(str(part) for part in parts)


def tail_lines(count=10):
    """The newest `count` snapshots, numbered by their position in the whole
    journal, so "record 412" in the tail is the same record everywhere else."""
    try:
        count = max(1, min(int(count), 500))
    except (TypeError, ValueError):
        count = 10
    records = read_records()
    shown = records[-count:]
    start = len(records) - len(shown) + 1
    return [describe_record(record, start + offset)
            for offset, record in enumerate(shown)]


def resume_recipe(record=None):
    """The upstream-standard way back to a journaled seam. No patch, no monkey
    patch: these are Klipper's own gcode commands (virtual_sdcard.py:180-232)."""
    record = last_record() if record is None else record
    if not isinstance(record, dict):
        return ["nothing journaled yet - with the journal on, the byte offset of "
                "every snapshot lands in {}".format(journal_path())]
    position = record.get("file_position")
    size = record.get("file_size")
    lines = [
        "resume recipe (upstream gcode, nothing patched):",
        "  M23 FILE={}".format(record.get("filename") or "<name.gcode>"),
        "  M26 S={}".format(position if position is not None else "<byte>"),
        "  M24",
        "  M27                       # reports 'SD printing byte <pos>/<size>'",
    ]
    notes = []
    if size:
        notes.append("  the offset is {} of {} bytes ({})".format(
            position, size, "{:.1f}%".format(100.0 * position / size)
            if _num(position) is not None and size else "?"))
    notes.append("  M26 is refused while the print's timer is live "
                 "(virtual_sdcard.py:220-225): the file must be selected and "
                 "NOT yet running, so this is a cold resume, not a mid-print "
                 "seek.")
    notes.append("  JUDGE THE SEAM BY EYE FIRST. Klipper kept no state file "
                 "(0 hits for state_file in klippy/*.py) and upstream has no "
                 "recover_at_seam, so a byte offset is a position in the "
                 "G-code, not a proven thermal/mechanical state: purge the "
                 "nozzle, check the layer, expect a scar. This tool never "
                 "auto-resumes.")
    return lines + ["  # " + note for note in notes]


# --------------------------------------------- what Moonraker really offers --
def interrupted_jobs(limit=25):
    """Rows Moonraker's own startup sweep marked 'interrupted' (or left at
    'in_progress'): history.py:250-265 flips them at the next startup. Filename
    and elapsed time only - job_history has no byte-offset column."""
    answer = _http_get("/server/history/list?limit={}".format(
        max(1, min(int(limit) if isinstance(limit, (int, float)) else 25, 200))))
    jobs = ((answer or {}).get("result") or {}).get("jobs")
    if not isinstance(jobs, list):
        return []
    return [job for job in jobs if isinstance(job, dict) and
            job.get("status") in ("interrupted", "in_progress")]


def moonraker_lines(query=True, full=False, limit=25):
    """What upstream actually offers on THIS Mac, in its own words being the
    citations. `query` (off in the offline tests and on a stopped stack) decides
    whether the interrupted rows are looked up live."""
    out = []
    rows = interrupted_jobs(limit) if query else []
    if rows:
        out.append("moonraker's own history marks {} job(s) interrupted: {}. "
                   "That is upstream's ONLY cross-restart signal - filename and "
                   "elapsed time, never position."
                   .format(len(rows), ", ".join(
                       str(row.get("filename") or row.get("job_id"))[:40]
                       for row in rows[:3])))
    else:
        out.append("moonraker's only durable power-loss signal is history's "
                   "startup sweep (history.py:250-265): a job left at "
                   "'in_progress' becomes 'interrupted' at the next start. It "
                   "names the file and its elapsed time, never the byte offset.")
    if query:
        out.append("our marker is the one thing that survives a reboot here: "
                   "state.json lives in run/ and process._load() deletes it when "
                   "kern.boottime moves (process.py:110-112), which is why this "
                   "journal is in data/powerloss/.")
    if full:
        out.append("moonraker has no restart awareness on macOS: our configgen "
                   "pins [machine] provider: none (configgen.py:47-50) because "
                   "there is no systemd/supervisord here, and job_state.py + "
                   "[job_queue] are RAM-only, so neither a restart nor a queued "
                   "print is known to it after the fact.")
        out.append("moonraker's own unsafe_shutdowns counter (database.py:274-280, "
                   "written back by database.close()) is NOT readable over REST: "
                   "the `database` namespace answers 403 'Read/Write access to "
                   "namespace database is forbidden' (verified live), so no "
                   "command here depends on it.")
        out.append("klipper keeps no state file and its only [virtual_sdcard] "
                   "hook is on_error_gcode (virtual_sdcard.py:35, fired at 292), "
                   "which needs a klipper-driven shutdown - SIGKILL or a dead "
                   "12V rail never runs it. `recover_at_seam`/`auto_resume` do "
                   "not exist upstream (0 hits in either tree).")
        out.append("the resume mechanism itself is plain upstream gcode: M23 "
                   "selects, M26 sets the byte offset (virtual_sdcard.py:220-225), "
                   "M24 starts there, M27 reports pos/size - see "
                   "`klipperformac powerloss status` for the recipe from the "
                   "last journaled seam.")
    return out


def explain(stack_up=False, query=True):
    """Everything `klipperformac powerloss status` says. Read-only apart from one
    size check: it never writes a marker, so running it cannot rewrite history."""
    cfg = settings()
    lines = ["power loss: journal is {} ({} records, {} bytes, cap {}), "
             "markers in {}".format(
                 "ON" if cfg["journal"] else "off",
                 len(read_records()), journal_size(), cfg["maxBytes"], folder())]
    lines.extend(verdict_lines(stack_up))
    lines.extend("  - " + line for line in
                 moonraker_lines(query=query, full=True))
    records = read_records(limit=5)
    if records:
        lines.append("last journaled snapshots:")
        for record in records:
            lines.append("  " + describe_record(record))
    lines.extend(resume_recipe())
    return lines


def watch(interval=None, sweeps=None, log=print):
    """Poll and journal until the stack stops answering (or `sweeps` runs out).

    Run in the foreground, or backgrounded by the user the way `batteryguard run`
    is: `up` never starts it, because a resident disk writer nobody asked for is
    exactly what AGENTS.md 5 refuses - and journaling is only as continuous as
    this loop being alive, which `powerloss status` says out loud. Returns the
    number of snapshots written."""
    cfg = settings()
    if not cfg["journal"]:
        log("[!] the journal is off: klipperformac powerloss on")
        return 0
    try:
        tick = float(interval) if interval is not None else cfg["intervalSec"]
    except (TypeError, ValueError):
        tick = cfg["intervalSec"]
    tick = max(MIN_INTERVAL, min(tick, MAX_INTERVAL))
    remaining = None if sweeps is None else max(1, int(sweeps))
    written = 0
    while True:
        stats = query_stats()
        if stats is None:
            log("  powerloss: moonraker did not answer - the journal loop ends")
            break
        record = append(record_for(stats))
        if record is not None:
            written += 1
            log("  powerloss: {} -> byte {}/{}".format(
                record["filename"], record["file_position"],
                record["file_size"]))
        if remaining is not None:
            remaining -= 1
            if remaining <= 0:
                break
        time.sleep(tick)
    return written


# ------------------------------------------------------------------ doctor --
def writable():
    """Can the marker folder be written? Judged without creating a file, so a
    read-only data folder is reported by `status`/`doctor` and not repaired by
    side effect. A non-folder sitting ON the path counts as not writable: that is
    the failure mode where every append silently returns None."""
    target = folder()
    if target.exists() and not target.is_dir():
        return False
    if target.is_dir():
        return os.access(str(target), os.W_OK)
    return paths.DATA.is_dir() and os.access(str(paths.DATA), os.W_OK)


def doctor(query=True, stack_up=False, cfg=None):
    """[(ok, msg)] for `klipperformac doctor`. Never raises.

    Advisory in the CLI (it must not flip doctor's exit code): a Mac whose last
    session died badly is not a broken INSTALL, and `make release-check` gates on
    that code - same reasoning as the backup lines. The [!] tag is still the
    honest signal the user acts on."""
    out = []
    cfg = settings() if cfg is None else cfg
    if cfg["journal"]:
        if writable():
            out.append((True, "powerloss journal: ON ({} records, {} bytes, cap "
                              "{}, poll {}s) - the byte offset upstream has no "
                              "column for, in {}".format(
                                  len(read_records()), journal_size(),
                                  cfg["maxBytes"], cfg["intervalSec"],
                                  journal_path())))
        else:
            out.append((False, "powerloss journal: ON but {} cannot be written - "
                               "nothing is being recorded".format(folder())))
    else:
        out.append((True, "powerloss journal: off (`klipperformac powerloss on` "
                          "records the print's byte offset so a power loss has "
                          "a seam to read)"))
    verdict = last_stop(stack_up)
    if verdict is None:
        out.append((True, "powerloss: no session recorded yet (nothing to say "
                          "about the last stop)"))
    elif verdict["state"] == "running":
        out.append((True, "powerloss: this session is open and will be judged "
                          "by the next `up`/`status`"))
    elif verdict["state"] == "clean":
        out.append((True, "powerloss " + verdict["detail"]))
    else:
        out.append((False, "LAST STOP WAS UNCLEAN - " + verdict["detail"]))
        if verdict.get("journal"):
            out.append((True, "powerloss: last journaled seam is {} at byte {}"
                              .format(verdict["journal"].get("filename"),
                                      verdict["journal"].get("file_position"))))
        out.append((True, "powerloss: upstream offers no automatic help here - "
                          "see `klipperformac powerloss status`"))
    # ...and what upstream DOES offer, in one line, because the honest answer to
    # "did the power loss lose my print" has to name the source: Moonraker's
    # history sweep (filename + elapsed time, no position). The GET behind it is
    # only made when a stack is answering and `query` is on.
    try:
        lines = moonraker_lines(query=query and stack_up, full=False)
        if lines:
            out.append((True, "powerloss: " + lines[0]))
    except Exception as e:
        out.append((True, "powerloss: upstream evidence not read ({})".format(e)))
    return out


# ------------------------------------------------- the kill-sequence machine --
# `klipperformac powerloss-test` is the ONLY thing here that kills a process or
# POSTs to the printer, and it refuses to run unless KLIPPERFORMAC_ALLOW_LIVE=1
# says the bench board is meant to be cut off mid-print. The SEQUENCE below is
# pure: every contact with the machine goes through a harness object, so the
# offline suite drives the identical sequence against recorders
# (tests/powerloss/test_kill_sequence_machine.py) and the integrator drives it
# against the SKR Pico with the same assertions.
TEST_GCODE_NAME = "klipperformac-powerloss-test.gcode"
TEST_GCODE_DWELLS = 240          # 240 x G4 P0.5 ~= two minutes of dwelling
TEST_GCODE_HEADER = (
    "; Klipper-for-Mac power-loss test print\n"
    "; G4 ONLY: no motion, no extrusion, no heaters, no homing. The bench board\n"
    "; dwells, so a print this command cuts off cannot damage anything - and\n"
    "; virtual_sdcard still advances file_position, which is the whole point.\n"
    "G90\n")


def test_gcode_text():
    return TEST_GCODE_HEADER + "G4 P0.5\n" * TEST_GCODE_DWELLS


class KillTestHarness:
    """The machine-facing half of `powerloss-test`. Each method is one contact;
    the offline fake implements all of them with counters and scripted states."""

    def enable_journal(self):
        raise NotImplementedError

    def write_test_gcode(self):
        raise NotImplementedError

    def start_print(self, name):
        raise NotImplementedError

    def wait_printing(self, timeout=40.0):
        raise NotImplementedError

    def journal_tick(self):
        raise NotImplementedError

    def pause(self, seconds):
        raise NotImplementedError

    def journal_records(self):
        raise NotImplementedError

    def stack_pids(self):
        raise NotImplementedError

    def kill(self, pid):
        raise NotImplementedError

    def alive(self, pid):
        raise NotImplementedError

    def wait_dead(self, pid, timeout=20.0):
        raise NotImplementedError

    def last_stop(self, stack_up=False):
        raise NotImplementedError

    def bring_up(self):
        raise NotImplementedError

    def stop_stack(self):
        raise NotImplementedError

    def klippy_state(self, timeout=40.0):
        raise NotImplementedError

    def services_up(self):
        raise NotImplementedError

    def orphan_pids(self):
        raise NotImplementedError

    def port_conflicts(self):
        raise NotImplementedError

    def print_state(self):
        raise NotImplementedError


def run_kill_sequence(harness, log=print, ticks=3, timeout=40.0):
    """Kill klipper, then Moonraker, mid-print; prove what survives.

    Returns (ok, [(name, ok, detail)]). Every step is a check on the record, not
    a comment: the journal must outlive both SIGKILLs, the marker must call the
    death unclean and the clean stop clean, the reconnect must reach `ready`, and
    the machine must be left with no orphans, no held ports and a healthy stack.
    A print killed this way is NOT resumed by anything here (upstream has no
    recover_at_seam), which is why the journal's offsets are the assertion.
    """
    checks = []

    def check(name, ok, detail=""):
        verdict = bool(ok)
        checks.append((name, verdict, detail))
        log("  [{}] {}{}".format("ok" if verdict else "FAIL", name,
                                 "  ({})".format(detail) if detail else ""))
        return verdict

    def bail(name):
        log("[!] {} - the sequence cannot continue safely".format(name))
        return False, checks

    cfg = harness.enable_journal()
    if not check("journal switched on for the run", cfg.get("journal")):
        return bail("journal off")
    name = harness.write_test_gcode()
    if not check("G4-only test print written to gcodes/", name):
        return bail("test print not written")
    if not check("test print started over REST", harness.start_print(name)):
        return bail("print would not start")
    stats = harness.wait_printing(timeout)
    if not check("klipper is dwelling on the test print",
                 stats and stats.get("state") == "printing",
                 "byte {}".format(stats.get("file_position") if stats else "-")):
        return bail("print never reached `printing`")

    offsets = []
    for _ in range(max(1, int(ticks))):
        record = harness.journal_tick()
        if record is not None:
            offsets.append(record.get("file_position"))
        harness.pause(3.0)
    advancing = len(offsets) >= 2 and offsets[-1] > offsets[0] and all(
        later >= earlier for earlier, later in
        zip(offsets, offsets[1:], strict=False))  # pairs, not lengths
    if not check("journal recorded advancing byte offsets", advancing,
                 str(offsets)):
        return bail("journal is not recording the seam")

    before = harness.journal_records()
    pids = harness.stack_pids()
    klipper = pids.get("klipper")
    moonraker = pids.get("moonraker")
    if not check("klipper and moonraker pids are recorded",
                 klipper and moonraker, "klipper {} moonraker {}".format(
                     klipper, moonraker)):
        return bail("no pids to kill")
    # The precondition that makes the rest mean anything: `up` opened a session
    # marker. Asked with the stack up it must read "running" (not "unclean", not
    # "no verdict") - and that same file is what the kill below turns into
    # evidence, which is exactly the behaviour `powerloss-test` is for.
    open_session = harness.last_stop(stack_up=True)
    if not check("this session is tracked by an open marker",
                 open_session and open_session["state"] == "running",
                 open_session.get("state") if open_session else "no verdict"):
        return bail("no open session marker: the stack was not started by `up`")

    harness.kill(klipper)
    check("SIGKILL took klipper down", harness.wait_dead(klipper, timeout))
    check("moonraker survived the klipper kill", harness.alive(moonraker))
    verdict = harness.last_stop(stack_up=False)
    check("unclean stop detected after SIGKILL klipper",
          verdict and verdict["state"] == "unclean",
          verdict.get("detail") if verdict else "no verdict")
    check("nothing journalled after the kill",
          len(harness.journal_records()) == len(before))
    check("stack rebuilt after the klipper kill", harness.bring_up())
    ready = harness.klippy_state(timeout)
    check("klipper reconnects to ready", ready == "ready", str(ready))
    after = harness.journal_records()
    check("journal survived SIGKILL klipper",
          len(after) >= len(before) and after[-1] == before[-1],
          "{} records".format(len(after)))
    check("every record is a parseable seam",
          all(isinstance(record.get("file_position"), int) for record in after))

    if not check("test print restarted for the moonraker kill",
                 harness.start_print(name)):
        return bail("second print would not start")
    stats = harness.wait_printing(timeout)
    if not check("klipper is dwelling on the print again",
                 stats and stats.get("state") == "printing",
                 "state {}".format(stats.get("state") if stats else "-")):
        return bail("the second print never reached `printing`")
    harness.pause(3.0)
    harness.journal_tick()
    before = harness.journal_records()
    moonraker = harness.stack_pids().get("moonraker")
    klipper = harness.stack_pids().get("klipper")
    harness.kill(moonraker)
    check("SIGKILL took moonraker down", harness.wait_dead(moonraker, timeout))
    check("klipper outlived moonraker (klippy is not its child)",
          harness.alive(klipper))
    verdict = harness.last_stop(stack_up=False)
    check("the unclean verdict is still the live one",
          verdict and verdict["state"] == "unclean")
    check("stack rebuilt after the moonraker kill", harness.bring_up())
    ready = harness.klippy_state(timeout)
    check("klipper reconnects after the moonraker kill", ready == "ready",
          str(ready))
    check("journal survived SIGKILL moonraker",
          len(harness.journal_records()) >= len(before))

    check("graceful down ran", harness.stop_stack())
    verdict = harness.last_stop(stack_up=False)
    check("clean-stop marker written by down",
          verdict and verdict["state"] == "clean",
          verdict.get("reason") if verdict else "no verdict")
    up = harness.bring_up()
    running = harness.services_up() or {}
    check("stack healthy at the end",
          up and all(running.get(service) for service in SERVICE_KEYS),
          str(sorted(service for service, alive in running.items() if not alive)))
    ready = harness.klippy_state(timeout)
    check("klippy ready at the end", ready == "ready", str(ready))
    orphans = harness.orphan_pids()
    check("no orphan service processes", not orphans, str(orphans))
    conflicts = harness.port_conflicts()
    check("no foreign process holds a stack port", not conflicts, str(conflicts))
    state = harness.print_state()
    check("nothing printing at the end", state in ("standby", "complete",
                                                  "cancelled", "error"),
          str(state))
    return all(item[1] for item in checks), checks


class LiveHarness(KillTestHarness):
    """`powerloss-test` on this Mac: our own process module plus localhost REST.

    Deliberately no new mechanism: the pids come from the same state.json
    `status` reads, the kill goes through process._signal() (which range-checks
    the pid the same way `down` does), and the print is started/cancelled with
    Moonraker's own documented endpoints."""

    def __init__(self, log=print):
        self.log = log
        self.proc = _process()
        if self.proc is None:
            raise RuntimeError("klipperformac.process is not importable")

    def _state(self):
        try:
            return self.proc._load() or {}
        except Exception:
            return {}

    def enable_journal(self):
        return save_settings(journal=True)

    def write_test_gcode(self):
        folder = paths.DATA / "gcodes"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / TEST_GCODE_NAME).write_text(test_gcode_text())
        return TEST_GCODE_NAME

    def start_print(self, name):
        return _http_post("/printer/print/start?filename="
                          + urllib.parse.quote(name)) is not None

    def cancel_print(self):
        return _http_post("/printer/print/cancel") is not None

    def print_state(self):
        stats = query_stats()
        return stats.get("state") if stats else None

    def wait_printing(self, timeout=40.0):
        deadline = time.time() + timeout
        stats = None
        while time.time() < deadline:
            stats = query_stats()
            if stats and stats.get("state") == "printing":
                return stats
            time.sleep(0.5)
        return stats

    def journal_tick(self):
        return journal_tick()

    def pause(self, seconds):
        time.sleep(max(0.0, float(seconds)))

    def journal_records(self):
        return read_records()

    def stack_pids(self):
        state = self._state()
        return {name: state.get(name) for name in SERVICE_KEYS}

    def kill(self, pid):
        return self.proc._signal(pid, signal.SIGKILL)

    def alive(self, pid):
        try:
            return bool(self.proc._live_cmds([pid]))
        except Exception:
            return False

    def wait_dead(self, pid, timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.alive(pid):
                return True
            time.sleep(0.3)
        return not self.alive(pid)

    def last_stop(self, stack_up=False):
        return last_stop(stack_up)

    def bring_up(self):
        """Whatever is left of the stack goes down first (stop() records the
        marker honestly: a service that was already dead keeps the session
        unclean), then a fresh stack starts and is written down."""
        self.stop_stack()
        return self.proc.start()

    def stop_stack(self):
        return self.proc.stop()

    def klippy_state(self, timeout=40.0):
        deadline = time.time() + timeout
        state = None
        while time.time() < deadline:
            answer = _http_get("/printer/info", 2)
            state = ((answer or {}).get("result") or {}).get("state")
            if state == "ready":
                return state
            time.sleep(0.7)
        return state

    def services_up(self):
        return self.proc.running() or {}

    def orphan_pids(self):
        tracked = set(self._state().values())
        try:
            return [pid for pid in self.proc._scan_service_pids()
                    if pid not in tracked]
        except Exception:
            return []

    def port_conflicts(self):
        try:
            return self.proc.port_conflicts()
        except Exception:
            return []
