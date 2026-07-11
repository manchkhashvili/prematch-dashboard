"""Crocobet gameType classifier — parses the captured 2026-07-11 fixture.

The scraper maps on the numeric `gameType` (stable), NOT the Georgian market
names the en locale returns. Outcome labels are the (stable) Georgian words,
mapped by identity. Mappings were cross-verified against Lider via SR-id join
(0.00pp median devigged gap) — see src/scrapers/crocobet.py.
"""
import json
import pathlib
from datetime import datetime, timezone

from src.scrapers.crocobet import _parse_event, _prices

DATA = pathlib.Path(__file__).parent / "data" / "crocobet"
NOW = datetime(2026, 7, 11, tzinfo=timezone.utc)


def _load_event():
    board = json.loads((DATA / "basketball_board.json").read_text())["data"]
    full = json.loads((DATA / "basketball_event.json").read_text())["data"]
    # the saved full-ladder event is the richest board event
    ev = next((e for e in board if str(e["eventId"]) == str(full["eventId"])), board[0])
    return ev, full


def test_prices_map_georgian_outcomes():
    g = {"outcomes": [
        {"outcomeName": "1", "outcomeOdds": 1.8},
        {"outcomeName": "2", "outcomeOdds": 2.0},
        {"outcomeName": "მეტი", "outcomeOdds": 1.9},
        {"outcomeName": "ნაკლები", "outcomeOdds": 1.85},
        {"outcomeName": "X", "outcomeOdds": 3.1},
        {"outcomeName": "suspended", "outcomeOdds": 1.0}]}
    p = _prices(g)
    assert p == {"home": 1.8, "away": 2.0, "over": 1.9, "under": 1.85, "draw": 3.1}


def test_basketball_ft_families_and_sr_id():
    ev, full = _load_event()
    rows = _parse_event(ev, full.get("eventGames") or [], "basketball", NOW)
    by = {}
    for r in rows:
        by.setdefault(r.market_type, []).append(r)
    assert {"moneyline", "spread", "total"} <= set(by)
    ml = by["moneyline"]
    assert len(ml) == 1 and set(ml[0].selections) == {"home", "away"}
    for r in by["spread"]:
        assert set(r.selections) == {"home", "away"} and r.line is not None
    for r in by["total"]:
        assert set(r.selections) == {"over", "under"} and r.line is not None
    assert all(r.source == "crocobet" for r in rows)
    assert all(r.sr_match_id for r in rows), "remoteId → sr_match_id must be set"
    # home/away come from participants (Georgian names, non-empty)
    assert ml[0].home and ml[0].away


def test_only_verified_gametypes_emitted():
    """Unmapped gameTypes (exotics, 2nd-half, odd/even, race-to…) must be
    skipped — the 'avoid confusion' rule: emit only cross-verified codes.
    Verified set as of 2026-07-11: ML/spread/total/team_total across
    FT + H1 + Q1..Q4 (all 0.00pp vs Lider via SR join)."""
    ev, full = _load_event()
    rows = _parse_event(ev, full.get("eventGames") or [], "basketball", NOW)
    assert {r.market_type for r in rows} <= {"moneyline", "spread", "total", "team_total"}
    assert {r.period for r in rows} <= {"FT", "H1", "Q1", "Q2", "Q3", "Q4"}


def test_extended_periods_and_team_totals():
    ev, full = _load_event()
    rows = _parse_event(ev, full.get("eventGames") or [], "basketball", NOW)
    by = {}
    for r in rows:
        by.setdefault((r.market_type, r.period), []).append(r)
    # the captured fixture (Italy U20 W v Germany U20 W, 92 games) carries
    # H1 + quarter ladders and both team totals
    assert by.get(("total", "H1")) and by.get(("spread", "H1"))
    for q in ("Q1", "Q2", "Q3", "Q4"):
        assert by.get(("total", q)), f"missing quarter total {q}"
    tt = [r for r in rows if r.market_type == "team_total"]
    assert tt and {r.team_side for r in tt} == {"home", "away"}
    for r in tt:
        assert set(r.selections) == {"over", "under"} and r.line is not None


def test_suspended_and_missing_side_dropped():
    ev = {"eventId": 1, "remoteId": 99, "eventStart": None,
          "participants": [{"number": 1, "name": "A"}, {"number": 2, "name": "B"}]}
    # a total with only one priced side must not emit
    games = [{"gameType": -2966, "argument": 150.5,
              "outcomes": [{"outcomeName": "მეტი", "outcomeOdds": 1.9},
                           {"outcomeName": "ნაკლები", "outcomeOdds": 1.0}]}]
    assert _parse_event(ev, games, "basketball", NOW) == []
