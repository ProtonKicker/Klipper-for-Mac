"""klipperformac backup - whole-data archives, and the only way back in.

`klipperformac presets --snapshot` version-tracks one file (printer.cfg). When a
Mac dies, is reinstalled, or has its data folder moved, one config version is
not enough: the presets, the config history, the uploaded gcode, the Moonraker
database, the installer pins and the user's settings all live next to it. This
module archives the data folder as a whole into DATA/backups/*.tar.gz and can
put it back.

WHAT A BACKUP CARRIES (the inclusion list - a whitelist, never a blacklist)
  DATA/config/            printer.cfg, moonraker.conf, presets/, history/, and
                          everything else Moonraker's config API serves
  DATA/gcodes/            uploaded print files ([virtual_sdcard] path)
  DATA/database/          Moonraker's own data folder (sqlite jobs/print history)
  DATA/misc/              Moonraker's misc folder (templates, saved queues)
  APP_HOME/settings.json  data-folder pointer, cam/lan prefs (paths.SETTINGS)
  APP_HOME/lockfile.json  the upstream pins `setup` wrote (paths.LOCKFILE)

WHAT IT DELIBERATELY LEAVES OUT
  DATA/web/        the Mainsail + Fluidd bundles (tens of MB of minified JS the
                   installer re-fetches: installer.fetch_webui). Archiving them
                   makes every backup huge and every restore redundant.
  DATA/run/        sockets, pids, state.json. A unix socket copied into a tar is
                   junk, and none of it survives a reboot anyway.
  DATA/logs/       klippy.log/moonraker.log/klippacam.log only with --with-logs:
                   the biggest thing here, they grow forever, and a log is never
                   the reason you restore.
  DATA/backups/    itself, or a backup would archive its own archive.
  DATA/timelapse/  rendered media (loose jpg/mp4 the user copies by hand); not
                   config, and not needed to boot the printer.
  DATA/certs/, DATA/comms/, DATA/.moonraker.uuid, DATA/moonraker.asvc and the
                   rest of the DATA root: all of it is generated on first use
                   (certs), rebuilt by klippy on restart (comms/*.persist), or a
                   machine identity Moonraker writes again when it is missing
                   (.moonraker.uuid, moonraker.asvc). None of it is a setting a
                   user chose, so none of it is the reason a backup exists.

Anything outside the whitelist is never even looked at, which is why a new
folder appearing under DATA can neither silently bloat a backup nor silently
break one.

ARCHIVE LAYOUT - one top-level prefix per destination root, so a restore never
has to guess where a member belongs:

  manifest.json         our own metadata (see _manifest()); missing on an archive
                        made by hand, which `verify` reports
  data/<rel>            -> DATA/<rel>
  app/<name>            -> APP_HOME/<name>

UPSTREAM STAYS PRISTINE: nothing here reads or writes anything under
APP_HOME/klipper, APP_HOME/moonraker or DATA/web. The restore path only copies
into the data folder and APP_HOME, write-then-replace, one file at a time.
"""
import configparser
import gzip
import io
import json
import os
import re
import shutil
import stat as statmod
import sys
import tarfile
import tempfile
import time
import zlib
from pathlib import Path

from . import __version__, paths

# Ten is cli.HISTORY_KEEP (how many config versions we keep); 255 bytes is
# installer._MAX_NAME (what one macOS path component can hold).
DEFAULT_KEEP = 10
MAX_COMPONENT_BYTES = 255
# A printer.cfg/moonraker.conf/settings.json is a few kB. Reading one out of an
# archive to validate it is only safe with a cap, or a "backup" naming a 2 GB
# file printer.cfg would be verified by loading it into memory.
MAX_VALIDATE_BYTES = 8 * 1024 * 1024

PREFIX = "klipperformac-backup-"
SUFFIX = ".tar.gz"
# create() always names backups like this, and prune() only ever deletes files
# named like this - so a foreign file (or folder) in DATA/backups is untouchable.
NAME_RE = re.compile(r"^klipperformac-backup-\d{8}-\d{6}(-\d{1,3})?\.tar\.gz$")
STAMP_RE = re.compile(r"^klipperformac-backup-(\d{8}-\d{6})(?:-(\d{1,3}))?"
                      r"\.tar\.gz$")

MANIFEST_NAME = "manifest.json"
DATA_PREFIX = "data"
APP_PREFIX = "app"
BACKUPS_NAME = "backups"
# The tree a restore stages into, inside DATA/backups: the leading dot keeps it
# out of `list`, and it never matches NAME_RE, so `prune` cannot delete it.
STAGE_PREFIX = ".restore-"
PARTIAL_SUFFIX = ".partial"

PAYLOAD_DIRS = ("config", "gcodes", "database", "misc")
LOGS_DIR = "logs"
# Under DATA/config: the two files the stack boots from. A backup missing either
# one is a backup that cannot restore, and has to say so.
REQUIRED_CONFIG = ("printer.cfg", "moonraker.conf")

_CHUNK = 1 << 20

# Everything a damaged archive can raise on the read side, and all of it becomes
# one clean line: a truncated file (EOFError), a bad gzip header or CRC (OSError),
# a byte flipped inside the deflate stream (zlib.error - not an OSError, and the
# one that used to escape as a traceback), a corrupt tar header (TarError).
_READ_ERRORS = (OSError, EOFError, zlib.error, tarfile.TarError)


def _fail(msg):
    """One clean line on stderr + exit 1 (cli._die's shape, kept local so this
    module never has to import cli.py at module scope)."""
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def _why(exc):
    return getattr(exc, "strerror", None) or str(exc)


def _short(name, limit=72):
    """A member name in a message, bounded: the hostile archive with a 100 KB name
    must not print a 100 KB line (that is the same bug from the other side)."""
    if len(name) <= limit:
        return "'{}'".format(name)
    return "'{}' ... ({} characters)".format(name[:limit], len(name))


