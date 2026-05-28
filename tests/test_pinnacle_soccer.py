"""
Phase 2 / C2.3 regression tests for soccer-specific pinnacle parser paths.

Each test guards a soccer-only behavior we wired up 2026-05-26:

  - _index_child_matchups: corners children picked up via parentId + units;
    bookings deferred; specials skipped; no-parentId skipped
  - _index_matchups: type=special filtered (new Phase 2); back-compat for
    parent-based filtering preserved
  - _build_odds_for_league with 3-way moneyline: selections={home,draw,away}
  - _build_odds_for_league with team_total: top-level `side` field
    populates team_side on emitted Odds
  - _build_odds_for_league with corners child_to_parent map: market rewritten
    to PARENT matchupId, Odds tagged submarket="corners"
  - team_total without `side` field → skip
  - team_total alt-line (isAlternate=true) included — we don't filter alt-lines
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scrapers.pinnacle import (  # noqa: E402
    _build_odds_for_league,
    _index_child_matchups,
    _index_matchups,
)


NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
FUTURE_ISO = (NOW + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parent_matchup(mid: int, home="PSG", away="Arsenal") -> dict:
    return {
        "id": mid,
        "type": "matchup",
        "parent": None,
        "startTime": FUTURE_ISO,
        "participants": [
            {"alignment": "home", "name": home},
            {"alignment": "away", "name": away},
        ],
    }


def _child_matchup(mid: int, parent_id: int, units: str, type_="matchup") -> dict:
    """A corners/bookings child matchup as returned by /sports/29/matchups."""
    return {
        "id": mid,
        "type": type_,
        "parentId": parent_id,
        "parent": {"id": parent_id},
        "units": units,
        "startTime": FUTURE_ISO,
        "participants": [
            {"alignment": "home", "name": "PSG (Corners)"},
            {"alignment": "away", "name": "Arsenal (Corners)"},
        ],
    }


def _market(*, matchup_id, type_, period, prices, side=None) -> dict:
    m = {
        "matchupId": matchup_id,
        "type": type_,
        "period": period,
        "prices": prices,
        "key": f"s;{period};{type_[0]}",
    }
    if side is not None:
        m["side"] = side
    return m


def _price(designation, price, points=None) -> dict:
    p = {"designation": designation, "price": price}
    if points is not None:
        p["points"] = points
    return p


# ── _index_child_matchups ─────────────────────────────────────────────────────
class TestIndexChildMatchups:
    def test_corners_child_picked_up(self):
        result = _index_child_matchups([
            _child_matchup(mid=200, parent_id=100, units="Corners"),
        ])
        assert result == {200: (100, "corners")}

    def test_units_case_insensitive(self):
        result = _index_child_matchups([
            _child_matchup(mid=201, parent_id=101, units="corners"),
            _child_matchup(mid=202, parent_id=102, units="CORNERS"),
        ])
        assert result == {201: (101, "corners"), 202: (102, "corners")}

    def test_bookings_skipped_v1(self):
        # Bookings deferred per build_log 2026-05-26; should NOT appear.
        result = _index_child_matchups([
            _child_matchup(mid=300, parent_id=100, units="Bookings"),
        ])
        assert result == {}

    def test_missing_units_skipped(self):
        bad = _child_matchup(mid=400, parent_id=100, units="")
        bad.pop("units")
        result = _index_child_matchups([bad])
        assert result == {}

    def test_no_parent_id_skipped(self):
        # Top-level (parent) matchups should NOT appear in child map.
        result = _index_child_matchups([
            _parent_matchup(100),
        ])
        assert result == {}

    def test_special_with_parent_id_skipped(self):
        # Specials (props/futures) have parentId but aren't priceable
        # children — they shouldn't get folded as corners.
        sp = _child_matchup(mid=500, parent_id=100, units="Corners", type_="special")
        result = _index_child_matchups([sp])
        assert result == {}

    def test_multiple_mixed(self):
        result = _index_child_matchups([
            _parent_matchup(100),
            _child_matchup(mid=200, parent_id=100, units="Corners"),
            _child_matchup(mid=300, parent_id=100, units="Bookings"),
            _parent_matchup(101, home="Real", away="Barca"),
            _child_matchup(mid=201, parent_id=101, units="Corners"),
        ])
        assert result == {
            200: (100, "corners"),
            201: (101, "corners"),
        }


# ── _index_matchups: Phase 2 additions ────────────────────────────────────────
class TestIndexMatchupsPhase2:
    def test_type_special_filtered(self):
        sp = _parent_matchup(999, home="World Cup", away="Winner")
        sp["type"] = "special"
        # Specials have parent=None too (futures are top-level)
        result = _index_matchups([sp], NOW)
        assert result == {}

    def test_parent_matchup_still_included(self):
        # Back-compat: regular type=matchup with parent=None should still appear.
        result = _index_matchups([_parent_matchup(100)], NOW)
        assert 100 in result
        assert result[100]["home"] == "PSG"
        assert result[100]["away"] == "Arsenal"

    def test_child_matchup_still_excluded(self):
        # Children (with parent != None) should NOT appear here — they're
        # handled by _index_child_matchups separately.
        ck = _child_matchup(mid=200, parent_id=100, units="Corners")
        result = _index_matchups([ck], NOW)
        assert result == {}


# ── _build_odds_for_league: 3-way moneyline ───────────────────────────────────
class TestBuildOdds3WayMoneyline:
    def _info(self):
        return {100: {"home": "PSG", "away": "Arsenal", "start_time": NOW + timedelta(hours=4)}}

    def test_3way_ml_emits_three_selections(self):
        markets = [_market(
            matchup_id=100, type_="moneyline", period=0,
            prices=[
                _price("home", -255),
                _price("away", 766),
                _price("draw", 336),
            ],
        )]
        odds = _build_odds_for_league(
            markets, self._info(), "Test League", sport_name="soccer",
        )
        assert len(odds) == 1
        o = odds[0]
        assert o.market_type == "moneyline"
        assert o.period == "FT"
        assert set(o.selections.keys()) == {"home", "draw", "away"}
        assert o.line is None
        assert o.sport == "soccer"
        assert o.submarket is None
        assert o.team_side is None

    def test_3way_ml_missing_draw_skipped(self):
        # If "draw" designation present but its price=None, the whole line drops.
        markets = [_market(
            matchup_id=100, type_="moneyline", period=0,
            prices=[
                _price("home", -255),
                _price("away", 766),
                _price("draw", None),  # suspended
            ],
        )]
        odds = _build_odds_for_league(
            markets, self._info(), "Test League", sport_name="soccer",
        )
        assert odds == []

    def test_2way_ml_still_works_basketball(self):
        # Basketball moneyline: no draw → emit 2-way as before.
        markets = [_market(
            matchup_id=100, type_="moneyline", period=0,
            prices=[
                _price("home", -150),
                _price("away", 130),
            ],
        )]
        odds = _build_odds_for_league(markets, self._info(), "Test League")
        assert len(odds) == 1
        assert set(odds[0].selections.keys()) == {"home", "away"}
        assert odds[0].sport == "basketball"  # default sport_name


# ── _build_odds_for_league: team_total ────────────────────────────────────────
class TestBuildOddsTeamTotal:
    def _info(self):
        return {100: {"home": "PSG", "away": "Arsenal", "start_time": NOW + timedelta(hours=4)}}

    def test_team_total_home_side(self):
        markets = [_market(
            matchup_id=100, type_="team_total", period=0,
            side="home",
            prices=[
                _price("over", -141, points=1.5),
                _price("under", 112, points=1.5),
            ],
        )]
        odds = _build_odds_for_league(
            markets, self._info(), "Test", sport_name="soccer",
        )
        assert len(odds) == 1
        o = odds[0]
        assert o.market_type == "team_total"
        assert o.team_side == "home"
        assert o.line == 1.5
        assert set(o.selections.keys()) == {"over", "under"}

    def test_team_total_away_side(self):
        markets = [_market(
            matchup_id=100, type_="team_total", period=1,
            side="away",
            prices=[
                _price("over", -110, points=0.5),
                _price("under", -110, points=0.5),
            ],
        )]
        odds = _build_odds_for_league(
            markets, self._info(), "Test", sport_name="soccer",
        )
        assert len(odds) == 1
        assert odds[0].team_side == "away"
        assert odds[0].period == "H1"

    def test_team_total_both_sides_separate_odds(self):
        markets = [
            _market(matchup_id=100, type_="team_total", period=0, side="home",
                    prices=[_price("over", -141, 1.5), _price("under", 112, 1.5)]),
            _market(matchup_id=100, type_="team_total", period=0, side="away",
                    prices=[_price("over", -110, 1.5), _price("under", -110, 1.5)]),
        ]
        odds = _build_odds_for_league(
            markets, self._info(), "Test", sport_name="soccer",
        )
        assert len(odds) == 2
        sides = {o.team_side for o in odds}
        assert sides == {"home", "away"}

    def test_team_total_missing_side_skipped(self):
        # No top-level `side` field → malformed → skip
        markets = [_market(
            matchup_id=100, type_="team_total", period=0,
            # side intentionally omitted
            prices=[_price("over", -110, 1.5), _price("under", -110, 1.5)],
        )]
        odds = _build_odds_for_league(
            markets, self._info(), "Test", sport_name="soccer",
        )
        assert odds == []

    def test_team_total_invalid_side_skipped(self):
        markets = [_market(
            matchup_id=100, type_="team_total", period=0,
            side="neutral",  # not home/away
            prices=[_price("over", -110, 1.5), _price("under", -110, 1.5)],
        )]
        odds = _build_odds_for_league(
            markets, self._info(), "Test", sport_name="soccer",
        )
        assert odds == []

    def test_team_total_alt_line_included(self):
        # We don't filter isAlternate — all lines come through.
        m1 = _market(matchup_id=100, type_="team_total", period=0, side="home",
                     prices=[_price("over", -141, 1.5), _price("under", 112, 1.5)])
        m1["isAlternate"] = False
        m2 = _market(matchup_id=100, type_="team_total", period=0, side="home",
                     prices=[_price("over", 200, 2.5), _price("under", -300, 2.5)])
        m2["isAlternate"] = True
        odds = _build_odds_for_league(
            [m1, m2], self._info(), "Test", sport_name="soccer",
        )
        assert len(odds) == 2
        lines = sorted(o.line for o in odds)
        assert lines == [1.5, 2.5]


# ── _build_odds_for_league: corners child folding ─────────────────────────────
class TestBuildOddsCornersChild:
    def _info(self):
        return {100: {"home": "PSG", "away": "Arsenal", "start_time": NOW + timedelta(hours=4)}}

    def test_corners_market_attributed_to_parent(self):
        # Market keyed to child matchupId=200, child_to_parent maps to parent 100.
        markets = [_market(
            matchup_id=200, type_="total", period=0,
            prices=[_price("over", -110, 9.5), _price("under", -110, 9.5)],
        )]
        child_to_parent = {200: (100, "corners")}
        odds = _build_odds_for_league(
            markets, self._info(), "Copa Sudamericana",
            child_to_parent=child_to_parent, sport_name="soccer",
        )
        assert len(odds) == 1
        o = odds[0]
        # PSG / Arsenal come from the PARENT matchup info — not "PSG (Corners)"
        assert o.home == "PSG"
        assert o.away == "Arsenal"
        assert o.submarket == "corners"
        assert o.raw_event_id == "100"  # parent matchupId, not 200
        assert o.market_type == "total"
        assert o.line == 9.5

    def test_corners_spread_alt_lines(self):
        markets = [
            _market(matchup_id=200, type_="spread", period=1,
                    prices=[_price("home", -110, -0.5), _price("away", -110, 0.5)]),
            _market(matchup_id=200, type_="spread", period=1,
                    prices=[_price("home", 150, -1.5), _price("away", -180, 1.5)]),
        ]
        child_to_parent = {200: (100, "corners")}
        odds = _build_odds_for_league(
            markets, self._info(), "Copa Sudamericana",
            child_to_parent=child_to_parent, sport_name="soccer",
        )
        assert len(odds) == 2
        for o in odds:
            assert o.submarket == "corners"
            assert o.period == "H1"
            assert o.market_type == "spread"

    def test_no_child_map_basketball_back_compat(self):
        # Pre-Phase-2 callers don't pass child_to_parent; should behave
        # exactly like basketball: no submarket, no folding.
        markets = [_market(
            matchup_id=100, type_="total", period=0,
            prices=[_price("over", -110, 9.5), _price("under", -110, 9.5)],
        )]
        odds = _build_odds_for_league(markets, self._info(), "NBA")
        assert len(odds) == 1
        assert odds[0].submarket is None
        assert odds[0].team_side is None
        assert odds[0].sport == "basketball"

    def test_child_market_with_unknown_parent_dropped(self):
        # If child_to_parent maps to a parent that's NOT in matchup_by_id
        # (e.g. parent rolled past start time and got filtered), drop quietly.
        markets = [_market(
            matchup_id=200, type_="total", period=0,
            prices=[_price("over", -110, 9.5), _price("under", -110, 9.5)],
        )]
        child_to_parent = {200: (999, "corners")}  # 999 not in info
        odds = _build_odds_for_league(
            markets, self._info(), "Copa",
            child_to_parent=child_to_parent, sport_name="soccer",
        )
        assert odds == []


# ── Per-sport team_total gating ───────────────────────────────────────────────
# Phase 2 followup: basketball ships team_total on Pinnacle (~10 entries/cycle)
# but our CB classifier doesn't classify team_total titles, so those Pin Odds
# would be phantom (unpaired, no edge, no display). Per-sport gating means
# basketball ignores team_total at fetch time. Soccer accepts all four types.
class TestTeamTotalGating:
    def _info(self):
        return {100: {"home": "PSG", "away": "Arsenal", "start_time": NOW + timedelta(hours=4)}}

    def test_basketball_team_total_filtered(self):
        markets = [_market(
            matchup_id=100, type_="team_total", period=0,
            side="home",
            prices=[_price("over", -110, 1.5), _price("under", -110, 1.5)],
        )]
        # sport_name="basketball" → team_total NOT in allowed set
        odds = _build_odds_for_league(
            markets, self._info(), "NBA", sport_name="basketball",
        )
        assert odds == []

    def test_soccer_team_total_allowed(self):
        markets = [_market(
            matchup_id=100, type_="team_total", period=0,
            side="home",
            prices=[_price("over", -110, 1.5), _price("under", -110, 1.5)],
        )]
        odds = _build_odds_for_league(
            markets, self._info(), "Copa", sport_name="soccer",
        )
        assert len(odds) == 1
        assert odds[0].market_type == "team_total"

    def test_unknown_sport_falls_back_to_full_set(self):
        # If we ever fetch a sport not in ALLOWED_MARKET_TYPES_BY_SPORT,
        # don't accidentally filter everything out — fall back to the full
        # union so the parser still does useful work.
        # (Originally used "tennis" here; tennis got its own entry in
        # Phase 3.1 so this test now uses a hypothetical future sport.)
        markets = [_market(
            matchup_id=100, type_="team_total", period=0,
            side="home",
            prices=[_price("over", -110, 1.5), _price("under", -110, 1.5)],
        )]
        odds = _build_odds_for_league(
            markets, self._info(), "Test", sport_name="hockey",
        )
        assert len(odds) == 1
        assert odds[0].sport == "hockey"
