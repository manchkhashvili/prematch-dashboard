"""
Tests for src/scrapers/change_cache.py.

Cache state transitions and decision rules — pure data, no I/O. Each test
constructs its own ChangeCache to avoid the module-level singleton bleeding
state between tests.

Run from prematch/:
    python -m pytest tests/ -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scrapers.change_cache import (  # noqa: E402
    CacheEntry,
    ChangeCache,
    get_cache,
    reset_cache,
)


LOADINFO_A = '[{"name":"1","bet":"1.50"},{"name":" 2","bet":"2.50"}]'
LOADINFO_A_PRIME = '[{"name":"1","bet":"1.55"},{"name":" 2","bet":"2.40"}]'  # different
LOADINFO_B = '[{"name":"1","bet":"1.91"},{"name":" 2","bet":"1.91"}]'


# ── Decision logic: needs_expansion ───────────────────────────────────────────
class TestNeedsExpansion:
    def test_first_seen_event_needs_expansion(self):
        c = ChangeCache()
        assert c.needs_expansion("evt-1", LOADINFO_A) is True

    def test_unchanged_hash_does_not_need_expansion(self):
        c = ChangeCache()
        c.mark_loaded("evt-1", LOADINFO_A)
        assert c.needs_expansion("evt-1", LOADINFO_A) is False

    def test_changed_hash_needs_expansion(self):
        c = ChangeCache()
        c.mark_loaded("evt-1", LOADINFO_A)
        # Different loadinfo → different hash → re-expand.
        assert c.needs_expansion("evt-1", LOADINFO_A_PRIME) is True

    def test_unchanged_hash_with_prior_failure_does_not_retry(self):
        """
        Failure happened last cycle, hash hasn't changed since → don't
        hammer the broken path. A real market move (hash change) reopens
        the door. Recorded status is still surfaced via get_status.
        """
        c = ChangeCache()
        c.mark_expand_failed("evt-1", LOADINFO_A)
        assert c.needs_expansion("evt-1", LOADINFO_A) is False
        assert c.get_status("evt-1") == "expand_failed"

    def test_changed_hash_after_failure_does_retry(self):
        c = ChangeCache()
        c.mark_expand_failed("evt-1", LOADINFO_A)
        # Odds moved → another attempt warranted.
        assert c.needs_expansion("evt-1", LOADINFO_A_PRIME) is True


# ── State recording ───────────────────────────────────────────────────────────
class TestMarking:
    def test_mark_loaded_sets_status_and_timestamp(self):
        c = ChangeCache()
        before = datetime.now(timezone.utc)
        c.mark_loaded("evt-1", LOADINFO_A)
        after = datetime.now(timezone.utc)
        e = c.entries["evt-1"]
        assert e.detail_status == "loaded"
        assert e.loadinfo_hash == c.hash_loadinfo(LOADINFO_A)
        assert e.last_expanded_at is not None
        assert before <= e.last_expanded_at <= after

    def test_mark_expand_failed_preserves_prior_timestamp(self):
        """
        A subsequent failure doesn't wipe the timestamp of the LAST successful
        expansion — useful for diagnostics ("we had detail data 30 min ago,
        the last 2 cycles failed").
        """
        c = ChangeCache()
        c.mark_loaded("evt-1", LOADINFO_A)
        original_ts = c.entries["evt-1"].last_expanded_at
        c.mark_expand_failed("evt-1", LOADINFO_A_PRIME)
        assert c.entries["evt-1"].detail_status == "expand_failed"
        assert c.entries["evt-1"].last_expanded_at == original_ts

    def test_mark_loaded_clears_prior_failure(self):
        c = ChangeCache()
        c.mark_expand_failed("evt-1", LOADINFO_A)
        c.mark_loaded("evt-1", LOADINFO_A)
        assert c.entries["evt-1"].detail_status == "loaded"

    def test_mark_list_only_first_time(self):
        c = ChangeCache()
        c.mark_list_only("evt-1", LOADINFO_A)
        e = c.entries["evt-1"]
        assert e.detail_status == "list_only"
        assert e.last_expanded_at is None


# ── Per-event isolation ───────────────────────────────────────────────────────
class TestIsolation:
    def test_two_events_tracked_independently(self):
        c = ChangeCache()
        c.mark_loaded("evt-1", LOADINFO_A)
        c.mark_expand_failed("evt-2", LOADINFO_B)
        assert c.get_status("evt-1") == "loaded"
        assert c.get_status("evt-2") == "expand_failed"
        # evt-1 needs expansion when ITS hash changes; evt-2's hash is independent.
        assert c.needs_expansion("evt-1", LOADINFO_A) is False
        assert c.needs_expansion("evt-1", LOADINFO_A_PRIME) is True
        assert c.needs_expansion("evt-2", LOADINFO_B) is False


# ── Pruning ───────────────────────────────────────────────────────────────────
class TestPruning:
    def test_prune_drops_missing_events(self):
        c = ChangeCache()
        c.mark_loaded("evt-1", LOADINFO_A)
        c.mark_loaded("evt-2", LOADINFO_B)
        c.mark_loaded("evt-3", LOADINFO_A)
        # Only evt-1 and evt-3 still on the list this cycle.
        dropped = c.prune_missing({"evt-1", "evt-3"})
        assert dropped == 1
        assert "evt-1" in c.entries
        assert "evt-2" not in c.entries
        assert "evt-3" in c.entries

    def test_prune_with_no_missing_returns_zero(self):
        c = ChangeCache()
        c.mark_loaded("evt-1", LOADINFO_A)
        dropped = c.prune_missing({"evt-1"})
        assert dropped == 0

    def test_prune_empty_set_drops_everything(self):
        c = ChangeCache()
        c.mark_loaded("evt-1", LOADINFO_A)
        c.mark_loaded("evt-2", LOADINFO_B)
        dropped = c.prune_missing(set())
        assert dropped == 2
        assert c.entries == {}


# ── Hash stability ────────────────────────────────────────────────────────────
class TestHashing:
    def test_same_input_same_hash(self):
        c = ChangeCache()
        h1 = c.hash_loadinfo(LOADINFO_A)
        h2 = c.hash_loadinfo(LOADINFO_A)
        assert h1 == h2
        # SHA-256 → 64 hex chars
        assert len(h1) == 64

    def test_different_input_different_hash(self):
        c = ChangeCache()
        assert c.hash_loadinfo(LOADINFO_A) != c.hash_loadinfo(LOADINFO_A_PRIME)

    def test_empty_string_hashable(self):
        """Edge case: a game with no loadinfo (Format B div-positional)."""
        c = ChangeCache()
        h = c.hash_loadinfo("")
        assert len(h) == 64
        # Subsequent empty-string lookups should match.
        assert c.hash_loadinfo("") == h


# ── Module singleton ──────────────────────────────────────────────────────────
class TestSingleton:
    def setup_method(self):
        reset_cache()

    def teardown_method(self):
        reset_cache()

    def test_get_cache_returns_singleton(self):
        c1 = get_cache()
        c2 = get_cache()
        assert c1 is c2

    def test_reset_cache_clears_state(self):
        c = get_cache()
        c.mark_loaded("evt-1", LOADINFO_A)
        assert "evt-1" in c.entries
        reset_cache()
        # After reset, get_cache returns a NEW empty instance
        new_c = get_cache()
        assert new_c is not c
        assert new_c.entries == {}


# ── get_status for unknown events ─────────────────────────────────────────────
class TestStatus:
    def test_unknown_event_status_is_list_only(self):
        c = ChangeCache()
        assert c.get_status("never-seen") == "list_only"

    def test_known_event_returns_recorded_status(self):
        c = ChangeCache()
        c.mark_loaded("evt-1", LOADINFO_A)
        c.mark_expand_failed("evt-2", LOADINFO_B)
        assert c.get_status("evt-1") == "loaded"
        assert c.get_status("evt-2") == "expand_failed"
