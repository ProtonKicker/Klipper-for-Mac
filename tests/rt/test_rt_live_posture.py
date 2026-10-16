"""Live tier, READ-ONLY: what macOS is actually giving a running klippy.

Skipped unless ``KLIPPERFORMAC_ALLOW_LIVE=1 make test-live`` (pytest.ini: the
default tiers filter ``live`` out, so CI never hangs on a machine with a printer
attached). This is the human step the research lists as "verify the *real*
klippy", reduced to reads:

    ps -o pid=,ni=,pri=   pmset -g assertions   ioreg -rn AppleSmartBattery
    our own klippy.log    `klipperformac timing --json`

Nothing here starts, stops, restarts or signals anything, so it is safe to run
during a print - which is exactly when the answer matters. Location note: it sits
in tests/rt/ (the RT slice's own directory) rather than tests/live/; ``make
test-live`` selects by the ``live`` marker across the whole tests/ tree, so the
tier is unchanged.
"""
import json
import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.live

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def _klipper_pid():
    from klipperformac import paths, process
    state = process.running()
    if not state or not state.get("klipper"):
        return None
    try:
        record = json.loads(paths.STATE.read_text())
    except (OSError, ValueError):
        return None
    pid = record.get("klipper")
    return pid if isinstance(pid, int) and 1 < pid <= 99999 else None


def _ps(pid):
    out = subprocess.run(["ps", "-o", "pid=,ni=,pri=,state=", "-p", str(pid)],
                         capture_output=True, timeout=10).stdout.decode()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == str(pid):
            return {"pid": pid, "nice": int(parts[1]), "pri": int(parts[2]),
                    "state": parts[3]}
    return None


@pytest.fixture(scope="module")
def klipper_pid():
    pid = _klipper_pid()
    if pid is None:
        pytest.skip("no klipperformac klipper running: `klipperformac up` "
                    "first, or run this against a stack you started yourself")
    return pid


def test_klippy_holds_a_normal_priority_while_idle(klipper_pid):
    """The measured healthy posture on this Mac is nice 0 / pri 31
    (research: "Real-time scheduling"). Anything under 20 is BACKGROUND/App-Nap
    and a print will not survive it; 20..30 is UTILITY-class throttling."""
    from klipperformac import cli
    samples = []
    for _ in range(5):
        row = _ps(klipper_pid)
        assert row is not None, "ps lost the pid between samples"
        samples.append(row["pri"])
        time.sleep(0.2)
    pri = samples[-1]
    assert len(set(samples)) == 1, (
        "klippy's priority moved while nothing was printing: %s" % samples)
    verdict = cli._pri_class(pri)
    assert verdict == "normal", (
        "klippy pid %s sits at pri %d (%s). macOS gives no RT scheduler and "
        "negative nice is root-only, so the fix is on the macOS side: close "
        "the apps hogging the machine, check Low Power Mode, and see "
        "`klipperformac timing` for the full picture." % (klipper_pid, pri,
                                                          verdict))


def test_klippy_is_not_niced_down(klipper_pid):
    """nice must stay 0: a *positive* nice pushes klippy down, and a negative
    one is refused by the kernel without root (os.setpriority -> EACCES,
    os.nice(-1) -> EPERM, both measured)."""
    assert _ps(klipper_pid)["nice"] == 0


def test_timing_command_reads_the_live_machine(klipper_pid):
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT
    done = subprocess.run([sys.executable, "-m", "klipperformac", "timing",
                           "--json"], capture_output=True, env=env, cwd=REPO_ROOT,
                          timeout=120)
    report = json.loads(done.stdout.decode())
    assert report["klipper"]["pid"] == klipper_pid
    assert report["klipper"]["running"] is True
    assert report["host_budget"]["stats_lines"] > 0
    # The retransmit floor is MIN_RTO (klippy/chelper/serialqueue.c:107); a
    # smaller number would mean klippy is asking for sub-25ms retries.
    if report["host_budget"]["rto_min"] is not None:
        assert report["host_budget"]["rto_min"] >= 0.025
    assert report["power"]["external_connected"] in (True, False)
    speed = report["power"]["cpu_speed_limit"]
    assert speed is None or 0 <= speed <= 100
    assert done.returncode in (0, 1), done.stderr.decode(errors="replace")
    if report["problems"]:
        pytest.fail("timing found something to fix: %s" % report["problems"])


def test_qos_shim_state_matches_the_venv_it_was_written_to(klipper_pid):
    """Cross-check between the two halves of the mitigation: if klippy's
    environment says the switch is on, the venv's sitecustomize must actually
    carry the QoS block - otherwise a foreign sitecustomize silently skipped it
    (compat/__init__.py refuses to clobber files it did not write)."""
    from klipperformac import cli, paths
    state = cli._qos_env_state(klipper_pid)
    if state == "unknown":
        pytest.skip("this Mac will not show another process's environment")
    probe = subprocess.run(
        [str(paths.VENV_PY), "-c",
         "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        capture_output=True, timeout=30)
    sitecustomize = None
    if probe.returncode == 0:
        sitecustomize = os.path.join(
            probe.stdout.decode().strip(), "sitecustomize.py")
    if state == "off":
        pytest.skip("klippy's environment has no QoS switch: this stack "
                    "predates the shim (or a foreign sitecustomize.py owns the "
                    "venv). Run `klipperformac restart` with nothing printing, "
                    "then re-run this test")
    assert sitecustomize and os.path.exists(sitecustomize)
    with open(sitecustomize, errors="replace") as f:
        assert "KLIPPERFORMAC_QOS" in f.read()


def test_caffeinate_guard_is_ours(klipper_pid):
    """The sleep prevention timing trusts must be a caffeinate this install
    started and state.json still vouches for - never somebody else's."""
    from klipperformac import cli, process
    state = process.running() or {}
    if not state.get("caffeinate"):
        pytest.skip("this stack has no tracked caffeinate (started by hand?)")
    held = cli._caffeinate_assert_held([cli._tracked_pid("caffeinate")])
    assert held is True
