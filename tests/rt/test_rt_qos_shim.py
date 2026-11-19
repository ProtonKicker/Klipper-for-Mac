"""The QoS half of the generated sitecustomize (compat.SITECUSTOMIZE_SRC).

macOS has no SCHED_FIFO and a negative nice is root-only (measured in the
"Real-time scheduling" research: os.setpriority(PRIO_PROCESS, 0, -5) -> EACCES,
os.nice(-1) -> EPERM), so the only posture klippy can still ask for is a QoS
class, and the ask lives in the venv's auto-imported sitecustomize. It is
switched on by KLIPPERFORMAC_QOS=ui, which process.start() puts in the klippy
child's environment and in nobody else's.

The contract worth pinning is the one that keeps Moonraker safe: no switch -> no
behaviour change at all, switch -> patch applied once, missing symbol -> klippy
still boots. Offline: a throwaway interpreter exec()s the rendered file in a
scratch namespace; the only process spawned is that probe itself, against a
KLIPPERFORMAC_HOME in tmp_path.
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest

from klipperformac import compat

# exec()'d by the child below; the rendered shim never touches a real venv.
DRIVER = textwrap.dedent('''
    import json, os, sys, threading, time

    src_path, out_path = sys.argv[1], sys.argv[2]
    src = open(src_path).read()
    if os.environ.get("RT_BREAK_CTYPES") == "1":
        import ctypes
        def _boom(*a, **kw):
            raise OSError(2, "pthread_set_qos_class_self_np: symbol not found")
        ctypes.CDLL = _boom

    report = {}
    first = {"__name__": "klipperformac_probe", "__file__": src_path}
    exec(compile(src, src_path, "exec"), first)
    report["clock_boottime"] = hasattr(time, "CLOCK_BOOTTIME")
    sock = __import__("socket")
    report["sock_flags"] = (hasattr(sock, "SOCK_CLOEXEC")
                            and hasattr(sock, "SOCK_NONBLOCK"))
    report["patched_after_first"] = bool(
        getattr(threading.Thread, "_klipperformac_qos", False))

    # A second import of the same generated file must not stack a second
    # wrapper around Thread._bootstrap_inner.
    second = {"__name__": "klipperformac_probe2", "__file__": src_path}
    exec(compile(src, src_path, "exec"), second)

    report["set_qos_present"] = "_klipperformac_set_qos" in first
    report["qos_applied_directly"] = (
        first["_klipperformac_set_qos"]() if report["set_qos_present"]
        else None)

    # The wrapper closes over the module global, so replacing it counts how many
    # times - and on which OS thread - a thread start asks for QoS. Note that
    # _bootstrap_inner runs before the interpreter registers the thread's name,
    # so identity is the native id, never Thread.name.
    calls = []
    if report["set_qos_present"]:
        real = first["_klipperformac_set_qos"]

        def counted():
            calls.append(threading.get_ident())
            return real()

        first["_klipperformac_set_qos"] = counted

    report["main_ident"] = threading.get_ident()
    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()
    report["thread_calls"] = calls
    report["patched_after_second"] = bool(
        getattr(threading.Thread, "_klipperformac_qos", False))
    open(out_path, "w").write(json.dumps(report))
''')


def run_shim(tmp_path, env=None):
    """Render SITECUSTOMIZE_SRC and exec it in a child python; returns the
    probe's report."""
    src = tmp_path / "sitecustomize.py"
    src.write_text(compat.SITECUSTOMIZE_SRC)
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    out = tmp_path / "report.json"
    child_env = dict(os.environ)
    child_env.pop("KLIPPERFORMAC_QOS", None)
    child_env.update(env or {})
    done = subprocess.run([sys.executable, str(driver), str(src), str(out)],
                          capture_output=True, env=child_env, timeout=60)
    assert done.returncode == 0, done.stderr.decode(errors="replace")
    return json.loads(out.read_text())


def test_no_switch_leaves_threading_untouched(tmp_path):
    """Moonraker, the proxies and the camera import this same generated file.
    Without KLIPPERFORMAC_QOS they must get the old behaviour and nothing
    else: no wrapper on threading.Thread, no extra work on any thread start, the
    Linux-attribute block intact."""
    report = run_shim(tmp_path, env={"KLIPPERFORMAC_QOS": ""})
    assert report["patched_after_first"] is False
    assert report["patched_after_second"] is False
    assert report["thread_calls"] == []
    assert report["clock_boottime"] is True
    assert report["sock_flags"] is True


def test_a_foreign_value_is_not_a_switch(tmp_path):
    """`KLIPPERFORMAC_QOS=1`/`UI` must not boost a process by accident: the gate
    is the exact value process.start() writes into klippy's environment."""
    for value in ("1", "UI", "yes", " "):
        report = run_shim(tmp_path, env={"KLIPPERFORMAC_QOS": value})
        assert report["patched_after_first"] is False, value


def test_switch_asks_qos_on_every_thread_once(tmp_path):
    report = run_shim(tmp_path, env={"KLIPPERFORMAC_QOS": "ui"})
    assert report["patched_after_first"] is True
    assert report["patched_after_second"] is True
    # One ask per thread start, and it happened ON the new thread - a thread
    # inherits the class of whoever created it, which is why the patch is on
    # _bootstrap_inner at all.
    assert len(report["thread_calls"]) == 1
    assert report["thread_calls"][0] != report["main_ident"]


