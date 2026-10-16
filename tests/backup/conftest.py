"""Offline fixtures for `klipperformac backup`.

Everything here runs against the throwaway KLIPPERFORMAC_HOME/KLIPPERFORMAC_DATA
the root tests/conftest.py `stack` fixture already builds, and no test touches
the live stack, the network, or a printer. `backup` is not in that fixture's
reload list (it is a new module), so it is reloaded here onto the same reloaded
paths object, which is how the module ends up bound to the tmp home.
"""
import gzip
import importlib
import io
import json
import tarfile
import time
import types

import pytest

PRINTER_CFG = """\
# Test printer config.
[printer]
kinematics: cartesian
max_velocity: 300
max_accel: 3000

[mcu]
serial: /dev/cu.usbserial-110

[stepper_x]
step_pin: PF0
dir_pin: PF1
enable_pin: !PD7
microsteps: 16
rotation_distance: 40
endstop_pin: ^PE5
position_endstop: 0
position_max: 200
homing_speed: 50

[virtual_sdcard]
path: {gcodes}

[pause_resume]

[display_status]

[gcode_macro PARK]
gcode:
    G90
    G1 X10 F1000
"""


@pytest.fixture
def bstack(stack):
    """The reloaded `stack` plus a data folder that looks like a used install."""
    backup = importlib.reload(importlib.import_module("klipperformac.backup"))
    stack.backup = backup
    populate(stack)
    return stack


def populate(stack, *, logs=True, database=True, misc=True, settings=True,
             lockfile=True, web=True, run=True):
    """Build the folder shape a real install has, and return it.

    Deliberately every folder the inclusion list names, plus the ones it
    refuses (web/, run/) - so a test can assert on both sides of the list."""
    paths = stack.paths
    paths.CONFIG.mkdir(parents=True, exist_ok=True)
    paths.PRESETS.mkdir(parents=True, exist_ok=True)
    (paths.CONFIG / "history").mkdir(parents=True, exist_ok=True)
    (paths.DATA / "gcodes").mkdir(parents=True, exist_ok=True)
    if run:
        paths.RUN.mkdir(parents=True, exist_ok=True)
        (paths.RUN / "klippy.sock").write_text("not a socket, just junk")
        (paths.RUN / "state.json").write_text("{}\n")
    if logs:
        paths.LOGS.mkdir(parents=True, exist_ok=True)
        (paths.LOGS / "klippy.log").write_text("klippy started\n")
        (paths.LOGS / "moonraker.log").write_text("moonraker started\n")
    if database:
        (paths.DATA / "database").mkdir(parents=True, exist_ok=True)
        (paths.DATA / "database" / "moonraker_db.sqlite").write_bytes(
            b"SQLite format 3\x00fake")
    if misc:
        (paths.DATA / "misc").mkdir(parents=True, exist_ok=True)
        (paths.DATA / "misc" / "templates").mkdir(parents=True, exist_ok=True)
        (paths.DATA / "misc" / "templates" / "t.gcode").write_text("M105\n")
    if web:
        paths.WEB_MAINSAIL.mkdir(parents=True, exist_ok=True)
        (paths.WEB_MAINSAIL / "index.html").write_text("<html>23 MB of JS"
                                                       "</html>\n")
    paths.PRINTER_CFG.write_text(PRINTER_CFG.format(
        gcodes=str(paths.DATA / "gcodes")))
    # The generated Moonraker config, verbatim: it is what a real backup carries
    # and it is the shape _config_problems() has to accept.
    paths.MOONRAKER_CONF.write_text(stack.configgen.MOONRAKER_CFG.format(
        host="127.0.0.1", moonraker_port=paths.MOONRAKER_PORT,
        uds_address=str(paths.API_SOCKET), web_port=paths.WEB_PORT,
        fluidd_port=paths.FLUIDD_PORT))
    (paths.PRESETS / "voron2.cfg").write_text("[printer]\nmax_velocity: 500\n")
    (paths.CONFIG / "history" / "printer-20260101-000000-manual.cfg").write_text(
        "[printer]\nmax_velocity: 200\n")
    (paths.DATA / "gcodes" / "cube.gcode").write_text("G28\nG1 X10\n")
    if settings:
        paths.APP_HOME.mkdir(parents=True, exist_ok=True)
        paths.save_settings({"lan": False, "data": str(paths.DATA)})
    if lockfile:
        paths.LOCKFILE.write_text(json.dumps({"ui": "fluidd",
                                              "klipper": "v0.12.0"}) + "\n")
    return paths.DATA


