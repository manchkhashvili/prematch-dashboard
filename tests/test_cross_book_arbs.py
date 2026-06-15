"""
Tests for the cross-book arbitrage join (Phase 7).

Local books (Lider-Bet / Betlive / CB) are joined EXACTLY on the SportRadar
match id, then the best price per side across books is checked for an arb.
These anchor: (1) a real arb is found and its legs span >=2 books with a correct
stake split; (2) a single book that prices both sides best is NOT reported (you
can't place a placeable cross-book arb against yourself); (3) rows only form
when the SportRadar ids actually match.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

import src.app as app_mod
from src.models import Odds

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


def _ml(source, sr, home_odds, away_odds, sr_id="900001"):
    return Odds(
        source=source, sport="tennis", home="A. Player", away="B. Player",
        market_type="moneyline", period="FT",
        selections={"home": home_odds, "away": away_odds},
        fetched_at=NOW, start_time=NOW, sr_match_id=sr_id, raw_event_id=sr,
    )


@pytest.fixture
def two_books(monkeypatch):
    """Enable liderbet+betlive slots for the duration of a test."""
    monkeypatch.setattr(app_mod, "SOFT_BOOKS", ("cb", "liderbet", "betlive"))
    snap = app_mod._state.get("tennis")
    app_mod._state["tennis"] = {
        "cb": app_mod._empty_source_state(),
        "pin": app_mod._empty_source_state(),
        "liderbet": app_mod._empty_source_state(),
        "betlive": app_mod._empty_source_state(),
    }
    yield
    if snap is not None:
        app_mod._state["tennis"] = snap


def test_positive_cross_book_arb_found(two_books):
    # Lider home@2.10, Betlive away@2.10 → 1/2.1 + 1/2.1 = 0.952 → +4.76% arb.
    app_mod._state["tennis"]["liderbet"]["odds"] = [_ml("liderbet", "L1", 2.10, 1.50)]
    app_mod._state["tennis"]["betlive"]["odds"] = [_ml("betlive", "B1", 1.50, 2.10)]
    rows = asyncio.run(app_mod.api_cross_arbs(min_edge=0.0))
    assert len(rows) == 1
    arb = rows[0]
    assert arb["edge_pct"] == pytest.approx(4.76, abs=0.05)
    # legs span both books, best price per side
    legs = {l["side"]: l for l in arb["legs"]}
    assert legs["home"]["book"] == "liderbet" and legs["home"]["odds"] == 2.10
    assert legs["away"]["book"] == "betlive" and legs["away"]["odds"] == 2.10
    # equal-payout stake split sums to 100%
    assert sum(l["stake_pct"] for l in arb["legs"]) == pytest.approx(100.0, abs=0.1)


def test_single_book_best_on_both_sides_not_reported(two_books):
    # Lider prices BOTH sides higher → no placeable cross-book arb (one book).
    app_mod._state["tennis"]["liderbet"]["odds"] = [_ml("liderbet", "L1", 2.10, 2.10)]
    app_mod._state["tennis"]["betlive"]["odds"] = [_ml("betlive", "B1", 1.80, 1.80)]
    rows = asyncio.run(app_mod.api_cross_arbs(min_edge=0.0))
    assert rows == []


def test_no_join_when_sr_ids_differ(two_books):
    app_mod._state["tennis"]["liderbet"]["odds"] = [_ml("liderbet", "L1", 2.10, 1.50, sr_id="111")]
    app_mod._state["tennis"]["betlive"]["odds"] = [_ml("betlive", "B1", 1.50, 2.10, sr_id="222")]
    rows = asyncio.run(app_mod.api_cross_arbs(min_edge=0.0))
    assert rows == []  # different fixtures, never joined
