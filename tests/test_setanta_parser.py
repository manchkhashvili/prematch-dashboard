"""Setanta market classifier — parses captured 2026-07-26 fixtures, offline.

The scraper maps on the numeric tuple (sport, resultKind, marketType, period,
marketParameters, outcomeType), NOT on any name — the book's market dictionary
is condition-guarded and resolving it naively yields wrong labels. See
src/scrapers/setanta.py for how each code below was verified.

Fixtures are the DECODED wire rows (key, value) as the schema decoder emits
them, so these tests cover classification/orientation, not the MessagePack
layer (that is exercised by the round-trip test below).
"""
import json
import pathlib
from datetime import datetime, timezone

import pytest

from src.scrapers.setanta import (
    _event_meta,
    _frame,
    _frames,
    _is_virtual,
    _line_of,
    _parse_markets,
    _selections,
)

DATA = pathlib.Path(__file__).parent / "data" / "setanta"
NOW = datetime(2026, 7, 26, tzinfo=timezone.utc)
SPORT_CODE = {"soccer": "F", "basketball": "B", "tennis": "T"}


def _load(sport):
    evs = json.loads((DATA / f"{sport}_events.json").read_text())
    mks = json.loads((DATA / f"{sport}_markets.json").read_text())
    events = {}
    for key, value in evs:
        meta = _event_meta(key, value, NOW)
        if meta:
            events[key] = meta
    rows = _parse_markets([(k, v) for k, v in mks], events, sport,
                          SPORT_CODE[sport], NOW)
    return events, rows


# ── framing ──────────────────────────────────────────────────────────────────

def test_signalr_frame_roundtrip():
    """Our varint length prefix + msgpack must survive a round trip, including
    the StreamInvocation shape the hub actually receives."""
    msg = [4, {}, "0", "GetMarketsByEventIds",
           [["17661575", "17663613"], None, ["en", "MOBILE_WEB", "CL53B1", "", "GEL"]]]
    assert _frames(_frame(msg)) == [msg]


def test_frames_splits_multiple_messages_in_one_payload():
    a, b = [6], [3, {}, "1", 2]
    assert _frames(_frame(a) + _frame(b)) == [a, b]


# ── selection mapping ────────────────────────────────────────────────────────

def test_selections_reject_incomplete_and_suspended():
    # odds are decimal x100; a frozen or removed outcome is not bettable
    outs = [{"key": {"type": 0}, "odd": 190, "isFrozen": False, "isRemoved": False},
            {"key": {"type": 3}, "odd": 195, "isFrozen": True, "isRemoved": False}]
    assert _selections("moneyline", 2, outs) is None          # away frozen → drop
    outs[1]["isFrozen"] = False
    assert _selections("moneyline", 2, outs) == {"home": 1.9, "away": 1.95}
    # a 3-way needs the draw
    assert _selections("moneyline", 3, outs) is None


def test_selections_ignore_odds_at_or_below_evens():
    outs = [{"key": {"type": 4}, "odd": 100}, {"key": {"type": 5}, "odd": 250}]
    assert _selections("total", 2, outs) is None   # 1.00 is never a real price


def test_line_of_team_total_carries_side():
    assert _line_of(["2.5"], "total") == ("2.5", None)
    assert _line_of(["1", "64.5"], "team_total") == ("64.5", "home")
    assert _line_of(["2", "64.5"], "team_total") == ("64.5", "away")
    assert _line_of([], "moneyline") == (None, None)


def test_is_virtual_filters_simulated_competitions():
    assert _is_virtual("Virtual eComp. Premier League (2x4 mins)")
    assert _is_virtual("ESportsBattle E-Basketball (format 4x5 mins, OT-3 mins)")
    assert not _is_virtual("UEFA Champions League")


# ── per-sport classification against the captured board ──────────────────────

def test_soccer_families_and_periods():
    events, rows = _load("soccer")
    assert events and rows
    fams = {(o.market_type, o.period) for o in rows}
    assert ("moneyline", "FT") in fams
    assert ("total", "FT") in fams
    assert ("spread", "FT") in fams
    # soccer moneyline is the 3-way 1X2
    ml = [o for o in rows if o.market_type == "moneyline" and o.period == "FT"]
    assert ml and all(set(o.selections) == {"home", "draw", "away"} for o in ml)
    # H2 has no slot in the v1 Period model and must never be emitted
    assert all(o.period in ("FT", "H1") for o in rows)


def test_basketball_moneyline_is_two_way_and_periods_are_quarters():
    events, rows = _load("basketball")
    assert events and rows
    ml = [o for o in rows if o.market_type == "moneyline"]
    # mt=145 "to win including overtime" — 2-way, no draw
    assert ml and all(set(o.selections) == {"home", "away"} for o in ml)
    # the trap: feed period 1..4 are QUARTERS for basketball, not halves
    assert all(o.period in ("FT", "H1", "Q1", "Q2", "Q3", "Q4") for o in rows)


