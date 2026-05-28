"""
Phase 3.1.4 regression tests for tennis Sets/Games matchup-level split.

Pinnacle ships tennis as TWO matchups per real match:
  - parent matchup (units="Sets"): contains ML + set-handicap ±1.5 + set-total 2.5
  - child matchup  (units="Games", parentId=parent): contains games-handicap
    (continuous lines) + games-total + first-set (period=1) markets

CB tennis ships GAMES-handicap as primary, so we want Pin's games markets,
not sets. Fix: fold "Games" child onto parent matchupId without a submarket
tag (so it's the primary spread/total), AND skip spread/total/team_total
markets when matchup units="sets" (drop the set-based variants).

Moneyline is kept regardless of units — ML doesn't care about sets vs games.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scrapers.pinnacle import (  # noqa: E402
    _build_odds_for_league,
    _index_child_matchups,
    _index_matchups,
)


NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
FUTURE_ISO = (NOW + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tennis_parent(mid: int, home="Bulgaru", away="Primorac") -> dict:
    """Pinnacle tennis parent matchup — units='Sets'."""
    return {
        "id": mid,
        "type": "matchup",
        "parent": None,
        "units": "Sets",
        "startTime": FUTURE_ISO,
        "participants": [
            {"alignment": "home", "name": home},
            {"alignment": "away", "name": away},
        ],
    }


def _tennis_games_child(mid: int, parent_id: int) -> dict:
    """Pinnacle tennis games-units child matchup."""
    return {
        "id": mid,
        "type": "matchup",
        "parentId": parent_id,
        "parent": {"id": parent_id},
        "units": "Games",
        "startTime": FUTURE_ISO,
        "participants": [
            {"alignment": "home", "name": "Bulgaru (Games)"},
            {"alignment": "away", "name": "Primorac (Games)"},
        ],
    }


def _market(*, matchup_id, type_, period, prices) -> dict:
    return {
        "matchupId": matchup_id,
        "type": type_,
        "period": period,
        "prices": prices,
        "key": f"s;{period};{type_[0]}",
    }


def _price(designation, price, points=None) -> dict:
    p = {"designation": designation, "price": price}
    if points is not None:
        p["points"] = points
    return p


# ── _index_child_matchups: games-units folding ────────────────────────────────
class TestIndexGamesChild:
    def test_games_child_folded_with_no_submarket(self):
        """Tennis games-units child folds onto parent matchupId with submarket=None
        (NOT 'games' or 'sets' — these ARE the primary tennis spread/total)."""
        result = _index_child_matchups([
            _tennis_games_child(mid=200, parent_id=100),
        ])
        assert result == {200: (100, None)}

    def test_games_and_corners_can_coexist(self):
        """A theoretical future sport with both 'Games' and 'Corners' children
        gets both folded correctly with their respective submarket tags."""
        result = _index_child_matchups([
            _tennis_games_child(mid=200, parent_id=100),
            {
                "id": 201, "type": "matchup", "parentId": 100,
                "parent": {"id": 100}, "units": "Corners",
                "startTime": FUTURE_ISO,
                "participants": [{"alignment": "home", "name": "X"},
                                 {"alignment": "away", "name": "Y"}],
            },
        ])
        assert result == {200: (100, None), 201: (100, "corners")}


# ── _index_matchups: units field captured ─────────────────────────────────────
class TestIndexMatchupsUnits:
    def test_tennis_parent_carries_sets_units(self):
        """Tennis parent matchup's 'units' field carries through to matchup_by_id
        — downstream filter uses it to skip set-handicap/total markets."""
        result = _index_matchups([_tennis_parent(100)], NOW)
        assert 100 in result
        assert result[100]["units"] == "sets"

    def test_no_units_field_defaults_to_empty(self):
        """Back-compat: matchups without a units field get empty string."""
        m = _tennis_parent(100)
        del m["units"]
        result = _index_matchups([m], NOW)
        assert result[100]["units"] == ""


# ── _build_odds_for_league: skip set-based spread/total ───────────────────────
class TestSkipSetBasedMarkets:
    def _info(self):
        return {100: {
            "home": "Bulgaru", "away": "Primorac",
            "start_time": NOW + timedelta(hours=4),
            "units": "sets",
        }}

    def test_set_handicap_spread_skipped(self):
        """Spread market on a units=sets matchup → skipped entirely."""
        markets = [_market(
            matchup_id=100, type_="spread", period=0,
            prices=[_price("home", 183, points=-1.5),
                    _price("away", -248, points=1.5)],
        )]
        odds = _build_odds_for_league(
            markets, self._info(), "ITF Women Bol R1",
            sport_name="tennis",
        )
        assert odds == []

    def test_set_total_skipped(self):
        markets = [_market(
            matchup_id=100, type_="total", period=0,
            prices=[_price("over", 137, points=2.5),
                    _price("under", -180, points=2.5)],
        )]
        odds = _build_odds_for_league(
            markets, self._info(), "ITF Women Bol R1",
            sport_name="tennis",
        )
        assert odds == []

    def test_moneyline_KEPT_on_units_sets(self):
        """Moneyline on a units=sets matchup is KEPT (ML is the same prediction
        regardless of whether the spread variant is sets or games)."""
        markets = [_market(
            matchup_id=100, type_="moneyline", period=0,
            prices=[_price("home", -137),
                    _price("away", 105)],
        )]
        odds = _build_odds_for_league(
            markets, self._info(), "ITF Women Bol R1",
            sport_name="tennis",
        )
        assert len(odds) == 1
        assert odds[0].market_type == "moneyline"
        assert odds[0].sport == "tennis"

    def test_games_units_does_NOT_skip(self):
        """If a matchup somehow has units='games' (it wouldn't in practice,
        but verifying the filter is set-specific) spread/total are kept."""
        info = {100: {
            "home": "X", "away": "Y",
            "start_time": NOW + timedelta(hours=4),
            "units": "games",
        }}
        markets = [_market(
            matchup_id=100, type_="spread", period=0,
            prices=[_price("home", -110, points=-1.5),
                    _price("away", -110, points=1.5)],
        )]
        odds = _build_odds_for_league(
            markets, info, "Tennis", sport_name="tennis",
        )
        assert len(odds) == 1

    def test_no_units_does_NOT_skip(self):
        """Matchups without a units field (e.g. existing test fixtures and
        basketball/soccer) are unaffected — no skip."""
        info = {100: {
            "home": "Hawks", "away": "Lakers",
            "start_time": NOW + timedelta(hours=4),
            "units": "",  # empty = not 'sets'
        }}
        markets = [_market(
            matchup_id=100, type_="spread", period=0,
            prices=[_price("home", -110, points=-3.5),
                    _price("away", -110, points=3.5)],
        )]
        odds = _build_odds_for_league(markets, info, "NBA")
        assert len(odds) == 1


# ── End-to-end: Games child folded onto Sets parent ───────────────────────────
class TestEndToEnd:
    def _info(self):
        # Parent matchup with units=sets (our _index_matchups output for tennis)
        return {100: {
            "home": "Bulgaru", "away": "Primorac",
            "start_time": NOW + timedelta(hours=4),
            "units": "sets",
        }}

    def test_games_child_market_attributed_to_parent_with_no_submarket(self):
        """Pin games-handicap market on child matchup → folded to parent matchupId,
        emitted as a primary spread (submarket=None)."""
        # Games child's market: spread -1.5 (games handicap)
        markets = [_market(
            matchup_id=200, type_="spread", period=0,
            prices=[_price("home", -108, points=-1.5),
                    _price("away", -123, points=1.5)],
        )]
        child_to_parent = {200: (100, None)}  # games child folds with no submarket
        odds = _build_odds_for_league(
            markets, self._info(), "ITF Women Bol R1",
            child_to_parent=child_to_parent, sport_name="tennis",
        )
        assert len(odds) == 1
        o = odds[0]
        assert o.market_type == "spread"
        assert o.line == -1.5
        assert o.raw_event_id == "100"  # parent matchupId
        assert o.submarket is None     # primary, NOT corners-style submarket
        assert o.home == "Bulgaru"     # parent's name, not "Bulgaru (Games)"

    def test_parent_set_spread_and_child_games_spread_both_at_same_line(self):
        """The real-world scenario: parent has set-handicap -1.5 (skip), child
        has games-handicap -1.5 (fold as primary). Only games version should
        end up in the output."""
        markets = [
            # Parent set-handicap at line -1.5 (Bulgaru -1.5 sets = 2.83)
            _market(
                matchup_id=100, type_="spread", period=0,
                prices=[_price("home", 183, points=-1.5),
                        _price("away", -248, points=1.5)],
            ),
            # Child games-handicap at line -1.5 (Bulgaru -1.5 games ≈ 1.93)
            _market(
                matchup_id=200, type_="spread", period=0,
                prices=[_price("home", -108, points=-1.5),
                        _price("away", -123, points=1.5)],
            ),
        ]
        child_to_parent = {200: (100, None)}
        odds = _build_odds_for_league(
            markets, self._info(), "ITF Women Bol R1",
            child_to_parent=child_to_parent, sport_name="tennis",
        )
        # Only the GAMES variant should survive (parent's set-handicap dropped
        # via units=sets filter; child's games-handicap folded as primary).
        assert len(odds) == 1
        o = odds[0]
        # American -108 → decimal 1.926 (close to Pinnacle's Bulgaru -1.5 games 1.925)
        assert 1.92 < o.selections["home"] < 1.94, (
            f"expected games-handicap odds ~1.93, got {o.selections['home']}"
        )