def backups_dir():
    """DATA/backups as a Path. Created only by the commands that write."""
    return paths.DATA / BACKUPS_NAME


def _human(num):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return "{} {}".format(int(num) if unit == "B" else round(num, 1),
                                  unit)
        num = num / 1024.0


def _remove(path):
    try:
        Path(path).unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- gates -----

def _require_data(what):
    """Before archiving from - or restoring over - the data folder, it has to be
    readable and actually be ours.

    paths.protected_data_dir()/looks_like_data_dir() exist for exactly this: a
    junk settings.json value or a stray KLIPPERFORMAC_DATA can point the whole
    tool at ~/Documents, and `backup restore` would overwrite whatever it finds
    there. The alternative to refusing is a tar of somebody's photo library, or
    a restore into a folder this install never made."""
    data = paths.DATA
    if not data.is_absolute():
        _fail("The data folder is not an absolute path: {} - refusing to {} "
              "using it. Point it at a full path: klipperformac data --set PATH"
              .format(data, what))
    if paths.protected_data_dir(data):
        _fail("The data folder is set to {}, which is too important a folder to "
              "hold printer data - refusing to {} it. Choose a folder of its "
              "own: klipperformac data --set PATH".format(data, what))
    if data.is_symlink():
        _fail("The data folder {} is a symlink; refusing to {} through it."
              .format(data, what))
    if not data.is_dir():
        _fail("No data folder at {} - nothing to {}. Run: klipperformac setup"
              .format(data, what))
    if not os.access(str(data), os.R_OK | os.X_OK):
        _fail("Cannot read the data folder {} (permissions?) - nothing to {}."
              .format(data, what))
    if not paths.looks_like_data_dir(data):
        _fail("{} does not look like a Klipper-for-Mac data folder (no config/, "
              "gcodes/, logs/, ... in it) - refusing to {}. If your data lives "
              "elsewhere: klipperformac data --set PATH".format(data, what))
    return data


def _live_services():
    """Which real services are up ([] when the stack was never started, or when
    only caffeinate is - `cmd_doctor` filters that one out the same way)."""
    from . import process
    state = process.running()
    if not state:
        return []
    return sorted(name for name in state if state[name] and name != "caffeinate")


def _gate_for_create(allow_running):
    """Moonraker keeps DATA/database/moonraker_db.sqlite open: copying a live
    sqlite file captures whatever pages happen to be on disk, which can restore
    as a half database. So `create` wants the stack down, and --allow-running is
    the honest escape hatch for someone mid-print who needs everything else."""
    live = _live_services()
    if not live:
        return
    if not allow_running:
        _fail("The stack is running ({}) and Moonraker holds its database open, "
              "so a backup taken now can carry a half-written sqlite file. Stop "
              "it (klipperformac down), or pass --allow-running to take the "
              "snapshot anyway.".format(", ".join(live)))
    print("[!] The stack is running ({}) - the copy of DATA/database/ may be "
          "inconsistent.".format(", ".join(live)))


def _gate_for_restore():
    """No --allow-running here. Swapping printer.cfg/moonraker.conf under a
    running klippy/moonraker leaves two truths - the file on disk and the config
    in memory - and the printer moves according to the one nobody can see."""
    live = _live_services()
    if live:
        _fail("The stack is running ({}) - restore replaces the configs it has "
              "open. Stop it first: klipperformac down".format(", ".join(live)))


def _make_backups_dir():
    d = backups_dir()
    if d.is_symlink():
        _fail("Refusing to use {} for backups: that path is a symlink.".format(d))
    try:
        d.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        _fail("{} is a file, not a folder - backups need a folder of their own. "
              "Move or delete it.".format(d))
    except OSError as exc:
        _fail("Cannot prepare the backup folder {}: {} (read-only disk? move the "
              "data folder with: klipperformac data --set PATH)".format(
                  d, _why(exc)))
    return d


# ------------------------------------------------------------- membership ---

def _arc(parts, prefix=DATA_PREFIX):
    return "/".join([prefix] + list(parts))


def _payload(with_logs=False):
    """[(arcname, Path)] for everything a backup carries, plus the reasons we
    skipped something.

    Sorted inside each folder (os.walk returns directory order, which changes as
    files are rewritten) so two backups of an unchanged folder differ only in
    their timestamps. Symlinks and special files are skipped, not archived: a
    restore writes only folders and regular files, so archiving a symlink would
    produce a backup that can be listed but never put back."""
    data = paths.DATA
    entries = []
    skipped = []
    for top in list(PAYLOAD_DIRS) + ([LOGS_DIR] if with_logs else []):
        root = data / top
        if root.is_symlink():
            skipped.append("{} is a symlink - not archived".format(root))
            continue
        if not root.is_dir():
            continue  # simply not there yet (a fresh install has no database/)
        for cur, dirnames, filenames in os.walk(str(root)):
            dirnames.sort()
            filenames.sort()
            cur_path = Path(cur)
            rel = os.path.relpath(cur, str(data)).split(os.sep)
            entries.append((_arc(rel), cur_path))
            for name in filenames:
                src = cur_path / name
                if src.is_symlink():
                    skipped.append("{} is a symlink - not archived".format(src))
                elif not src.is_file():
                    skipped.append("{} is not a regular file - not archived"
                                   .format(src))
                else:
                    entries.append((_arc(rel + [name]), src))
    for src, name in ((paths.SETTINGS, "settings.json"),
                      (paths.LOCKFILE, "lockfile.json")):
        if src.is_symlink():
            skipped.append("{} is a symlink - not archived".format(src))
        elif src.is_file():
            entries.append((_arc([name], APP_PREFIX), src))
    return entries, skipped


