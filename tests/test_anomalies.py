"""Tests for src/anomalies.py — alt-line ladder monotonicity-violation detector."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.anomalies import find_ladder_anomalies
from src.models import Odds

FETCHED = datetime(2026, 5, 31, tzinfo=timezone.utc)


def _spread(line: float, home: float, away: float, *, event="E1") -> Odds:
    return Odds(
        source="crystalbet", sport="basketball", home="Home", away="Away",
        market_type="spread", period="FT", selections={"home": home, "away": away},
        fetched_at=FETCHED, line=line, league="Test League", raw_event_id=event,
    )


def _total(line: float, over: float, under: float, *, event="E1") -> Odds:
    return Odds(
        source="crystalbet", sport="basketball", home="Home", away="Away",
        market_type="total", period="FT", selections={"over": over, "under": under},
        fetched_at=FETCHED, line=line, league="Test League", raw_event_id=event,
    )


# ── Clean ladders produce nothing ─────────────────────────────────────────────

def test_clean_spread_ladder_has_no_anomalies():
    # home odds fall as line rises; away odds rise. Textbook.
    ladder = [
        _spread(-2.0, home=2.25, away=1.55),
        _spread(-1.0, home=2.10, away=1.60),
        _spread(+1.0, home=2.00, away=1.65),
        _spread(+2.0, home=1.90, away=1.75),
    ]
    assert find_ladder_anomalies(ladder) == []


def test_clean_total_ladder_has_no_anomalies():
    # over odds rise as the total rises; under odds fall.
    ladder = [
        _total(210.0, over=1.35, under=2.70),
        _total(211.0, over=1.40, under=2.55),
        _total(212.0, over=1.45, under=2.40),
    ]
    assert find_ladder_anomalies(ladder) == []


# ── The canonical violations ──────────────────────────────────────────────────

def test_home_side_violation_detected():
    # user's example: +10.0 @ 1.50 then +11.0 @ 1.60 — home odds went UP.
    ladder = [_spread(10.0, home=1.50, away=3.10),
              _spread(11.0, home=1.60, away=3.00)]
    anoms = find_ladder_anomalies(ladder)
    home = [a for a in anoms if a.side == "home"]
    assert len(home) == 1
    a = home[0]
    assert (a.line_lo, a.line_hi) == (10.0, 11.0)
    assert (a.odds_lo, a.odds_hi) == (1.50, 1.60)
    assert a.expected == "down"
    assert round(a.pct, 1) == pytest.approx(6.7, abs=0.1)


def test_away_side_violation_detected():
    # away odds must rise with the line; here they fall 1.60 -> 1.55.
    ladder = [_spread(1.0, home=2.00, away=1.60),
              _spread(2.0, home=1.90, away=1.55)]
    away = [a for a in find_ladder_anomalies(ladder) if a.side == "away"]
    assert len(away) == 1
    assert away[0].expected == "up"
    assert (away[0].odds_lo, away[0].odds_hi) == (1.60, 1.55)


def test_total_over_violation_detected():
    # over odds must rise with the total; here they fall.
    ladder = [_total(210.0, over=1.50, under=2.40),
              _total(211.0, over=1.45, under=2.50)]
    sides = {a.side for a in find_ladder_anomalies(ladder)}
    assert "over" in sides    # over fell (wrong way)
    assert "under" in sides   # under rose (wrong way) — mirror violation


# ── Structural details ────────────────────────────────────────────────────────

def test_gap_in_ladder_compares_adjacent_present_rungs():
    # +5.5 is missing; we compare +5.0 to +6.0 (the next present rung) and
    # must NOT invent a violation from the gap.
    ladder = [_spread(5.0, home=1.60, away=2.10),
              _spread(6.0, home=1.50, away=2.30)]   # both move correctly
    assert find_ladder_anomalies(ladder) == []


def test_separate_events_do_not_cross_compare():
    # A low home price in event E2 must not be compared against E1's ladder.
    ladder = [_spread(1.0, home=2.00, away=1.65, event="E1"),
              _spread(2.0, home=1.40, away=1.80, event="E2")]
    assert find_ladder_anomalies(ladder) == []


def test_min_pct_threshold_filters_small_wiggles():
    ladder = [_spread(1.0, home=1.80, away=1.90),
              _spread(2.0, home=1.82, away=1.85)]  # home up ~1.1%, away down ~2.7%
    assert find_ladder_anomalies(ladder, min_pct=0.0)            # both surface
    big = find_ladder_anomalies(ladder, min_pct=5.0)
    assert big == []                                             # neither >= 5%


def test_spread_only_skips_totals():
    ladder = [
        _spread(1.0, home=2.00, away=1.65),
        _spread(2.0, home=2.10, away=1.60),       # home violation
        _total(210.0, over=1.50, under=2.40),
        _total(211.0, over=1.45, under=2.50),     # total violation
    ]
    only_spread = find_ladder_anomalies(ladder, markets=("spread",))
    assert {a.market_type for a in only_spread} == {"spread"}


def test_results_sorted_by_pct_desc():
    ladder = [
        _spread(1.0, home=1.80, away=1.90),
        _spread(2.0, home=1.85, away=1.88),   # small home violation
        _spread(3.0, home=2.40, away=1.80),   # large home violation
    ]
    anoms = [a for a in find_ladder_anomalies(ladder) if a.side == "home"]
    pcts = [a.pct for a in anoms]
    assert pcts == sorted(pcts, reverse=True)


# ── Reconstruction of the user's screenshot ladder ────────────────────────────

def test_screenshot_ladder_flags_the_highlighted_pair():
    # Home (1) column and away (2) column from the screenshot, normalised to
    # the home_line convention. The two yellow cells were home +5.0 @ 2.00 and
    # away -4.5 (i.e. home_line +4.5) @ 2.00.
    rows = [
        _spread(1.0, home=1.80, away=1.75),
        _spread(1.5, home=1.90, away=1.70),
        _spread(2.0, home=1.70, away=1.85),
        _spread(2.5, home=1.80, away=1.80),
        _spread(3.0, home=1.60, away=2.05),
        _spread(3.5, home=1.65, away=1.90),
        _spread(4.0, home=1.50, away=2.20),
        _spread(4.5, home=1.60, away=2.00),
        _spread(5.0, home=2.00, away=1.60),
        _spread(6.0, home=1.80, away=1.75),
        _spread(7.0, home=1.75, away=1.80),
        _spread(8.0, home=1.60, away=2.00),
        _spread(9.0, home=1.55, away=2.10),
    ]
    anoms = find_ladder_anomalies(rows)

    # The biggest violation should be the home +4.5 -> +5.0 spike (1.60 -> 2.00).
    top = anoms[0]
    assert top.side == "home"
    assert (top.line_lo, top.line_hi) == (4.5, 5.0)
    assert (top.odds_lo, top.odds_hi) == (1.60, 2.00)

    # The away column must also flag +4.0 -> +4.5 (2.20 -> 2.00, fell wrongly).
    away_pair = [a for a in anoms if a.side == "away"
                 and (a.line_lo, a.line_hi) == (4.0, 4.5)]
    assert len(away_pair) == 1
    assert (away_pair[0].odds_lo, away_pair[0].odds_hi) == (2.20, 2.00)