# ------------------------------------------------------- archive factories --

def tarinfo(name, kind="dir", size=0, mode=0o644, link="", mtime=None):
    info = tarfile.TarInfo(name)
    info.mtime = int(time.time()) if mtime is None else mtime
    info.mode = mode
    if kind == "dir":
        info.type = tarfile.DIRTYPE
    elif kind == "sym":
        info.type = tarfile.SYMTYPE
        info.linkname = link
        info.mode = 0o777
    elif kind == "lnk":
        info.type = tarfile.LNKTYPE
        info.linkname = link
    elif kind == "fifo":
        info.type = tarfile.FIFOTYPE
    else:
        info.type = tarfile.REGTYPE
        info.size = size
    return info


def build(members, gz=True, manifest=None):
    """Bytes of a .tar.gz (or a bare tar) holding exactly `members`.

    `members` is [(name, kind, payload[, linkname])]; this is how the hostile
    archives are written - by hand, with names and types no `backup create`
    would ever emit. kind is "dir", "reg", "sym", "lnk" or "fifo"."""
    buf = io.BytesIO()
    raw = tarfile.open(fileobj=buf, mode="w")
    if manifest is not None:
        blob = json.dumps(manifest).encode()
        raw.addfile(tarinfo("manifest.json", "reg", len(blob)), io.BytesIO(blob))
    for entry in members:
        name, kind, payload = entry[0], entry[1], entry[2]
        link = entry[3] if len(entry) > 3 else ""
        if kind == "dir":
            raw.addfile(tarinfo(name, "dir"))
            continue
        blob = payload if isinstance(payload, bytes) else payload.encode()
        info = tarinfo(name, kind, len(blob) if kind == "reg" else 0, link=link)
        raw.addfile(info, io.BytesIO(blob) if kind == "reg" else None)
    raw.close()
    return buf.getvalue() if not gz else _gz(buf.getvalue())


def _gz(raw):
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(raw)
    return out.getvalue()


def write_backup(data, name, blob):
    """Put `blob` in DATA/backups as `name` and hand back the path."""
    where = data / "backups"
    where.mkdir(parents=True, exist_ok=True)
    path = where / name
    path.write_bytes(blob)
    return path


def names_in(path):
    with tarfile.open(str(path), "r:gz") as tf:
        return [m.name for m in tf.getmembers()]


def manifest_of(path):
    with tarfile.open(str(path), "r:gz") as tf:
        for member in tf:
            if member.name == "manifest.json":
                return json.loads(tf.extractfile(member).read())
    return None


def fake_stack(stack, monkeypatch, **which):
    """Pretend the stack is up. process.running() proves liveness with ps, which
    no offline test can fake, so the gate is fed at the seam it reads from."""
    state = {"web_mainsail": False, "web_fluidd": False, "moonraker": False,
             "klipper": False, "caffeinate": False}
    state.update(which or {"klipper": True, "moonraker": True})
    monkeypatch.setattr(stack.process, "running", lambda: state)
    return state


@pytest.fixture
def tools():
    """The helpers as a fixture.

    tests/ has no __init__.py (pytest's prepend import mode), so a test module
    cannot `from .conftest import ...` - this is how the archive factories reach
    the tests."""
    return types.SimpleNamespace(
        PRINTER_CFG=PRINTER_CFG, populate=populate, tarinfo=tarinfo,
        build=build, write_backup=write_backup, names_in=names_in,
        manifest_of=manifest_of, fake_stack=fake_stack)
