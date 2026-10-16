"""Forged ``run/state.json`` against klipperformac/process.py.

state.json is the only thing that tells the CLI -- and the SwiftUI app, which
re-implements the same read (KlipperMac/Sources/KlipperMac/Core/StackReader.swift)
-- what is running. It survives crashes, truncated writes, hand edits, sync
tools and foreign installs, so every shape it can take on disk must degrade
safely.

Fully offline: sysctl/ps/os.kill are faked *inside the module under test* and
the state file always lives under the throwaway tmp_path home.
"""
import json
import os
import shutil
import subprocess
import types

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

BOOT = "1730000000"
SYSCTL_OK = ("kern.boottime: {{ sec = {0}, usec = 412314 }} "
             "Thu Jan  1 00:00:00 2026\n").format(BOOT).encode()


class Clock:
    """Virtual clock: nothing in these tests ever really sleeps."""

    def __init__(self, start=100000.0):
        self.t = start

    def time(self):
        return self.t

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds

    def strftime(self, fmt):
        return "2026-01-01T00:00:00"


class Completed:
    def __init__(self, stdout=b"", code=0):
        self.stdout = stdout
        self.returncode = code
        self.stderr = b""


class FakeSubprocess:
    """Answers only the sysctl/ps argv built by process.py."""

    DEVNULL = subprocess.DEVNULL

    @staticmethod
    def Popen(*args, **kwargs):            # pragma: no cover - guard rail
        raise AssertionError(
            "no real process may be spawned by the offline test harness")

    def __init__(self, table=None, scan=b"", sysctl=SYSCTL_OK, sysctl_code=0):
        self.table = table or {}          # {pid str: (state, command)}
        self.scan = scan
        self.sysctl = sysctl
        self.sysctl_code = sysctl_code
        self.argv = []                    # every argv handed to subprocess.run

    def drop(self, pid, sig=None):
        """A signalled process leaves the table: `ps` stops listing it, which
        is what lets stop()'s TERM->KILL escalation be told apart from the
        plain TERM path (the sibling batteries fake this the same way)."""
        self.table.pop(str(pid), None)

    def run(self, cmd, *args, **kwargs):
        cmd = [str(c) for c in cmd]
        self.argv.append(cmd)
        if cmd[0].endswith("sysctl"):
            if self.sysctl_code and kwargs.get("check"):
                raise subprocess.CalledProcessError(self.sysctl_code, cmd)
            return Completed(self.sysctl, self.sysctl_code)
        if cmd[0] == "ps" and "-p" in cmd:
            wanted = cmd[cmd.index("-p") + 1].split(",")
            lines = ["%6s %s %s" % (p, self.table[p][0], self.table[p][1])
                     for p in wanted if p in self.table]
            return Completed(("\n".join(lines) + "\n").encode()
                             if lines else b"")
        if cmd[0] == "ps":
            return Completed(self.scan)
        return Completed(b"")             # anything else: empty, never executed

    def ps_pid_calls(self):
        return [c for c in self.argv if c[0] == "ps" and "-p" in c]


class FakeOS:
    """Records the signals process.py *would* send; no real process is touched.
    `on_kill` lets the forged process table lose the row, the way a process that
    really dies stops showing up in `ps`."""

    def __init__(self, on_kill=None):
        self.calls = []
        self._on_kill = on_kill

    def kill(self, pid, sig):
        self.calls.append((pid, sig))
        if self._on_kill:
            self._on_kill(pid, sig)

    def waitpid(self, pid, options):
        raise ChildProcessError(3, "No child processes")

    def __getattr__(self, name):
        return getattr(os, name)


class FakeProc:
    def __init__(self, pid):
        self.pid = pid


@pytest.fixture
def proc(stack, monkeypatch):
    """process.py with sysctl/ps/time faked, plus state-file helpers."""
    fake = FakeSubprocess()
    kills = FakeOS(on_kill=fake.drop)
    monkeypatch.setattr(stack.process, "subprocess", fake)
    monkeypatch.setattr(stack.process, "time", Clock())
    monkeypatch.setattr(stack.process, "os", kills)
    monkeypatch.setattr(stack.process, "invalidate_stale_build", lambda d: False)
    monkeypatch.setattr(stack.process, "ensure_runtime_shims", lambda p: False)
    stack.process._boot_cache[:] = [0.0, None]
    stack.paths.RUN.mkdir(parents=True, exist_ok=True)

    def write(text):
        stack.paths.STATE.write_text(text)
        return stack.paths.STATE

    return types.SimpleNamespace(p=stack.process, paths=stack.paths,
                                 fake=fake, kills=kills, write=write,
                                 home=stack.home, data=stack.data)