def _member_target(name):
    """Where a member belongs on disk, or a clean refusal.

    This is the zip-slip guard, and it is deliberately stricter than a generic
    one: the name must already match the documented layout, so a crafted
    archive cannot even ask to write somewhere the format has no prefix for.
    Judged on the raw name, never on a path that was already joined, and long
    before anything is written. installer._safe_extract() is the house precedent
    (NUL byte, '..' after normalisation, over-long component)."""
    if "\x00" in name:
        _fail("Unsafe backup member {}: a NUL byte is not a path. This archive "
              "was not written by klipperformac.".format(_short(name)))
    if name.startswith("/") or name.startswith("~"):
        _fail("Unsafe backup member {}: it is an absolute path. Backup members "
              "are relative to data/ or app/.".format(_short(name)))
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts:
        _fail("Unsafe backup member {}: it names no file.".format(_short(name)))
    if ".." in parts:
        _fail("Unsafe backup member {}: it walks out of the backup with '..'."
              .format(_short(name)))
    for part in parts:
        if len(part.encode("utf-8", "replace")) > MAX_COMPONENT_BYTES:
            _fail("Unsafe backup member {}: one component is over the {} byte "
                  "limit macOS gives a single name, so nothing here could ever "
                  "write it back.".format(_short(name), MAX_COMPONENT_BYTES))
    if parts == [MANIFEST_NAME]:
        return None  # metadata: read for display, never restored
    if parts[0] in (DATA_PREFIX, APP_PREFIX):
        # ("data", []) is the archive's own root folder entry - `tar czf` writes
        # one and `data/` names nothing restorable, so _audit() skips it as a
        # folder and refuses it as a file.
        return parts[0], parts[1:]
    _fail("Unsafe backup member {}: this format carries only '{}', "
          "'{}/...' and '{}/...'.".format(_short(name), MANIFEST_NAME,
                                          DATA_PREFIX, APP_PREFIX))


def _is_config_member(name):
    """True for the members a stack reads as config, as opposed to a user's gcode.

    The size cap belongs on these and only these: a print file of 500 MB is
    normal, a printer.cfg of 500 MB is a disk bomb waiting for a staging tree."""
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if parts[:1] == [DATA_PREFIX]:
        return len(parts) > 2 and parts[2] == "config"
    if parts[:1] == [APP_PREFIX]:
        return len(parts) > 1
    return False


def _audit(archive):
    """Read the tar headers (no bodies) and audit every member name.

    Returns [(member, (root, parts))] for regular files and for folders, in
    archive order. A member type that cannot be written back - symlink, hard
    link, socket, FIFO, device - is refused here instead of being quietly dropped
    while extracting, because a backup that silently loses a file is worse than
    one that refuses to restore. Both refusals happen before a single byte is
    staged, which is also what keeps a size bomb out of the staging tree."""
    files = []
    dirs = []
    try:
        with tarfile.open(str(archive), "r:gz") as tf:
            for member in tf:
                target = _member_target(member.name)
                if member.name == MANIFEST_NAME or target is None:
                    continue
                root, parts = target
                if not parts:
                    if member.isdir():
                        continue  # the archive's own "data/" / "app/" root entry
                    _fail("Unsafe backup member {}: '{}' is a backup root, not "
                          "a restorable file.".format(_short(member.name), root))
                if member.isreg() and member.size > MAX_VALIDATE_BYTES \
                        and _is_config_member(member.name):
                    _fail("Cannot restore '{}': {} claims to be {} bytes, over "
                          "the {} byte limit for a config file. A print file can "
                          "be huge; a config cannot.".format(
                              archive.name, _short(member.name), member.size,
                              MAX_VALIDATE_BYTES))
                if member.isdir():
                    dirs.append((member, target))
                elif member.isreg():
                    files.append((member, target))
                else:
                    kind = ("symbolic link" if member.issym() else
                            "hard link" if member.islnk() else "special file")
                    _fail("Cannot restore '{}': member {} is a {}, and a backup "
                          "carries only folders and regular files.".format(
                              archive.name, member.name, kind))
    except _READ_ERRORS as exc:
        _fail("Cannot read {}: {} (is it a .tar.gz written by 'klipperformac "
              "backup create'?)".format(archive, _why(exc)))
    return files, dirs


# ------------------------------------------------------------ the manifest --

def _manifest(entries, with_logs, data, app_home):
    """Small, honest, and trusted for display only.

    data_dir/app_home record where an archive came from; restore deliberately
    ignores them and writes where the folders are *now* - which is the entire
    point of being able to move a data folder."""
    files = 0
    total = 0
    for _name, path in entries:
        try:
            info = path.stat()
        except OSError:
            continue
        if statmod.S_ISREG(info.st_mode):
            files += 1
            total += info.st_size
    return {
        "klipperformac_backup": 1,
        "tool_version": __version__,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "with_logs": bool(with_logs),
        "data_dir": str(data),
        "app_home": str(app_home),
        "payload_dirs": list(PAYLOAD_DIRS) + ([LOGS_DIR] if with_logs else []),
        "file_count": files,
        "total_bytes": total,
    }


def _manifest_of(archive):
    """manifest.json of an archive as a dict, or None. Never raises: `list` must
    still show a row for an archive written by hand."""
    try:
        with tarfile.open(str(archive), "r:gz") as tf:
            for member in tf:  # streamed: the body follows its header
                if member.name != MANIFEST_NAME or not member.isreg():
                    continue
                handle = tf.extractfile(member)
                if handle is None:
                    return None
                got = json.loads(handle.read().decode("utf-8", "replace"))
                return got if isinstance(got, dict) else None
    except Exception:
        return None
    return None


# ----------------------------------------------------------------- create ---

