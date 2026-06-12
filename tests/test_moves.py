"""
Tests for the Pinnacle steam / top-moves detector (Phase 5.1).

The detector lives in src/app.py: _compute_pin_moves measures each market
against a CHANGE-ANCHORED baseline (the snapshot where it last moved, or was
first seen — see TestChangeAnchoredDetection) and stores the rising-side
moves in _recent_moves.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.models import Odds  # noqa: E402
from src import app  # noqa: E402

NOW = datetime(2026, 5, 28, 15, 0, tzinfo=timezone.utc)


def _ml(home, away, h, a, eid="m1", sport="basketball"):
    return Odds(
        source="pinnacle", sport=sport, home=home, away=away,
        market_type="moneyline", period="FT",
        selections={"home": h, "away": a},
        fetched_at=NOW, line=None, start_time=NOW, league="NBA", raw_event_id=eid,
    )


def _total(home, away, line, over, under, eid="m1", sport="basketball"):
    return Odds(
        source="pinnacle", sport=sport, home=home, away=away,
        market_type="total", period="FT", line=line,
        selections={"over": over, "under": under},
        fetched_at=NOW, start_time=NOW, league="NBA", raw_event_id=eid,
    )


@pytest.fixture(autouse=True)
def reset_move_state():
    """Each test starts with clean move state for the 'basketball' sport."""
    app._pin_prev_fair["basketball"] = {}
    app._recent_moves["basketball"] = []
    yield
    app._pin_prev_fair["basketball"] = {}
    app._recent_moves["basketball"] = []


class TestComputePinMoves:
    def test_first_cycle_seeds_no_moves(self):
        app._compute_pin_moves("basketball", [_ml("Lakers", "Celtics", 2.0, 2.0)])
        assert app._recent_moves["basketball"] == []
        # But prev is now populated for the next cycle.
        assert len(app._pin_prev_fair["basketball"]) == 1

    def test_shortening_favorite_emits_move_on_rising_side(self):
        app._compute_pin_moves("basketball", [_ml("Lakers", "Celtics", 2.0, 2.0)])
        app._compute_pin_moves("basketball", [_ml("Lakers", "Celtics", 1.70, 2.40)])
        moves = app._recent_moves["basketball"]
        assert len(moves) == 1
        m = moves[0]
        assert m["side"] == "home"           # home shortened → fair prob rose
        assert m["delta_pp"] > 0
        assert m["old_odds"] == 2.0
        assert m["new_odds"] == 1.70
        assert m["new_prob_pct"] > m["old_prob_pct"]

    def test_only_rising_side_reported_not_both(self):
        # 2-way: if home rises, away falls. We must NOT report the falling side.
        app._compute_pin_moves("basketball", [_ml("A", "B", 2.0, 2.0)])
        app._compute_pin_moves("basketball", [_ml("A", "B", 1.50, 3.00)])
        moves = app._recent_moves["basketball"]
        assert len(moves) == 1
        assert moves[0]["side"] == "home"

    def test_stable_market_no_move(self):
        app._compute_pin_moves("basketball", [_ml("A", "B", 1.90, 1.90)])
        app._compute_pin_moves("basketball", [_ml("A", "B", 1.90, 1.90)])
        assert app._recent_moves["basketball"] == []

    def test_sub_threshold_move_filtered(self):
        # A tiny move (< _MOVE_MIN_PP=2.0) should not surface.
        app._compute_pin_moves("basketball", [_ml("A", "B", 2.00, 2.00)])
        # 1.98/2.02 is roughly a 0.5pp shift — below threshold.
        app._compute_pin_moves("basketball", [_ml("A", "B", 1.98, 2.02)])
        assert app._recent_moves["basketball"] == []

    def test_new_market_midstream_no_phantom_move(self):
        app._compute_pin_moves("basketball", [_ml("A", "B", 2.0, 2.0, eid="m1")])
        # Next cycle: m1 unchanged + a brand-new m2. m2 must not emit a move.
        app._compute_pin_moves("basketball", [
            _ml("A", "B", 2.0, 2.0, eid="m1"),
            _ml("C", "D", 1.5, 3.0, eid="m2"),
        ])
        assert app._recent_moves["basketball"] == []

    def test_fair_now_odds_present_and_correct(self):
        # fair_now_odds = 1 / current no-vig fair prob for the moved side.
        app._compute_pin_moves("basketball", [_ml("A", "B", 2.0, 2.0)])
        app._compute_pin_moves("basketball", [_ml("A", "B", 1.70, 2.40)])
        m = app._recent_moves["basketball"][0]
        assert m["fair_now_odds"] is not None
        # 1 / (new_prob_pct/100) ≈ fair_now_odds (allowing for rounding).
        expected = 1.0 / (m["new_prob_pct"] / 100.0)
        assert abs(m["fair_now_odds"] - expected) < 0.01
        # Fair odds should be >= posted (devig removes vig → longer fair price).
        assert m["fair_now_odds"] >= m["new_odds"]

    def test_total_market_move(self):
        app._compute_pin_moves("basketball", [_total("A", "B", 220.5, 1.90, 1.90)])
        # Over shortens → over fair prob rises.
        app._compute_pin_moves("basketball", [_total("A", "B", 220.5, 1.65, 2.30)])
        moves = app._recent_moves["basketball"]
        assert len(moves) == 1
        assert moves[0]["side"] == "over"

    def test_line_change_breaks_market_identity(self):
        # A total at 220.5 then at 221.5 are DIFFERENT markets (different line).
        # The 221.5 has no prev → no move (correct: we don't compare across lines).
        app._compute_pin_moves("basketball", [_total("A", "B", 220.5, 1.90, 1.90)])
        app._compute_pin_moves("basketball", [_total("A", "B", 221.5, 1.65, 2.30)])
        assert app._recent_moves["basketball"] == []

    def test_api_sort_recency_then_magnitude(self):
        # Phase 5.3: storage order is chronological; /api/moves sorts by
        # (recorded_at desc, delta_pp desc). Within one cycle the two moves
        # share a recorded_at, so the bigger mover wins the tiebreak.
        app._compute_pin_moves("basketball", [
            _ml("A", "B", 2.0, 2.0, eid="m1"),
            _ml("C", "D", 2.0, 2.0, eid="m2"),
        ])
        app._compute_pin_moves("basketball", [
            _ml("A", "B", 1.90, 2.10, eid="m1"),   # small move
            _ml("C", "D", 1.40, 3.20, eid="m2"),   # big move
        ])
        moves = app._recent_moves["basketball"]
        assert len(moves) == 2
        # Apply the same sort the /api/moves endpoint uses.
        ordered = sorted(moves, key=lambda m: (m["recorded_at"], m["delta_pp"]),
                         reverse=True)
        assert ordered[0]["match_label"] == "C — D"   # bigger mover first
        assert ordered[0]["delta_pp"] >= ordered[1]["delta_pp"]


class TestRetentionWindow:
    """Phase 5.3 — _recent_moves accumulates across cycles and prunes by age."""

    def test_moves_accumulate_across_cycles(self):
        # Cycle 1 seeds m1. Cycle 2 moves m1. Cycle 3 moves a DIFFERENT market m2.
        # After cycle 3, BOTH moves should still be in the window (recent).
        app._compute_pin_moves("basketball", [_ml("A", "B", 2.0, 2.0, eid="m1")])
        app._compute_pin_moves("basketball", [_ml("A", "B", 1.50, 3.0, eid="m1")])
        # Re-seed m2 then move it, while m1 stays put.
        app._compute_pin_moves("basketball", [
            _ml("A", "B", 1.50, 3.0, eid="m1"),
            _ml("C", "D", 2.0, 2.0, eid="m2"),
        ])
        app._compute_pin_moves("basketball", [
            _ml("A", "B", 1.50, 3.0, eid="m1"),
            _ml("C", "D", 1.40, 3.2, eid="m2"),
        ])
        labels = {m["match_label"] for m in app._recent_moves["basketball"]}
        assert "A — B" in labels   # the cycle-2 move is still within 5 min
        assert "C — D" in labels   # the latest move

    def test_old_moves_pruned_beyond_retention(self, monkeypatch):
        # Inject a move with a recorded_at older than the retention window and
        # confirm the next compute prunes it.
        old_ts = (datetime.now(tz=timezone.utc)
                  - timedelta(seconds=app._MOVE_RETENTION_SEC + 60)).isoformat()
        app._recent_moves["basketball"] = [{
            "sport": "basketball", "match_label": "Old — Stale",
            "market": "moneyline FT", "side": "home",
            "old_odds": 2.0, "new_odds": 1.5,
            "old_prob_pct": 50.0, "new_prob_pct": 66.0, "delta_pp": 16.0,
            "start_time": None, "recorded_at": old_ts,
        }]
        # A fresh compute cycle (seed — no new moves) should prune the stale row.
        app._compute_pin_moves("basketball", [_ml("A", "B", 2.0, 2.0)])
        assert app._recent_moves["basketball"] == []


class TestCumulativeMoves:
    """Phase 5.6 — net moneyline drift over the 1h window."""

    def _ml_self(self, ho, ao, eid="m1"):
        return Odds(source="pinnacle", sport="basketball", home="A", away="B",
                    market_type="moneyline", period="FT",
                    selections={"home": ho, "away": ao},
                    fetched_at=NOW, line=None, start_time=NOW, league="NBA",
                    raw_event_id=eid)

    def setup_method(self):
        app._pin_prev_fair["basketball"] = {}
        app._recent_moves["basketball"] = []
        app._pin_ml_series["basketball"] = {}

    def teardown_method(self):
        app._pin_ml_series["basketball"] = {}

    def test_net_drift_over_window(self):
        # The user's example: a side drifts 1.5 → 1.7 → 1.9 over three cycles.
        # Cumulative should report first_odds → last_odds for the biggest mover.
        app._compute_pin_moves("basketball", [self._ml_self(1.5, 2.6)])
        app._compute_pin_moves("basketball", [self._ml_self(1.7, 2.2)])
        app._compute_pin_moves("basketball", [self._ml_self(1.9, 1.95)])
        cum = app._compute_cumulative_moves("basketball", min_move=2.0)
        assert len(cum) == 1
        m = cum[0]
        # 3 points recorded, net move present, first/last odds captured.
        assert m["points"] == 3
        assert abs(m["net_pp"]) >= 2.0
        # The home line drifted from 1.5; whichever side is reported, the
        # first/last odds must come from the series ends.
        assert m["first_odds"] in (1.5, 2.6)
        assert m["last_odds"] in (1.9, 1.95)

    def test_single_point_no_cumulative(self):
        # One cycle → only one series point → nothing to compare.
        app._compute_pin_moves("basketball", [self._ml_self(1.5, 2.6)])
        assert app._compute_cumulative_moves("basketball", min_move=2.0) == []

    def test_flat_market_no_cumulative_move(self):
        app._compute_pin_moves("basketball", [self._ml_self(1.90, 1.90)])
        app._compute_pin_moves("basketball", [self._ml_self(1.90, 1.90)])
        assert app._compute_cumulative_moves("basketball", min_move=2.0) == []

    def test_series_only_tracks_moneyline(self):
        # A total market should NOT enter the ML cumulative series.
        tot = Odds(source="pinnacle", sport="basketball", home="A", away="B",
                   market_type="total", period="FT", line=220.5,
                   selections={"over": 1.9, "under": 1.9},
                   fetched_at=NOW, start_time=NOW, league="NBA", raw_event_id="m1")
        app._compute_pin_moves("basketball", [tot])
        app._compute_pin_moves("basketball", [tot])
        # No moneyline series entries at all.
        assert app._pin_ml_series["basketball"] == {}


class TestMoveMarketLabel:
    def test_moneyline_label(self):
        o = _ml("A", "B", 2.0, 2.0)
        assert app._move_market_label(o) == "moneyline FT"

    def test_total_label_includes_line(self):
        o = _total("A", "B", 220.5, 1.9, 1.9)
        assert app._move_market_label(o) == "total FT +220.5"


class TestChangeAnchoredDetection:
    """2026-06-12: moves are measured against the snapshot where the market
    LAST moved (or was first seen), not just the previous poll cycle — a
    creeping line surfaces the moment its cumulative shift crosses the
    threshold, with the time window it took (Pinnacle is poll-only; this is
    the closest thing to 'record moves when they happen')."""

    def test_creeping_drift_accumulates_and_fires(self):
        # Three sub-threshold steps (~1pp each) — old per-cycle logic never
        # fired; change-anchored fires once the cumulative shift is >= 2pp.
        app._compute_pin_moves("basketball", [_ml("A", "B", 2.00, 2.00)])
        app._compute_pin_moves("basketball", [_ml("A", "B", 1.96, 2.04)])
        assert app._recent_moves["basketball"] == []
        app._compute_pin_moves("basketball", [_ml("A", "B", 1.92, 2.08)])
        moves = app._recent_moves["basketball"]
        assert len(moves) == 1
        m = moves[0]
        assert m["side"] == "home"
        assert m["old_odds"] == 2.00        # baseline = first sighting, not prev cycle
        assert m["new_odds"] == 1.92
        assert m["delta_pp"] >= 2.0
        assert m["window_sec"] is not None and m["window_sec"] >= 0

    def test_baseline_reanchors_after_fire(self):
        app._compute_pin_moves("basketball", [_ml("A", "B", 2.00, 2.00)])
        app._compute_pin_moves("basketball", [_ml("A", "B", 1.70, 2.40)])  # fires
        assert len(app._recent_moves["basketball"]) == 1
        # Same prices again — re-anchored baseline means no repeat move.
        app._compute_pin_moves("basketball", [_ml("A", "B", 1.70, 2.40)])
        assert len(app._recent_moves["basketball"]) == 1

    def test_oscillation_around_baseline_never_fires(self):
        app._compute_pin_moves("basketball", [_ml("A", "B", 2.00, 2.00)])
        app._compute_pin_moves("basketball", [_ml("A", "B", 1.96, 2.04)])
        app._compute_pin_moves("basketball", [_ml("A", "B", 2.04, 1.96)])
        app._compute_pin_moves("basketball", [_ml("A", "B", 2.00, 2.00)])
        assert app._recent_moves["basketball"] == []

    def test_market_missing_one_cycle_keeps_anchor(self):
        app._compute_pin_moves("basketball", [_ml("A", "B", 2.00, 2.00, eid="m1")])
        # m1 vanishes for a cycle (suspension flicker) ...
        app._compute_pin_moves("basketball", [_ml("C", "D", 1.9, 1.9, eid="m2")])
        # ... and returns moved: the original anchor must still be there.
        app._compute_pin_moves("basketball", [_ml("A", "B", 1.70, 2.40, eid="m1")])
        moves = [m for m in app._recent_moves["basketball"] if m["match_label"] == "A — B"]
        assert len(moves) == 1
        assert moves[0]["old_odds"] == 2.00

    def test_move_carries_max_stake(self):
        rich = _ml("A", "B", 2.0, 2.0)
        rich.max_stake = 750.0
        app._compute_pin_moves("basketball", [rich])
        moved = _ml("A", "B", 1.70, 2.40)
        moved.max_stake = 900.0
        app._compute_pin_moves("basketball", [moved])
        m = app._recent_moves["basketball"][0]
        assert m["max_stake"] == 900.0     # the CURRENT limit, not the baseline's