def service_cmdline(paths, name):
    """A plausible `ps` command line for service `name` of this install."""
    py = str(paths.VENV_PY)
    if name == "klipper":
        return "%s %s/klippy/klippy.py -a %s -l %s %s" % (
            py, paths.KLIPPER, paths.API_SOCKET, paths.KLIPPY_LOG,
            paths.PRINTER_CFG)
    if name == "moonraker":
        return "%s -m moonraker -d %s -c %s -l %s" % (
            py, paths.DATA, paths.MOONRAKER_CONF, paths.MOONRAKER_LOG)
    if name in ("web_mainsail", "web_fluidd"):
        port = (paths.WEB_PORT if name == "web_mainsail"
                else paths.FLUIDD_PORT)
        root = paths.WEB_MAINSAIL if name == "web_mainsail" else paths.WEB_FLUIDD
        return "%s -m klipperformac.proxy %s %s %s 127.0.0.1" % (
            py, port, root, paths.MOONRAKER_PORT)
    return "caffeinate -dis"


def make_live(proc, pid_for):
    """Install a ps table where each name at its pid looks alive and is ours."""
    proc.fake.table = {str(pid): ("S+", service_cmdline(proc.paths, name))
                       for name, pid in pid_for.items()}


# --------------------------------------------------------------------------
# shapes that must simply read as "no usable state"
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "", "   ", "{", '{"boot_time": ', "\x00\x01\x02binary",
    "not json at all", '{"boot_time": "%s"}garbage' % BOOT,
    "\N{ZERO WIDTH SPACE}" + BOOT, "\n\n\n",
])
def test_unparseable_state_is_not_a_crash(proc, raw):
    """Corrupt/truncated/binary state must read as unknown, never raise."""
    proc.write(raw)
    assert proc.p._load() is None
    assert proc.p.running() is None
    assert proc.p.stop() is False


def test_empty_and_falsy_pid_values_read_as_stopped(proc):
    proc.write(json.dumps({"boot_time": BOOT, "klipper": None,
                           "moonraker": 0, "caffeinate": ""}))
    assert proc.p.running() == {n: False for n in proc.p.SERVICES}
    # nothing worth asking ps about -> not even one ps call may happen
    assert proc.fake.ps_pid_calls() == []


def test_missing_service_keys_just_mean_stopped(proc):
    proc.write(json.dumps({"boot_time": BOOT, "klipper": 4321}))
    make_live(proc, {"klipper": 4321})
    state = proc.p.running()
    assert state["klipper"] is True
    assert state["moonraker"] is False
    assert set(state) == set(proc.p.SERVICES)


def test_unknown_extra_keys_never_reach_the_kill_list(proc):
    """A state key that is not a service must not make us signal anything."""
    proc.write(json.dumps({"boot_time": BOOT, "klipper": 4321,
                           "evil_broadcast": -1, "evil_self": os.getpid(),
                           "evil_init": 1, "cam_pid": -9999}))
    make_live(proc, {"klipper": 4321})
    assert proc.p.stop() is True
    assert [pid for pid, _ in proc.kills.calls] == [4321]


def test_state_from_a_previous_boot_is_cleared_not_reused(proc):
    proc.write(json.dumps({"boot_time": "1600000000", "klipper": 4321}))
    assert proc.p._load() is None
    assert not proc.paths.STATE.exists(), "stale-boot state must be dropped"


def test_boot_time_written_as_int_is_cleared(proc):
    """StackReader.swift requires a JSON *string*; anything else is foreign."""
    proc.write(json.dumps({"boot_time": int(BOOT), "klipper": 4321}))
    assert proc.p._load() is None


def test_state_path_is_a_directory(proc):
    proc.paths.STATE.mkdir(parents=True, exist_ok=True)
    assert proc.p._load() is None
    assert proc.p.running() is None
    assert proc.p.stop() is False


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_unreadable_state_file_is_not_a_crash(proc):
    proc.write(json.dumps({"boot_time": BOOT, "klipper": 4321}))
    os.chmod(proc.paths.STATE, 0o000)
    try:
        assert proc.p._load() is None
        assert proc.p.running() is None
    finally:
        os.chmod(proc.paths.STATE, 0o644)


def test_huge_but_valid_state_is_still_parsed(proc):
    """A ~1 MB state file of unrelated keys must not break or hang the read."""
    data = {"boot_time": BOOT, "klipper": 4321}
    for i in range(20000):
        data["filler_%d" % i] = "x" * 40
    raw = json.dumps(data)
    assert len(raw) > 900000
    proc.write(raw)
    state = proc.p._load()
    assert state is not None and state["klipper"] == 4321
    assert set(proc.p.running()) == set(proc.p.SERVICES)


@pytest.mark.parametrize("depth", [1, 40, 200])
def test_deeply_nested_state_values_survive_read(proc, depth):
    inner = {"pid": 4321}
    for _ in range(depth):
        inner = {"n": inner}
    proc.write(json.dumps({"boot_time": BOOT, "klipper": 4321, "junk": inner}))
    assert proc.p._load()["klipper"] == 4321


def test_unicode_and_emoji_in_state_do_not_raise(proc):
    proc.write(json.dumps({"boot_time": BOOT, "klipper": 4321,
                           "started": "2026-01-01T00:00:00 \N{SNOWMAN}",
                           "web_mainsail": "/tmp/ünïcødé \N{SNOWMAN}"},
                          ensure_ascii=False))
    assert proc.p._load()["klipper"] == 4321


