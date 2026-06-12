"""
Regression tests for prematch.src.scrapers.pinnacle parser internals.

Each test guards a behavior or bug from the build log:

  - Sub-matchup filter: parent != None dropped (2026-05-24 Checkpoint 2)
  - Live-game filter: startTime <= now dropped
  - Participants/alignment filter: missing home or away dropped
  - Period filter: only 0/1 kept (Q1-Q4 not offered prematch)
  - Market-type filter: only ml/spread/total
  - Suspended-side handling: price=None → drop whole line
  - Decimal odds ≤ 1.0 → drop whole line
  - Per-type completeness: ml needs both sides, spread/total need line + both sides
  - _parse_iso handles Z suffix
  - League blocklist (cyber/esport/etc) — covered as a smoke test

These tests exercise the pure-data internals; no HTTP is involved. The HTTP
layer (retry, failure tracker) is covered separately if needed.

Run from prematch/:
    python -m pytest tests/ -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scrapers.pinnacle import (  # noqa: E402
    LEAGUE_SKIP,
    _build_odds_for_league,
    _index_matchups,
    _parse_iso,
)


NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
FUTURE_ISO = (NOW + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
PAST_ISO = (NOW - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _matchup(
    mid: int,
    *,
    home: str = "Hawks",
    away: str = "Lakers",
    start_iso: str = FUTURE_ISO,
    parent=None,
) -> dict:
    return {
        "id": mid,
        "parent": parent,
        "startTime": start_iso,
        "participants": [
            {"alignment": "home", "name": home},
            {"alignment": "away", "name": away},
        ],
    }


def _market(
    *,
    matchup_id: int,
    market_type: str,
    period: int,
    prices: list[dict],
) -> dict:
    return {
        "matchupId": matchup_id,
        "type": market_type,
        "period": period,
        "prices": prices,
        "key": f"s;{period};{market_type[0]}",
    }


# ── _index_matchups ───────────────────────────────────────────────────────────
class TestIndexMatchups:
    def test_top_level_prematch_indexed(self):
        m = _matchup(100)
        idx = _index_matchups([m], NOW)
        assert 100 in idx
        assert idx[100]["home"] == "Hawks"
        assert idx[100]["away"] == "Lakers"

    def test_sub_matchup_with_parent_dropped(self):
        """parent != None → sub-matchup (player props / derivatives). Dropped."""
        m = _matchup(200, parent={"id": 100})
        idx = _index_matchups([m], NOW)
        assert idx == {}

    def test_live_game_dropped(self):
        """startTime <= now → already-started, drop (prematch only)."""
        m = _matchup(300, start_iso=PAST_ISO)
        idx = _index_matchups([m], NOW)
        assert idx == {}

    def test_missing_home_or_away_dropped(self):
        m = _matchup(400)
        m["participants"] = [{"alignment": "home", "name": "Hawks"}]  # away missing
        idx = _index_matchups([m], NOW)
        assert idx == {}

    def test_alignment_case_insensitive(self):
        """Defensive: if Pinnacle ever returns 'Home'/'Away' we still parse."""
        m = _matchup(500)
        m["participants"] = [
            {"alignment": "HOME", "name": "Hawks"},
            {"alignment": "AWAY", "name": "Lakers"},
        ]
        idx = _index_matchups([m], NOW)
        assert 500 in idx

    def test_non_dict_matchup_skipped(self):
        """Defensive against malformed API responses."""
        idx = _index_matchups([None, "garbage", _matchup(600)], NOW)
        assert 600 in idx
        assert len(idx) == 1


# ── _build_odds_for_league: filtering ─────────────────────────────────────────
class TestBuildOddsFiltering:
    def _by_id(self):
        return {100: {"home": "Hawks", "away": "Lakers", "start_time": NOW}}

    def test_unknown_matchup_dropped(self):
        """Market keyed to a matchupId not in the bulk-matchup index → skipped.
        This is how sub-matchups and live games are filtered out at the
        markets level."""
        mkts = [_market(matchup_id=999, market_type="moneyline", period=0,
                        prices=[
                            {"designation": "home", "price": -110},
                            {"designation": "away", "price": -110},
                        ])]
        out = _build_odds_for_league(mkts, self._by_id(), "NBA")
        assert out == []

    def test_unknown_period_dropped(self):
        """period not in {0, 1} → drop (Q1-Q4 = period 2/3/4/5 etc)."""
        mkts = [_market(matchup_id=100, market_type="moneyline", period=2,
                        prices=[
                            {"designation": "home", "price": -110},
                            {"designation": "away", "price": -110},
                        ])]
        assert _build_odds_for_league(mkts, self._by_id(), "NBA") == []

    def test_unknown_market_type_dropped(self):
        """team_total / player_props etc → drop."""
        mkts = [_market(matchup_id=100, market_type="team_total", period=0,
                        prices=[
                            {"designation": "over", "price": -110},
                            {"designation": "under", "price": -110},
                        ])]
        assert _build_odds_for_league(mkts, self._by_id(), "NBA") == []

    def test_suspended_side_drops_whole_line(self):
        """price=None on one side → drop the entire market entry."""
        mkts = [_market(matchup_id=100, market_type="moneyline", period=0,
                        prices=[
                            {"designation": "home", "price": -110},
                            {"designation": "away", "price": None},
                        ])]
        assert _build_odds_for_league(mkts, self._by_id(), "NBA") == []

    def test_american_odds_zero_drops_whole_line(self):
        """Defensive: american_to_decimal(0) raises ValueError. The parser
        must catch and drop the whole market line, not let Odds.__post_init__
        propagate the failure up the call stack and kill the entire poll."""
        mkts = [_market(matchup_id=100, market_type="moneyline", period=0,
                        prices=[
                            {"designation": "home", "price": 0},
                            {"designation": "away", "price": +200},
                        ])]
        assert _build_odds_for_league(mkts, self._by_id(), "NBA") == []

    def test_unknown_designation_skipped(self):
        """Designations like 'draw' or 'tie' (not in the home/away/over/under
        set) are silently ignored at the price level. Basketball is binary;
        if the side set ends up incomplete, the per-type completeness check
        drops the market."""
        mkts = [_market(matchup_id=100, market_type="moneyline", period=0,
                        prices=[
                            {"designation": "home", "price": -110},
                            {"designation": "draw", "price": +500},  # ignored
                        ])]
        # Only home parsed → moneyline-needs-both-sides check drops it.
        assert _build_odds_for_league(mkts, self._by_id(), "NBA") == []

    def test_moneyline_requires_both_sides(self):
        mkts = [_market(matchup_id=100, market_type="moneyline", period=0,
                        prices=[
                            {"designation": "home", "price": -110},
                        ])]
        assert _build_odds_for_league(mkts, self._by_id(), "NBA") == []

    def test_spread_requires_line(self):
        """spread with no `points` field on either side → drop."""
        mkts = [_market(matchup_id=100, market_type="spread", period=0,
                        prices=[
                            {"designation": "home", "points": None, "price": -110},
                            {"designation": "away", "points": None, "price": -110},
                        ])]
        assert _build_odds_for_league(mkts, self._by_id(), "NBA") == []

    def test_total_requires_over_and_under(self):
        mkts = [_market(matchup_id=100, market_type="total", period=0,
                        prices=[
                            {"designation": "over", "points": 220.5, "price": -110},
                        ])]
        assert _build_odds_for_league(mkts, self._by_id(), "NBA") == []


# ── _build_odds_for_league: happy paths ───────────────────────────────────────
class TestBuildOddsHappyPath:
    def _by_id(self):
        return {100: {"home": "Hawks", "away": "Lakers", "start_time": NOW}}

    def test_moneyline_period_0_ft(self):
        mkts = [_market(matchup_id=100, market_type="moneyline", period=0,
                        prices=[
                            {"designation": "home", "price": -110},
                            {"designation": "away", "price": -110},
                        ])]
        out = _build_odds_for_league(mkts, self._by_id(), "NBA")
        assert len(out) == 1
        assert out[0].market_type == "moneyline"
        assert out[0].period == "FT"
        assert out[0].line is None
        # -110 → decimal 1.9090909
        assert abs(out[0].selections["home"] - 1.9090909) < 1e-6

    def test_moneyline_period_1_h1(self):
        mkts = [_market(matchup_id=100, market_type="moneyline", period=1,
                        prices=[
                            {"designation": "home", "price": -110},
                            {"designation": "away", "price": -110},
                        ])]
        out = _build_odds_for_league(mkts, self._by_id(), "NBA")
        assert len(out) == 1
        assert out[0].period == "H1"

    def test_spread_with_alt_line(self):
        """Alt-line should be emitted, not just the main line."""
        mkts = [_market(matchup_id=100, market_type="spread", period=0,
                        prices=[
                            {"designation": "home", "points": -6.5, "price": -110},
                            {"designation": "away", "points": +6.5, "price": -110},
                        ])]
        out = _build_odds_for_league(mkts, self._by_id(), "NBA")
        assert len(out) == 1
        assert out[0].market_type == "spread"
        assert out[0].line == -6.5

    def test_total_with_line(self):
        mkts = [_market(matchup_id=100, market_type="total", period=0,
                        prices=[
                            {"designation": "over", "points": 220.5, "price": -105},
                            {"designation": "under", "points": 220.5, "price": -115},
                        ])]
        out = _build_odds_for_league(mkts, self._by_id(), "NBA")
        assert len(out) == 1
        assert out[0].market_type == "total"
        assert out[0].line == 220.5
        assert out[0].selections.keys() == {"over", "under"}


# ── _parse_iso ────────────────────────────────────────────────────────────────
class TestParseIso:
    def test_z_suffix(self):
        result = _parse_iso("2026-05-25T19:00:00Z")
        assert result == datetime(2026, 5, 25, 19, 0, 0, tzinfo=timezone.utc)

    def test_offset(self):
        result = _parse_iso("2026-05-25T19:00:00+00:00")
        assert result == datetime(2026, 5, 25, 19, 0, 0, tzinfo=timezone.utc)

    def test_none(self):
        assert _parse_iso(None) is None

    def test_malformed(self):
        assert _parse_iso("garbage") is None


# ── League blocklist ──────────────────────────────────────────────────────────
class TestLeagueBlocklist:
    """
    The blocklist lives in fetch_pinnacle_basketball() as an inline lambda
    over LEAGUE_SKIP. Test the contents directly.
    """

    def test_blocklist_contains_expected_terms(self):
        # If any of these disappears, real cyber/esport leagues would leak in.
        assert "cyber" in LEAGUE_SKIP
        assert "esport" in LEAGUE_SKIP
        assert "ebasket" in LEAGUE_SKIP
        assert "outright" in LEAGUE_SKIP
        assert "specials" in LEAGUE_SKIP

    def test_skip_matches_substring_case_insensitive(self):
        # Mimic the filter logic in fetch_pinnacle_basketball.
        def is_skipped(name: str) -> bool:
            return any(s in name.lower() for s in LEAGUE_SKIP)

        assert is_skipped("Cyber Arena Basketball") is True
        assert is_skipped("ESports League") is True
        assert is_skipped("NBA Outrights") is True
        assert is_skipped("USA NBA") is False
        assert is_skipped("Spain ACB") is False


# ── Limits capture + spread line sign (v2 findings, 2026-06-12) ───────────────
class TestLimitsAndLineSign:
    def _by_id(self):
        return {100: {"home": "Hawks", "away": "Lakers", "start_time": NOW}}

    def test_max_stake_captured_from_limits(self):
        mkt = _market(matchup_id=100, market_type="moneyline", period=0,
                      prices=[{"designation": "home", "price": -110},
                              {"designation": "away", "price": -110}])
        mkt["limits"] = [{"amount": 400, "type": "maxRiskStake"},
                         {"amount": 9999, "type": "other"}]
        out = _build_odds_for_league([mkt], self._by_id(), "NBA")
        assert len(out) == 1 and out[0].max_stake == 400

    def test_no_limits_means_none(self):
        mkt = _market(matchup_id=100, market_type="moneyline", period=0,
                      prices=[{"designation": "home", "price": -110},
                              {"designation": "away", "price": -110}])
        out = _build_odds_for_league([mkt], self._by_id(), "NBA")
        assert out[0].max_stake is None

    def test_spread_line_is_homes_points_even_when_away_listed_first(self):
        # v2 finding #2: taking points from the FIRST price flipped the sign
        # whenever Pinnacle listed the away price first. home is +1.5 here.
        mkt = _market(matchup_id=100, market_type="spread", period=0,
                      prices=[{"designation": "away", "price": -120, "points": -1.5},
                              {"designation": "home", "price": 100, "points": 1.5}])
        out = _build_odds_for_league([mkt], self._by_id(), "NBA")
        assert len(out) == 1
        assert out[0].line == 1.5   # was -1.5 with the first-price bug

    def test_total_line_unaffected_by_order(self):
        mkt = _market(matchup_id=100, market_type="total", period=0,
                      prices=[{"designation": "under", "price": -105, "points": 220.5},
                              {"designation": "over", "price": -115, "points": 220.5}])
        out = _build_odds_for_league([mkt], self._by_id(), "NBA")
        assert out[0].line == 220.5
