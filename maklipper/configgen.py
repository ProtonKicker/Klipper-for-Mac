"""Generates the initial user-editable configs under KlipperData/config."""
import re

from . import paths

# Starter config = an upstream example copied verbatim (so it always parses
# against its own Klipper version), with our serial placeholder and the
# Moonraker-required extras appended if missing.
STARTER_EXAMPLE = "generic-rambo.cfg"

HEADER = """\
# MaKlipper starter config, derived from upstream {example} on {date}.
# Replace this whole file with the printer.cfg for your machine if you have
# one (see https://github.com/Klipper3d/klipper/tree/master/config).
# Tip: `maklipper serial` lists connected USB printer boards.

"""

EXTRAS = """
[virtual_sdcard]
path: {gcodes_dir}

[pause_resume]

[display_status]
"""


def starter_cfg_text():
    import time
    src = paths.KLIPPER / "config" / STARTER_EXAMPLE
    if src.exists():
        text = re.sub(r"serial: [^\n]*",
                      "serial: /dev/REPLACE_WITH_YOUR_PRINTER",
                      src.read_text(), count=1)
        extras = "" if "[virtual_sdcard]" in text else EXTRAS.format(
            gcodes_dir=str(paths.DATA / "gcodes"))
        return HEADER.format(example=STARTER_EXAMPLE,
                             date=time.strftime("%Y-%m-%d")) + text + extras
    return None

PRINTER_CFG = """\
# MaKlipper generated starter config (generic RAMPS-style cartesian).
# Replace with the printer.cfg for your machine (see the Klipper example
# configs: https://github.com/Klipper3d/klipper/tree/master/config),
# then set the serial line below to your printer board.
# Tip: `maklipper serial` lists connected USB printer boards.

[mcu]
serial: /dev/REPLACE_WITH_YOUR_PRINTER

[printer]
kinematics: cartesian
max_velocity: 300
max_accel: 3000
max_z_velocity: 5
max_z_accel: 100

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

[stepper_y]
step_pin: PF6
dir_pin: !PF7
enable_pin: !PF2
microsteps: 16
rotation_distance: 40
endstop_pin: ^PJ1
position_endstop: 0
position_max: 200
homing_speed: 50

[stepper_z]
step_pin: PL3
dir_pin: PL1
enable_pin: !PK0
microsteps: 16
rotation_distance: 8
endstop_pin: ^PD3
position_endstop: 0
position_max: 200

[extruder]
step_pin: PA4
dir_pin: PA6
enable_pin: !PA2
microsteps: 16
rotation_distance: 33.500
nozzle_diameter: 0.400
filament_diameter: 1.750
heater: extruder
control: watermark
max_temp: 260
sensor_type: ATC Semitec 104GT-2
sensor_pin: PK7
min_temp: 0

[heater_bed]
heater_pin: PA3
control: watermark
max_temp: 110
sensor_type: ATC Semitec 104GT-2
sensor_pin: PK4
min_temp: 0

[heater_fan hotend_fan]
pin: PL5
heater: extruder
heater_temp: 50.0

[fan]
pin: PH5

[virtual_sdcard]
path: {gcodes_dir}

[pause_resume]

[display_status]
"""

MOONRAKER_CFG = """\
# MaKlipper generated Moonraker config (macOS-tuned).

[server]
host: 0.0.0.0
port: {moonraker_port}
klippy_uds_address: {uds_address}

[file_manager]
# macOS has no inotify; use polling so file watching still works.
file_system_observer: polling

[data_store]
history_store_duration: 31

[authorization]
cors_domains:
    http://localhost:{web_port}
    http://127.0.0.1:{web_port}
trusted_clients:
    127.0.0.1
    ::1
"""


def ensure_configs():
    paths.CONFIG.mkdir(parents=True, exist_ok=True)
    (paths.DATA / "gcodes").mkdir(exist_ok=True)
    created = []
    if not paths.PRINTER_CFG.exists():
        text = starter_cfg_text() or PRINTER_CFG.format(
            gcodes_dir=str(paths.DATA / "gcodes"))
        paths.PRINTER_CFG.write_text(text)
        created.append(paths.PRINTER_CFG)
    if not paths.MOONRAKER_CONF.exists():
        paths.MOONRAKER_CONF.write_text(
            MOONRAKER_CFG.format(
                moonraker_port=paths.MOONRAKER_PORT,
                uds_address=str(paths.API_SOCKET),
                web_port=paths.WEB_PORT,
            )
        )
        created.append(paths.MOONRAKER_CONF)
    return created