def _new_name(where):
    """klipperformac-backup-YYYYmmdd-HHMMSS.tar.gz, -N on a same-second clash.

    The name is both the sort key and the prune rule, so it is stamped rather
    than random - and never overwritten: two backups in one second each keep
    their own bytes."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = "{}{}{}".format(PREFIX, stamp, SUFFIX)
    for i in range(1, 1000):
        if not (where / name).exists():
            return name
        name = "{}{}-{}{}".format(PREFIX, stamp, i, SUFFIX)
    _fail("More than 999 backups were created in the same second. Prune some: "
          "klipperformac backup prune")


def _add_bytes(tf, arcname, raw, mode=0o644):
    info = tarfile.TarInfo(arcname)
    info.size = len(raw)
    info.mtime = int(time.time())
    info.mode = mode
    tf.addfile(info, io.BytesIO(raw))


def create(with_logs=False, allow_running=False):
    """Write one .tar.gz into DATA/backups and return its path."""
    data = _require_data("back up")
    _gate_for_create(allow_running)
    entries, skipped = _payload(with_logs=with_logs)
    if not (data / "config" / "printer.cfg").is_file():
        skipped.append("config/printer.cfg is not there, so this backup will "
                       "not verify as restorable")
    where = _make_backups_dir()
    target = where / _new_name(where)
    tmp = target.with_name(target.name + PARTIAL_SUFFIX)
    manifest = _manifest(entries, with_logs, data, paths.APP_HOME)
    body = (json.dumps(manifest, indent=2, sort_keys=True,
                       allow_nan=False) + "\n").encode()
    written = 0
    try:
        with tarfile.open(str(tmp), "w:gz") as tf:
            _add_bytes(tf, MANIFEST_NAME, body)
            for arcname, src in entries:
                try:
                    tf.add(str(src), arcname=arcname, recursive=False)
                except FileNotFoundError:
                    skipped.append("{} vanished while the backup was being made"
                                   .format(src))
                    continue
                except OSError as exc:
                    _fail("Cannot add {} to the backup: {}".format(
                        src, _why(exc)))
                if src.is_file():
                    written += 1
        # Write-then-replace, exactly the discipline paths.write_config() applies
        # to printer.cfg: a crash or a full disk leaves a .partial behind, never
        # a half archive that `list` advertises as a backup.
        os.replace(str(tmp), str(target))
    except BaseException:
        _remove(tmp)
        raise
    for line in skipped:
        print("[!] " + line)
    print("Backup created: {} ({} file(s), {})".format(
        target, written, _human(target.stat().st_size)))
    print("Carries: {}, settings.json, lockfile.json{}".format(
        ", ".join(PAYLOAD_DIRS), " + logs/" if with_logs else " (no logs/)"))
    print("Check it: klipperformac backup verify --from {}".format(target.name))
    stored = len(_backup_files(where))
    if stored > DEFAULT_KEEP:
        print("[!] {} backups are stored; 'klipperformac backup prune' keeps "
              "the newest {}.".format(stored, DEFAULT_KEEP))
    return target


# ------------------------------------------------------------------- list ---

def _sort_key(path):
    """The timestamp in the name is the authority (what prune uses too); mtime
    only for archives that were renamed by hand.

    The same-second suffix is compared as a number, or the tenth backup of one
    second would sort before the second."""
    stamp = STAMP_RE.match(path.name)
    if stamp:
        try:
            return (1, time.strptime(stamp.group(1), "%Y%m%d-%H%M%S"),
                    int(stamp.group(2) or 0), str(path))
        except ValueError:
            pass
    try:
        when = time.localtime(path.stat().st_mtime)
    except OSError:
        when = time.localtime(0)
    return (0, when, "", str(path))


def _backup_files(where):
    """Every .tar.gz in `where`, newest first. Symlinks and dot files are not
    candidates for anything: not for listing, not for pruning."""
    try:
        found = [p for p in where.iterdir()
                 if p.name.endswith(SUFFIX) and not p.name.startswith(".")
                 and not p.is_symlink() and p.is_file()]
    except OSError as exc:
        _fail("Cannot read the backup folder {}: {}".format(where, _why(exc)))
    return sorted(found, key=_sort_key, reverse=True)


def _is_ours(name):
    return bool(NAME_RE.match(name))


def list_backups():
    """Print what DATA/backups holds; returns the paths listed."""
    where = backups_dir()
    if not where.is_dir():
        print("No backups yet. Make one: klipperformac backup create")
        return []
    files = _backup_files(where)
    if not files:
        print("No backups in {} yet. Make one: klipperformac backup create"
              .format(where))
        return []
    foreign = sum(1 for p in files if not _is_ours(p.name))
    for p in files:
        manifest = _manifest_of(p)
        if manifest is None:
            tag = "no manifest"
            created = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                    time.gmtime(p.stat().st_mtime))
            count = "?"
        else:
            tag = "logs" if manifest.get("with_logs") else "no logs"
            created = manifest.get("created_utc") or "?"
            count = manifest.get("file_count", "?")
        print("  {}  {:>9}  {}  {:>6} file(s)  {:<11}{}".format(
            p.name, _human(p.stat().st_size), created, count, tag,
            "*" if not _is_ours(p.name) else ""))
    print("{} backup(s) in {}".format(len(files), where))
    if foreign:
        print("  * not a klipperformac name: 'prune' leaves those alone, and "
              "nothing here ever deletes a folder")
    print("Restore one: klipperformac backup restore --from NAME")
    return files


# ------------------------------------------------------ config validation ---

def _decode(raw):
    """bytes -> text, with paths.read_config()'s exact fallback chain.

    paths.read_config() takes a path and `verify` reads the archive's bytes
    without touching disk (a read-only command must not write), so the decoding
    policy is mirrored here: utf-8-sig only when a BOM is really there, then
    utf-8, then cp1252, then latin-1, which is byte-lossless. Klipper and
    Moonraker configs are routinely saved as cp1252/Latin-1 by editors, and one
    non-UTF-8 byte in a comment must not be able to call a good backup broken."""
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "cp1252"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode(paths.CFG_FALLBACK_ENCODING), paths.CFG_FALLBACK_ENCODING


def _config_problems(label, raw=None, needs=None, path=None):
    """Problems with a config's bytes, [] when the stack could read it.

    Two checks, both ours to make and neither a guess about upstream: the file
    must decode, and it must parse as INI - the shape klippy's configfile.py and
    Moonraker read. strict=False because upstream lets a section repeat an
    option (last one wins); a file configparser cannot read at all cannot be
    read by them either. `needs` is a section the program stops without:
    klippy's Printer.__init__ reads max_velocity out of [printer], so a
    printer.cfg without that section is a printer that never boots.

    With `path` this is literally the house reader (paths.read_config_text, the
    one up/doctor/lan use on the live files); with `raw` it is the same decode
    chain applied to bytes taken out of the archive, because `verify` must not
    write anything to disk to find out whether a backup is good."""
    text = paths.read_config_text(path) if path is not None else _decode(raw)[0]
    cp = configparser.RawConfigParser(strict=False)
    try:
        cp.read_string(text, source=label)
    except configparser.Error as exc:
        return ["does not parse as a Klipper/Moonraker config: {} ({})".format(
            exc, type(exc).__name__)]
    if not cp.sections():
        return ["holds no [section], so not one setting in it can be read"]
    if needs and not cp.has_section(needs):
        return ["has no [{}] section - Klipper refuses to start without it".format(
            needs)]
    return []


def _read_configs(archive, wanted):
    """{arcname: bytes} for the members `wanted` names, and nothing else.

    Only the config/JSON files a validation has to parse are read into memory,
    each under the size cap: a 500 MB backup of prints must not need 500 MB of
    RAM to verify, and the payload is exactly the part nobody has to look inside
    to answer "would this restore"."""
    got = {}
    try:
        with tarfile.open(str(archive), "r:gz") as tf:
            for member in tf:
                if member.name not in wanted or not member.isreg():
                    continue
                if member.size > MAX_VALIDATE_BYTES:
                    # A real printer.cfg is a few kB; a "config" this big is a
                    # size bomb, not a backup, and reading it is the damage.
                    _fail("{} names a {} byte config file, over the {} byte "
                          "limit for one. This archive is not a backup this tool "
                          "wrote.".format(member.name, member.size,
                                          MAX_VALIDATE_BYTES))
                handle = tf.extractfile(member)
                if handle is not None:
                    got[member.name] = handle.read()
    except _READ_ERRORS as exc:
        _fail("Cannot read {}: {}".format(archive, _why(exc)))
    return got


# ----------------------------------------------------------------- verify ---

def _gzip_problems(archive):
    """Read the whole file through gzip and throw the bytes away.

    This is the layer a tar check cannot vouch for: a truncated file or a plain
    .tar renamed to .tar.gz only shows up after every header was read, and a bad
    CRC only at the very end. Restoring from a half-decompressed stream is exactly
    how a torn printer.cfg gets onto disk."""
    try:
        with open(str(archive), "rb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="rb") as gz:
                while gz.read(_CHUNK):
                    pass
    except _READ_ERRORS as exc:
        return ["the gzip layer is broken, or this is not a .tar.gz: {}".format(
            _why(exc) or type(exc).__name__)]
    return []


def _wanted_configs():
    """The archive members whose content `verify` parses: the two files the
    stack boots from, plus the two json files a restore would overwrite."""
    return set([_arc(["config", name]) for name in REQUIRED_CONFIG] +
               [_arc(["settings.json"], APP_PREFIX),
                _arc(["lockfile.json"], APP_PREFIX)])


def verify(name=None):
    """(ok, lines) - would this archive restore?

    Structure first (gzip, tar headers, every member name), then the config
    content parsed with our own readers. Reports instead of raising so the CLI
    can print it and exit 1, the way cmd_doctor() grades its own lines."""
    archive = _resolve_source(name)
    lines = [(True, "file: {} ({})".format(
        archive.name, _human(archive.stat().st_size)))]
    problems = _gzip_problems(archive)
    if problems:
        return False, lines + [(False, p) for p in problems]
    lines.append((True, "gzip stream intact (every byte decompressed, CRC "
                        "checked)"))
    files, dirs = _audit(archive)
    if not files:
        return False, lines + [(False, "the archive holds no files at all, so "
                                       "restoring it would write nothing")]
    lines.append((True, "{} folder(s) + {} file(s), and every member name "
                        "stays inside the backup layout".format(
                            len(dirs), len(files))))
    ok = True
    manifest = _manifest_of(archive)
    if manifest is None:
        lines.append((False, "warning: no readable {} inside, so this archive "
                             "was not written by klipperformac (the files it "
                             "does carry are still checked)".format(
                                 MANIFEST_NAME)))
    else:
        lines.append((True, "manifest: klipperformac {}, {}, {} file(s), "
                            "data folder was {}".format(
                                manifest.get("tool_version", "?"),
                                manifest.get("created_utc", "?"),
                                manifest.get("file_count", "?"),
                                manifest.get("data_dir", "?"))))
    got = _read_configs(archive, _wanted_configs())
    for fname in REQUIRED_CONFIG:
        arcname = _arc(["config", fname])
        if arcname not in got:
            ok = False
            lines.append((False, "{} is missing - the stack cannot boot without "
                                 "it".format(arcname)))
            continue
        needs = "printer" if fname == "printer.cfg" else None
        problems = _config_problems(arcname, raw=got[arcname], needs=needs)
        if problems:
            ok = False
            lines += [(False, "{}: {}".format(arcname, p)) for p in problems]
        else:
            lines.append((True, "{} parses as a {} config".format(
                arcname, "Klipper" if fname == "printer.cfg" else "Moonraker")))
        if fname == "moonraker.conf" and not problems:
            cp = configparser.RawConfigParser(strict=False)
            cp.read_string(_decode(got[arcname])[0], source=arcname)
            if not cp.has_section("server"):
                lines.append((False, "warning: {} has no [server] host line - "
                                     "upstream Moonraker binds 0.0.0.0 when it is "
                                     "missing, so this stack would be reachable "
                                     "from your network".format(arcname)))
    for arcname in sorted(got):
        if not arcname.endswith(".json") or arcname == MANIFEST_NAME:
            continue
        try:
            json.loads(_decode(got[arcname])[0])
        except ValueError as exc:
            ok = False
            lines.append((False, "{} is not valid JSON: {}".format(
                arcname, exc)))
    return ok, lines


# ---------------------------------------------------------------- restore ---

def _validate_name(name, what="backup name"):
    """cli._safe_name()'s rules, because a --from value names one file inside
    DATA/backups: no separators, no '..', no leading dot, no control characters,
    and not longer than a filesystem component."""
    if not name or re.search(r"[/\\]|\.\.|^\.", name):
        _fail("Invalid {}: '{}'. No path separators, no '..', no leading dot. "
              "A backup name is a file in DATA/backups - see: klipperformac "
              "backup list".format(what, name))
    if re.search(r"[\x00-\x1f\x7f]", name):
        _fail("Invalid {}: '{}' (no control characters - a newline is not part "
              "of a file name)".format(what, name))
    if len(name.encode("utf-8", "replace")) > MAX_COMPONENT_BYTES:
        _fail("Invalid {}: '{}' is longer than one macOS filename ({} bytes)"
              .format(what, name[:32] + "...", MAX_COMPONENT_BYTES))
    return name


def _resolve_source(name):
    """The archive to work on: a bare name in DATA/backups, 'latest' (or
    nothing), or an explicit path - a backup copied from another Mac, or one on
    an external disk.

    Three things a fuzzy --from value taught the hard way: '~0' is not a home
    folder (Path.expanduser() raises RuntimeError for a user that does not
    exist, and os.path.expanduser() hands it straight back), a name with a NUL in
    it makes every stat() raise ValueError, and a bare '..' is a folder. All of
    them are one clean line, never a traceback."""
    if name in (None, "", "latest"):
        return _latest_backup()
    if "\x00" in name:
        _fail("Invalid backup name: a NUL byte is not part of a file name.")
    if "/" in name or name == "~" or name.startswith("~/"):
        archive = Path(os.path.expanduser(name))
    else:
        archive = backups_dir() / _validate_name(name)
    if archive.is_symlink():
        _fail("Refusing to read {}: it is a symlink. Copy the real archive into "
              "DATA/backups instead.".format(archive))
    if not archive.is_file():
        _fail("No backup named '{}' in {} (see: klipperformac backup list)"
              .format(name, archive.parent))
    if not archive.name.endswith(SUFFIX):
        _fail("{} is not a {} file - backups are gzip tarballs".format(
            archive, SUFFIX))
    return archive


def _latest_backup():
    where = backups_dir()
    found = _backup_files(where) if where.is_dir() else []
    if not found:
        _fail("No backups yet in {}. Make one: klipperformac backup create"
              .format(where))
    return found[0]


def _stage(archive, files, dirs):
    """Extract into a throwaway tree inside DATA/backups and return its Path.

    Deliberately not tarfile.extractall(): only the folders and regular files
    that _audit() already accepted are written, one open()/write() at a time, so
    a symlink or device member cannot be created, and nothing can land outside
    the stage tree (the names were judged against the layout before this ran).
    Everything is staged and validated here first: a backup that cannot restore
    never touches a live config path."""
    where = _make_backups_dir()
    staged = Path(tempfile.mkdtemp(prefix=STAGE_PREFIX, dir=str(where)))
    try:
        with tarfile.open(str(archive), "r:gz") as tf:
            for member, (root, parts) in dirs + files:
                dest = staged.joinpath(root, *parts)
                if member.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                    _set_mode(dest, member.mode, True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                handle = tf.extractfile(member)
                if handle is None:
                    _fail("Cannot read {} out of {}".format(
                        member.name, archive.name))
                with handle, open(str(dest), "wb") as out:
                    shutil.copyfileobj(handle, out, _CHUNK)
                _set_mode(dest, member.mode, False)
    except _READ_ERRORS as exc:
        shutil.rmtree(str(staged), ignore_errors=True)
        _fail("Cannot unpack {} into a staging tree: {} (nothing was restored)"
              .format(archive.name, _why(exc)))
    except BaseException:
        shutil.rmtree(str(staged), ignore_errors=True)
        raise
    return staged


def _set_mode(path, mode, is_dir):
    """Keep the archived permissions, but never one that defeats the restore.

    A 0o000 printer.cfg would read back as a broken backup, and a folder without
    the owner's search bit cannot be written into at all - which is what a
    hand-made archive (whose directory members often carry 0o644, because that is
    what `tar` was told) used to do to staging. Note this only ever affects the
    stage tree: the swap copies file stats, and the folders it creates get this
    install's own 0o755, not the archive's."""
    keep = ((mode if mode else (0o755 if is_dir else 0o644)) & 0o7777) | 0o600
    if is_dir:
        keep |= 0o700
    try:
        os.chmod(str(path), keep)
    except OSError:
        pass


def _stage_plan(staged):
    """([destination folders], [(source, destination)]) for a staged tree.

    The folders are part of the plan because an archive carries them: a data
    folder whose database/ was deleted has to get it back, not fail halfway with
    ENOENT. Nothing is written here - this is only what the caller then checks
    against what is on disk."""
    folders = []
    plan = []
    for root, target in ((DATA_PREFIX, paths.DATA),
                         (APP_PREFIX, paths.APP_HOME)):
        base = staged / root
        if not base.is_dir():
            continue
        for cur, dirnames, filenames in os.walk(str(base)):
            dirnames.sort()
            filenames.sort()
            rel = os.path.relpath(cur, str(base))
            parts = () if rel == "." else tuple(rel.split(os.sep))
            here = Path(str(target)).joinpath(*parts)
            if parts:
                folders.append(here)
            for name in filenames:
                src = Path(cur, name)
                if src.is_symlink() or not src.is_file():
                    continue
                plan.append((src, here / name))
    return folders, plan


def _atomic_copy(src, dst):
    """Copy onto a live config path without ever leaving it half-written - the
    reason cli._copy_atomic() and paths.write_config() exist in the first place.

    Local rather than reusing cli's helper because a failed restore has to name
    the file it could not write and say what is already on disk. And note what
    os.replace() does to a path that happens to be a symlink: it replaces the
    link, so a DATA/config/printer.cfg pointing at /etc/something ends up holding
    the backup's own bytes instead of having them written through the link."""
    dst = Path(dst)
    tmp = dst.with_name(dst.name + ".tmp")
    try:
        shutil.copy2(str(src), str(tmp))
        os.replace(str(tmp), str(dst))
    except OSError as exc:
        _remove(tmp)
        _fail("Could not write {} while restoring: {} (the files already "
              "restored stay as they are; the rest of the backup was not "
              "applied)".format(dst, _why(exc)))


