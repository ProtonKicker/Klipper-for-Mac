"""The cli-surface tier for multi-MCU: `serial`, `serial --json`, `--set PATH
MCUNAME`, auto_serial, doctor/selftest per-MCU, and `presets --import` warnings.

Everything here is OFFLINE and READ-ONLY against the machine: no /dev node is
opened, no `ioreg` is run (detect.scan and detect.all_identities are handed
fixed facts), no stack is restarted, and the bench SKR Pico is never reset. The
one test that talks to the running stack is marked `live` and only queries
read-only Moonraker endpoints.

Two worlds are pinned, because the CLI must work in both:
  * mcus.py present (wave 3 landed) - the real reader/writer underneath;
  * mcus.py absent or wrong - the CLI's own parse and detect.write_serial, so a
    missing module is never a traceback in a read-only command.
`stub_mcus` fakes the module through sys.modules-free monkeypatching of
cli._mcus_module(), which is the single seam every call goes through.
"""
import argparse
import importlib
import types

import pytest


MASTER = "/dev/cu.usbmodem14101"
TOOL_NODE = "/dev/cu.URT0"


def serial_ns(**kw):
    """The Namespace argparse builds for `serial [--set PATH] [--auto] [--json]
    [MCUNAME]`, so a test can call the handler directly."""
    base = dict(set=None, auto=False, json=False, mcu=None)
    base.update(kw)
    return argparse.Namespace(**base)


def preset_args(**kw):
    base = dict(library=False, use_library=None, history=False, restore=None,
                snapshot=None, save=None, use=None, import_=None,
                import_name=None, auto_serial=False)
    base.update(kw)
    return argparse.Namespace(**base)


def identity_row(path, vid_pid="1D50:614E", serial="5044506110A45B1C",
                 name="rp2040", vendor="Klipper", location="14100000"):
    """One detect.identity() dict (the bench SKR Pico's real shape).

    The key is detect's own format - 'usb:<vid>:<pid>:<USB serial>' - because
    that string is what a '# klipperformac:identity' marker in printer.cfg has
    to match, and a test helper that invented a different one would test
    nothing.
    """
    return {"vid_pid": vid_pid, "serial": serial, "name": name,
            "path": path, "vendor": vendor, "location": location,
            "ioservice_path": "Root/IOService/{}".format(path),
            "key": "usb:{}:{}".format(vid_pid.lower(), serial)}


MASTER_KEY = "usb:1d50:614e:5044506110A45B1C"
TOOL_KEY = "usb:1a86:7523:00A1B2C3"


CFG_SINGLE = """[mcu]
serial: /dev/cu.usbmodem14101
baud: 250000

[stepper_x]
step_pin: P2.1
"""

CFG_MULTI = """[mcu]
serial: /dev/cu.usbmodem14101
baud: 250000

[mcu tool]
# klipperformac:identity usb:1a86:7523:00A1B2C3
baud: 250000

[stepper_x]
step_pin: P2.1
"""

CFG_MULTI_SET = """[mcu]
serial: /dev/cu.usbmodem14101
baud: 250000

[mcu tool]
# klipperformac:identity usb:1a86:7523:00A1B2C3
serial: /dev/cu.URT0
baud: 250000

[stepper_x]
step_pin: P2.1
"""


@pytest.fixture
def facts():
    """The builders above as one namespace.

    A fixture and not `from conftest import ...` for the same reason
    tests/identity/conftest.py does it: several feature dirs own a conftest.py
    and pytest keys them by module name, so importing one by name is ambiguous
    when the whole tree runs together.
    """
    return types.SimpleNamespace(
        identity_row=identity_row, serial_ns=serial_ns,
        preset_args=preset_args, CFG_SINGLE=CFG_SINGLE, CFG_MULTI=CFG_MULTI,
        CFG_MULTI_SET=CFG_MULTI_SET, MASTER_KEY=MASTER_KEY, TOOL_KEY=TOOL_KEY,
        MASTER=MASTER, TOOL_NODE=TOOL_NODE)


@pytest.fixture
def cstack(stack):
    """`stack` plus the lazily-imported modules cmd_serial reaches for."""
    stack.flash = importlib.reload(
        importlib.import_module("klipperformac.flash"))
    try:
        stack.mcus = importlib.reload(
            importlib.import_module("klipperformac.mcus"))
    except Exception:
        stack.mcus = None
    return stack


@pytest.fixture
def attached(cstack, monkeypatch, tmp_path):
    """Fake the two hardware reads: which nodes exist, and what is behind them.

    `attached(ports, identities)` hands `detect.scan()` and
    `detect.all_identities()` those exact strings, so no test opens a port, runs
    ioreg, or depends on what is plugged into this Mac right now.

    `existing=[...]` additionally creates real files for the paths a test wants
    `os.path.exists()` to accept (the `--set` gate) and returns {name: path};
    a test cannot invent a /dev/cu.* node, and it must not use the bench's real
    one either.
    """
    def _attach(ports=(), identities=(), existing=()):
        rows = [{"path": path,
                 "description": cstack.detect._describe(path)} for path in ports]
        monkeypatch.setattr(cstack.detect, "scan",
                            lambda: [dict(row) for row in rows])
        monkeypatch.setattr(cstack.detect, "all_identities",
                            lambda *a, **k: [dict(i) for i in identities])
        nodes = {}
        for name in existing:
            real = tmp_path / name.rsplit("/", 1)[-1]
            real.write_text("")
            nodes[name] = str(real)
        return nodes
    return _attach


@pytest.fixture
def no_mcus(cstack, monkeypatch):
    """Pretend wave 3 has not landed: every mcus call degrades."""
    monkeypatch.setattr(cstack.cli, "_mcus_module", lambda: None)
    return cstack


@pytest.fixture
def stub_mcus(cstack, monkeypatch):
    """Install a fake mcus module (the contract's four functions).

    `stub_mcus(list_mcus=..., validate=..., set_serial=...)` - anything not
    given is absent, which is how a half-finished wave 3 behaves.
    """
    def _install(**functions):
        mod = types.ModuleType("klipperformac.mcus")
        for name, fn in functions.items():
            setattr(mod, name, fn)
        monkeypatch.setattr(cstack.cli, "_mcus_module", lambda: mod)
        return mod
    return _install


@pytest.fixture
def printer_cfg(cstack):
    def _write(text):
        cstack.paths.PRINTER_CFG.write_text(text)
        return text
    return _write
