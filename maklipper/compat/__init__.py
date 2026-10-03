"""macOS compatibility shims injected via CPATH at Klipper build time.

Upstream Klipper is never edited. Two headers missing on macOS are supplied
here instead: <linux/can.h> and <sys/prctl.h>. GCC/clang search directories
listed in CPATH before system directories for #include <...>, so the pristine
upstream sources compile bit-for-bit unchanged.
"""
import os
from pathlib import Path

INCLUDE = str((Path(__file__).resolve().parent / "include"))


def build_env(base=None):
    env = dict(os.environ if base is None else base)
    existing = env.get("CPATH")
    env["CPATH"] = INCLUDE if not existing else INCLUDE + os.pathsep + existing
    return env
