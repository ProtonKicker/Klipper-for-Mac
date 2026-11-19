"""Offline harness for the cam record/audio slice (klipperformac/cam.py).

Nothing is ever launched and no socket is ever opened. The helper is a file in a
throwaway home and every one of its answers is canned exactly the way
Sources/KlipperCam/*.swift promises it:

  * `--help`, `--list-mics`, `--check-mic-permission` (its read-only questions,
    asked through subprocess.run) are answered from strings - and an OLD helper
    refuses an option it does not advertise, the way the real one exits 1 on it;
  * a spawn records its argv and lives out the contract: its stdout lines land
    in the camera log at the byte offset the real spawn would use, a `--record`
    run leaves a file at the path it was handed, SIGTERM (or its own --seconds
    timer) produces the `Recorded ...` close line, and its exit code is the
    contract's - 0 a complete file, 3 no file at all, 1 refused;
  * its loopback `?action=recordstatus` body is canned key=value.

The clock is a harness object: a bounded wait finishes without burning wall
clock, and a wait that is NOT bounded never would. No test here starts a
process, opens a port, or touches a camera.
"""
import io
import json
import re
import signal
import subprocess
import time
import urllib.error
from pathlib import Path

import pytest

BOOT = "1700000000"
PID = 4242
PORT = 8082
GOOD_CMD = "/home/bin/KlipperCam --stream --port 8082"
START_NOW = 1760000000.0

# The two helper builds and their own --help. The old one has no video at all;
# this is the capability difference every test here turns on.
OLD_HELP = """KlipperCam - show your Mac camera inside your own dashboard.

  KlipperCam --stream [--port 8082] [--width 1280] [--height 720]
                     [--fps 15] [--quality 80] [--device NAME_OR_INT]
  KlipperCam --list
  KlipperCam --snapshot PATH [--timeout 10]
  KlipperCam --check-permission
  KlipperCam --help
"""
NEW_HELP = """KlipperCam - show your Mac camera inside your own printer dashboard.

  KlipperCam --stream [--port 8082] [--width 1280] [--height 720]
                     [--fps 15] [--quality 80] [--device NAME_OR_INT]
  KlipperCam --record PATH [--seconds N] [--mic] [--mic NAME]
      http://127.0.0.1:PORT/?action=recordstatus
  KlipperCam --list
  KlipperCam --list-mics
  KlipperCam --snapshot PATH [--timeout 10]
  KlipperCam --check-permission
  KlipperCam --check-mic-permission
  KlipperCam --help
"""
MICS = "0\tMacBook Pro Microphone\n1\tUSB Condenser\n"
# --check-mic-permission: exactly one word, exit 0/2/3/4, never a prompt.
MIC_EXITS = {"authorized": 0, "denied": 2, "restricted": 3,
             "notDetermined": 4}
UNKNOWN_OPTION = "I don't know the option {}. Run: KlipperCam --help"

IDLE_BODY = "state=idle\nrecord=not-requested\nmic=notDetermined"
# The degraded paths, in the helper's own words (its stderr, "KlipperCam: ...").
NO_SOUND = ("KlipperCam: No sound: Microphone access is switched off for "
            "KlipperCam. Recording the picture only.")
UNANSWERED = ("KlipperCam: The microphone permission question went unanswered "
              "for 30 seconds. Recording the picture only.")
NOPICTURE = ("KlipperCam: Nothing was recorded: the camera sent no picture, "
             "so no file was left behind.")


def body(state="recording", path="/v.mp4", secs="8.0", elapsed="8.1",
         bytez="1048576", audio="off", mic="authorized", error=None):
    """One recordstatus answer as MjpegServer.recordStatus() writes it: the
    key=value line, then `file=`, then `mic=`."""
    line = "state={} seconds={} elapsed={} bytes={} audio={}".format(
        state, secs, elapsed, bytez, audio)
    if error:
        line += " error=" + error
    return "{}\nfile={}\nmic={}".format(line, path, mic)


def opened(path, sound=False):
    """The helper's open line (stdout)."""
    return "Recording to {} ({})".format(
        path, "with sound" if sound else "picture only")


def closed(path, secs=4.0, bytez=4096, sound="off"):
    """The helper's close line (stdout) - the recording measured by the thing
    that wrote it."""
    return "Recorded {} ({} s, {} bytes, sound {})".format(
        path, "{:.1f}".format(float(secs)), int(bytez), sound)


CAMERA_READY = "Camera ready: built-in 1280x720 at 15 fps."
SERVING = "KlipperCam is serving built-in on http://127.0.0.1:8082/?action=stream"
MIC_READY = ("Microphone ready: USB Condenser. Sound goes into the recording "
             "only, never into the stream.")


class NoExec(BaseException):
    """Raised instead of letting this battery start a real process."""


