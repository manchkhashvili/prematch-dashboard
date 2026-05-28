"""
Phase 2 / C2.4 regression tests for soccer-specific edge math + join logic.

Covers:
  - edge.py _fair_pairs handles 3-way moneyline (devig_3way) + team_total
  - edge.py _opposing_pairs: 3-way ML returns [] (no ARB-3 in v1);
    team_total returns over/under pairs
  - edge.py _market_label formatting for soccer cases (submarket/team_side suffixes)
  - edge.py _find_pin_match filters by submarket + team_side
  - edge.py compute_opportunities end-to-end for soccer 3-way ML
  - app.py _closest_pin filters by submarket + team_side
  - app.py _maybe_devig handles 3-way moneyline + team_total
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.edge import (  # noqa: E402
    _fair_pairs,
    _find_pin_match,
    _market_label,
    _opposing_pairs,
    compute_opportunities,
)
from src.matcher import MatchedEvent  # noqa: E402
from src.models import Odds  # noqa: E402


NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _odds(
    source: str,
    market_type: str,
    selections: dict,
    *,
    period: str = "FT",
    line: float | None = None,
    home: str = "PSG",
    away: str = "Arsenal",
    event_id: str = "evt-1",
    submarket: str | None = None,
    team_side: str | None = None,
) -> Odds:
    return Odds(
        source=source,  # type: ignore[arg-type]
        sport="soccer",
        home=home,
        away=away,
        market_type=market_type,  # type: ignore[arg-type]
        period=period,  # type: ignore[arg-type]
        selections=selections,
        fetched_at=NOW,
        line=line,
        start_time=NOW,
        league="UCL",
        raw_event_id=event_id,
        submarket=submarket,  # type: ignore[arg-type]
        team_side=team_side,  # type: ignore[arg-type]
    )


def _match(cb_list: list[Odds], pin_list: list[Odds]) -> MatchedEvent:
    return MatchedEvent(
        cb=cb_list, pin=pin_list,
        home="PSG", away="Arsenal", score=100.0,
    )


# ── _opposing_pairs ───────────────────────────────────────────────────────────
class TestOpposingPairs:
    def test_2way_moneyline_unchanged(self):
        cb = _odds("crystalbet", "moneyline", {"home": 2.0, "away": 2.0})
        assert _opposing_pairs(cb) == [("home", "away"), ("away", "home")]

    def test_3way_moneyline_returns_empty(self):
        """ARB-3 within a single book vanishingly rare; skipped in v1."""
        cb = _odds("crystalbet", "moneyline",
                   {"home": 2.20, "draw": 3.10, "away": 2.95})
        assert _opposing_pairs(cb) == []

    def test_spread_unchanged(self):
        cb = _odds("crystalbet", "spread", {"home": 1.91, "away": 1.91}, line=-1.5)
        assert _opposing_pairs(cb) == [("home", "away"), ("away", "home")]

    def test_total_unchanged(self):
        cb = _odds("crystalbet", "total", {"over": 1.95, "under": 1.85}, line=2.5)
        assert _opposing_pairs(cb) == [("over", "under"), ("under", "over")]

    def test_team_total_returns_over_under_pairs(self):
        cb = _odds("crystalbet", "team_total",
                   {"over": 1.85, "under": 1.95}, line=1.5, team_side="home")
        assert _opposing_pairs(cb) == [("over", "under"), ("under", "over")]


# ── _fair_pairs ───────────────────────────────────────────────────────────────
class TestFairPairs:
    def test_3way_moneyline_returns_three_tuples(self):
        cb = _odds("crystalbet", "moneyline",
                   {"home": 2.30, "draw": 3.20, "away": 3.00})
        # Pinnacle 3-way with ~5% vig overall
        pin = _odds("pinnacle", "moneyline",
                    {"home": 2.20, "draw": 3.10, "away": 2.95})
        pairs = _fair_pairs(cb, pin)
        assert pairs is not None
        sides = [p[0] for p in pairs]
        assert sides == ["home", "draw", "away"]
        # Fair probs sum to ~1.0
        total = sum(p[2] for p in pairs)
        assert total == pytest.approx(1.0, abs=1e-9)
        # CB decimal pass-through
        cb_decs = {p[0]: p[1] for p in pairs}
        assert cb_decs == {"home": 2.30, "draw": 3.20, "away": 3.00}

    def test_2way_moneyline_unchanged(self):
        cb = _odds("crystalbet", "moneyline", {"home": 2.0, "away": 2.0})
        pin = _odds("pinnacle", "moneyline", {"home": 1.91, "away": 1.91})
        pairs = _fair_pairs(cb, pin)
        assert pairs is not None
        assert len(pairs) == 2
        sides = [p[0] for p in pairs]
        assert sides == ["home", "away"]

    def test_team_total_uses_over_under_devig(self):
        cb = _odds("crystalbet", "team_total",
                   {"over": 1.90, "under": 1.95}, line=1.5, team_side="home")
        pin = _odds("pinnacle", "team_total",
                    {"over": 1.85, "under": 1.95}, line=1.5, team_side="home")
        pairs = _fair_pairs(cb, pin)
        assert pairs is not None
        sides = [p[0] for p in pairs]
        assert sides == ["over", "under"]
        total = sum(p[2] for p in pairs)
        assert total == pytest.approx(1.0, abs=1e-9)

    def test_devig_failure_returns_none(self):
        """Pinnacle ML missing draw side → devig_3way raises KeyError → None."""
        cb = _odds("crystalbet", "moneyline",
                   {"home": 2.30, "draw": 3.20, "away": 3.00})
        # Pinnacle ML pretending to be 3-way (has draw) but missing home —
        # devig_3way raises KeyError, _fair_pairs catches and returns None.
        pin = _odds("pinnacle", "moneyline", {"draw": 3.10, "away": 2.95})
        pairs = _fair_pairs(cb, pin)
        assert pairs is None


# ── _market_label ─────────────────────────────────────────────────────────────
class TestMarketLabel:
    def test_basketball_spread_unchanged(self):
        cb = _odds("crystalbet", "spread", {"home": 1.91, "away": 1.91}, line=-2.5)
        cb.sport = "basketball"  # cosmetic; label doesn't read sport
        assert _market_label(cb) == "Spread FT -2.5"

    def test_moneyline_no_line(self):
        cb = _odds("crystalbet", "moneyline", {"home": 2.0, "away": 2.0})
        assert _market_label(cb) == "Moneyline FT"

    def test_3way_moneyline_label_same(self):
        # 3-way doesn't change the label text — selections do the talking.
        cb = _odds("crystalbet", "moneyline",
                   {"home": 2.2, "draw": 3.1, "away": 2.95})
        assert _market_label(cb) == "Moneyline FT"

    def test_team_total_label_includes_team_side(self):
        cb = _odds("crystalbet", "team_total",
                   {"over": 1.85, "under": 1.95}, line=1.5, team_side="home")
        assert _market_label(cb) == "Team Total FT +1.5 (home)"

    def test_team_total_away(self):
        cb = _odds("crystalbet", "team_total",
                   {"over": 1.85, "under": 1.95}, line=2.5, team_side="away")
        assert _market_label(cb) == "Team Total FT +2.5 (away)"

    def test_corners_total_label_includes_submarket(self):
        cb = _odds("crystalbet", "total",
                   {"over": 1.80, "under": 1.80}, line=9.5, submarket="corners")
        assert _market_label(cb) == "Total FT +9.5 (corners)"

    def test_corners_spread_h1(self):
        cb = _odds("crystalbet", "spread",
                   {"home": 1.50, "away": 2.25}, line=0.5,
                   period="H1", submarket="corners")
        assert _market_label(cb) == "Spread H1 +0.5 (corners)"


# ── _find_pin_match: submarket + team_side filtering ──────────────────────────
class TestFindPinMatchSoccer:
    def test_submarket_filter_corners_doesnt_match_parent_total(self):
        cb_corners = _odds("crystalbet", "total",
                           {"over": 1.80, "under": 1.80}, line=9.5,
                           submarket="corners")
        # Same matchup, same line — but goals total, not corners.
        pin_goals = _odds("pinnacle", "total",
                          {"over": 1.91, "under": 1.89}, line=9.5,
                          submarket=None)
        assert _find_pin_match(cb_corners, [pin_goals]) is None

    def test_submarket_filter_corners_matches_corners(self):
        cb = _odds("crystalbet", "total",
                   {"over": 1.80, "under": 1.80}, line=9.5,
                   submarket="corners")
        pin = _odds("pinnacle", "total",
                    {"over": 1.85, "under": 1.85}, line=9.5,
                    submarket="corners")
        assert _find_pin_match(cb, [pin]) is pin

    def test_team_side_filter_home_doesnt_match_away(self):
        cb_home = _odds("crystalbet", "team_total",
                        {"over": 1.85, "under": 1.95}, line=1.5,
                        team_side="home")
        pin_away = _odds("pinnacle", "team_total",
                         {"over": 1.85, "under": 1.95}, line=1.5,
                         team_side="away")
        assert _find_pin_match(cb_home, [pin_away]) is None

    def test_team_side_filter_home_matches_home(self):
        cb = _odds("crystalbet", "team_total",
                   {"over": 1.85, "under": 1.95}, line=1.5,
                   team_side="home")
        pin = _odds("pinnacle", "team_total",
                    {"over": 1.80, "under": 1.95}, line=1.5,
                    team_side="home")
        assert _find_pin_match(cb, [pin]) is pin

    def test_basketball_none_none_still_matches(self):
        """Pre-Phase-2 Odds (both fields None) still pair as before."""
        cb = _odds("crystalbet", "moneyline", {"home": 2.0, "away": 2.0})
        cb.sport = "basketball"
        pin = _odds("pinnacle", "moneyline", {"home": 1.91, "away": 1.91})
        pin.sport = "basketball"
        # Both have submarket=None, team_side=None — should match.
        assert _find_pin_match(cb, [pin]) is pin


# ── compute_opportunities end-to-end (3-way ML) ───────────────────────────────
class TestCompute3WayEndToEnd:
    def test_3way_ml_emits_per_side_ev_no_arb(self):
        # Pinnacle 2.20/3.10/2.95 (vigged ~11.6%) devigs to fair decimals
        # ~2.456 home, ~3.460 draw, ~3.292 away. CB needs to beat the
        # FAIR price (not just Pin's vigged price) to be +EV. We bump
        # CB's home to 2.60 → +5.87% edge over fair; draw and away stay
        # below fair to also exercise the "no row when negative" path.
        cb = _odds("crystalbet", "moneyline",
                   {"home": 2.60, "draw": 3.20, "away": 3.00})
        pin = _odds("pinnacle", "moneyline",
                    {"home": 2.20, "draw": 3.10, "away": 2.95})

        opps = compute_opportunities([_match([cb], [pin])], min_edge_pct=0.0)
        evs = [o for o in opps if o.kind == "+EV"]
        arbs = [o for o in opps if o.kind == "ARB"]

        # Exactly the home side should be +EV (positive edge over fair).
        sides_emitted = {o.side for o in evs}
        assert "home" in sides_emitted
        # draw and away are below fair → negative edge → suppressed at 0.0
        assert "draw" not in sides_emitted
        assert "away" not in sides_emitted

        # No ARB rows for 3-way ML in v1
        assert arbs == []

        # Sanity: the home edge is roughly +5-6% vs fair
        home_ev = next(o for o in evs if o.side == "home")
        assert 4.0 < home_ev.edge_pct < 8.0, f"unexpected edge: {home_ev.edge_pct}"
        # pin_no_vig on the home row should be the fair decimal (~2.456)
        assert 2.4 < home_ev.pin_no_vig < 2.5

    def test_3way_ml_market_label(self):
        cb = _odds("crystalbet", "moneyline",
                   {"home": 2.30, "draw": 3.20, "away": 3.00})
        pin = _odds("pinnacle", "moneyline",
                    {"home": 2.20, "draw": 3.10, "away": 2.95})
        opps = compute_opportunities([_match([cb], [pin])], min_edge_pct=0.0)
        # Whatever opp comes back, its `market` should read "Moneyline FT"
        for o in opps:
            assert o.market == "Moneyline FT"


class TestComputeTeamTotalEndToEnd:
    def test_team_total_ev_emits_no_collision_across_team_sides(self):
        cb_home_tt = _odds("crystalbet", "team_total",
                           {"over": 1.95, "under": 1.85}, line=1.5,
                           team_side="home")
        cb_away_tt = _odds("crystalbet", "team_total",
                           {"over": 1.85, "under": 1.95}, line=1.5,
                           team_side="away")
        # Pin home-tt is fair (~equal); Pin away-tt favors over heavily.
        pin_home_tt = _odds("pinnacle", "team_total",
                            {"over": 1.91, "under": 1.91}, line=1.5,
                            team_side="home")
        pin_away_tt = _odds("pinnacle", "team_total",
                            {"over": 1.50, "under": 2.50}, line=1.5,
                            team_side="away")
        opps = compute_opportunities(
            [_match([cb_home_tt, cb_away_tt], [pin_home_tt, pin_away_tt])],
            min_edge_pct=0.0,
        )
        # All emitted opps should have a market label that includes (home)/(away)
        for o in opps:
            assert "(home)" in o.market or "(away)" in o.market
        # Should NOT pair cb_home_tt against pin_away_tt — verify by edge sanity:
        # if CB home-over 1.95 were paired with Pin home-over 1.91 → ~2% edge
        # if it were paired with Pin away-over 1.50 → ~+30% edge (impossible!).
        home_over_evs = [o for o in opps if o.side == "over"
                         and "(home)" in o.market and o.kind == "+EV"]
        for o in home_over_evs:
            # 1.95 / fair(1.91/1.91)=2.0 → edge should be ~-2.5%; suppressed
            # by min_edge=0.0 still emits negatives. Just check edge < 10%
            # (would be ~30% if mis-paired).
            assert o.edge_pct < 10.0, f"phantom cross-pairing on team_side: {o}"


class TestComputeCornersEndToEnd:
    def test_corners_market_doesnt_pair_with_parent(self):
        # CB corners total 9.5; CB parent total 2.5 (goals). Pin only has
        # parent goals 9.5 (no corners). Without submarket filter, the
        # corners 9.5 would match parent 9.5 → phantom edge.
        cb_corners = _odds("crystalbet", "total",
                           {"over": 1.80, "under": 1.80}, line=9.5,
                           submarket="corners")
        cb_goals = _odds("crystalbet", "total",
                         {"over": 1.95, "under": 1.85}, line=2.5)
        # Pin only ships parent goals (no corners child for this match)
        pin_goals_at_25 = _odds("pinnacle", "total",
                                {"over": 1.91, "under": 1.89}, line=2.5)
        pin_goals_at_95 = _odds("pinnacle", "total",
                                {"over": 1.30, "under": 3.50}, line=9.5)
        opps = compute_opportunities(
            [_match([cb_corners, cb_goals], [pin_goals_at_25, pin_goals_at_95])],
            min_edge_pct=0.0,
        )
        # No opp should reference corners — Pin had no corners counterpart.
        for o in opps:
            assert "(corners)" not in o.market, (
                f"corners cross-paired with parent total: {o}"
            )
