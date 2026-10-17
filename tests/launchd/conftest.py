"""Offline harness for the launchd/login slice.

Three things a test in this folder must never do: write into this Mac's real
~/Library/LaunchAgents, talk to launchctl, or open a socket. Each is closed off
by the fixture rather than by convention:

  * KLIPPERFORMAC_LAUNCH_AGENTS (the module's own documented seam) points the
    agent directory at tmp_path, so the plist tests cannot create a login item
    on the machine they are proving.
  * launchd._launchctl - the single place launchctl is ever exec'd - is replaced
    by a recorder that answers from a table, so "bootstrap happens after the
    file is written" is a fact about recorded argv.
  * an autouse `no_network` makes any socket creation abort the test: the REST
    corroboration inside a sweep is faked like everything else, and a real GET
    against the developer's running Moonraker would make the state machine
    unprovable (and would not be offline).

`launchd` is reloaded rather than patched in place, because it reads paths.* at
call time: the reload binds it to the same reloaded `paths`/`process` modules
the shared `stack` fixture already rebuilt.
"""
import importlib
import socket

import pytest

LAUNCHCTL_REPLY = (0, "", "")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Hard offline proof: any socket creation aborts the test."""
    def _boom(*args, **kwargs):
        raise AssertionError("BUG IN TEST: the offline launchd suite must not "
                             "open a socket (the live round-trip is the "
                             "integrator's, on a bench Mac)")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)


class Launchctl:
    """launchd._launchctl as a table of answers plus a log of the asking."""

    def __init__(self):
        self.calls = []
        self.replies = {}
        self.default = LAUNCHCTL_REPLY

    def __call__(self, args, timeout=15):
        argv = [str(a) for a in args]
        self.calls.append(argv)
        reply = self.replies.get(argv[0] if argv else "", self.default)
        if isinstance(reply, list):        # queued answers, one per call
            reply = reply.pop(0) if reply else self.default
        return reply

    def verbs(self):
        return [call[0] for call in self.calls]

    def joined(self):
        return [" ".join(call) for call in self.calls]


@pytest.fixture
def agent(stack, monkeypatch, tmp_path):
    """launchd on a throwaway home, throwaway LaunchAgents dir, no outside world."""
    launchd = importlib.reload(importlib.import_module("klipperformac.launchd"))
    agents = tmp_path / "Library" / "LaunchAgents"
    monkeypatch.setenv("KLIPPERFORMAC_LAUNCH_AGENTS", str(agents))
    stack.paths.ensure_dirs()
    ctl = Launchctl()
    monkeypatch.setattr(launchd, "_launchctl", ctl)
    # The supervisor's restart ledger is keyed on kern.boottime, which is a
    # sysctl subprocess: pinned here so a test can name "this boot" and, in one
    # test, change it to prove a latch never survives a reboot.
    stack.boot = {"value": "test-boot"}
    monkeypatch.setattr(launchd.process, "_boot_time",
                        lambda: stack.boot["value"])
    # The one seam tests of the sweep need next: never a real socket, never a
    # real 2 s timeout.
    monkeypatch.setattr(launchd, "rest_answers", lambda timeout=2.0: True)

    def svc(*names):
        """A process.running() dict with exactly `names` reported up."""
        data = {name: False for name in launchd.process.SERVICES}
        for name in names:
            data[name] = True
        return data

    stack.launchd = launchd
    stack.ctl = ctl
    stack.agents = agents
    stack.monkey = monkeypatch
    stack.svc = svc
    stack.calls = {"start": 0, "stop": 0, "repair": [], "signal": [],
                   "rest": 0}
    # Helper functions ride on the namespace rather than being imported: the
    # test folders have no __init__.py (pytest's prepend import mode), so a
    # `from .conftest import` in a test module would fail collection.
    stack.installed_plist = lambda tick=None: installed_plist(stack, tick)
    stack.make_setup_home = lambda: make_setup_home(stack)
    stack.fake_process = lambda **kw: fake_process(stack, **kw)
    stack.fake_rest = lambda answers=True: fake_rest(stack, answers)
    return stack


def fake_process(agent, running=None, start=True):
    """Patch the four process primitives the supervisor uses with counters.

    `running` may be a dict, None, or a list consumed one sweep at a time (that
    is how a test shows a service dying *between* sweeps). Everything goes
    through monkeypatch, so a test that fakes a death cannot leak it into the
    next one.
    """
    launchd = agent.launchd
    process = launchd.process
    calls = agent.calls
    sequence = {"index": 0}

    def _running():
        if isinstance(running, list):
            item = (running[sequence["index"]]
                    if sequence["index"] < len(running) else None)
            sequence["index"] += 1
            return item
        return running

    def _start():
        calls["start"] += 1
        return start

    def _stop(timeout=6.0):
        calls["stop"] += 1
        return True

    def _repair(state=None):
        names = sorted(n for n, ok in (state or {}).items() if not ok)
        calls["repair"].append(names)
        return names

    agent.monkey.setattr(process, "running", _running)
    agent.monkey.setattr(process, "start", _start)
    agent.monkey.setattr(process, "stop", _stop)
    agent.monkey.setattr(process, "repair", _repair)
    return calls


def fake_rest(agent, answers=True):
    """The REST corroboration as a counter: a list is consumed per call."""
    launchd = agent.launchd
    state = {"index": 0}

    def _rest(timeout=2.0):
        agent.calls["rest"] += 1
        if isinstance(answers, list):
            item = (answers[state["index"]]
                    if state["index"] < len(answers) else False)
            state["index"] += 1
            return item
        return bool(answers)

    agent.monkey.setattr(launchd, "rest_answers", _rest)


def installed_plist(agent, tick=None):
    """Write our agent through the module's own path (no hand-built plists)."""
    ok, lines = agent.launchd.install_plist(tick=tick)
    assert ok, lines
    return agent.launchd.plist_path()


def make_setup_home(agent):
    """A home that looks set up (a venv python), which `login on` insists on."""
    agent.paths.VENV_PY.parent.mkdir(parents=True, exist_ok=True)
    agent.paths.VENV_PY.write_text("#!/bin/sh\n")
    agent.paths.VENV_PY.chmod(0o755)


@pytest.fixture
def launchd(agent):
    """The module under test, bound to the throwaway home `agent` built."""
    return agent.launchd