class FakeProc:
    pid = PID


class FakeTime:
    """A clock that only moves when the code under test knocks (or sleeps)."""

    def __init__(self, start=START_NOW):
        self.now = float(start)
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += float(seconds)
        self.slept.append(float(seconds))

    def __getattr__(self, name):
        return getattr(time, name)      # strftime/mktime/strptime/localtime...


class HelperCommands:
    """Stands in for cam.subprocess: the helper's read-only questions answered
    from strings, every argv recorded, and Popen refused outright.

    The answers follow the helper's own rules: an option it does not advertise
    in --help is answered the way the real build answers it (exit 1 plus "I don't
    know the option"), so a test that hands an old helper a flag it never
    offered sees the failure the user would."""

    DEVNULL = getattr(subprocess, "DEVNULL", -3)
    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, help_text=OLD_HELP):
        self.help_text = help_text
        self.runs = []
        self.mics = MICS
        self.mic_word = "notDetermined"
        self.cam_code = 0

    def _answer(self, code, out=""):
        return type("Answer", (), {"returncode": code, "stdout": out.encode(),
                                   "stderr": b""})()

    def run(self, cmd, *a, **kw):
        argv = [str(c) for c in cmd]
        self.runs.append(argv)
        flags = set(re.findall(r"(--[a-z][a-z-]+)", self.help_text))
        for flag in ("--record", "--seconds", "--mic", "--list-mics",
                     "--check-mic-permission"):
            if flag in argv and flag not in flags:
                return self._answer(1, UNKNOWN_OPTION.format(flag))
        if "--help" in argv:
            return self._answer(0, self.help_text)
        if "--list-mics" in argv:
            return self._answer(0, self.mics)
        if "--check-mic-permission" in argv:
            return self._answer(MIC_EXITS[self.mic_word],
                                self.mic_word + "\n")
        if "--check-permission" in argv:
            return self._answer(self.cam_code, "authorized\n")
        if argv[0].endswith("KlipperCam"):
            raise AssertionError(
                "this battery may only ask the helper its read-only "
                "questions, not: " + repr(argv))
        return self._answer(0, "")      # `ps -eo pid=,command=` and friends

    def Popen(self, cmd, *a, **kw):
        raise NoExec("subprocess.Popen blocked: " + repr(cmd))

    def asked(self, flag):
        """Every recorded argv that carried this flag."""
        return [run for run in self.runs if flag in run]


class Reply:
    """What urllib.request.urlopen hands back (context managed, read once)."""

    def __init__(self, code, body):
        self._code = code
        self._data = body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        return self._data if n is None or n < 0 else self._data[:n]

    def getcode(self):
        return self._code


def live_cmds(rig):
    """`ps` output for the shared pid helpers: the camera is either there or
    not, and this module decides which."""
    def _cmds(pids):
        out = {}
        if not rig.live:
            return out
        for candidate in pids:
            try:
                key = int(candidate)
            except (TypeError, ValueError):
                continue
            if key == PID:
                out[str(key)] = GOOD_CMD
        return out
    return _cmds


