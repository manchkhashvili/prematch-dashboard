"""Date filtering on bets — the "start fresh" window.

Filters on `placed_at` (when the bet was logged), which is the axis the bets
page's date control uses: picking a start date shows only what you have taken
since then, so a fresh window reads zero everywhere.

Comparison is lexicographic, which is correct for ISO-8601. A bare `until` date
is extended to end-of-day so "to 2026-08-01" includes that whole day rather than
cutting at midnight — the off-by-one that would silently hide a day's bets.
"""
from __future__ import annotations

import pytest

from src import bets


@pytest.fixture
def db(tmp_path):
    bets._reset_for_tests()
    bets.init_db(tmp_path / "test_bets.db")
    yield
    bets._reset_for_tests()


def _add(placed_at, label):
    return bets.create_bet(
        placed_at=placed_at, sport="soccer", match_label=label,
        period="FT", market_type="moneyline", side="home",
        line=None, odds_taken=2.0, stake=10.0, book="cb", bankroll_at_time=1000.0,
    )


def _labels(rows):
    return sorted(r["match_label"] for r in rows)


@pytest.fixture
def seeded(db):
    _add("2026-07-01T12:00:00+00:00", "july")
    _add("2026-08-01T00:30:00+00:00", "aug-early")
    _add("2026-08-01T23:45:00+00:00", "aug-late")
    _add("2026-08-15T12:00:00+00:00", "aug-mid")


def test_no_filter_returns_everything(seeded):
    assert len(bets.list_bets()) == 4


def test_since_is_inclusive_from_start_of_day(seeded):
    got = bets.list_bets(since="2026-08-01")
    assert _labels(got) == ["aug-early", "aug-late", "aug-mid"]


def test_until_bare_date_includes_the_whole_day(seeded):
    """The off-by-one that would drop a day's bets: "until 2026-08-01" must
    include a bet placed at 23:45 that day, not cut at midnight."""
    got = bets.list_bets(until="2026-08-01")
    assert _labels(got) == ["aug-early", "aug-late", "july"]


def test_since_and_until_together(seeded):
    got = bets.list_bets(since="2026-08-01", until="2026-08-01")
    assert _labels(got) == ["aug-early", "aug-late"]


def test_future_window_is_empty_start_fresh(seeded):
    """The headline behaviour: filter to a period you haven't bet in yet and
    everything reads zero."""
    assert bets.list_bets(since="2027-01-01") == []


def test_range_composes_with_status(seeded):
    bid = _add("2026-08-20T12:00:00+00:00", "settled-one")
    bets.settle_bet(bid, "won")
    assert _labels(bets.list_bets(status="settled", since="2026-08-16")) == ["settled-one"]
    # same status, window that excludes it
    assert bets.list_bets(status="settled", since="2026-09-01") == []
    # open bets respect the window too
    assert _labels(bets.list_bets(status="open", since="2026-08-01")) == [
        "aug-early", "aug-late", "aug-mid"]


def test_history_is_not_destroyed_by_filtering(seeded):
    """Filtering is a view, not a delete — clearing it brings everything back."""
    assert len(bets.list_bets(since="2027-01-01")) == 0
    assert len(bets.list_bets()) == 4
