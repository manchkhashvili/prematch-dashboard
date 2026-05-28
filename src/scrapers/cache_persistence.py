"""
Disk persistence for the CB change-detection cache + detail-odds cache.

Without persistence, restarting the dashboard wipes both caches and forces
a full ~3-4 min cold expansion cycle. With persistence, restart resumes
where we left off — the cache loads from disk, hashes are compared against
fresh list-view data, and only games whose odds moved since the save get
re-expanded. Typical restart re-warm: <1 minute.

Format: single JSON file at `data/cache/cb_change_cache.json`. Two top-level
keys:
  - entries        — change_cache.ChangeCache state per event_id
  - detail_odds    — the actual cached Odds rows per event_id

Staleness handling: if the saved file is older than MAX_AGE_HOURS, we
discard it on load. Restarts after a long break would just re-fetch
everything anyway (every odd has likely moved), so reusing day-old cached
odds would be net-negative — fresh hashes would mismatch en masse and
we'd re-expand everything regardless.

Lifecycle: save on FastAPI shutdown via the lifespan handler in app.py.
For graceful shutdowns (Ctrl+C, SIGTERM) this catches everything; SIGKILL
or crash would lose any since-last-save state. v1 keeps it simple — no
periodic saves. Add later if crash recovery becomes a felt problem.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from src.models import Odds
from src.scrapers import change_cache, crystalbet

log = logging.getLogger(__name__)

# Schema version. Bumped if we ever change the on-disk shape so old files
# get discarded rather than crashing the loader on missing/renamed fields.
SCHEMA_VERSION = 1

# Cache older than this gets discarded on load — odds have surely moved enough
# in that time that a fresh fetch is faster than reconciling against stale hashes.
MAX_AGE_HOURS = 12

CACHE_DIR = (
    Path(__file__).resolve().parent.parent.parent / "data" / "cache"
)

# Pre-Phase-2 file path. Kept for back-compat with existing on-disk caches —
# basketball will read from here as a fallback if the new sport-namespaced
# file (cb_change_cache_basketball.json) doesn't exist yet.
CACHE_FILE_PATH = CACHE_DIR / "cb_change_cache.json"


def _path_for_sport(sport_name: str) -> Path:
    """Default on-disk path for a sport's cache file. Phase 2: sport-namespaced."""
    return CACHE_DIR / f"cb_change_cache_{sport_name}.json"


# ── Public API ────────────────────────────────────────────────────────────────
def save(path: Optional[Path] = None, *, sport_name: str = "basketball") -> bool:
    """
    Serialize the change cache + detail-odds cache for `sport_name` to disk.
    Returns True on success, False on any I/O failure (logged but never raised
    — saving is best-effort, the dashboard shouldn't fail to shut down because
    of it).

    `path` defaults to the sport-namespaced location (Phase 2). Tests that
    pass an explicit path continue to work unchanged.
    """
    if path is None:
        path = _path_for_sport(sport_name)
    try:
        cache = change_cache.get_cache(sport_name)
        detail = crystalbet.get_detail_odds_cache(sport_name)

        payload = {
            "version": SCHEMA_VERSION,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "entries": {
                eid: _entry_to_dict(e)
                for eid, e in cache.entries.items()
            },
            "detail_odds": {
                eid: [_odds_to_dict(o) for o in odds_list]
                for eid, odds_list in detail.items()
            },
        }

        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to tmp and atomically rename, so a crash mid-write doesn't
        # leave a corrupt file.
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))
        tmp.replace(path)

        size_kb = path.stat().st_size / 1024
        log.info(
            "cache_persistence: saved %d entries, %d events with detail odds → %s (%.1f KB)",
            len(payload["entries"]), len(payload["detail_odds"]),
            path.name, size_kb,
        )
        return True
    except Exception as e:
        log.warning("cache_persistence: save failed (%s)", e)
        return False