def _snapshot_current():
    """The one-way door gets a receipt first, using the machinery that already
    exists: cli.snapshot_printer_cfg() writes
    DATA/config/history/printer-<stamp>-before-restore.cfg (deduped, newest 10),
    which `klipperformac presets --history/--restore` can put back on its own."""
    if not paths.PRINTER_CFG.is_file():
        return
    from . import cli  # lazy: cli.py pulls in this module only inside a function
    try:
        saved = cli.snapshot_printer_cfg("before-restore")
    except OSError as exc:
        _fail("Could not snapshot the current printer.cfg, so nothing was "
              "restored: {}".format(_why(exc)))
    print("Current config kept as: " +
          (saved.name if saved else "unchanged since the last saved version"))


def restore(name):
    """Put a backup back: resolve -> gate -> stage -> validate -> swap.

    The swap overwrites the files the archive carries and deletes nothing else:
    a file the backup does not hold (created later, or excluded on purpose like
    DATA/web) stays exactly where it is. Restoring is not a factory reset, and
    the reason is written here rather than discovered by a user: `restore` is
    safe on a machine that has moved on, not a time machine."""
    if not name:
        _fail("restore needs --from NAME (or --from latest). See: "
              "klipperformac backup list")
    archive = _resolve_source(name)
    _require_data("restore over")
    _gate_for_restore()
    problems = _gzip_problems(archive)
    if problems:
        _fail("Will not restore {}: {}".format(archive.name, problems[0]))
    files, dirs = _audit(archive)
    if not files:
        _fail("Will not restore {}: the archive holds no files, so restoring it "
              "would write nothing.".format(archive.name))
    staged = _stage(archive, files, dirs)
    try:
        broken = []
        for fname in REQUIRED_CONFIG:
            probe = staged / DATA_PREFIX / "config" / fname
            arcname = _arc(["config", fname])
            if not probe.is_file():
                broken.append("carries no {}".format(arcname))
                continue
            needs = "printer" if fname == "printer.cfg" else None
            try:
                problems = _config_problems(arcname, needs=needs, path=probe)
            except OSError as exc:
                broken.append("{} cannot be read: {}".format(
                    arcname, _why(exc)))
                continue
            broken += ["{}: {}".format(arcname, p) for p in problems]
        if broken:
            _fail("Will not restore {} - this backup cannot boot the stack "
                  "({}). Nothing was changed.".format(
                      archive.name, "; ".join(broken)))
        folders, plan = _stage_plan(staged)
        # Both shapes of "the disk and the archive disagree about what this path
        # is" are refused here, before the first write: a file the backup thinks
        # is a folder, and a folder the backup thinks is a file. Deleting either
        # one to make room is not what "restore" was asked to do.
        for dst in folders:
            if dst.exists() and not dst.is_dir():
                _fail("Will not restore {}: {} is a file on disk but a folder in "
                      "the backup. Move that file away and try again."
                      .format(archive.name, dst))
        for _src, dst in plan:
            if dst.is_dir():
                _fail("Will not restore {}: {} is a folder on disk but a file in "
                      "the backup. Move that folder away and try again."
                      .format(archive.name, dst))
        _snapshot_current()
        for dst in folders:
            try:
                dst.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                _fail("Could not create {} while restoring: {}".format(
                    dst, _why(exc)))
        for src, dst in plan:
            _atomic_copy(src, dst)
        print("Restored {} file(s) from {} into {}.".format(
            len(plan), archive.name, paths.DATA))
        print("[!] Files the backup does not carry (DATA/web, anything newer) "
              "were left as they were.")
        print("Restart to load it: klipperformac restart")
        return [dst for _src, dst in plan]
    finally:
        shutil.rmtree(str(staged), ignore_errors=True)


