"""macOS compatibility shims injected via CPATH at Klipper build time.

Upstream Klipper is never edited. Two headers missing on macOS are supplied
here instead: <linux/can.h> and <sys/prctl.h>. The GNU make manual documents
that CPATH directories are searched for #include <...> ahead of system
directories (verified empirically with `gcc -E -v` on Apple clang), so the
pristine upstream sources compile bit-for-bit unchanged.
"""
import os
from pathlib import Path

INCLUDE = str((Path(__file__).resolve().parent / "include"))


def build_env(base=None):
    env = dict(os.environ if base is None else base)
    existing = env.get("CPATH")
    env["CPATH"] = INCLUDE if not existing else INCLUDE + os.pathsep + existing
    return env


def invalidate_stale_build(klipper_dir):
    """Klipper's mtime check cannot see CPATH headers; force a rebuild when a
    shim is newer than the compiled c_helper.so."""
    so = Path(klipper_dir) / "klippy" / "chelper" / "c_helper.so"
    if not so.exists():
        return False
    newest = max(
        (f.stat().st_mtime for f in Path(INCLUDE).rglob("*.h")), default=0)
    if newest > so.stat().st_mtime:
        so.unlink()
        return True
    return False
