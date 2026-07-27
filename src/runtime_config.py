"""Runtime-toggleable config — the Config tab's backing store.

Every knob here used to be an env var read once at import, which meant the only
way to stop paying for a book or a scan was to restart the process. This module
makes them **live**: the poll loops re-read the config at the top of every
iteration, so turning a book off stops its next fetch (and its parse, its tick
write and its ladder-anomaly pass) without a restart.

Design rules:

- **Env is the seed, not the authority.** On first run the defaults come from
  the same env vars as before, so an existing deployment behaves identically
  until someone actually changes something in the UI. After that the saved
  file wins, and env changes only matter for keys never touched.
- **Persisted** to `data/runtime_config.json` (gitignored runtime state) so a
  restart keeps your settings. Written atomically via a temp file + rename.
- **Validated on write.** Unknown keys are rejected outright and numbers are
  clamped to sane ranges — this endpoint can otherwise be used to set a 0 s
  poll interval and hammer a book into rate-limiting us.
- **Cheap to read.** `is_on()` / `num()` hit an in-memory dict; the loops call
  them every iteration.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "data" / "runtime_config.json"

_lock = threading.Lock()
_cfg: dict[str, Any] = {}
_loaded = False


def _env_on(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name.upper(), os.environ.get(name.lower()))
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "on", "true", "yes")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


# Toggle spec: key -> (section, label, kind, default_factory, min, max)
# `kind` drives the UI widget and the validator.
BOOKS = ("crystalbet", "liderbet", "betlive", "crocobet", "setanta", "xbet")
SCANS = ("anomaly", "anomaly_extra", "anomaly_watch", "betlive_anomaly", "soft_scan")

# Cadence knobs: key -> (default_factory, min_sec, max_sec)
CADENCES: dict[str, tuple] = {
    "pinnacle_poll_sec":   (lambda: _env_int("PINNACLE_POLL_SEC", 60), 15, 3600),
    "crystalbet_poll_sec": (lambda: _env_int("CRYSTALBET_POLL_SEC", 60), 15, 3600),
    "extra_book_poll_sec": (lambda: _env_int("EXTRA_BOOK_POLL_SEC", 150), 15, 3600),
    "xbet_poll_sec":       (lambda: _env_int("XBET_POLL_SEC", 150), 15, 3600),
    "anomaly_scan_sec":    (lambda: _env_int("ANOMALY_SCAN_SEC", 300), 60, 21600),
    "anomaly_extra_sec":   (lambda: _env_int("ANOMALY_EXTRA_SEC", 900), 60, 21600),
    "anomaly_watch_sec":   (lambda: _env_int("ANOMALY_WATCH_SEC", 300), 0, 21600),
    "betlive_discover_sec": (lambda: _env_int("BETLIVE_DISCOVER_SEC", 150), 30, 21600),
    "betlive_watch_sec":   (lambda: _env_int("BETLIVE_WATCH_SEC", 8), 3, 3600),
    "soft_scan_sec":       (lambda: _env_int("SOFT_SCAN_SEC", 150), 30, 21600),
}

# Cost/horizon knobs — the levers that cut per-cycle work without turning a
# whole book off (see docs/performance.md and notes/build_log.md 2026-07-26).
LIMITS: dict[str, tuple] = {
    "anomaly_extra_horizon_h": (lambda: _env_float("ANOMALY_EXTRA_HORIZON_H", 12.0), 1.0, 240.0),
    # CB expansion horizon: only ExpandDetail games starting within this many
    # hours; farther games keep their list-view odds, so coverage is unchanged
    # and only the expensive html5lib expansion is skipped. 0 = unlimited,
    # which is the pre-2026-07-26 behaviour and stays the default.
    "cb_expand_within_hours": (lambda: _env_float("CB_EXPAND_WITHIN_HOURS", 0.0), 0.0, 240.0),
    "setanta_detail_hours":    (lambda: _env_float("SETANTA_DETAIL_HOURS", 24.0), 1.0, 240.0),
    "crocobet_detail_hours":   (lambda: _env_float("CROCOBET_DETAIL_HOURS", 24.0), 1.0, 240.0),
}


def _defaults() -> dict[str, Any]:
    """Seed from env — identical behaviour to the pre-Config-tab process."""
    return {
        "books": {
            # CrystalBet and Pinnacle are the original pipeline: on unless
            # explicitly disabled. The rest keep their opt-in env flags.
            "crystalbet": _env_on("CRYSTALBET", True),
            "liderbet":   _env_on("LIDERBET"),
            "betlive":    _env_on("BETLIVE"),
            "crocobet":   _env_on("CROCOBET"),
            "setanta":    _env_on("SETANTA"),
            "xbet":       _env_on("XBET"),
        },
        "scans": {
            "anomaly":         _env_on("ANOMALY_SCAN"),
            "anomaly_extra":   _env_on("ANOMALY_SCAN"),   # rode ANOMALY_SCAN before
            "anomaly_watch":   _env_on("ANOMALY_SCAN"),
            "betlive_anomaly": _env_on("BETLIVE_ANOMALY"),
            "soft_scan":       _env_on("SOFT_SCAN"),
        },
        "cadence": {k: f() for k, (f, _lo, _hi) in CADENCES.items()},
        "limits": {k: f() for k, (f, _lo, _hi) in LIMITS.items()},
    }


def _merge(base: dict, saved: dict) -> dict:
    """Saved values win, but only for keys we still know about."""
    out = json.loads(json.dumps(base))
    for section in ("books", "scans", "cadence", "limits"):
        for k, v in (saved.get(section) or {}).items():
            if k in out[section]:
                out[section][k] = v
    return out


def load(force: bool = False) -> dict[str, Any]:
    global _cfg, _loaded
    with _lock:
        if _loaded and not force:
            return _cfg
        base = _defaults()
        try:
            if CONFIG_PATH.exists():
                base = _merge(base, json.loads(CONFIG_PATH.read_text()))
                log.info("runtime config loaded from %s", CONFIG_PATH)
        except Exception as e:
            log.warning("runtime config unreadable (%s) — using env defaults: %s",
                        CONFIG_PATH, e)
        _cfg = base
        _loaded = True
        return _cfg


def get() -> dict[str, Any]:
    """The whole config (a copy — callers must not mutate the store)."""
    return json.loads(json.dumps(load()))


def is_on(section: str, key: str) -> bool:
    return bool(load().get(section, {}).get(key, False))


def book_on(book: str) -> bool:
    return is_on("books", book)


def num(section: str, key: str, default: float = 0.0) -> float:
    v = load().get(section, {}).get(key)
    return float(v) if isinstance(v, (int, float)) else default


def secs(key: str, default: int = 60) -> int:
    return int(num("cadence", key, default))


def _clamp(val, lo, hi):
    return max(lo, min(hi, val))


def update(patch: dict[str, Any]) -> dict[str, Any]:
    """Apply a partial update. Raises ValueError on unknown keys / bad types."""
    cfg = load()
    with _lock:
        for section, values in (patch or {}).items():
            if section not in ("books", "scans", "cadence", "limits"):
                raise ValueError(f"unknown config section {section!r}")
            if not isinstance(values, dict):
                raise ValueError(f"section {section!r} must be an object")
            for key, val in values.items():
                if key not in cfg[section]:
                    raise ValueError(f"unknown config key {section}.{key}")
                if section in ("books", "scans"):
                    if not isinstance(val, bool):
                        raise ValueError(f"{section}.{key} must be true/false")
                    cfg[section][key] = val
                else:
                    if isinstance(val, bool) or not isinstance(val, (int, float)):
                        raise ValueError(f"{section}.{key} must be a number")
                    lo, hi = (CADENCES if section == "cadence" else LIMITS)[key][1:3]
                    cfg[section][key] = (int(_clamp(val, lo, hi))
                                         if section == "cadence"
                                         else float(_clamp(val, lo, hi)))
        _save_locked(cfg)
    return get()


def reset() -> dict[str, Any]:
    """Back to the env-seeded defaults (and forget the saved file)."""
    global _cfg
    with _lock:
        _cfg = _defaults()
        _save_locked(_cfg)
    return get()


def _save_locked(cfg: dict) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=2, sort_keys=True))
        tmp.replace(CONFIG_PATH)          # atomic
    except Exception as e:
        log.warning("could not persist runtime config: %s", e)
