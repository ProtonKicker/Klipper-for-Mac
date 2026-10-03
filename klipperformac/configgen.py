"""Generates the initial user-editable configs under the data folder."""
import re

from . import paths

# Starter config = an upstream example copied verbatim (so it always parses
# against its own Klipper version), with our serial placeholder and the
# Moonraker-required extras appended if missing.
STARTER_EXAMPLE = "generic-rambo.cfg"

HEADER = """\
# Klipper for Mac starter config, derived from upstream {example} on {date}.
# Replace this whole file with the printer.cfg for your machine if you have
# one (see https://github.com/Klipper3d/klipper/tree/master/config).
# Tip: `klipperformac serial` lists connected USB printer boards.
#      `klipperformac presets` saves/swaps whole configs.

"""

EXTRAS = """
[virtual_sdcard]
path: {gcodes_dir}

[pause_resume]

[display_status]
"""

MOONRAKER_CFG = """\
# Klipper for Mac generated Moonraker config (macOS-tuned).
# Loopback-only by default; use `klipperformac lan on` to reach the stack
# from other devices on your network.

[server]
host: {host}
port: {moonraker_port}
klippy_uds_address: {uds_address}

[file_manager]
# macOS has no inotify; file watching is disabled (lists refresh on access
# and uploads still register).
file_system_observer: none

[machine]
# systemd/DBus service control does not exist on macOS; Klipper for Mac's CLI
# manages the processes instead.
provider: none

[authorization]
cors_domains:
    http://localhost:{web_port}
    http://127.0.0.1:{web_port}
    http://localhost:{fluidd_port}
    http://127.0.0.1:{fluidd_port}
trusted_clients:
    127.0.0.1
    ::1
"""

# Written verbatim into [authorization].trusted_clients when LAN access is
# on; these are the RFC1918 private ranges, so only a local network can talk
# to Moonraker without an API key.
LAN_CIDRS = ("192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12")


def apply_lan(enabled):
    """Sync the managed lines of an existing moonraker.conf with the LAN
    setting: [server] host + our CIDR entries in trusted_clients. Returns
    True when the file changed (Moonraker needs a restart)."""
    if not paths.MOONRAKER_CONF.exists():
        return False
    text = paths.MOONRAKER_CONF.read_text()
    want_host = "0.0.0.0" if enabled else "127.0.0.1"
    updated = re.sub(r"(?m)^host: \S+.*$", "host: " + want_host,
                     text, count=1)

    def _repl(m):
        items = [l for l in m.group(0).splitlines()[1:] if l.strip()]
        keep = [l for l in items if l.strip() not in LAN_CIDRS]
        if enabled:
            keep += ["    " + c for c in LAN_CIDRS]
        return "trusted_clients:\n" + "\n".join(keep) + "\n"

    updated = re.sub(r"trusted_clients:\n(?:[ \t]+\S[^\n]*\n)*", _repl,
                     updated, count=1)
    if updated != text:
        paths.MOONRAKER_CONF.write_text(updated)
        return True
    return False


def starter_cfg_text():
    import time
    src = paths.KLIPPER / "config" / STARTER_EXAMPLE
    if not src.exists():
        return None
    text = re.sub(r"serial: [^\n]*",
                  "serial: /dev/REPLACE_WITH_YOUR_PRINTER",
                  src.read_text(), count=1)
    extras = "" if "[virtual_sdcard]" in text else EXTRAS.format(
        gcodes_dir=str(paths.DATA / "gcodes"))
    return HEADER.format(example=STARTER_EXAMPLE,
                         date=time.strftime("%Y-%m-%d")) + text + extras


def sync_virtual_sdcard():
    """Keep [virtual_sdcard] path pointing into the current data folder."""
    if not paths.PRINTER_CFG.exists():
        return False
    text = paths.PRINTER_CFG.read_text()
    if "[virtual_sdcard]" not in text:
        return False
    desired = str(paths.DATA / "gcodes")
    updated = re.sub(
        r"(\[virtual_sdcard\]\s*\n\s*path:\s*)[^\n]*",
        lambda m: m.group(1) + desired,
        text, count=1)
    if updated != text:
        paths.PRINTER_CFG.write_text(updated)
        return True
    return False


def sync_moonraker_conf():
    """Keep moonraker.conf's klippy socket path in the current data folder."""
    if not paths.MOONRAKER_CONF.exists():
        return False
    text = paths.MOONRAKER_CONF.read_text()
    desired = str(paths.API_SOCKET)
    updated, n = re.subn(
        r"(klippy_uds_address:\s*)[^\n]*",
        lambda m: m.group(1) + desired,
        text)
    if n and updated != text:
        paths.MOONRAKER_CONF.write_text(updated)
        return True
    return False


def sync_data_paths():
    changed = sync_virtual_sdcard()
    changed = sync_moonraker_conf() or changed
    return changed


def ensure_configs():
    paths.CONFIG.mkdir(parents=True, exist_ok=True)
    (paths.DATA / "gcodes").mkdir(exist_ok=True)
    paths.PRESETS.mkdir(exist_ok=True)
    created = []
    if not paths.PRINTER_CFG.exists():
        text = starter_cfg_text()
        if text is None:
            raise SystemExit(
                "No starter example found in the Klipper checkout. Run "
                "'klipperformac setup' first, then 'klipperformac setup' again.")
        paths.PRINTER_CFG.write_text(text)
        created.append(paths.PRINTER_CFG)
    if not paths.MOONRAKER_CONF.exists():
        paths.MOONRAKER_CONF.write_text(
            MOONRAKER_CFG.format(
                host="0.0.0.0" if paths.lan_enabled() else "127.0.0.1",
                moonraker_port=paths.MOONRAKER_PORT,
                uds_address=str(paths.API_SOCKET),
                web_port=paths.WEB_PORT,
                fluidd_port=paths.FLUIDD_PORT,
            )
        )
        if paths.lan_enabled():
            apply_lan(True)
        created.append(paths.MOONRAKER_CONF)
    return created
