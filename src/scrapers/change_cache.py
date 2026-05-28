"""
Per-game change-detection cache for CrystalBet detail-page expansion.

Without caching, scraping detail pages for every CB game every cycle would
mean ~300 ExpandDetail postbacks per 5-min cycle. Even on a Cloudflare-friendly
schedule that's 60× our current CB load and would almost certainly trip
rate-limiting.

Strategy: hash each game's list-view loadinfo (the JSON CB ships on the
collapsed game row). The hash captures the headline market state. When the
hash changes between cycles, the game's odds moved → re-expand to get fresh
alt-lines. When the hash is stable, the alt-lines almost certainly haven't
moved either → re-use last cycle's detail-page result.

Cache state per game:
  hash                — sha256 of the list-view loadinfo (used to detect change)
  last_expanded_at    — datetime of the most recent successful detail-page fetch
  detail_status       — "loaded" | "list_only" | "expand_failed"

A game needs expansion when:
  - First seen (no cache entry)            → expand
  - Hash changed since last expansion       → expand
  - Otherwise                               → skip (use cached / list-only data)

This module is pure data + decision logic. No HTTP, no Playwright. The scraper
owns the actual ExpandDetail call and result handling.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

log = logging.getLogger(__name__)


DetailStatus = Literal["loaded", "list_only", "expand_failed"]


@dataclass
class CacheEntry:
    loadinfo_hash: str
    last_expanded_at: Optional[datetime] = None
    detail_status: DetailStatus = "list_only"


@dataclass
class ChangeCache:
    """Per-event-id cache of list-view hashes and detail-fetch status."""
    entries: dict[str, CacheEntry] = field(default_factory=dict)

    def hash_loadinfo(self, loadinfo: str) -> str:
        """Stable SHA-256 of a loadinfo JSON string. Empty string → fixed sentinel."""
        return hashlib.sha256((loadinfo or "").encode("utf-8")).hexdigest()

    def needs_expansion(self, event_id: str, loadinfo: str) -> bool:
        """
        Return True if this game should be re-expanded this cycle.

        Conditions:
          - First-seen event_id (no cache entry yet)            → True
          - Loadinfo hash changed since last expansion          → True
          - Loadinfo unchanged AND we already have detail       → False
          - Loadinfo unchanged AND last expansion failed        → False
            (don't retry forever on persistently-failing games; the
            failure is recorded and surfaced via detail_status. A
            subsequent hash change reopens the door.)
        """
        h = self.hash_loadinfo(loadinfo)
        entry = self.entries.get(event_id)
        if entry is None:
            return True
        if entry.loadinfo_hash != h:
            return True
        # Hash stable: skip regardless of last detail_status. If last cycle
        # got "loaded", we keep using it; if "expand_failed", we don't keep
        # hammering. A real market move (hash change) reopens the door.
        return False

    def mark_loaded(self, event_id: str, loadinfo: str) -> None:
        """Record a successful detail-page fetch — clears any failure state."""
        h = self.hash_loadinfo(loadinfo)
        self.entries[event_id] = CacheEntry(
            loadinfo_hash=h,
            last_expanded_at=datetime.now(timezone.utc),
            detail_status="loaded",
        )

    def mark_expand_failed(self, event_id: str, loadinfo: str) -> None:
        """Record a failed detail-page fetch. The list-view odds are still usable."""
        h = self.hash_loadinfo(loadinfo)
        existing = self.entries.get(event_id)
        # Preserve last_expanded_at if we had a successful expansion previously.
        self.entries[event_id] = CacheEntry(
            loadinfo_hash=h,
            last_expanded_at=existing.last_expanded_at if existing else None,
            detail_status="expand_failed",
        )

    def mark_list_only(self, event_id: str, loadinfo: str) -> None:
        """
        Record that the game was observed but never expanded this cycle.
        Used for the first-seen-but-skipped case (e.g., reached the cycle's
        expansion budget) or as the initial state before any expansion attempt.
        """
        h = self.hash_loadinfo(loadinfo)
        existing = self.entries.get(event_id)
        self.entries[event_id] = CacheEntry(
            loadinfo_hash=h,
            last_expanded_at=existing.last_expanded_at if existing else None,
            detail_status="list_only",
        )

    def get_status(self, event_id: str) -> DetailStatus:
        """Get the detail_status for a known event, or 'list_only' if unknown."""
        entry = self.entries.get(event_id)
        if entry is None:
            return "list_only"
        return entry.detail_status

    def prune_missing(self, present_event_ids: set[str]) -> int:
        """
        Drop entries for events that are no longer on the list (game started,
        game removed by CB, etc.). Returns the number of entries dropped.
        Call this once per cycle after the list-view scrape so the cache
        doesn't grow unboundedly.
        """
        before = len(self.entries)
        self.entries = {
            eid: e for eid, e in self.entries.items()
            if eid in present_event_ids
        }
        return before - len(self.entries)


# ── Per-sport singletons ──────────────────────────────────────────────────────
# Phase 2: each sport gets its own ChangeCache so basketball's prune_missing
# doesn't wipe soccer entries (and vice versa). The default sport_name is
# "basketball" so every pre-Phase-2 call site (tests, cache_persistence,
# crystalbet) keeps working without modification.
#
# Tests construct their own ChangeCache to keep state isolated — they don't
# go through this singleton table.
_caches: dict[str, ChangeCache] = {}


def get_cache(sport_name: str = "basketball") -> ChangeCache:
    """Return the module-level cache singleton for this sport. Creates on first access."""
    if sport_name not in _caches:
        _caches[sport_name] = ChangeCache()
    return _caches[sport_name]


def reset_cache(sport_name: Optional[str] = None) -> None:
    """
    Clear cache singletons. With no arg, clears ALL sports (matches the
    pre-Phase-2 behavior tests rely on). With a sport_name, clears just
    that sport's cache.
    """
    if sport_name is None:
        _caches.clear()
    else:
        _caches.pop(sport_name, None)
