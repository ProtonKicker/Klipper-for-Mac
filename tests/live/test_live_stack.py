"""Live tier: assertions against the RUNNING local stack.

Skipped unless KLIPPERFORMAC_ALLOW_LIVE=1 (see pytest.ini). These run on the
developer machine against the real klipper/moonraker/ports - CI never does.
"""
import json
import subprocess
import sys
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.live

BASE = "http://127.0.0.1:7125"
HEALTHY = {"ready", "printing", "paused", "complete"}


def get(url, timeout=8, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.load(r)


def test_moonraker_healthy():
    """Tolerant of the preceding hardware test's FIRMWARE_RESTART: Moonraker's
    view of klippy lags a few seconds behind an MCU reset."""
    import time
    deadline = time.time() + 25
    info = None
    while time.time() < deadline:
        status, data = get(BASE + "/server/info")
        assert status == 200
        info = data["result"]
        if info["klippy_connected"] and info["klippy_state"] in HEALTHY:
            return
        time.sleep(2)
    pytest.fail("klippy never reached a healthy view: {}".format(info))


def test_printer_state_queryable():
    status, data = get(BASE + "/printer/info")
    assert status == 200
    assert data["result"]["state"] in HEALTHY


def test_ui_proxies_serve_static():
    for port in (8080, 8081):
        with urllib.request.urlopen(
                "http://127.0.0.1:{}/".format(port), timeout=8) as r:
            assert r.status == 200
            assert b"<!doctype html" in r.read(256).lower()


def test_proxy_replaces_client_supplied_xff():
    """A browser-sent X-Forwarded-For must NOT reach Moonraker's identity
    checks: 8.8.8.8 is not in trusted_clients, so if the proxy were relaying
    it, Moonraker would answer 401. A 200 proves the proxy overwrote it with
    the real socket peer (127.0.0.1)."""
    try:
        status, _ = get(BASE.replace("7125", "8081") + "/server/info",
                        headers={"X-Forwarded-For": "8.8.8.8",
                                 "X-Real-IP": "8.8.8.8"})
    except urllib.error.HTTPError as e:
        pytest.fail("spoofed XFF was trusted upstream: HTTP {}".format(e.code))
    assert status == 200


def test_cli_status_reports_klippy_line():
    repo = __import__("pathlib").Path(__file__).resolve().parents[2]
    env = {"PYTHONPATH": str(repo), "PATH": "/usr/bin:/bin:/usr/local/bin",
           "HOME": str(__import__("pathlib").Path.home())}
    out = subprocess.run([sys.executable, "-m", "klipperformac", "status"],
                         cwd=repo, env=env, capture_output=True, text=True,
                         timeout=90)
    assert out.returncode == 0
    assert "klippy" in out.stdout
    assert any(word in out.stdout for word in
               ("ready", "printing", "paused", "error", "shutdown"))