# ------------------------------------------------------------------ prune ---

def prune(keep=DEFAULT_KEEP, dry_run=False):
    """Delete everything older than the newest `keep` backups, and nothing else.

    Only files this tool named (NAME_RE), only regular files, only inside
    DATA/backups - each of those re-checked per file at delete time, not assumed
    from how the list was built. `uninstall --data` taught this house that the
    folder you delete is never the one you think you named, and keep >= 1 means
    a typo cannot remove your only copy."""
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        _fail("Invalid --keep value: {}. Prune keeps at least 1 backup, so a "
              "typo can never delete the only copy you have.".format(keep))
    where = backups_dir()
    if not where.is_dir():
        print("No backups in {} - nothing to prune.".format(where))
        return []
    real = os.path.realpath(str(where))
    candidates = [p for p in _backup_files(where) if _is_ours(p.name)]
    stale = candidates[keep:]
    removed = []
    # Oldest first, always: the list arrived newest-first (that is what `list`
    # shows), and if a prune is interrupted halfway it should have spent its time
    # on the oldest copies rather than the newest.
    for p in reversed(stale):
        if p.is_symlink() or not p.is_file():
            print("[!] Left {} alone: it is not a regular file this tool wrote."
                  .format(p.name))
            continue
        if not _is_ours(p.name) or p.parent != where:
            print("[!] Left {} alone: not a backup name in {}.".format(
                p.name, where))
            continue
        if Path(os.path.realpath(str(p))).parent != Path(real):
            print("[!] Left {} alone: it does not resolve inside {}.".format(
                p.name, real))
            continue
        if dry_run:
            print("  would delete: {}".format(p.name))
            removed.append(p)
            continue
        try:
            p.unlink()
        except OSError as exc:
            _fail("Could not delete {}: {} (the backup is still there)".format(
                p, _why(exc)))
        print("  deleted: {}".format(p.name))
        removed.append(p)
    if not stale:
        print("{} backup(s) in {}, all within the newest {} - nothing to prune."
              .format(len(candidates), where, keep))
    else:
        print("{} backup(s) left in {}{}".format(
            len(candidates) - len(removed), where,
            " (dry run: nothing was deleted)" if dry_run else ""))
    return removed


