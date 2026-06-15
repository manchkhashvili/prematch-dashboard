"""
Tests for /api/cross_book — the Pinnacle-anchored "best line across books" grid.

For each market both Pinnacle and >=1 soft book price, the grid lines the books
up, picks the best price per side, and computes +EV vs Pinnacle's devigged fair
(arb when best opposing prices sum <1). These anchor: best-book selection, the
+EV sign vs Pinnacle, and arb detection across two books.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

import src.app as app_mod
from src.models import Odds

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


def _ml(source, home_odds, away_odds):
    return Odds(
        source=source, sport="tennis", home="Alpha", away="Beta",
        market_type="moneyline", period="FT",
        selections={"home": home_odds, "away": away_odds},
        fetched_at=NOW, start_time=NOW,
    )


@pytest.fixture
def grid_state(monkeypatch):
    monkeypatch.setattr(app_mod, "SOFT_BOOKS", ("cb", "liderbet", "betlive"))
    snap = app_mod._state.get("tennis")
    app_mod._state["tennis"] = {
        "cb": app_mod._empty_source_state(),
        "pin": app_mod._empty_source_state(),
        "liderbet": app_mod._empty_source_state(),
        "betlive": app_mod._empty_source_state(),
    }
    # Balanced Pinnacle ML → devigged fair = 2.0 per side.
    app_mod._state["tennis"]["pin"]["odds"] = [_ml("pinnacle", 1.90, 1.90)]
    yield
    if snap is not None:
        app_mod._state["tennis"] = snap


def _home_row(rows):
    return next(r for r in rows if r["side"] == "home")


def test_best_book_and_positive_ev(grid_state):
    # Lider home 2.10 (+5% vs fair 2.0), Betlive home 2.05 (+2.5%). Best = Lider.
    app_mod._state["tennis"]["liderbet"]["odds"] = [_ml("liderbet", 2.10, 1.80)]
    app_mod._state["tennis"]["betlive"]["odds"] = [_ml("betlive", 2.05, 1.85)]
    rows = asyncio.run(app_mod.api_cross_book(min_edge=-100.0, kind=None))
    home = _home_row(rows)
    assert home["pin_fair"] == pytest.approx(2.0, abs=0.01)
    assert home["prices"] == {"liderbet": 2.10, "betlive": 2.05}
    assert home["best_book"] == "liderbet" and home["best_odds"] == 2.10
    assert home["best_edge_pct"] == pytest.approx(5.0, abs=0.2)   # 2.10/2.0 − 1


def test_ev_filter_drops_negative(grid_state):
    # Both books price home BELOW fair → no +EV; ev filter at 1% should drop it.
    app_mod._state["tennis"]["liderbet"]["odds"] = [_ml("liderbet", 1.90, 1.95)]
    app_mod._state["tennis"]["betlive"]["odds"] = [_ml("betlive", 1.85, 2.00)]
    rows = asyncio.run(app_mod.api_cross_book(min_edge=1.0, kind="ev"))
    assert all(r["side"] != "home" or r["best_edge_pct"] >= 1.0 for r in rows)


def test_cross_book_arb_flagged(grid_state):
    # Lider home 2.10 + Betlive away 2.10 → 1/2.1 + 1/2.1 = 0.952 → arb +4.76%.
    app_mod._state["tennis"]["liderbet"]["odds"] = [_ml("liderbet", 2.10, 1.60)]
    app_mod._state["tennis"]["betlive"]["odds"] = [_ml("betlive", 1.60, 2.10)]
    rows = asyncio.run(app_mod.api_cross_book(min_edge=0.0, kind="arb"))
    assert rows, "expected an arb market"
    assert rows[0]["arb_pct"] == pytest.approx(4.76, abs=0.05)
    # best price per side comes from the two different books
    by_side = {r["side"]: r for r in rows}
    assert by_side["home"]["best_book"] == "liderbet"
    assert by_side["away"]["best_book"] == "betlive"