# --------------------------------------------------------------------------
# _save: what we hand to the next read
# --------------------------------------------------------------------------

def test_save_writes_the_documented_contract(proc):
    """boot_time string + started + int pids, exactly as StackReader reads it."""
    proc.p._save({"klipper": FakeProc(4321), "moonraker": FakeProc(4322)})
    raw = json.loads(proc.paths.STATE.read_text())
    assert isinstance(raw["boot_time"], str) and raw["boot_time"] == BOOT
    assert isinstance(raw["klipper"], int) and raw["klipper"] == 4321
    assert isinstance(raw["started"], str)
    assert proc.p._load() == raw


def test_save_creates_the_run_directory(proc):
    shutil.rmtree(proc.paths.RUN)
    assert not proc.paths.RUN.exists()
    proc.p._save({"klipper": FakeProc(4321)})
    assert proc.paths.STATE.exists()


def test_save_is_repeatable(proc):
    proc.p._save({"klipper": FakeProc(4321)})
    first = json.loads(proc.paths.STATE.read_text())
    proc.p._save({"klipper": FakeProc(4321)})
    second = json.loads(proc.paths.STATE.read_text())
    assert {k: v for k, v in first.items() if k != "started"} == \
           {k: v for k, v in second.items() if k != "started"}


def test_save_persists_only_what_it_is_given(proc):
    proc.p._save({"klipper": FakeProc(4321), "made_up_service": FakeProc(1)})
    state = proc.p._load()
    assert state["klipper"] == 4321
    assert state["made_up_service"] == 1
    # an unknown key must still not be signalled by stop()
    proc.kills.calls.clear()
    proc.p.stop()
    assert proc.kills.calls == [(4321, 15)]


def test_clear_tolerates_a_missing_or_undeletable_state(proc):
    proc.p._clear()                          # nothing on disk
    proc.paths.STATE.mkdir(parents=True, exist_ok=True)
    proc.p._clear()                          # unlink() -> OSError, must not raise
    assert proc.paths.STATE.exists()


@settings(max_examples=40, database=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.dictionaries(
    st.sampled_from(["klipper", "moonraker", "web_mainsail", "web_fluidd",
                     "caffeinate"]),
    st.integers(min_value=1, max_value=999999), max_size=5))
def test_pid_roundtrip_property(proc, pids):
    """Whatever pids start() persists, _load() must hand back unchanged."""
    proc.p._save({n: FakeProc(p) for n, p in pids.items()})
    state = proc.p._load()
    assert state is not None
    assert {n: state[n] for n in pids} == pids


# --------------------------------------------------------------------------
# BUGS
# --------------------------------------------------------------------------

NON_DICT_JSON = ["[1, 2, 3]", '"klipper"', "12345", "null", "true", "NaN",
                 '[{"boot_time": "%s", "klipper": 1}]' % BOOT]


@pytest.mark.parametrize("raw", NON_DICT_JSON)
def test_valid_json_that_is_not_a_dict_self_heals(proc, raw):
    """_load() must not assume the parsed document is a dict.

    Was BUG DEEP (fixed in klipperformac/process.py): state.json holding any
    valid non-object JSON raised AttributeError out of
    _load()/running()/stop()/kill_all() -- every entry point, including the
    `klipperformac killall` escape hatch -- so the stack became unmanageable
    without a manual rm of the state file. cam.py guards exactly this way
    (`pid = state.get("pid") if isinstance(state, dict) else None`); process.py
    now does the same.
    """
    proc.write(raw)
    errors = []
    for call in (proc.p._load, proc.p.running, proc.p.stop, proc.p.kill_all):
        try:
            call()
        except Exception as exc:              # noqa: BLE001 - that is the point
            errors.append("%s: %s" % (type(exc).__name__, exc))
    assert errors == [], (
        "corrupt-but-parseable state.json must self-heal, got %r" % errors)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_start_does_not_leak_an_untracked_stack_when_state_is_unwritable(
        proc, monkeypatch):
    """If the state file cannot be written, the children must not be spawned.

    Was BUG DEEP (fixed in klipperformac/process.py): start() launched five
    processes and only persisted them at the very end. When that write failed
    -- read-only data volume, permission-denied run/, disk full -- it raised a
    raw OSError *after* the children were alive: no state.json existed, so
    `down` and `status` reported "not running" and the orphans kept the serial
    port and 7125/8080/8081 squatted with no tracked way to stop them. start()
    now proves the record is writable (_can_persist()) before spawning a single
    process and says so as a one-line [!] instead of leaving an untracked stack.
    """
    spawned = []

    def fake_popen(cmd, **kw):
        spawned.append(cmd)
        return FakeProc(9000 + len(spawned))

    monkeypatch.setattr(proc.p, "_popen", fake_popen)
    os.chmod(proc.paths.RUN, 0o500)
    try:
        try:
            proc.p.start()
        except Exception:                      # noqa: BLE001 - tolerated here
            pass
    finally:
        os.chmod(proc.paths.RUN, 0o755)
    assert spawned == [], (
        "start() spawned %d process(es) it could not track; a stack that "
        "cannot be persisted must not be started at all" % len(spawned))