# --------------------------------------------------------------- doctor -----

DOCTOR_STALE_DAYS = 30


def doctor_lines():
    """[(ok, message)] in cam.doctor()'s shape, printed by `klipperformac doctor`.

    Wired into cmd_doctor() by the wave-1 integrator (the two lines this module
    promised it would need):

        for good, msg in backup.doctor_lines():
            print(("[ok] " if good else "[!] ") + msg)

    The boolean is NOT folded into doctor's own `ok`, deliberately: "no backups
    yet" is true of every healthy fresh install, and AGENTS.md 4 makes
    `[!] services not running` style advisories exactly that - a line you read,
    not a failed gate. See tests/backup/test_backup_doctor_wired.py, which pins
    both halves (the line is printed, the exit code is unchanged).

    It reports that backups exist and how old the newest one is. It does not open
    an archive: `doctor` runs on every invocation and a full gzip read of a
    500 MB backup does not belong in it - that is what `backup verify` is for.
    Never raises: an unreadable folder is a line, not a doctor that stops
    printing lines halfway down."""
    lines = []
    where = backups_dir()
    try:
        if not where.is_dir():
            if paths.PRINTER_CFG.is_file():
                lines.append((False, "no backups yet - 'klipperformac backup "
                                     "create' before the next print"))
            else:
                lines.append((True, "no backups yet (and no config to "
                                    "back up)"))
            return lines
        found = _backup_files(where)
        if not found:
            lines.append((False, "no backups in {} - 'klipperformac backup "
                                 "create'".format(where)))
            return lines
        newest = found[0]
        age = _age_days(newest)
        if age is None:
            lines.append((True, "backups: {} archive(s) in {} (newest undated)"
                          .format(len(found), where)))
        elif age > DOCTOR_STALE_DAYS:
            lines.append((False, "newest backup is {} days old ({} of them) - "
                                 "klipperformac backup create".format(
                                     int(age), len(found))))
        else:
            lines.append((True, "backups: {} archive(s), newest {} day(s) ago"
                          .format(len(found), int(age))))
    except (OSError, SystemExit) as exc:
        # _fail() raises SystemExit for an unreadable folder; doctor turns that
        # into a line and keeps going.
        lines.append((False, "backups: cannot read {} ({})".format(
            where, _why(exc) or "unknown")))
    return lines