@pytest.mark.skipif(not sys.platform.startswith("darwin"),
                    reason="pthread_set_qos_class_self_np is macOS-only")
def test_qos_request_actually_lands_on_this_mac(tmp_path):
    """Not a paperwork test: the symbol exists here and the call succeeds, so
    klippy really does get a USER_INTERACTIVE request. What that buys is a
    separate question - sys/qos.h:51 says QoS cannot supersede process-wide
    limits - which is why `klipperformac timing` measures the outcome."""
    report = run_shim(tmp_path, env={"KLIPPERFORMAC_QOS": "ui"})
    assert report["qos_applied_directly"] is True


def test_missing_symbol_still_boots_klippy(tmp_path):
    """Fail-silent contract: a python/SDK without the symbol must import the
    shim cleanly and keep the Moonraker half working."""
    report = run_shim(tmp_path, env={"KLIPPERFORMAC_QOS": "ui",
                                     "RT_BREAK_CTYPES": "1"})
    assert report["qos_applied_directly"] is False
    assert report["clock_boottime"] is True
    assert report["sock_flags"] is True


# --------------------------------------------------------------------------
# the same file through the real activation path: `site` auto-importing it
# --------------------------------------------------------------------------

PROBE = ("import json, threading, time, socket; "
         "print(json.dumps({"
         "'patched': bool(getattr(threading.Thread, '_klipperformac_qos', False)),"
         "'boottime': int(getattr(time, 'CLOCK_BOOTTIME', -1)),"
         "'cloexec': int(getattr(socket, 'SOCK_CLOEXEC', -1))}))")


def run_through_site(tmp_path, env):
    """Put the rendered file on PYTHONPATH and let CPython's `site` import it -
    exactly how the venv picks it up, so nothing here depends on our own exec()."""
    site = tmp_path / "fakesite"
    site.mkdir(exist_ok=True)
    (site / "sitecustomize.py").write_text(compat.SITECUSTOMIZE_SRC)
    child_env = dict(os.environ)
    child_env.pop("KLIPPERFORMAC_QOS", None)
    child_env.update(env)
    child_env["PYTHONPATH"] = str(site)
    done = subprocess.run([sys.executable, "-c", PROBE], capture_output=True,
                          env=child_env, timeout=60)
    assert done.returncode == 0, done.stderr.decode(errors="replace")
    return json.loads(done.stdout.decode())


def test_site_autoimports_the_shim_only_for_klippy(tmp_path):
    with_switch = run_through_site(tmp_path, {"KLIPPERFORMAC_QOS": "ui"})
    without = run_through_site(tmp_path, {})
    assert with_switch["patched"] is True
    assert without["patched"] is False
    # The Moonraker half is unconditional - klippy needs neither part of it,
    # and both are already in the file the shaper venv now also gets.
    for report in (with_switch, without):
        assert report["boottime"] >= 0
        assert report["cloexec"] == 0


# --------------------------------------------------------------------------
# the writer: our generated file must reach the venv, a foreign one must not
# --------------------------------------------------------------------------

class FakeVenvProbe:
    """Answers only the sysconfig probe of compat.ensure_runtime_shims()."""

    def __init__(self, purelib):
        self.purelib = str(purelib)
        self.calls = []

    def run(self, cmd, *args, **kwargs):
        self.calls.append([str(c) for c in cmd])
        return subprocess.CompletedProcess(cmd, 0,
                                           stdout=self.purelib.encode(),
                                           stderr=b"")


def test_ensure_runtime_shims_writes_only_its_own_sitecustomize(
        stack, tmp_path, monkeypatch):
    """compat refuses to clobber a sitecustomize it did not write (a venv can
    carry a user's own). That guard is exactly what can leave klippy without a
    QoS shim in silence - which is why `timing` reads the environment of the
    running pid instead of trusting that the file is ours.

    `stack` rebinds APP_HOME into tmp_path, so the ip shim and the sitecustomize
    land in the throwaway install, never in the user's ~/.klipperformac."""
    purelib = tmp_path / "site-packages"
    purelib.mkdir()
    foreign = purelib / "sitecustomize.py"
    foreign.write_text("# someone else's file\nprint('foreign')\n")
    monkeypatch.setattr(compat, "subprocess", FakeVenvProbe(purelib))
    compat.ensure_runtime_shims(tmp_path / "venv" / "bin" / "python")
    assert foreign.read_text() == "# someone else's file\nprint('foreign')\n"

    foreign.unlink()
    assert compat.ensure_runtime_shims(tmp_path / "venv" / "bin" / "python") \
        is True
    written = (purelib / "sitecustomize.py").read_text()
    assert "klipperformac" in written and "KLIPPERFORMAC_QOS" in written
    assert written == compat.SITECUSTOMIZE_SRC
    # Idempotent: a second call re-writes nothing.
    assert compat.ensure_runtime_shims(tmp_path / "venv" / "bin" / "python") \
        is False
