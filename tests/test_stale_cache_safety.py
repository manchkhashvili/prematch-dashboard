"""
Tests for the stale-cache safety net in crystalbet.py.

When expand keeps failing for the same game, we shouldn't serve increasingly-
old cached detail Odds forever. Past _STALE_CACHE_MAX_AGE_SEC (30 min), we
fall back to fresh list-view Odds instead. This guards against the failure
mode the user hit on 2026-05-25 (ITD Santa Tecla case) where the dashboard
showed 4+ hour old prices because expansion silently failed every cycle.

Run from prematch/:
    python -m pytest tests/ -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.models import Odds  # noqa: E402
from src.scrapers import change_cache  # noqa: E402
from src.scrapers.crystalbet import (  # noqa: E402
    _STALE_CACHE_MAX_AGE_SEC,
    _is_cached_detail_fresh_enough,
)


NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _make_odds() -> Odds:
    return Odds(
        source="crystalbet",
        sport="basketball",
        home="Hawks",
        away="Lakers",
        market_type="moneyline",
        period="FT",
        selections={"home": 1.91, "away": 1.91},
        fetched_at=NOW,
        line=None,
        start_time=NOW,
        league="NBA",
        raw_event_id="evt-1",
    )


def _entry_aged(seconds_ago: float) -> change_cache.CacheEntry:
    return change_cache.CacheEntry(
        loadinfo_hash="abc",
        last_expanded_at=NOW - timedelta(seconds=seconds_ago),
        detail_status="loaded",
    )


# ── Happy path: recently cached data is fresh enough ──────────────────────────
class TestFreshEnough:
    def test_just_expanded_is_fresh(self):
        cached = [_make_odds()]
        entry = _entry_aged(seconds_ago=5)
        assert _is_cached_detail_fresh_enough(cached, entry, now=NOW) is True

    def test_5_minutes_old_is_fresh(self):
        cached = [_make_odds()]
        entry = _entry_aged(seconds_ago=5 * 60)
        assert _is_cached_detail_fresh_enough(cached, entry, now=NOW) is True

    def test_just_under_max_age_is_fresh(self):
        cached = [_make_odds()]
        entry = _entry_aged(seconds_ago=_STALE_CACHE_MAX_AGE_SEC - 1)
        assert _is_cached_detail_fresh_enough(cached, entry, now=NOW) is True

    def test_exactly_at_max_age_is_fresh(self):
        """Boundary: ≤ max_age means still fresh."""
        cached = [_make_odds()]
        entry = _entry_aged(seconds_ago=_STALE_CACHE_MAX_AGE_SEC)
        assert _is_cached_detail_fresh_enough(cached, entry, now=NOW) is True


# ── Drop paths: too old or missing ────────────────────────────────────────────
class TestTooStale:
    def test_just_over_max_age_is_stale(self):
        cached = [_make_odds()]
        entry = _entry_aged(seconds_ago=_STALE_CACHE_MAX_AGE_SEC + 1)
        assert _is_cached_detail_fresh_enough(cached, entry, now=NOW) is False

    def test_hours_old_is_stale(self):
        """Very-old cache (8h) — past the 6h cap bumped in Phase 2.5 #4.

        Pre-Phase-2.5 #4 the cap was 30 min and the canonical 'definitely
        stale' case was the ITD Santa Tecla incident (4h+ old) from
        2026-05-25. The cap got bumped to 6h so disk-loaded warm caches
        actually serve detail Odds across normal restart gaps. The
        'really stale' boundary now lives at 6h+.
        """
        cached = [_make_odds()]
        entry = _entry_aged(seconds_ago=8 * 3600)
        assert _is_cached_detail_fresh_enough(cached, entry, now=NOW) is False

    def test_no_cached_returns_false(self):
        """Nothing cached → nothing to serve. Caller uses list-view."""
        entry = _entry_aged(seconds_ago=5)
        assert _is_cached_detail_fresh_enough(None, entry, now=NOW) is False

    def test_empty_cached_list_returns_false(self):
        """Empty list is falsy and shouldn't be served."""
        entry = _entry_aged(seconds_ago=5)
        assert _is_cached_detail_fresh_enough([], entry, now=NOW) is False

    def test_no_entry_returns_false(self):
        """No cache entry → no confidence in the cached odds' freshness."""
        cached = [_make_odds()]
        assert _is_cached_detail_fresh_enough(cached, None, now=NOW) is False

    def test_entry_with_no_timestamp_returns_false(self):
        """Entry exists but never had a successful expansion → no timestamp.
        Don't serve cached data we don't know the age of."""
        cached = [_make_odds()]
        entry = change_cache.CacheEntry(
            loadinfo_hash="abc",
            last_expanded_at=None,
            detail_status="list_only",
        )
        assert _is_cached_detail_fresh_enough(cached, entry, now=NOW) is False


# ── Custom max_age ────────────────────────────────────────────────────────────
class TestCustomMaxAge:
    def test_caller_can_override_max_age(self):
        cached = [_make_odds()]
        entry = _entry_aged(seconds_ago=120)   # 2 min
        # Default 30 min → fresh
        assert _is_cached_detail_fresh_enough(cached, entry, now=NOW) is True
        # But if caller wants only 60s tolerance → stale
        assert _is_cached_detail_fresh_enough(
            cached, entry, now=NOW, max_age_sec=60,
        ) is False


# ── Default `now` uses wall clock ─────────────────────────────────────────────
class TestDefaultNow:
    def test_no_now_uses_utcnow(self):
        """When `now` is None we use datetime.now(timezone.utc). Just verify
        it doesn't crash and behaves sensibly for a very-recent entry."""
        cached = [_make_odds()]
        entry = change_cache.CacheEntry(
            loadinfo_hash="abc",
            last_expanded_at=datetime.now(timezone.utc),
            detail_status="loaded",
        )
        assert _is_cached_detail_fresh_enough(cached, entry) is True
