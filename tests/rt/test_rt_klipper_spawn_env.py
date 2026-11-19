"""What ``start()`` hands the klippy child: an env-only QoS switch, argv intact.

The RT mitigation slice is deliberately small. macOS gives us one lever that
needs no root - a QoS class request (tests/rt/test_rt_qos_shim.py) - and it is
switched on per process through the environment. That makes two promises worth
pinning, both invisible from the outside when they break:

  * klippy's environment carries ``KLIPPERFORMAC_QOS=ui``; moonraker, the UI
    proxies and caffeinate do not. They are not on the step-timing path, and a
    blanket boost is exactly the kind of thing the research refuses.
  * the *command line* is byte-identical to what it was before the change:
    ``_service_kind()`` and ``running()`` identify a service by its argv shape
    under this install (klipperformac/process.py:243-263), so any argv edit
    would silently break status, down, orphan reaping and the SwiftUI reader.

Offline: ``subprocess`` in the module under test is forged. ``Popen`` records
argv + kwargs and publishes the command line in a fake ``ps`` table, so nothing
is spawned, no port is bound, no serial device is opened, and every file lives
under pytest's tmp_path (the ``stack`` fixture rebinds KLIPPERFORMAC_HOME).
"""
import json
import subprocess
import types

import pytest

BOOT = "1791932927"
FAKE_KPY = "FABRICATED-KPY"


class Completed:
    def __init__(self, stdout=b"", code=0, stderr=b""):
        self.stdout = stdout
        self.returncode = code
        self.stderr = stderr


class FakeProc:
    def __init__(self, pid):
        self.pid = pid


class Forge:
    """``ps`` + ``sysctl`` + ``Popen``, forged inside process.py's namespace."""

    DEVNULL = subprocess.DEVNULL

    def __init__(self):
        self.rows = {}
        self.spawned = []
        self.runs = []
        self._next_pid = 41000

    def add(self, pid, cmd, state="Ss"):
        self.rows[str(pid)] = {"state": state, "cmd": cmd}
        return pid

    def Popen(self, cmd, **kwargs):
        argv = [str(c) for c in cmd]
        proc = FakeProc(self._next_pid)
        self._next_pid += 1
        self.spawned.append({"argv": argv, "kwargs": kwargs, "pid": proc.pid})
        self.add(proc.pid, " ".join(argv))
        return proc

    def run(self, cmd, *args, **kwargs):
        argv = [str(c) for c in cmd]
        self.runs.append(argv)
        if argv[0].endswith("sysctl"):
            return Completed(("{ sec = %s, usec = 1 } x" % BOOT).encode())
        if argv[:2] == ["ps", "-p"]:
            tokens = argv[argv.index("-p") + 1].split(",")
            lines = ["%6s %-3s %s" % (t, self.rows[t]["state"],
                                      self.rows[t]["cmd"])
                     for t in tokens if t in self.rows]
            return Completed(("\n".join(lines) + "\n").encode()
                             if lines else b"", 0 if lines else 1)
        if argv[:1] == ["ps"]:
            return Completed(b"")
        return Completed(b"")

    def one(self, needle):
        """The single spawn whose argv contains `needle` (fails if not unique)."""
        hits = [rec for rec in self.spawned
                if any(needle in a for a in rec["argv"])]
        assert len(hits) == 1, [rec["argv"] for rec in self.spawned]
        return hits[0]


@pytest.fixture
def txn(stack, monkeypatch):
    """A start()-able install in tmp_path with every external read forged."""
    ps = Forge()
    monkeypatch.setattr(stack.process, "subprocess", ps)
    monkeypatch.setattr(stack.process, "_boot_time", lambda: BOOT)
    monkeypatch.setattr(stack.process, "invalidate_stale_build",
                        lambda d: False)
    monkeypatch.setattr(stack.process, "ensure_runtime_shims", lambda p: False)
    monkeypatch.setattr(stack.process, "_reap_orphans", lambda: None)
    monkeypatch.setattr(stack.process, "reap_stale_caffeinate", lambda: [])
    monkeypatch.setattr(stack.process, "port_conflicts", lambda: [])
    # A shaper-venv interpreter of our own choosing, so the assertion below can
    # tell klippy's spawn from moonraker's even when both live in one venv.
    kpy = stack.home / "venv-shaper" / "bin" / FAKE_KPY
    monkeypatch.setattr(stack.shaper, "interpreter_for_klipper",
                        lambda: (str(kpy), ""))
    # The switch must come from start(), never from whatever the shell happens
    # to have exported.
    monkeypatch.delenv("KLIPPERFORMAC_QOS", raising=False)
    stack.paths.ensure_dirs()
    return types.SimpleNamespace(p=stack.process, paths=stack.paths, ps=ps,
                                 kpy=kpy, home=stack.home, shaper=stack.shaper)


