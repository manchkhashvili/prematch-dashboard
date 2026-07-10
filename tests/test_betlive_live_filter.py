"""Betlive prematch scraper must drop in-play events (2026-07-11 fix).

getLeagueEvents occasionally returns already-started / live fixtures mixed in
with the prematch line. Their prices have moved off the pre-game number and
were firing phantom edges/arbs on the dashboard. Betlive marks live events
with eventType == 2 (verified live: eventType==2 exactly equalled the set of
events whose startDate was already in the past). We drop on that marker AND on
a start-time-in-the-past check (grace for clock skew).
"""
from datetime import datetime, timedelta, timezone

from src.scrapers.betlive import _is_live, _parse_event

NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
_EPOCH = datetime(1, 1, 1, tzinfo=timezone.utc)


def _ticks(dt: datetime) -> int:
    return int((dt - _EPOCH).total_seconds() * 1e7)


def _event(event_type: int, start: datetime) -> dict:
    return {
        "eventType": event_type, "homeTeamName": "Alpha", "awayTeamName": "Beta",
        "startDate": _ticks(start), "leagueName": "Test League", "id": 1,
        "markets": [{"outcomes": [
            {"name": "1", "odd": 2.0, "marketName": "1x2"},
            {"name": "X", "odd": 3.2, "marketName": "1x2"},
            {"name": "2", "odd": 3.6, "marketName": "1x2"}]}],
    }


def test_live_marker_is_dropped():
    assert _is_live({"eventType": 2}, NOW + timedelta(hours=1), NOW) is True


def test_started_in_past_is_dropped():
    assert _is_live({"eventType": 1}, NOW - timedelta(minutes=30), NOW) is True


def test_future_prematch_is_kept():
    assert _is_live({"eventType": 1}, NOW + timedelta(hours=2), NOW) is False


def test_within_grace_is_kept():
    # 60s after scheduled start, no live marker → clock skew, still prematch
    assert _is_live({"eventType": 1}, NOW - timedelta(seconds=60), NOW) is False


def test_missing_start_time_relies_on_marker():
    assert _is_live({"eventType": 1}, None, NOW) is False
    assert _is_live({"eventType": 2}, None, NOW) is True


def test_parse_event_drops_live_entirely():
    live = _event(2, NOW - timedelta(minutes=5))
    assert _parse_event(live, "soccer", NOW) == []


def test_parse_event_keeps_future_prematch():
    fut = _event(1, NOW + timedelta(hours=3))
    rows = _parse_event(fut, "soccer", NOW)
    assert rows, "a future 1X2 prematch event should still yield Odds rows"
    assert all(o.source == "betlive" for o in rows)
