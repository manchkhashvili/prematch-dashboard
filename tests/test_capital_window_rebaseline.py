"""Windowed capital — the "start fresh" re-baseline.

With a `since`, the period should read as a self-contained book: starting
capital is what the operation was worth AT the window start, and dividends count
only what was taken out during it. Balances/equity stay CURRENT — where the money
is now does not depend on which dates you are looking at, and zeroing them would
misstate the bankroll.

Nothing is rewritten: clearing the window returns the all-time view unchanged.
"""
from __future__ import annotations

import pytest

from src import bets, capital


@pytest.fixture
def clean_db(tmp_path):
    bets._reset_for_tests()
    bets.init_db(tmp_path / "test_bets.db")
    yield
    bets._reset_for_tests()


def _bet(placed, settled_at, outcome, stake=100.0, odds=2.0):
    bid = bets.create_bet(
        sport="basketball", match_label="H vs A", period="FT",
        market_type="moneyline", side="home", book="cb",
        odds_taken=odds, stake=stake, bankroll_at_time=1000.0,
        placed_at=placed,
    )
    bets.settle_bet(bid, outcome)
    bets.update_bet(bid, settled_at=settled_at)
    return bid


@pytest.fixture
def seeded(clean_db):
    acct = capital.add_account("Book", book_tag="cb")
    capital.add_entry(acct, "opening", 1000.0, ts="2026-01-01T00:00:00+00:00")
    _bet("2026-02-01T00:00:00+00:00", "2026-02-02T00:00:00+00:00", "won")   # +100
    _bet("2026-06-01T00:00:00+00:00", "2026-06-02T00:00:00+00:00", "won")   # +100
    return acct


def test_all_time_is_unchanged(seeded):
    t = capital.capital_summary()["totals"]
    assert t["starting_capital"] == 1000.0
    assert t["starting_capital"] == t["starting_capital_all_time"]


def test_fresh_window_rebaselines_to_current_equity(seeded):
    """The headline: filter to a period with no activity and the period opens at
    your CURRENT bankroll with everything else at zero."""
    all_t = capital.capital_summary()["totals"]
    t = capital.capital_summary(since="2027-01-01T00:00:00+00:00")["totals"]
    assert t["settled_pnl"] == 0
    assert t["dividend_total"] == 0
    assert t["starting_capital"] == pytest.approx(all_t["equity"], abs=0.01)
    # equity itself must NOT be zeroed — the money still exists
    assert t["equity"] == pytest.approx(all_t["equity"], abs=0.01)


def test_window_opening_plus_pnl_reconciles_to_equity(seeded):
    """With no capital moved inside the window, opening + PnL == equity."""
    t = capital.capital_summary(since="2026-03-01T00:00:00+00:00")["totals"]
    assert t["settled_pnl"] == pytest.approx(100.0, abs=0.01)   # only the June bet
    assert t["starting_capital"] + t["settled_pnl"] == pytest.approx(t["equity"], abs=0.01)


def test_all_time_figure_still_exposed_when_windowed(seeded):
    t = capital.capital_summary(since="2026-03-01T00:00:00+00:00")["totals"]
    assert t["starting_capital_all_time"] == 1000.0
    assert t["starting_capital"] != t["starting_capital_all_time"]


def test_dividends_are_scoped_to_the_window(clean_db):
    book = capital.add_account("Book", book_tag="cb")
    div = capital.add_account("Dividend", is_dividend=True)
    capital.add_entry(book, "opening", 1000.0, ts="2026-01-01T00:00:00+00:00")
    capital.add_entry(div, "deposit", 200.0, ts="2026-02-01T00:00:00+00:00")
    capital.add_entry(div, "deposit", 300.0, ts="2026-08-01T00:00:00+00:00")

    assert capital.capital_summary()["totals"]["dividend_total"] == 500.0
    t = capital.capital_summary(since="2026-07-01T00:00:00+00:00")["totals"]
    assert t["dividend_total"] == 300.0            # only the August one
    assert t["dividend_total_all_time"] == 500.0


def test_windowing_is_a_view_not_a_mutation(seeded):
    """Reading a window must not change what the all-time view reports."""
    before = capital.capital_summary()["totals"]
    capital.capital_summary(since="2027-01-01T00:00:00+00:00")
    after = capital.capital_summary()["totals"]
    assert before == after
