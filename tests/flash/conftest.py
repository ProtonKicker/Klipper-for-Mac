"""Offline fixtures for `klipperformac flash`.

Everything runs against the throwaway KLIPPERFORMAC_HOME/KLIPPERFORMAC_DATA the
root tests/conftest.py `stack` fixture builds, with four extra seams, because a
flasher's whole job is side effects this machine must not see:

  * `KLIPPERFORMAC_VOLUMES_DIR` points at a temp folder, so no test can wait on,
    write to, or eject a volume this Mac actually has;
  * `process.running()` is fed at the seam flash reads it (the real one proves
    liveness with `ps`, which no offline test can fake);
  * `_run`/`_run_bytes`/`find_tool` are records, never children - the exact argv
    of a `diskutil`/`dfu-util`/venv-python call is what these tests pin, and
    nothing may be executed on the way there;
  * `port_present()` is faked, because a test cannot create a /dev node.

The byte factories live in flashbytes.py next to this file (loaded by path, so
it can never collide with another conftest in the suite) and re-declare the UF2
constants rather than importing them from the module under test.
"""
import importlib
import importlib.util
import os
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "flashbytes", os.path.join(_HERE, "flashbytes.py"))
flashbytes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(flashbytes)


@pytest.fixture
def uf2bytes():
    """The UF2/vector-table/ioreg builders and the Clock/Recorder seams."""
    return flashbytes


@pytest.fixture
def fstack(stack, monkeypatch):
    """The reloaded `stack` plus flash.py, a fake /Volumes, and no children."""
    flash = importlib.reload(importlib.import_module("klipperformac.flash"))
    stack.flash = flash
    # The second copy of every magic number, so a test can insist flash.py and
    # upstream agree without either one defining the other's expectation.
    stack.uf2b = types.SimpleNamespace(
        MAGIC0=flashbytes.UF2_MAGIC_START0, MAGIC1=flashbytes.UF2_MAGIC_START1,
        MAGICEND=flashbytes.UF2_MAGIC_END, BLOCK=flashbytes.UF2_BLOCK_SIZE,
        PAYLOAD_AREA=flashbytes.UF2_PAYLOAD_AREA,
        FAMILY_RP2040=flashbytes.RP2040_FAMILY_ID,
        FAMILY_RP2350=flashbytes.RP2350_FAMILY_ID,
        FLAG_FAMILY=flashbytes.UF2_FLAG_FAMILY_ID_PRESENT,
        FLAG_CONTAINER=flashbytes.UF2_FLAG_FILE_CONTAINER)
    volumes = stack.home / "volumes"
    volumes.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KLIPPERFORMAC_VOLUMES_DIR", str(volumes))
    stack.volumes = volumes

    def make_volume(name, bootsel=True):
        """Mount a volume inside the fake /Volumes. bootsel=True gives it the
        INFO_UF2.TXT a real BOOTSEL ROM publishes at its root."""
        mount = volumes / name
        mount.mkdir(parents=True, exist_ok=True)
        if bootsel:
            (mount / "INFO_UF2.TXT").write_text(
                "UF2 Bootloader 0.30 lib/nanopb...\n")
        return mount

    stack.make_volume = make_volume

    def running(**which):
        """Pretend the stack is up (default: klipper + moonraker)."""
        state = {name: False for name in
                 ("web_mainsail", "web_fluidd", "moonraker", "klipper",
                  "caffeinate")}
        state.update(which or {"klipper": True, "moonraker": True})
        monkeypatch.setattr(stack.process, "running", lambda: state)
        return state

    stack.running = running

    def down():
        """Pretend nothing is tracked - the state every mutating flow demands."""
        monkeypatch.setattr(stack.process, "running", lambda: None)

    stack.down = down

    def artifacts(name="klipper.uf2", blob=None, size=None):
        """Write a firmware file under the throwaway home; return its path."""
        where = stack.paths.APP_HOME / "firmware"
        where.mkdir(parents=True, exist_ok=True)
        path = where / name
        data = blob if blob is not None else flashbytes.uf2_image()
        if size is not None:
            data = data[:size] if size < len(data) else data + b"\x00" * (
                size - len(data))
        path.write_bytes(data)
        return str(path)

    stack.artifacts = artifacts

    def use_recorder(replies=None, as_bytes=False):
        """Replace flash._run (or _run_bytes) with a Recorder."""
        rec = flashbytes.Recorder(replies, as_bytes=as_bytes)
        monkeypatch.setattr(flash, "_run_bytes" if as_bytes else "_run", rec)
        return rec

    stack.use_recorder = use_recorder

    def tools(*names):
        """Only these flashers 'exist' as far as find_tool() is concerned."""
        wanted = set(names)
        monkeypatch.setattr(flash, "find_tool", lambda name: (
            "/usr/local/bin/" + name if name in wanted else None))
        return wanted

    stack.tools = tools

    def port_present(value=True):
        """Which /dev/cu.* nodes the flow believes are there."""
        monkeypatch.setattr(flash, "port_present", lambda path: bool(value))

    stack.port_present = port_present

    def fake_clock():
        """flash.py's `time` becomes a Clock: waits are instant and countable."""
        clock = flashbytes.Clock()
        monkeypatch.setattr(flash, "time", clock)
        return clock

    stack.fake_clock = fake_clock
    return stack
