"""match_events prunes pairs that can never be accepted — without changing output.

Matching was 64 % of a full cycle's CPU, and on live data 96.8 % of its fuzzy
scoring was provably wasted: 895 x 532 = 476 140 (book, pinnacle) pairs scored,
only 15 205 within the +/-1 h window `_accept` requires. `match_events` now
skips the rest before scoring; `match_with_diagnostics` still scores everything
because the curation log reports the best candidate regardless of threshold.

Measured on live soccer boards: identical matched sets, ~81 % less CPU.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.matcher import (
    TIME_LOOSE_SECONDS,
    _time_compatible,
    match_events,
    match_with_diagnostics,
)
from src.models import Odds

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _ml(source, home, away, start, h=1.9, a=1.95):
    return Odds(source=source, sport="soccer", home=home, away=away,
                market_type="moneyline", period="FT",
                selections={"home": h, "away": a},
                fetched_at=NOW, start_time=start)


def test_time_compatible_bounds():
    assert _time_compatible(NOW, NOW)
    assert _time_compatible(NOW, NOW + timedelta(seconds=TIME_LOOSE_SECONDS))
    assert not _time_compatible(NOW, NOW + timedelta(seconds=TIME_LOOSE_SECONDS + 1))
    # a missing kickoff must stay compatible — _accept falls back to name score
    assert _time_compatible(None, NOW)
    assert _time_compatible(NOW, None)


def test_prefilter_does_not_change_matches():
    cb = [_ml("crystalbet", "Alpha FC", "Beta FC", NOW),
          _ml("crystalbet", "Gamma FC", "Delta FC", NOW + timedelta(hours=6))]
    pin = [_ml("pinnacle", "Alpha FC", "Beta FC", NOW),
           _ml("pinnacle", "Gamma FC", "Delta FC", NOW + timedelta(hours=6))]
    fast = match_events(cb, pin)
    slow = match_with_diagnostics(cb, pin, diagnostics=True).matched
    assert len(fast) == 2
    assert {(m.home, m.away) for m in fast} == {(m.home, m.away) for m in slow}


def test_same_names_far_apart_are_not_matched_either_way():
    """The pruned pairs are ones `_accept` would have rejected anyway — this is
    why pruning is output-neutral rather than a coverage trade."""
    cb = [_ml("crystalbet", "Alpha FC", "Beta FC", NOW)]
    pin = [_ml("pinnacle", "Alpha FC", "Beta FC", NOW + timedelta(hours=5))]
    assert match_events(cb, pin) == []
    assert match_with_diagnostics(cb, pin, diagnostics=True).matched == []


def test_unknown_kickoff_still_matches_on_name_alone():
    cb = [_ml("crystalbet", "Alpha FC", "Beta FC", None)]
    pin = [_ml("pinnacle", "Alpha FC", "Beta FC", None)]
    assert len(match_events(cb, pin)) == 1


def test_diagnostics_still_report_a_far_apart_near_miss():
    """The curation log's whole job is surfacing name mismatches, so the
    diagnostics path must keep scoring pairs the matcher would never accept."""
    cb = [_ml("crystalbet", "Alpha FC", "Beta FC", NOW)]
    pin = [_ml("pinnacle", "Alpha FC", "Beta FC", NOW + timedelta(hours=5))]
    res = match_with_diagnostics(cb, pin, diagnostics=True)
    assert res.matched == []
    assert res.unmatched and res.unmatched[0].best_score > 90