def test_klippy_alone_gets_the_qos_switch(txn):
    assert txn.p.start() is True
    klipper = txn.ps.one("klippy.py")
    moonraker = txn.ps.one("moonraker")
    mainsail = txn.ps.one(str(txn.paths.WEB_PORT))
    fluidd = txn.ps.one(str(txn.paths.FLUIDD_PORT))
    caffeinate = txn.ps.one("caffeinate")

    assert klipper["kwargs"]["env"]["KLIPPERFORMAC_QOS"] == "ui"
    for rec in (moonraker, mainsail, fluidd):
        assert "KLIPPERFORMAC_QOS" not in rec["kwargs"]["env"], rec["argv"]
    # caffeinate is spawned with the ambient env (no env kwarg at all): nothing
    # we did not ask for must leak into it.
    assert "env" not in caffeinate["kwargs"], caffeinate["argv"]


def test_a_shell_that_exported_the_switch_does_not_boost_the_whole_stack(txn,
                                                                        monkeypatch):
    """The QoS ask is only meaningful while klippy is the one asking; a blanket
    boost is exactly what the research refuses, so the ambient value is stripped
    from moonraker and the proxies and overwritten (to the same value) for
    klippy."""
    monkeypatch.setenv("KLIPPERFORMAC_QOS", "ui")
    assert txn.p.start() is True
    assert txn.ps.one("klippy.py")["kwargs"]["env"]["KLIPPERFORMAC_QOS"] == "ui"
    assert "KLIPPERFORMAC_QOS" not in txn.ps.one("moonraker")["kwargs"]["env"]
    for port in (txn.paths.WEB_PORT, txn.paths.FLUIDD_PORT):
        assert "KLIPPERFORMAC_QOS" not in txn.ps.one(str(port))["kwargs"]["env"]
    # repair() (a proxy or caffeinate that died mid-print) keeps the same rule
    txn.paths.STATE.unlink()
    monkeypatch.delenv("KLIPPERFORMAC_QOS")
    txn.ps.spawned.clear()
    txn.ps.rows.clear()
    monkeypatch.setattr(txn.p, "running", lambda: {
        name: name in ("klipper", "moonraker") for name in txn.p.SERVICES})
    started = txn.p.repair()
    assert started == ["caffeinate", "web_fluidd", "web_mainsail"]
    for rec in txn.ps.spawned:
        assert "KLIPPERFORMAC_QOS" not in rec["kwargs"].get("env", {}), \
            rec["argv"]


def test_klippy_keeps_the_build_env_it_was_given(txn):
    """The CPATH header shim (compat.build_env) and the QoS switch are separate
    concerns: klippy's env is the ambient env plus exactly those two keys and
    nothing else invented on the way. Written ambient-independently: git hooks
    may or may not already carry CPATH in os.environ."""
    txn.p.start()
    env = txn.ps.one("klippy.py")["kwargs"]["env"]
    import os
    assert "include" in env["CPATH"]
    assert set(env) - set(os.environ) <= {"CPATH", "KLIPPERFORMAC_QOS"}
    assert env.get("KLIPPERFORMAC_QOS") == "ui"
    # nothing of the ambient env was dropped on the way through
    assert not set(os.environ) - set(env)


def test_the_shim_reaches_the_venv_klippy_actually_uses(txn, monkeypatch):
    """sitecustomize is per-interpreter, and klippy may run on the 3.14 shaper
    venv (shaper.interpreter_for_klipper). installer.setup() only ever wrote the
    file into the main venv, so the QoS switch would have been set in an
    environment nobody reads - in silence. This is the half that fixes it."""
    written = []
    monkeypatch.setattr(txn.p, "ensure_runtime_shims",
                        lambda python: written.append(str(python)) or False)
    txn.p.start()
    assert str(txn.kpy) in written, (
        "klippy's own interpreter never got the shim: %s" % written)
    assert str(txn.paths.VENV_PY) in written  # moonraker's, as before

    written.clear()
    txn.ps.rows.clear()
    txn.ps.spawned.clear()
    txn.paths.STATE.unlink()
    # Same venv for both: no pointless second write.
    monkeypatch.setattr(txn.shaper, "interpreter_for_klipper",
                        lambda: (str(txn.paths.VENV_PY), ""))
    assert txn.p.start() is True
    assert written == [str(txn.paths.VENV_PY)]


def test_the_klipper_command_line_is_untouched(txn):
    """_service_kind() judges argv only, so an env-only change is invisible to
    it - and if the argv ever moves, every later read mis-identifies the stack."""
    txn.p.start()
    argv = txn.ps.one("klippy.py")["argv"]
    assert argv == [str(txn.kpy), str(txn.paths.KLIPPER / "klippy" / "klippy.py"),
                    "-a", str(txn.paths.API_SOCKET),
                    "-l", str(txn.paths.KLIPPY_LOG),
                    str(txn.paths.PRINTER_CFG)]
    assert txn.p._service_kind(" ".join(argv)) == "klipper"


def test_the_started_stack_is_still_recognisable(txn):
    """start() -> running() round trip with the new env in place: the promise
    status/down/the GUI rely on did not move."""
    assert txn.p.start() is True
    assert txn.p.running() == {name: True for name in txn.p.SERVICES}
    doc = json.loads(txn.paths.STATE.read_text())
    assert doc["klipper"] == txn.ps.one("klippy.py")["pid"]
    assert doc["caffeinate"] == txn.ps.one("caffeinate")["pid"]