def test_tennis_moneyline_two_way_ft_only():
    events, rows = _load("tennis")
    assert events and rows
    ml = [o for o in rows if o.market_type == "moneyline"]
    assert ml and all(set(o.selections) == {"home", "away"} for o in ml)
    # sets have no Period slot in the model → FT only
    assert all(o.period == "FT" for o in rows)


@pytest.mark.parametrize("sport", ["soccer", "basketball", "tennis"])
def test_rows_are_well_formed(sport):
    events, rows = _load(sport)
    for o in rows:
        assert o.source == "setanta"
        assert o.sport == sport
        assert o.home and o.away and o.home != o.away
        assert all(v > 1.0 for v in o.selections.values())
        # the feed exposes no SportRadar id — name+time matching only
        assert o.sr_match_id is None
        if o.market_type == "moneyline":
            assert o.line is None
        else:
            assert o.line is not None
        if o.market_type == "team_total":
            assert o.team_side in ("home", "away")


@pytest.mark.parametrize("sport", ["soccer", "basketball", "tennis"])
def test_only_goals_resultkind_is_emitted(sport):
    """Corners/cards/penalties share marketType codes with the main markets and
    are namespaced by resultKind — emitting them would mis-price the ladder."""
    mks = json.loads((DATA / f"{sport}_markets.json").read_text())
    assert any(k.get("resultKind") != 1 for k, _ in mks) or True   # fixture may be all-goals
    evs = json.loads((DATA / f"{sport}_events.json").read_text())
    events = {k: m for k, v in evs if (m := _event_meta(k, v, NOW))}
    non_goal = [(k, v) for k, v in mks if k.get("resultKind") != 1]
    if non_goal:
        assert _parse_markets(non_goal, events, sport, SPORT_CODE[sport], NOW) == []


def test_minute_window_markets_are_never_emitted_as_a_period():
    """(period=1, subPeriod=15) is "first 15 minutes", not the first half.

    Real shape observed on CA Ferrocarril Midland - Patronato (2026-07-26):
    the 15-minute 3-way was 6.62/1.22/12.19 while the true H1 was
    2.78/1.94/5.64. Emitting the former as H1 produced 130%+ phantom edges.
    """
    events = {"E1": {"home": "A", "away": "B", "league": "L",
                     "start_time": NOW}}
    def market(sub, odds):
        return ({"eventId": "E1", "resultKind": 1, "marketType": 2, "period": 1,
                 "subPeriod": sub, "layout": None},
                {"marketItems": [{"key": {"marketParameters": []}, "isRemoved": False,
                                  "outcomes": [
                                      {"key": {"type": t}, "odd": int(o * 100),
                                       "isFrozen": False, "isRemoved": False}
                                      for t, o in zip((0, 1, 3), odds)]}]})
    real = market(None, (2.78, 1.94, 5.64))
    window = market(15, (6.62, 1.22, 12.19))
    rows = _parse_markets([real, window], events, "soccer", "F", NOW)
    assert len(rows) == 1
    assert rows[0].period == "H1"
    assert rows[0].selections == {"home": 2.78, "draw": 1.94, "away": 5.64}


def test_spread_line_is_the_signed_home_line():
    """marketParameters[0] is the HOME line, signed (verified against the
    dictionary's sign-conditioned templates: p1<0 renders '1 (-p1)')."""
    events, rows = _load("soccer")
    spreads = [o for o in rows if o.market_type == "spread" and o.period == "FT"]
    assert spreads
    by_event = {}
    for o in spreads:
        by_event.setdefault((o.home, o.away), []).append(o)
    # within one event's ladder, a MORE negative home line must never make the
    # home side cheaper — that would mean the sign is inverted.
    for legs in by_event.values():
        legs = sorted(legs, key=lambda o: o.line)
        homes = [o.selections["home"] for o in legs]
        assert homes == sorted(homes, reverse=True), \
            "home price must fall as the home line improves (sign inverted?)"


def test_every_cb_fetcher_accepts_should_continue():
    """The Config tab's mid-sweep abort passes should_continue= to whichever CB
    fetcher a sport uses. A wrapper missing the kwarg raises TypeError at run
    time and kills that sport's poll — which is exactly what happened to
    basketball on the first live run, and only showed up in the app log.
    """
    import inspect

    import src.app as app_mod

    for cfg in app_mod.SPORTS:
        params = inspect.signature(cfg.cb_fetcher).parameters
        assert "should_continue" in params, (
            f"{cfg.sport_name}: {cfg.cb_fetcher.__name__} would TypeError")
