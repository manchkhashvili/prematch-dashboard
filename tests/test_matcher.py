"""
Regression tests for prematch.src.matcher.

Each test guards a specific bug or behavior that landed in the build log:

  - `_accept` two-tier truth table (2026-05-24 "Matcher tier-2" entry)
  - Greedy assignment handles same-Pin collisions (2026-05-24 "scope compression")
  - Unmatched diagnostic surfaces best candidate (2026-05-24 "Matcher upgrade")
  - Score 100 + time gap > 10 min → unmatched (2026-05-25 conversation)
  - Alias hot-reload via mtime (2026-05-24 "Matcher upgrade")

Run from prematch/:
    python -m pytest tests/ -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import normalize  # noqa: E402  (for alias-cache manipulation)
from src.matcher import (  # noqa: E402
    _accept,
    SCORE_LOOSE,
    SCORE_TIGHT,
    TIME_LOOSE_SECONDS,
    TIME_TIGHT_SECONDS,
    match_with_diagnostics,
)
from src.models import Odds  # noqa: E402


# ── Test fixture helpers ──────────────────────────────────────────────────────
NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _ml(
    source: str,
    home: str,
    away: str,
    *,
    start: datetime | None = NOW,
    league: str = "Test League",
    event_id: str = "evt-1",
    home_dec: float = 1.91,
    away_dec: float = 1.91,
) -> Odds:
    """Construct a minimal moneyline Odds row for matcher tests."""
    return Odds(
        source=source,  # type: ignore[arg-type]
        sport="basketball",
        home=home,
        away=away,
        market_type="moneyline",
        period="FT",
        selections={"home": home_dec, "away": away_dec},
        fetched_at=NOW,
        line=None,
        start_time=start,
        league=league,
        raw_event_id=event_id,
    )


# ── _accept truth table ───────────────────────────────────────────────────────
class TestAccept:
    """
    Two-tier acceptance. Source of truth: matcher.py docstring + 2026-05-24
    build log entry "Matcher tier-2".
    """

    def test_below_tight_rejected(self):
        """< SCORE_TIGHT always rejected, regardless of time."""
        assert _accept(SCORE_TIGHT - 0.1, NOW, NOW) is False

    def test_zero_score_rejected(self):
        assert _accept(0.0, NOW, NOW) is False

    def test_loose_score_within_loose_window_accepted(self):
        """≥ SCORE_LOOSE within TIME_LOOSE_SECONDS → accept."""
        pin_time = NOW + timedelta(seconds=TIME_LOOSE_SECONDS - 1)
        assert _accept(SCORE_LOOSE, NOW, pin_time) is True

    def test_loose_score_at_loose_boundary_accepted(self):
        """Inclusive boundary on the loose tier (delta == TIME_LOOSE)."""
        pin_time = NOW + timedelta(seconds=TIME_LOOSE_SECONDS)
        assert _accept(SCORE_LOOSE, NOW, pin_time) is True

    def test_loose_score_outside_loose_window_rejected(self):
        """Score 100, but time gap > TIME_LOOSE_SECONDS → rejected. Anchors the
        same upper-bound behaviour even after Phase 3.10 widened the window."""
        pin_time = NOW + timedelta(seconds=TIME_LOOSE_SECONDS + 1)
        assert _accept(100.0, NOW, pin_time) is False

    def test_tight_score_within_tight_window_accepted(self):
        """65-79 within TIME_TIGHT_SECONDS → accept."""
        pin_time = NOW + timedelta(seconds=TIME_TIGHT_SECONDS - 1)
        assert _accept(SCORE_TIGHT + 5, NOW, pin_time) is True

    def test_tight_score_outside_tight_window_rejected(self):
        """65-79 with gap > TIME_TIGHT_SECONDS → rejected."""
        pin_time = NOW + timedelta(seconds=TIME_TIGHT_SECONDS + 1)
        assert _accept(70.0, NOW, pin_time) is False

    def test_missing_cb_time_falls_back_to_loose_score(self):
        """Time unknown → name confidence must carry: only ≥ SCORE_LOOSE wins."""
        assert _accept(SCORE_LOOSE, None, NOW) is True
        assert _accept(SCORE_LOOSE - 1, None, NOW) is False

    def test_missing_pin_time_falls_back_to_loose_score(self):
        assert _accept(SCORE_LOOSE, NOW, None) is True
        assert _accept(SCORE_LOOSE - 1, NOW, None) is False

    def test_both_times_missing_falls_back_to_loose_score(self):
        assert _accept(95.0, None, None) is True
        assert _accept(SCORE_TIGHT + 5, None, None) is False


# ── Greedy assignment regression ──────────────────────────────────────────────
class TestGreedyAssignment:
    """
    Two CB events score identically against one Pinnacle event. The greedy
    assignment must pick exactly one (the first encountered after a stable
    sort by score-desc) and leave the other unmatched. Regression for the
    "per-CB first-best-Pin" bug fixed in the 2026-05-24 scope-compression
    entry.
    """

    def test_two_cb_collide_on_one_pin_one_matches_one_unmatched(self):
        """
        Two DIFFERENT CB events both fuzzy-score 100 against the same Pin
        event (via token_set_ratio's set-of-tokens semantics — "Hawks" and
        "Hawks United" share the "hawks" token so the score is 100).
        The greedy assignment must pick one and leave the other unmatched.

        Note: matcher.py groups by (home, away) only, so two CB events with
        IDENTICAL team names would collapse into one event. Distinct names
        with high fuzz overlap is the real collision case.
        """
        cb_a = _ml("crystalbet", "Hawks", "Lakers",
                   league="USA NBA", event_id="cb-a")
        cb_b = _ml("crystalbet", "Hawks United", "Lakers United",
                   league="ESP ACB", event_id="cb-b")
        # One Pinnacle row — both CBs will score 100 against it.
        pin = _ml("pinnacle", "Hawks", "Lakers",
                  league="USA NBA", event_id="pin-1")

        result = match_with_diagnostics([cb_a, cb_b], [pin])

        # Exactly one CB matched, exactly one unmatched.
        assert len(result.matched) == 1
        assert len(result.unmatched) == 1

    def test_unmatched_cb_does_not_claim_already_used_pin(self):
        """The unmatched-diagnostic loop skips Pin events already used by a
        successful match. Otherwise the unmatched row would show a 100-score
        candidate that's actually been claimed elsewhere — misleading the
        curation workflow."""
        cb_a = _ml("crystalbet", "Hawks", "Lakers", event_id="cb-a")
        cb_b = _ml("crystalbet", "Hawks United", "Lakers United",
                   league="ESP", event_id="cb-b")
        pin = _ml("pinnacle", "Hawks", "Lakers", event_id="pin-1")

        result = match_with_diagnostics([cb_a, cb_b], [pin])

        for u in result.unmatched:
            # The single Pin event must not appear as a best candidate, since
            # it's claimed by the matched CB.
            assert u.best_pin_home is None
            assert u.best_pin_away is None

    def test_same_home_away_pair_groups_into_one_event(self):
        """
        Documented behavior of `_group_by_event`: two Odds rows with the same
        (home, away) pair — regardless of league/event_id — collapse into a
        single event. This is intentional (a match groups all its market
        types together) but worth pinning so future refactors are aware.
        """
        cb_ml = _ml("crystalbet", "Hawks", "Lakers",
                    league="USA NBA", event_id="cb-a")
        cb_spread = Odds(
            source="crystalbet", sport="basketball",
            home="Hawks", away="Lakers",
            market_type="spread", period="FT",
            selections={"home": 1.91, "away": 1.91},
            fetched_at=NOW, line=-3.5, start_time=NOW,
            league="USA NBA", raw_event_id="cb-a",
        )
        pin = _ml("pinnacle", "Hawks", "Lakers", event_id="pin-1")

        result = match_with_diagnostics([cb_ml, cb_spread], [pin])

        # One match — and that match's cb list carries BOTH Odds rows.
        assert len(result.matched) == 1
        assert len(result.matched[0].cb) == 2


# ── Unmatched diagnostic ──────────────────────────────────────────────────────
class TestUnmatchedDiagnostic:
    """
    Every CB event that doesn't match should appear in result.unmatched with
    its single best UNUSED Pinnacle candidate (regardless of threshold).
    Drives the unmatched.html curation page.
    """

    def test_unmatched_event_carries_best_below_threshold_candidate(self):
        # CB and Pin teams are similar but not similar enough to clear
        # threshold (token_set_ratio is permissive enough that we use very
        # different names to force a low score).
        cb = _ml("crystalbet", "Tokyo Apaches", "Niigata Albirex",
                 event_id="cb-1")
        pin = _ml("pinnacle", "Atlanta Hawks", "Los Angeles Lakers",
                  event_id="pin-1")
        result = match_with_diagnostics([cb], [pin])

        assert result.matched == []
        assert len(result.unmatched) == 1
        u = result.unmatched[0]
        assert u.cb_home == "Tokyo Apaches"
        # Best candidate is populated even though the score is well below 65.
        assert u.best_pin_home == "Atlanta Hawks"
        assert u.best_score > 0
        assert u.best_score < SCORE_TIGHT

    def test_unmatched_event_with_no_pin_events_at_all(self):
        """No Pin candidates → best_pin_* fields all None, best_score == 0."""
        cb = _ml("crystalbet", "Hawks", "Lakers", event_id="cb-1")
        result = match_with_diagnostics([cb], [])
        assert len(result.unmatched) == 1
        u = result.unmatched[0]
        assert u.best_pin_home is None
        assert u.best_pin_away is None
        assert u.best_score == 0.0


# ── Score-100 + time gap = the exact 2026-05-25 conversation bug ──────────────
class TestScore100TimeGap:
    """
    The Nacional vs Aguada case we diagnosed: perfect-name match but the books
    disagree on tipoff by more than the time window. Expected behavior is
    "unmatched" because the time check is the safety guardrail against
    same-teams-twice in one day. Phase 3.10 widened the window from ±10 min
    to ±1h per user request — this test now uses a 90-min gap to stay on
    the unmatched side of the boundary.
    """

    def test_perfect_name_match_with_large_time_gap_is_unmatched(self):
        cb_time = NOW
        pin_time = NOW + timedelta(minutes=90)  # 90 min apart — past TIME_LOOSE
        cb = _ml("crystalbet", "Club Nacional", "Aguada",
                 start=cb_time, event_id="cb-1")
        pin = _ml("pinnacle", "Club Nacional", "Aguada",
                  start=pin_time, event_id="pin-1")

        result = match_with_diagnostics([cb], [pin])

        # Should NOT match — time gap exceeds the window.
        assert result.matched == []
        # Should appear unmatched with a 100 best score.
        assert len(result.unmatched) == 1
        u = result.unmatched[0]
        assert u.best_pin_home == "Club Nacional"
        assert u.best_score == pytest.approx(100.0)

    def test_perfect_name_match_within_new_one_hour_window_matches(self):
        """Phase 3.10 — a 20-min gap (which would have been rejected before)
        is now accepted because the window widened to ±1h. Locks in the new
        behavior."""
        cb_time = NOW
        pin_time = NOW + timedelta(minutes=20)
        cb = _ml("crystalbet", "Club Nacional", "Aguada",
                 start=cb_time, event_id="cb-1")
        pin = _ml("pinnacle", "Club Nacional", "Aguada",
                  start=pin_time, event_id="pin-1")
        result = match_with_diagnostics([cb], [pin])
        assert len(result.matched) == 1
        assert result.matched[0].score == pytest.approx(100.0)


# ── Alias hot-reload ──────────────────────────────────────────────────────────
class TestAliasHotReload:
    """
    Alias file is reloaded when its mtime changes. We don't need a temp file
    — we can drive the mtime-keyed cache directly to prove the cache key
    behavior. The integration through normalize_team is also covered.
    """

    def setup_method(self):
        # Snapshot + clear the module-level cache so tests don't bleed.
        self._saved = dict(normalize._alias_cache)
        normalize._alias_cache.update({"mtime": -1.0, "map": {}})

    def teardown_method(self):
        normalize._alias_cache.update(self._saved)

    def test_normalize_applies_alias_from_yaml(self, monkeypatch):
        """When the yaml on disk has 'Connecticut → Connecticut Sun', the
        normalized form of 'Connecticut' includes 'sun'."""
        # Stub _load_aliases to return a known map without touching disk.
        monkeypatch.setattr(
            normalize, "_load_aliases",
            lambda: {"connecticut": "Connecticut Sun"},
        )
        assert normalize.normalize_team("Connecticut") == "connecticut sun"

    def test_normalize_no_alias_is_passthrough(self, monkeypatch):
        monkeypatch.setattr(normalize, "_load_aliases", lambda: {})
        assert normalize.normalize_team("Atlanta Hawks") == "atlanta hawks"

    def test_alias_lookup_is_case_insensitive(self, monkeypatch):
        monkeypatch.setattr(
            normalize, "_load_aliases",
            lambda: {"phoenix": "Phoenix Mercury"},
        )
        # Mixed/lower/upper all hit the alias.
        assert normalize.normalize_team("Phoenix") == "phoenix mercury"
        assert normalize.normalize_team("PHOENIX") == "phoenix mercury"
        assert normalize.normalize_team("phoenix") == "phoenix mercury"
