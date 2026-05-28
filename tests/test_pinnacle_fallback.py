"""
Tests for Phase 3.8 — per-league matchups fallback when Pinnacle's bulk
/sports/{id}/matchups endpoint omits matchups.

Background: the Resende vs America RJ debug session (2026-05-27) showed that
Pinnacle's web UI displays matches their bulk matchup endpoint doesn't list.
Our scraper used to silently drop those rows; now it falls back to the
per-league /leagues/{id}/matchups endpoint to recover them.

These tests monkeypatch the module-level `_get` to return canned responses
based on URL, then exercise `_fetch_pinnacle_for_sport` end-to-end.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scrapers import pinnacle  # noqa: E402


def _future_iso(minutes: int = 60) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def _make_matchup(mid: int, home: str, away: str, *, start_iso: str | None = None,
                  type_: str = "matchup", parent: int | None = None,
                  units: str = "Regular") -> dict:
    return {
        "id": mid,
        "type": type_,
        "parent": parent,
        "startTime": start_iso or _future_iso(60),
        "units": units,
        "participants": [
            {"alignment": "home", "name": home},
            {"alignment": "away", "name": away},
        ],
    }


def _make_ml_market(matchup_id: int, home_price: int = -110, away_price: int = +100) -> dict:
    """One moneyline market entry for /markets/straight."""
    return {
        "matchupId": matchup_id,
        "type": "moneyline",
        "period": 0,
        "key": "s;0;m",
        "cutoffAt": _future_iso(60),
        "prices": [
            {"designation": "home", "price": home_price},
            {"designation": "away", "price": away_price},
        ],
    }


class _GetDispatcher:
    """Mimic the module-level `_get` helper. Routes by URL substring."""

    def __init__(self, *, leagues: list, bulk_matchups: list,
                 markets_by_lid: dict, per_league_matchups_by_lid: dict):
        self.leagues = leagues
        self.bulk_matchups = bulk_matchups
        self.markets_by_lid = markets_by_lid
        self.per_league_matchups_by_lid = per_league_matchups_by_lid
        self.per_league_matchup_calls: list[int] = []

    async def __call__(self, client, path, params=None):
        # path examples:
        #   /sports/29/leagues
        #   /sports/29/matchups
        #   /leagues/215951/markets/straight
        #   /leagues/215951/matchups   ← the fallback call
        if path.endswith("/leagues") and path.startswith("/sports/"):
            return self.leagues
        if path.endswith("/matchups") and path.startswith("/sports/"):
            return self.bulk_matchups
        if path.endswith("/markets/straight"):
            lid = int(path.split("/")[2])
            return self.markets_by_lid.get(lid, [])
        if path.endswith("/matchups") and path.startswith("/leagues/"):
            lid = int(path.split("/")[2])
            self.per_league_matchup_calls.append(lid)
            return self.per_league_matchups_by_lid.get(lid, [])
        raise AssertionError(f"unexpected path: {path}")


def test_fallback_recovers_missing_matchup(monkeypatch):
    """Bulk endpoint omits matchup 999; per-league endpoint includes it;
    after the fallback fires, the dropped odds rows are recovered."""
    lid = 215951
    league = {"id": lid, "name": "Brazil - Carioca A2"}

    # Bulk matchups: one match (mid=100, Foo vs Bar) — NOT the one we care about.
    bulk = [_make_matchup(100, "Foo", "Bar")]

    # Per-league markets: references TWO matchups — 100 (known) and 999 (missing).
    markets = [
        _make_ml_market(100, -110, +100),   # known to bulk
        _make_ml_market(999, +130, -150),   # missing from bulk — would drop
    ]

    # Per-league matchups endpoint includes BOTH (Pinnacle's UI sees both).
    per_league = [
        _make_matchup(100, "Foo", "Bar"),
        _make_matchup(999, "Resende", "America RJ"),
    ]

    dispatch = _GetDispatcher(
        leagues=[league],
        bulk_matchups=bulk,
        markets_by_lid={lid: markets},
        per_league_matchups_by_lid={lid: per_league},
    )
    monkeypatch.setattr(pinnacle, "_get", dispatch)

    odds = asyncio.run(pinnacle._fetch_pinnacle_for_sport(
        sport_id=29, sport_name="soccer", concurrency=2,
    ))

    # Both matches should now appear (2 ML rows each: a single Odds object
    # carries both home + away in its selections dict).
    homes = sorted({(o.home, o.away) for o in odds})
    assert ("Foo", "Bar") in homes
    assert ("Resende", "America RJ") in homes
    # And the fallback endpoint was actually called for this league
    assert dispatch.per_league_matchup_calls == [lid]


def test_no_fallback_when_bulk_has_everything(monkeypatch):
    """When the bulk endpoint covers every referenced matchup, the per-league
    matchups endpoint must NOT be called (preserves the fast path)."""
    lid = 100
    league = {"id": lid, "name": "Test League"}
    bulk = [_make_matchup(100, "Foo", "Bar"), _make_matchup(101, "Baz", "Qux")]
    markets = [_make_ml_market(100), _make_ml_market(101)]

    dispatch = _GetDispatcher(
        leagues=[league],
        bulk_matchups=bulk,
        markets_by_lid={lid: markets},
        per_league_matchups_by_lid={lid: []},  # would be empty if called
    )
    monkeypatch.setattr(pinnacle, "_get", dispatch)

    odds = asyncio.run(pinnacle._fetch_pinnacle_for_sport(
        sport_id=29, sport_name="soccer", concurrency=2,
    ))

    # Both matches present; fallback was NOT triggered (no per-league call).
    assert {(o.home, o.away) for o in odds} == {("Foo", "Bar"), ("Baz", "Qux")}
    assert dispatch.per_league_matchup_calls == []


def test_fallback_failure_is_graceful(monkeypatch):
    """If the per-league fallback HTTP call fails, the known matchups still
    return; the unknown matchup just stays dropped (current behaviour, but
    no crash, no regression on the known ones)."""
    lid = 215951
    league = {"id": lid, "name": "Test League"}
    bulk = [_make_matchup(100, "Foo", "Bar")]
    markets = [_make_ml_market(100), _make_ml_market(999)]

    class _FailingDispatch(_GetDispatcher):
        async def __call__(self, client, path, params=None):
            if path.endswith("/matchups") and path.startswith("/leagues/"):
                raise RuntimeError("per-league fallback boom")
            return await super().__call__(client, path, params)

    dispatch = _FailingDispatch(
        leagues=[league],
        bulk_matchups=bulk,
        markets_by_lid={lid: markets},
        per_league_matchups_by_lid={lid: []},
    )
    monkeypatch.setattr(pinnacle, "_get", dispatch)

    odds = asyncio.run(pinnacle._fetch_pinnacle_for_sport(
        sport_id=29, sport_name="soccer", concurrency=2,
    ))

    # The known matchup still surfaces.
    assert ("Foo", "Bar") in {(o.home, o.away) for o in odds}
    # The unknown one is dropped (no Resende — fallback failed, no recovery).
    assert "Resende" not in {o.home for o in odds}


# Note: corners-child recovery via fallback is a possible follow-up but isn't
# what Phase 3.8 was scoped for — the Resende case that exposed the bug was a
# plain parent matchup missing from bulk. The 3 tests above cover that contract.
