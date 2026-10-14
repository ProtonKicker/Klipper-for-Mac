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

# The Mac camera's section. Key spellings follow Moonraker's own webcam
# component ([webcam <name>], service/enabled/stream_url/snapshot_url); an
# unknown key would make Moonraker warn at startup, so keep this minimal.
WEBCAM_NAME = "maccam"

WEBCAM_SECTION = """\
[webcam {name}]
# Managed by Klipper for Mac (klipperformac cam on/off). The camera only
# captures while it is on.
enabled: {enabled}
service: mjpegstreamer
location: printer
stream_url: http://localhost:{port}/?action=stream
snapshot_url: http://localhost:{port}/?action=snapshot
"""


def webcam_section_text(name=WEBCAM_NAME, enabled=True, port=None):
    return WEBCAM_SECTION.format(
        name=name, enabled="True" if enabled else "False",
        port=int(port or paths.CAM_PORT))


def _webcam_span(text, name):
    """(start, end) offsets of [webcam <name>] in the config text, or None.
    The span runs to the next section header (or end of file). The header is
    allowed to be the very last line of the file, newline or no newline: a
    missed match here would append a second [webcam <name>] and Moonraker
    refuses to start on a duplicate section."""
    head = re.search(r"(?im)^[ \t]*\[webcam[ \t]+" + re.escape(name) +
                     r"[ \t]*\][ \t]*(?:\r?\n|$)", text)
    if head is None:
        return None
    rest = text[head.end():]
    nxt = re.search(r"(?m)^[ \t]*\[", rest)
    return head.start(), head.end() + (nxt.start() if nxt else len(rest))


def _join(*chunks):
    """Glue config text chunks apart with exactly one blank line."""
    out = ""
    for part in (c.strip("\n") for c in chunks):
        if not part:
            continue
        out = part if not out else out + "\n\n" + part
    return out + "\n" if out else ""


def _write_conf(text):
    """Write-then-replace: a crash mid-write cannot truncate the config."""
    tmp = paths.MOONRAKER_CONF.with_suffix(".conf.tmp")
    tmp.write_text(text)
    tmp.replace(paths.MOONRAKER_CONF)


def webcam_state(name=WEBCAM_NAME):
    """(present, enabled) for the managed section. Read-only."""
    if not paths.MOONRAKER_CONF.exists():
        return False, False
    text = paths.MOONRAKER_CONF.read_text()
    span = _webcam_span(text, name)
    if span is None:
        return False, False
    enabled = bool(re.search(
        r"(?im)^enabled[ \t]*:[ \t]*(true|1|yes)[ \t]*$",
        text[span[0]:span[1]]))
    return True, enabled


def ensure_webcam_section(name=WEBCAM_NAME, enabled=True, port=None):
    """Upsert the managed [webcam <name>] section in moonraker.conf. Returns
    True when the file changed (Moonraker reads it at startup)."""
    if not paths.MOONRAKER_CONF.exists():
        return False
    text = paths.MOONRAKER_CONF.read_text()
    section = webcam_section_text(name, enabled, port)
    span = _webcam_span(text, name)
    updated = (_join(text[:span[0]], section, text[span[1]:]) if span
               else _join(text, section))
    if updated == text:
        return False
    _write_conf(updated)
    return True


def remove_webcam_section(name=WEBCAM_NAME):
    """Drop the managed section entirely (other sections are left alone)."""
    if not paths.MOONRAKER_CONF.exists():
        return False
    text = paths.MOONRAKER_CONF.read_text()
    span = _webcam_span(text, name)
    if span is None:
        return False
    updated = _join(text[:span[0]], text[span[1]:])
    if updated == text:
        return False
    _write_conf(updated)
    return True


def set_webcam_enabled(enabled, name=WEBCAM_NAME):
    """Flip the `enabled:` line of the managed section and nothing else: the
    camera options a user added there (target_fps, flip_horizontal, ...) and
    the stream URLs must survive every `klipperformac cam off`. Never adds a
    section: turning the camera off should not create config that was not
    there before."""
    if not paths.MOONRAKER_CONF.exists():
        return False
    text = paths.MOONRAKER_CONF.read_text()
    span = _webcam_span(text, name)
    if span is None:
        return False
    value = "True" if enabled else "False"
    body, hits = re.subn(r"(?im)^(enabled[ \t]*:[ \t]*)[^\r\n]*?(\r?)$",
                         lambda m: m.group(1) + value + m.group(2),
                         text[span[0]:span[1]], count=1)
    if not hits:
        # The managed line was edited away by hand: put it back under the
        # section header, still leaving the rest of the section alone.
        cut = re.match(r"[^\r\n]*(?:\r\n|\n|$)", body).end()
        eol = "\r\n" if body[:cut].endswith("\r\n") else "\n"
        body = body[:cut].rstrip("\r\n") + eol + "enabled: " + value + eol \
            + body[cut:]
    updated = text[:span[0]] + body + text[span[1]:]
    if updated == text:
        return False
    _write_conf(updated)
    return True


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
