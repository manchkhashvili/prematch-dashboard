"""Regression: an empty fetch must not blank the arb/+EV list.

The bug (2026-08-03): every poller wrote `_state[sport][book]["odds"] = odds`
unconditionally, so a fetch that returned an EMPTY LIST — rather than raising —
wiped the last good snapshot. Measured on dashboard.log, Pinnacle returned 0 rows
on 19 of 984 cycles (~2%). Pinnacle is the REFERENCE and
`_compute_opportunities_now` does `if not pin_odds: continue`, so a single empty
Pinnacle cycle blanked the whole opportunity list for that sport. One poll later
it came back, every row looked new to alerts.js, and the alarm re-fired on
opportunities the user had already seen.

`_store_odds` now holds the last good snapshot through a transient empty, for at
most EMPTY_HOLD_SEC (so a genuinely empty board is still eventually honoured).
"""
from datetime import datetime, timedelta, timezone

import pytest

from src import app

SPORT = next(iter(app.SPORT_NAMES))
NOW = datetime.now(tz=timezone.utc)
GOOD = ["row"] * 120


@pytest.fixture(autouse=True)
def _clean_slot():
    prev = app._state[SPORT]["pin"]
    app._state[SPORT]["pin"] = app._empty_source_state()
    yield
    app._state[SPORT]["pin"] = prev


def test_transient_empty_does_not_wipe_last_good():
    app._store_odds(SPORT, "pin", GOOD, now=NOW)
    accepted = app._store_odds(SPORT, "pin", [], now=NOW + timedelta(seconds=60))
    assert accepted is False
    assert app._state[SPORT]["pin"]["count"] == 120
    assert app._state[SPORT]["pin"]["odds"] == GOOD


def test_recovery_clears_the_empty_marker():
    app._store_odds(SPORT, "pin", GOOD, now=NOW)
    app._store_odds(SPORT, "pin", [], now=NOW + timedelta(seconds=60))
    app._store_odds(SPORT, "pin", GOOD, now=NOW + timedelta(seconds=120))
    assert app._state[SPORT]["pin"]["count"] == 120
    assert app._state[SPORT]["pin"]["empty_since"] is None


def test_sustained_empty_is_eventually_accepted():
    """A board that is genuinely empty (overnight) must not be masked forever."""
    app._store_odds(SPORT, "pin", GOOD, now=NOW)
    app._store_odds(SPORT, "pin", [], now=NOW + timedelta(seconds=60))
    late = NOW + timedelta(seconds=app.EMPTY_HOLD_SEC + 120)
    assert app._store_odds(SPORT, "pin", [], now=late) is True
    assert app._state[SPORT]["pin"]["count"] == 0


def test_cold_start_empty_is_accepted():
    """Nothing to protect when there is no previous snapshot."""
    assert app._store_odds(SPORT, "pin", [], now=NOW) is True
    assert app._state[SPORT]["pin"]["count"] == 0


def test_non_empty_always_accepted():
    app._store_odds(SPORT, "pin", GOOD, now=NOW)
    smaller = ["row"] * 3
    assert app._store_odds(SPORT, "pin", smaller, now=NOW + timedelta(seconds=60)) is True
    assert app._state[SPORT]["pin"]["count"] == 3
