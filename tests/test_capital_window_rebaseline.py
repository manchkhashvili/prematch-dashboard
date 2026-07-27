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


# ── mode="fresh": bets AND ledger windowed, so an empty period reads all zeros ──

def test_fresh_mode_zeroes_everything_for_an_empty_window(seeded):
    """The ask: filter to a period with no activity and the WHOLE page reads 0 —
    balances and per-account rows included, not just performance."""
    t = capital.capital_summary(since="2027-01-01T00:00:00+00:00", mode="fresh")["totals"]
    assert t["starting_capital"] == 0
    assert t["settled_pnl"] == 0
    assert t["equity"] == 0
    assert t["open_exposure"] == 0
    assert t["total_stake"] == 0
    assert t["dividend_total"] == 0

    rows = capital.capital_summary(since="2027-01-01T00:00:00+00:00", mode="fresh")["accounts"]
    for r in rows:
        assert r["opening"] == 0 and r["ledger_net"] == 0 and r["balance"] == 0
        assert r["n_bets"] == 0 and r["total_stake"] == 0


def test_pnl_mode_keeps_the_money_columns(seeded):
    """The other mode: same empty window, but balances stay real."""
    t = capital.capital_summary(since="2027-01-01T00:00:00+00:00", mode="pnl")["totals"]
    assert t["settled_pnl"] == 0          # performance windowed
    assert t["equity"] > 0                # money is still there
    assert t["starting_capital"] == pytest.approx(t["equity"], abs=0.01)


def test_fresh_mode_counts_only_in_window_activity(seeded):
    """A window that DOES contain activity reports exactly that activity."""
    t = capital.capital_summary(since="2026-05-01T00:00:00+00:00", mode="fresh")["totals"]
    # only the June bet was placed in the window (+100 on a 100 stake)
    assert t["settled_pnl"] == pytest.approx(100.0, abs=0.01)
    assert t["total_stake"] == pytest.approx(100.0, abs=0.01)
    # the January opening entry is outside the window
    assert t["starting_capital"] == 0


def test_fresh_mode_is_still_only_a_view(seeded):
    before = capital.capital_summary()["totals"]
    capital.capital_summary(since="2027-01-01T00:00:00+00:00", mode="fresh")
    assert capital.capital_summary()["totals"] == before


def test_unfiltered_fresh_mode_is_identical_to_all_time(seeded):
    """With no window, the mode must make no difference at all."""
    assert (capital.capital_summary(mode="fresh")["totals"]
            == capital.capital_summary(mode="pnl")["totals"])
