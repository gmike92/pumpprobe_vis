"""
config_store.py
===============
Tiny persistent key/value store for settings that must survive app restarts —
notably each delay stage's zero position (t0), keyed by the stage serial number,
so a t0 set today is still there tomorrow.

Stored as JSON at ``<project>/pumpprobe_config.json`` (next to the app folder),
so it is easy to find/inspect. All access is guarded by a lock; writes are
atomic (write to a temp file then replace) so a crash mid-write can't corrupt it.
"""

import os
import json
import threading

_LOCK = threading.RLock()
_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "pumpprobe_config.json")


def config_path():
    return _CONFIG_PATH


def _load():
    try:
        with open(_CONFIG_PATH, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[config] read failed ({e}); starting fresh")
        return {}


def _save(data):
    try:
        tmp = _CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _CONFIG_PATH)   # atomic on Windows + POSIX
    except Exception as e:
        print(f"[config] write failed: {e}")


# ---------------------------------------------------------------------- #
# Stage zero (t0) — keyed by serial number
# ---------------------------------------------------------------------- #
def get_stage_zero(serial, default=0.0):
    """Return the saved t0 (mm) for a stage serial, or ``default`` if none."""
    if not serial:
        return default
    with _LOCK:
        zeros = _load().get("stage_zeros", {})
        try:
            return float(zeros.get(str(serial), default))
        except (TypeError, ValueError):
            return default


def set_stage_zero(serial, value):
    """Persist the t0 (mm) for a stage serial. No-op if serial is empty."""
    if not serial:
        return
    with _LOCK:
        data = _load()
        data.setdefault("stage_zeros", {})[str(serial)] = float(value)
        _save(data)
        print(f"[config] saved t0 for stage {serial}: {float(value):.4f} mm")


def has_stage_zero(serial):
    if not serial:
        return False
    with _LOCK:
        return str(serial) in _load().get("stage_zeros", {})


# ---------------------------------------------------------------------- #
# Stage encoder scale (counts per mm) — keyed by serial number
# ---------------------------------------------------------------------- #
def get_stage_counts_per_mm(serial, default=20000.0):
    """Saved encoder counts-per-mm for a stage serial, or ``default``."""
    if not serial:
        return default
    with _LOCK:
        cpm = _load().get("stage_counts_per_mm", {})
        try:
            v = cpm.get(str(serial))
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default


def set_stage_counts_per_mm(serial, value):
    """Persist encoder counts-per-mm for a stage serial."""
    if not serial:
        return
    with _LOCK:
        data = _load()
        data.setdefault("stage_counts_per_mm", {})[str(serial)] = float(value)
        _save(data)
        print(f"[config] saved counts/mm for stage {serial}: {float(value):g}")


def has_stage_counts_per_mm(serial):
    if not serial:
        return False
    with _LOCK:
        return str(serial) in _load().get("stage_counts_per_mm", {})