class Rig:
    """The camera world a test needs: helper, fake spawn, canned answers."""

    def __init__(self, stack, monkeypatch):
        self.stack = stack
        self.cam = stack.cam
        self.paths = stack.paths
        self.monkeypatch = monkeypatch
        self.cmd = HelperCommands()
        self.clock = FakeTime()
        self.popens = []
        self.urls = []
        self.replies = []
        self.helper_lines = []       # extra stdout/stderr for the next spawn
        self.plan = None             # what the next --record spawn does
        self.recording = None        # a file the live spawn is writing
        self.exit_code = None        # canned exit code of the spawned helper
        self.alive = True            # cam._alive: is the process up
        self.exit_at = None          # a --seconds run dies at this clock value
        self.live = True             # is it in `ps` (so state files are trusted)
        self.stubborn = False        # True: no signal at all takes it down
        self.ignores_sigterm = False  # True: SIGTERM bounces, SIGKILL lands
        self.hangs = False           # True: a --seconds run misses its deadline
        self.signals = []
        monkeypatch.setattr(stack.cam, "subprocess", self.cmd)
        monkeypatch.setattr(stack.cam, "time", self.clock)
        monkeypatch.setattr(stack.cam, "GUI_BUNDLES", ())
        monkeypatch.delenv("KLIPPERFORMAC_CAM_BIN", raising=False)
        monkeypatch.delenv("KLIPPERFORMAC_REPO_DIR", raising=False)
        monkeypatch.setattr(stack.cam, "_process", lambda: stack.process)
        monkeypatch.setattr(stack.cam, "_port_free", lambda port: True)
        monkeypatch.setattr(stack.cam, "permission",
                            lambda: ("authorized", True))
        monkeypatch.setattr(stack.cam, "_post_webcam", lambda payload: True)
        monkeypatch.setattr(stack.cam, "_alive", lambda pid: self.up())
        monkeypatch.setattr(stack.cam, "_sleep", self.clock.sleep)
        monkeypatch.setattr(stack.cam, "_wait_exit", self._wait_exit)
        monkeypatch.setattr(stack.cam, "_popen", self._popen)
        monkeypatch.setattr(stack.cam, "_signal", self._signal)
        monkeypatch.setattr(stack.cam, "_reap", lambda pid: None)
        monkeypatch.setattr(stack.process, "_boot_time", lambda: BOOT)
        monkeypatch.setattr(stack.process, "_live_cmds", live_cmds(self))
        monkeypatch.setattr(stack.cam.urllib.request, "urlopen", self._urlopen)
        stack.paths.RUN.mkdir(parents=True, exist_ok=True)

    # -- the helper binary -------------------------------------------------
    def install_helper(self, video=True, mic=None):
        """Put a helper in the throwaway home and give it a --help. `mic` is the
        word its read-only --check-mic-permission answers."""
        helper = self.paths.CAM_BIN
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_bytes(b"MACHO" if video else b"OLDMACHO")
        self.cmd.help_text = NEW_HELP if video else OLD_HELP
        if mic is not None:
            self.cmd.mic_word = mic
        return helper

    def helper_says(self, *lines):
        """Extra stdout/stderr lines for the next spawn, verbatim."""
        self.helper_lines = list(lines)
        return self

    def recording_finishes(self, size=4096, secs=4.0, sound=None, note=None):
        """The next `--record` spawn writes `size` bytes and closes the file
        itself (the --seconds case): the close line and exit 0 arrive at its own
        deadline, which this fake reads out of the argv it was handed."""
        self.plan = {"size": int(size), "secs": float(secs), "sound": sound,
                     "note": note}
        return self

    def recording_is_open(self, size=4096, secs=4.0, sound=None, note=None):
        """The next `--record` spawn starts a file and keeps it open until it is
        stopped: the close line and exit 0 arrive on SIGTERM. This is also what
        happens when a test says nothing, so most tests can leave it out."""
        self.plan = {"size": int(size), "secs": float(secs), "sound": sound,
                     "note": note}
        return self

    def recording_fails(self, exit_code=3, note=NOPICTURE):
        """The next `--record` spawn produces NOTHING: exit 3 and no file, which
        is the helper's promise about a movie it could not finish (it deletes it
        instead of leaving a broken one)."""
        self.plan = {"size": None, "note": note, "exit_code": exit_code}
        return self

    def _wait_exit(self, pid, seconds):
        """The contract's exit code, once the helper is gone - and never a code
        invented for a process that is still running."""
        return None if self.up() else self.exit_code

    def up(self):
        """Is the helper still alive on the harness clock? A `--seconds` run dies
        by itself at its deadline, and its last words (the close line) are
        written as it goes."""
        if self.exit_at is not None and self.clock.now >= self.exit_at:
            self.exit_at = None
            self.alive = False
            self.close_recording()
            return False
        return self.alive

    def _signal(self, pid, sig):
        self.signals.append((int(pid), int(sig)))
        if int(sig) == signal.SIGTERM:
            if self.stubborn or self.ignores_sigterm:
                return          # it will not take the hint, so no close line
            self.alive = False
            self.close_recording()
        elif int(sig) == signal.SIGKILL:
            if self.stubborn:
                return
            # SIGKILL cannot clean up after itself: the process goes, no close
            # line is ever written, and the bytes on disk are not a movie. That
            # is the helper's contract, not a gap in this fake.
            self.alive = False

    def _popen(self, cmd, **kw):
        argv = [str(c) for c in cmd]
        self.popens.append(argv)
        # a fresh process: alive, no exit code yet, no timer of its own
        self.alive, self.exit_code, self.exit_at = True, None, None
        dest = argv[argv.index("--record") + 1] if "--record" in argv else None
        plan = self.plan or {}
        self.plan = None
        asked_sound = "--mic" in argv
        sound = plan.get("sound") or ("on" if asked_sound else "off")
        lines = [opened(dest, asked_sound)] if dest else [SERVING]
        if plan.get("note"):
            lines.append(plan["note"])      # a degraded path, in its own words
        elif asked_sound and sound == "off":
            lines.append(NO_SOUND)
        lines += list(self.helper_lines)
        self.helper_lines = []
        lines.append(CAMERA_READY)
        if dest and asked_sound and sound == "on":
            lines.append(MIC_READY)
        if dest:
            self._make_file(dest, plan, sound)
            if "--seconds" in argv and not self.hangs:
                # the helper stops itself, right after the recording window
                self.exit_at = self.clock.now + float(
                    argv[argv.index("--seconds") + 1])
        self.write_log(lines)
        return FakeProc()

    def _make_file(self, dest, plan, sound):
        """The file the recording is writing while it runs."""
        target = Path(dest)
        target.unlink(missing_ok=True)      # prepare() never mixes old bytes in
        size = plan.get("size", 4096)
        if size is None:                    # nothing worth keeping was recorded
            self.recording = None
            self.exit_code = plan.get("exit_code", 3)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"ftypisom" + b"\0" * (max(8, int(size)) - 8))
        self.recording = {"path": str(target), "size": int(size),
                          "secs": float(plan.get("secs", 4.0)),
                          "sound": sound}

    def close_recording(self):
        """The helper finalises the movie: its close line appears and it exits 0
        with a complete file."""
        rec = self.recording
        if not rec:
            return
        self.recording = None
        self.write_log([closed(rec["path"], rec["secs"], rec["size"],
                               rec["sound"])])
        self.exit_code = 0

    def write_log(self, lines):
        """Append the canned lines to the camera log - append mode, which is
        exactly what the real spawn hands the helper as stdout/stderr."""
        if not lines:
            return self
        self.paths.CAM_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(self.paths.CAM_LOG, "ab") as handle:
            for line in lines:
                handle.write((line + "\n").encode())
        return self

    def log_lines(self):
        try:
            return self.paths.CAM_LOG.read_text().splitlines()
        except OSError:
            return []

    # -- the camera being up -----------------------------------------------
    def start_camera(self, **prefs):
        if prefs:
            self.cam.save_prefs(**prefs)
        ok, lines = self.cam.start()
        return ok, lines, (self.popens[-1] if self.popens else [])

    def start_recording(self, name="bench", **kw):
        """Start a recording the way the CLI does, then forget the bookkeeping of
        the restart that came with it: what a test asserts next is the recording,
        not the SIGTERM that made room for it."""
        ok, lines = self.cam.record(name, **kw)
        self.signals = []
        self.urls = []
        self.replies = []
        self.exit_at = None
        return ok, lines

    def state(self, **kw):
        data = {"boot_time": BOOT, "pid": PID, "port": PORT,
                "started": "2026-10-16T09:00:00",
                "width": 1280, "height": 720, "fps": 15, "quality": 80}
        data.update(kw)
        self.paths.RUN.mkdir(parents=True, exist_ok=True)
        if kw.get("_raw") is not None:
            self.paths.CAM_STATE.write_text(kw["_raw"])
        else:
            self.paths.CAM_STATE.write_text(json.dumps(data, indent=2) + "\n")
        return data

    def camera_up(self, recording=None, file=None, **kw):
        """A running camera from an earlier command: state file + live pid,
        optionally mid-recording with an open file behind it."""
        self.install_helper()
        data = {}
        if recording or file:
            data["record"] = recording or {
                "file": str(file), "secs": 0, "audio": False, "log_at": 0,
                "started": "2026-10-16_090000"}
            self.recording = {"path": str(file), "size": _size_of(file),
                              "secs": 4.0, "sound": "off"}
        data.update(kw)
        self.state(**data)
        return self

    # -- the loopback status endpoint --------------------------------------
    def reply(self, code=200, text=IDLE_BODY):
        self.replies.append((code, text))

    def _urlopen(self, url, timeout=None):
        self.urls.append(str(url))
        if not self.replies:
            return Reply(200, IDLE_BODY)
        code, text = self.replies.pop(0)
        if code == 0:
            raise urllib.error.URLError("connection refused")
        if code >= 400:
            # exactly what urllib raises for a 4xx/5xx, body and all
            raise urllib.error.HTTPError(str(url), code, text, {},
                                         io.BytesIO(text.encode()))
        return Reply(code, text)

    # -- recordings on disk ------------------------------------------------
    def make_video(self, name, size):
        d = self.cam.recordings_dir()
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        with open(str(f), "wb") as handle:
            handle.truncate(size)
        return f

    def write_video(self, name, size=4096):
        """A finished recording left behind where the CLI puts them."""
        d = self.cam.recordings_dir()
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        f.write_bytes(b"ftypisom" + b"\0" * (size - 8))
        return f

    @property
    def recordings(self):
        return self.cam.recordings_dir()

    def last_argv(self):
        assert self.popens, "the helper was never spawned"
        return self.popens[-1][1:]

    def sleep_count(self):
        return len(self.clock.slept)


def _size_of(path):
    try:
        return Path(str(path)).stat().st_size
    except OSError:
        return 4096


@pytest.fixture
def rig(stack, monkeypatch):
    return Rig(stack, monkeypatch)
