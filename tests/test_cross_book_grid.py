"""
Tests for /api/cross_book — the SportRadar-id-joined "best line" grid.

Local books are joined EXACTLY on sr_match_id (not via Pinnacle), Pinnacle is
attached as the fair reference for the +EV column, and only markets where >=2
books price a side are kept. These anchor: best-book selection + +EV vs Pinnacle,
the >=2-book requirement (single-book markets dropped), and arb across books.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

import src.app as app_mod
from src.models import Odds

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
SR = "900001"


def _ml(source, home_odds, away_odds, sr_id=None):
    return Odds(
        source=source, sport="tennis", home="Alpha", away="Beta",
        market_type="moneyline", period="FT",
        selections={"home": home_odds, "away": away_odds},
        fetched_at=NOW, start_time=NOW, sr_match_id=sr_id,
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
    # Balanced Pinnacle ML → devigged fair = 2.0 per side (the +EV reference).
    app_mod._state["tennis"]["pin"]["odds"] = [_ml("pinnacle", 1.90, 1.90)]
    yield
    if snap is not None:
        app_mod._state["tennis"] = snap


def _home_row(rows):
    return next(r for r in rows if r["side"] == "home")


def test_best_book_and_positive_ev(grid_state):
    # Lider home 2.10 (+5% vs fair 2.0), Betlive home 2.05. Best = Lider; both
    # books price home → a real cross-book comparison.
    app_mod._state["tennis"]["liderbet"]["odds"] = [_ml("liderbet", 2.10, 1.80, SR)]
    app_mod._state["tennis"]["betlive"]["odds"] = [_ml("betlive", 2.05, 1.85, SR)]
    rows = asyncio.run(app_mod.api_cross_book(min_edge=-100.0, kind=None))
    home = _home_row(rows)
    assert home["sr_match_id"] == SR
    assert home["pin_fair"] == pytest.approx(2.0, abs=0.01)
    assert home["prices"] == {"liderbet": 2.10, "betlive": 2.05}
    assert home["best_book"] == "liderbet" and home["best_odds"] == 2.10
    assert home["best_edge_pct"] == pytest.approx(5.0, abs=0.2)


def test_single_book_market_dropped(grid_state):
    # Only Lider prices this fixture → no second book → nothing to compare.
    app_mod._state["tennis"]["liderbet"]["odds"] = [_ml("liderbet", 2.10, 1.80, SR)]
    rows = asyncio.run(app_mod.api_cross_book(min_edge=-100.0, kind=None))
    assert rows == []


def test_no_join_when_sr_ids_differ(grid_state):
    # Same teams, DIFFERENT sr ids → not the same fixture → not joined.
    app_mod._state["tennis"]["liderbet"]["odds"] = [_ml("liderbet", 2.10, 1.80, "111")]
    app_mod._state["tennis"]["betlive"]["odds"] = [_ml("betlive", 2.05, 1.85, "222")]
    rows = asyncio.run(app_mod.api_cross_book(min_edge=-100.0, kind=None))
    assert rows == []


def test_cross_book_arb_flagged(grid_state):
    # Lider home 2.10 + Betlive away 2.10 → 1/2.1 + 1/2.1 = 0.952 → arb +4.76%.
    app_mod._state["tennis"]["liderbet"]["odds"] = [_ml("liderbet", 2.10, 1.60, SR)]
    app_mod._state["tennis"]["betlive"]["odds"] = [_ml("betlive", 1.60, 2.10, SR)]
    rows = asyncio.run(app_mod.api_cross_book(min_edge=0.0, kind="arb"))
    assert rows, "expected an arb market"
    assert rows[0]["arb_pct"] == pytest.approx(4.76, abs=0.05)
    by_side = {r["side"]: r for r in rows}
    assert by_side["home"]["best_book"] == "liderbet"
    assert by_side["away"]["best_book"] == "betlive"


def test_crystalbet_folds_in_via_pin_bridge(grid_state):
    # CB has no sr id, but it name-matches the same Pinnacle event the soft books
    # matched → it joins the cluster through the SR↔pin bridge and can be best.
    app_mod._state["tennis"]["liderbet"]["odds"] = [_ml("liderbet", 2.05, 1.85, SR)]
    app_mod._state["tennis"]["betlive"]["odds"] = [_ml("betlive", 2.00, 1.90, SR)]
    app_mod._state["tennis"]["cb"]["odds"] = [_ml("crystalbet", 2.20, 1.75)]
    rows = asyncio.run(app_mod.api_cross_book(min_edge=-100.0, kind=None))
    home = _home_row(rows)
    assert "cb" in home["prices"] and home["prices"]["cb"] == 2.20
    assert home["best_book"] == "cb"      # CB's 2.20 beats both soft books
