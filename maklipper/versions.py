"""Version pins for upstream components and update checks (GitHub tags)."""
import json
import urllib.request

from . import paths

REPOS = {
    "klipper": "Klipper3d/klipper",
    "moonraker": "Arksine/moonraker",
    "mainsail": "meteyou/mainsail",
    "fluidd": "fluidd-core/fluidd",
}

# Pinned upstream refs. These are download-time pins, not forks: each ref is
# fetched pristine from GitHub. Klipper tracks "master" (the community norm,
# and the tree verified to build on macOS); the resolved commit SHA is
# recorded in the lockfile so updates are explicit.
DEFAULT_PINS = {
    "klipper": "master",
    "moonraker": "v0.11.0",
    "mainsail": "v2.19.0",
    "fluidd": "v1.37.6",
    "ui": "fluidd",
}

API = "https://api.github.com/repos/{}/tags?per_page=100"


def load_pins():
    if paths.LOCKFILE.exists():
        with open(paths.LOCKFILE) as f:
            pins = json.load(f)
    else:
        pins = dict(DEFAULT_PINS)
    merged = dict(DEFAULT_PINS)
    merged.update(pins)
    return merged


def save_pins(pins):
    paths.LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
    with open(paths.LOCKFILE, "w") as f:
        json.dump(pins, f, indent=2, sort_keys=True)
        f.write("\n")


def _get_json(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "maklipper", "Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def _sort_key(tag):
    parts = tag.lstrip("v").split(".")
    nums = []
    for p in parts:
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def latest_tag(component):
    """Most version-like tag upstream, preferring release-looking tags."""
    tags = [t["name"] for t in _get_json(API.format(REPOS[component]))]
    plain = [t for t in tags if t[0] in "v0123456789"]
    if not plain:
        return None
    return max(plain, key=_sort_key)


def remote_master_sha():
    data = _get_json(
        "https://api.github.com/repos/{}/commits?per_page=1".format(REPOS["klipper"]))
    return data[0]["sha"]


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
                str(e)[:80]))
        if remote and remote != local_sha:
            out.append(("klipper", local_sha[:7], remote[:7] + " (master)"))
    for comp in ("moonraker", "mainsail", "fluidd"):
        try:
            latest = latest_tag(comp)
        except Exception as e:
            latest = None
            errors.append("{} update check failed: {}".format(comp, str(e)[:60]))
        if latest and _sort_key(latest) > _sort_key(pins[comp]):
            out.append((comp, pins[comp], latest))
    return out, errors