def _age_days(path):
    """Days since a backup was taken, from the stamp in its name (the timestamp
    on disk moves when a folder is copied; the name does not)."""
    stamp = STAMP_RE.match(path.name)
    when = None
    if stamp:
        try:
            when = time.strptime(stamp.group(1), "%Y%m%d-%H%M%S")
        except ValueError:
            when = None
    if when is None:
        try:
            when = time.localtime(path.stat().st_mtime)
        except OSError:
            return None
    return max(0.0, (time.time() - time.mktime(when)) / 86400.0)


# -------------------------------------------------------------- dispatch ---

def dispatch(args):
    """cli.cmd_backup() hands the parsed namespace here; the subparser owns the
    flags, this owns what they mean."""
    action = getattr(args, "action", None)
    if action == "create":
        create(with_logs=bool(getattr(args, "with_logs", False)),
               allow_running=bool(getattr(args, "allow_running", False)))
    elif action == "list":
        list_backups()
    elif action == "verify":
        ok, lines = verify(getattr(args, "from_", None))
        for good, line in lines:
            print(("[ok] " if good else "[!] ") + line)
        if ok:
            print("This backup restores.")
        else:
            print("This backup does NOT restore - do not count on it.")
            sys.exit(1)
    elif action == "restore":
        restore(getattr(args, "from_", None))
    elif action == "prune":
        keep = getattr(args, "keep", None)
        prune(keep=DEFAULT_KEEP if keep is None else keep,
              dry_run=bool(getattr(args, "dry_run", False)))
    else:
        _fail("Unknown backup action: {!r} (create, list, verify, restore, "
              "prune)".format(action))