def load(path: Optional[Path] = None, *, sport_name: str = "basketball") -> bool:
    """
    Restore the change cache + detail-odds cache for `sport_name` from disk.

    Returns True if the cache was successfully loaded, False otherwise
    (missing file, corrupt JSON, version mismatch, too old, or any
    exception). On failure the existing in-memory caches are left
    unchanged — typically that's empty (fresh start).

    `path` defaults to the sport-namespaced location. For basketball, if
    that path is missing but the pre-Phase-2 `cb_change_cache.json` exists,
    we fall back to it — preserves the user's existing on-disk cache
    across the rename.
    """
    if path is None:
        path = _path_for_sport(sport_name)
        # Pre-Phase-2 migration: if the sport-namespaced file doesn't yet
        # exist but the old basketball-default file does, use it.
        if sport_name == "basketball" and not path.exists() and CACHE_FILE_PATH.exists():
            log.info(
                "cache_persistence: using legacy %s for basketball "
                "(will write to %s on next save)",
                CACHE_FILE_PATH.name, path.name,
            )
            path = CACHE_FILE_PATH

    if not path.exists():
        log.info("cache_persistence: no cache file at %s — starting fresh", path)
        return False

    try:
        with path.open(encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("cache_persistence: file unreadable (%s) — starting fresh", e)
        return False

    if payload.get("version") != SCHEMA_VERSION:
        log.warning(
            "cache_persistence: schema version %s != %d — discarding stale cache",
            payload.get("version"), SCHEMA_VERSION,
        )
        return False

    saved_at_str = payload.get("saved_at")
    if saved_at_str:
        try:
            saved_at = datetime.fromisoformat(saved_at_str)
            if saved_at.tzinfo is None:
                saved_at = saved_at.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - saved_at
            if age > timedelta(hours=MAX_AGE_HOURS):
                log.info(
                    "cache_persistence: cache is %.1f h old (> %d h max) — discarding",
                    age.total_seconds() / 3600, MAX_AGE_HOURS,
                )
                return False
        except ValueError:
            log.warning("cache_persistence: bad saved_at %r — discarding", saved_at_str)
            return False

    # Rebuild change_cache.entries
    try:
        cache = change_cache.get_cache(sport_name)
        cache.entries.clear()
        for eid, entry_dict in payload.get("entries", {}).items():
            cache.entries[eid] = _entry_from_dict(entry_dict)
    except (KeyError, ValueError) as e:
        log.warning("cache_persistence: bad entries section (%s) — starting fresh", e)
        change_cache.reset_cache(sport_name)
        return False

    # Rebuild detail-odds cache. Per-event try/except — one bad event
    # shouldn't lose the whole cache.
    detail_cache: dict[str, list[Odds]] = {}
    for eid, odds_dicts in payload.get("detail_odds", {}).items():
        try:
            detail_cache[eid] = [_odds_from_dict(d) for d in odds_dicts]
        except (KeyError, ValueError, TypeError) as e:
            log.debug("cache_persistence: skipping bad odds for %s (%s)", eid, e)

    crystalbet.restore_detail_odds_cache(detail_cache, sport_name=sport_name)

    log.info(
        "cache_persistence: loaded %d entries, %d events with detail odds "
        "for %s (age=%s)",
        len(cache.entries), len(detail_cache), sport_name, saved_at_str,
    )
    return True


def save_all(sport_names: tuple[str, ...] = ("basketball", "soccer")) -> dict[str, bool]:
    """
    Convenience: save the change cache for each named sport. Returns a
    {sport_name: success} dict. Failures are non-fatal (logged + recorded).
    Called from app.py's lifespan on shutdown.
    """
    return {sport: save(sport_name=sport) for sport in sport_names}


def load_all(sport_names: tuple[str, ...] = ("basketball", "soccer")) -> dict[str, bool]:
    """
    Convenience: load the change cache for each named sport. Returns a
    {sport_name: success} dict. Called from app.py's lifespan on startup.
    """
    return {sport: load(sport_name=sport) for sport in sport_names}


# ── Serialization helpers ─────────────────────────────────────────────────────
def _entry_to_dict(e: change_cache.CacheEntry) -> dict:
    return {
        "loadinfo_hash": e.loadinfo_hash,
        "last_expanded_at": (
            e.last_expanded_at.isoformat()
            if e.last_expanded_at else None
        ),
        "detail_status": e.detail_status,
    }


def _entry_from_dict(d: dict) -> change_cache.CacheEntry:
    lea = d.get("last_expanded_at")
    return change_cache.CacheEntry(
        loadinfo_hash=d["loadinfo_hash"],
        last_expanded_at=datetime.fromisoformat(lea) if lea else None,
        detail_status=d.get("detail_status", "list_only"),
    )


def _odds_to_dict(o: Odds) -> dict:
    # Phase 2: submarket + team_side are additive — basketball Odds always
    # have them as None, soccer Odds may carry "corners" / "home" / etc.
    # Old saved files without these keys load fine via .get() → None default.
    return {
        "source": o.source,
        "sport": o.sport,
        "home": o.home,
        "away": o.away,
        "market_type": o.market_type,
        "period": o.period,
        "selections": dict(o.selections),
        "fetched_at": o.fetched_at.isoformat(),
        "line": o.line,
        "start_time": (
            o.start_time.isoformat() if o.start_time else None
        ),
        "league": o.league,
        "raw_event_id": o.raw_event_id,
        "submarket": o.submarket,
        "team_side": o.team_side,
    }


def _odds_from_dict(d: dict) -> Odds:
    return Odds(
        source=d["source"],
        sport=d["sport"],
        home=d["home"],
        away=d["away"],
        market_type=d["market_type"],
        period=d["period"],
        selections=d["selections"],
        fetched_at=datetime.fromisoformat(d["fetched_at"]),
        line=d.get("line"),
        start_time=(
            datetime.fromisoformat(d["start_time"])
            if d.get("start_time") else None
        ),
        league=d.get("league"),
        raw_event_id=d.get("raw_event_id"),
        submarket=d.get("submarket"),
        team_side=d.get("team_side"),
    )
