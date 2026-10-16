"""Version pins for upstream components and update checks (GitHub tags)."""
import json
import os
import re
import urllib.request

from . import paths

REPOS = {
    "klipper": "Klipper3d/klipper",
    "moonraker": "Arksine/moonraker",
    "mainsail": "meteyou/mainsail",
    "fluidd": "fluidd-core/fluidd",
}

# Pinned upstream refs. These are download-time pins, not forks: each ref is
# fetched pristine from GitHub. Every component is pinned to an exact tag or
# commit so a fresh install on any Mac resolves byte-identical sources to the
# tree verified to build on macOS. `update --apply` is how pins move forward.
DEFAULT_PINS = {
    "klipper": "461c4e3722c3a897fba1c6b3f0780a5315043842",
    "moonraker": "v0.11.0",
    "mainsail": "v2.19.0",
    "fluidd": "v1.37.6",
    "ui": "fluidd",
}

API = "https://api.github.com/repos/{}/tags?per_page=100"

# The web UIs the rest of the product can serve; the 'ui' pin must name one.
WEB_UI = ("mainsail", "fluidd")
# Components whose pin is a release tag compared against upstream tags.
TAG_COMPONENTS = ("moonraker", "mainsail", "fluidd")
# Every key of lockfile.json the product itself reads: the shipped pins plus the
# resolved commit ids setup()/`update` write next to them. A value here has to be
# a string because it reaches git, a URL, or a slice. Any *other* key is inert
# user data and is carried through verbatim (of any JSON type), so setup()'s
# re-save never silently deletes what it does not own.
PRODUCT_KEYS = tuple(DEFAULT_PINS) + tuple(c + "_sha" for c in REPOS)

_SHA40 = re.compile(r"[0-9a-f]{40}")
# A ref/URL-safe token: a leading alphanumeric then only characters that are
# safe in a git ref and in a releases/download/<tag> URL path segment. This
# rejects whitespace, control bytes, non-ASCII, '/', ';', etc.
_REF_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~+-]*")
_ASCII_NUM = re.compile(r"[0-9]+")
_STABLE_RELEASE = re.compile(r"v?[0-9]+(?:\.[0-9]+)+")
# Hard cap on an update-check response body (GitHub tag lists are far smaller).
_MAX_JSON_BYTES = 4_000_000


def _is_sha(value):
    return isinstance(value, str) and bool(_SHA40.fullmatch(value))


def _is_release_tag(value):
    """A real, usable release name: ref-safe, URL-path-safe, dotted version."""
    if not isinstance(value, str) or not value:
        return False
    if value[0] not in "v0123456789":
        return False
    if ".." in value:
        return False
    if not _REF_SAFE.fullmatch(value):
        return False
    return any(ch.isdigit() for ch in value)


def _clean(text, limit):
    """Strip control bytes (server-controlled text) then bound the length."""
    safe = "".join(ch for ch in str(text) if ch == " " or ord(ch) >= 0x20)
    return safe[:limit]


def load_pins():
    parsed = {}
    if paths.LOCKFILE.exists():
        try:
            with open(paths.LOCKFILE) as f:
                loaded = json.load(f)
        except (OSError, ValueError):
            # A truncated/empty/BOM/invalid-UTF-8 lockfile must degrade to the
            # shipped defaults, not raise out of status/update/ui/setup.
            loaded = None
        if isinstance(loaded, dict):
            parsed = loaded
    merged = dict(DEFAULT_PINS)
    for key, value in parsed.items():
        if key in TAG_COMPONENTS:
            if isinstance(value, str) and _is_release_tag(value):
                merged[key] = value
        elif key == "ui":
            if value in WEB_UI:
                merged[key] = value
        elif key in PRODUCT_KEYS:
            if isinstance(value, str):
                merged[key] = value        # klipper ref/sha, <comp>_sha
        else:
            merged[key] = value            # inert user keys survive verbatim
    return merged


def save_pins(pins):
    # Validate before touching the filesystem: the pin store is a mapping of
    # string names to values. Rejecting up front means a bad mapping can never
    # truncate the live lockfile the way open(path, "w") + a mid-dump crash did.
    if not isinstance(pins, dict):
        raise TypeError("pins must be a mapping of names to values")
    for key in pins:
        if not isinstance(key, str):
            raise TypeError("pin names must be strings")
    payload = json.dumps(pins, indent=2, sort_keys=True) + "\n"
    path = paths.LOCKFILE
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        # Never write through a symlink: drop the link so the pin store becomes
        # a regular file inside KLIPPERFORMAC_HOME instead of escaping it.
        path.unlink()
    # 0600 regardless of umask - the pins select upstream refs (a supply-chain
    # input), so they must not be group/world writable.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(payload)


def _get_json(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "klipperformac", "Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read(_MAX_JSON_BYTES)
    return json.loads(raw.decode())


def _sort_key(tag):
    if not isinstance(tag, str):
        raise TypeError("sort key requires a str tag")
    if tag[:1] == "v":
        body = tag[1:]
        if body[:1] == "v":
            # Only a single leading 'v' may be consumed; a double-'v' name is a
            # malformed tag, not the release it otherwise resembles.
            return (-1, -1, -1)
    else:
        body = tag
    parts = body.split(".")
    if not body or not parts:
        return (-1, -1, -1)
    nums = []
    for p in parts:
        if not _ASCII_NUM.fullmatch(p):
            return (-1, -1, -1)
        try:
            nums.append(int(p))
        except ValueError:
            return (-1, -1, -1)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def latest_tag(component):
    """Most version-like tag upstream, preferring release-looking tags."""
    data = _get_json(API.format(REPOS[component]))
    if not isinstance(data, list):
        raise ValueError("unexpected tags response")
    names = []
    for entry in data:
        if not isinstance(entry, dict):
            raise ValueError("malformed tag entry")
        if "name" not in entry:
            continue
        name = entry["name"]
        if not isinstance(name, str):
            raise ValueError("malformed tag name")
        if name:
            names.append(name)
    if data and not names:
        raise ValueError("no usable tag names in response")
    plain = [t for t in names if _is_release_tag(t)]
    if not plain:
        return None
    return max(plain, key=lambda tag: (_sort_key(tag),
                                       1 if _STABLE_RELEASE.fullmatch(tag) else 0))


def remote_master_sha():
    data = _get_json(
        "https://api.github.com/repos/{}/commits?per_page=1".format(REPOS["klipper"]))
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise ValueError("unexpected commits response")
    sha = data[0].get("sha")
    if not _is_sha(sha):
        raise ValueError("malformed commit response")
    return sha


def check_updates():
    """Returns ([(component, pinned, latest)], [error strings])."""
    out, errors = [], []
    pins = load_pins()
    local_sha = pins.get("klipper_sha")
    if pins["klipper"] in ("master", "main") and local_sha:
        try:
            remote = remote_master_sha()
        except Exception as e:
            remote = None
            errors.append("klipper update check failed: {}".format(
                _clean(str(e), 80)))
        if remote and remote != local_sha:
            out.append(("klipper", local_sha[:7], remote[:7] + " (master)"))
    for comp in TAG_COMPONENTS:
        if _is_sha(pins[comp]):
            continue  # a commit pin is not comparable against upstream tags
        try:
            latest = latest_tag(comp)
        except Exception as e:
            latest = None
            errors.append("{} update check failed: {}".format(comp, _clean(str(e), 60)))
        if latest and _sort_key(latest) > _sort_key(pins[comp]):
            out.append((comp, pins[comp], latest))
    return out, errors
