"""
half_result_vs_ft — a half's 1X2 against what the full-time 1X2 implies for it.

The full-time market fixes the goal rates; the halves are a fixed share of
them. So a half priced far from the full-time-implied figure is the book
contradicting its own headline market. Found on the live board — see the
check's comment in consistency.py for the case that prompted it.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import consistency  # noqa: E402
from src.consistency import find_consistency_flags  # noqa: E402
from src.models import Odds  # noqa: E402
from src.scrapers.sports import soccer  # noqa: E402

T = datetime(2026, 9, 11, 17, 30, tzinfo=timezone.utc)


def _row(mt, per, sel, home="Belarus U19", away="Gomel Region"):
    return Odds(source="crystalbet", sport="soccer", home=home, away=away,
                market_type=mt, period=per, selections=sel, fetched_at=T,
                start_time=T, league="Belarus, Friendly", raw_event_id="e1")


def _flags(odds, enabled=True):
    """The check is OFF by default (owner, 2026-09-14). The logic tests run it
    with the gate open so the calibrated behaviour stays pinned for the day it
    is switched back on."""
    prev = consistency.HALF_RESULT_ENABLED
    consistency.HALF_RESULT_ENABLED = enabled
    try:
        return [f for f in find_consistency_flags(odds) if f.kind == "half_result_vs_ft"]
    finally:
        consistency.HALF_RESULT_ENABLED = prev


FT = {"home": 1.10, "draw": 6.90, "away": 14.6}
H1 = {"home": 1.45, "draw": 2.85, "away": 11.8}      # matches the model to 0.0pp
H2 = {"home": 1.90, "draw": 3.20, "away": 3.50}      # the screenshot: 25% away in H2


class TestGate:
    def test_on_by_default(self):
        assert consistency.HALF_RESULT_ENABLED is True

    def test_gate_silences_it(self):
        odds = [_row("moneyline", "FT", FT), _row("moneyline", "H2", H2)]
        assert _flags(odds, enabled=False) == []


class TestTheBarIsHigh:
    """15pp: a tripwire for the absurd, not a precision instrument. Ordinary
    pricing never got past 12.6pp in three months of history."""

    def test_the_owners_example_fires(self):
        """'ml has 1.50 on one and ht ml has 3.50 on one' — 18.8pp."""
        f = _flags([_row("moneyline", "FT", {"home": 1.50, "draw": 4.20, "away": 6.50}),
                    _row("moneyline", "H1", {"home": 3.50, "draw": 2.30, "away": 3.60})])
        assert len(f) == 1 and f[0].outcome == "1" and f[0].severity > 15

    def test_a_ten_point_gap_does_not(self):
        """Real, and the sort of thing the 6pp bar used to name — a lopsided
        match whose half draw sits ~10pp under the model. Below the bar."""
        f = _flags([_row("moneyline", "FT", {"home": 1.12, "draw": 7.50, "away": 15.0}),
                    _row("moneyline", "H1", {"home": 1.42, "draw": 4.60, "away": 12.0})])
        assert f == []


class TestTheScreenshot:
    def test_second_half_fires_and_first_half_does_not(self):
        f = _flags([_row("moneyline", "FT", FT), _row("moneyline", "H1", H1),
                    _row("moneyline", "H2", H2)])
        assert [x.periods for x in f] == ["H2 vs FT"]

    def test_names_the_generous_leg_with_its_price(self):
        """The value is home in the second half — the leg posted furthest
        BELOW the model, not the away leg your eye lands on."""
        f = _flags([_row("moneyline", "FT", FT), _row("moneyline", "H2", H2)])[0]
        assert f.outcome == "1" and f.odds == pytest.approx(1.90)
        assert f.severity > 15 and "EV" in f.detail

    def test_a_half_consistent_with_full_time_is_silent(self):
        assert _flags([_row("moneyline", "FT", FT), _row("moneyline", "H1", H1)]) == []


class TestGuards:
    def test_no_full_time_market_means_no_check(self):
        assert _flags([_row("moneyline", "H2", H2)]) == []

    def test_two_way_full_time_is_not_enough(self):
        """Needs the 3-way 1X2 — the draw is part of the fit."""
        assert _flags([_row("moneyline", "FT", {"home": 1.15, "away": 6.0}),
                       _row("moneyline", "H2", H2)]) == []

    def test_corners_submarket_is_ignored(self):
        odds = [_row("moneyline", "FT", FT), _row("moneyline", "H2", H2)]
        for o in odds:
            o.submarket = "corners"
        assert _flags(odds) == []

    def test_other_sports_are_untouched(self):
        odds = [_row("moneyline", "FT", FT), _row("moneyline", "H2", H2)]
        for o in odds:
            o.sport = "basketball"
        assert _flags(odds) == []


class TestClassifier:
    """The second half is PERMISSIVE-only: the strict path (the Pinnacle-matched
    +EV pipeline) must keep skipping it, or unmatched rows leak into edges."""

    @pytest.mark.parametrize("title,mt,per", [
        ("2nd Half Result", "moneyline", "H2"),
        ("Handicap 2nd Period", "spread", "H2"),
        ("Under/Over 2nd Period", "total", "H2"),
        ("Under/Over 1st Period", "total", "H1"),
    ])
    def test_permissive_classifies_h2(self, title, mt, per):
        c = soccer.classify_market_title_permissive(title)
        assert c is not None and (c.market_type, c.period) == (mt, per)

    @pytest.mark.parametrize("title", ["2nd Half Result", "Handicap 2nd Period",
                                       "Under/Over 2nd Period"])
    def test_strict_still_skips_h2(self, title):
        assert soccer.classify_market_title(title) is None


class TestBiasCorrection:
    """soccer_model.HALF_BIAS — the learned correction on top of the fixed-split
    Poisson halves. Raw Poisson overstates the half draw and understates the
    favourite on every book measured (see the table in soccer_model)."""

    def test_corrected_halves_are_proper_distributions(self):
        from src.soccer_model import fit_lambdas, half_result_probs
        ft = (0.45, 0.27, 0.28)                       # an ordinary, close match
        lh, la, _ = fit_lambdas(*ft)
        h1, h2 = half_result_probs(lh, la, ft)
        assert sum(h1) == pytest.approx(1.0, abs=1e-9)
        assert sum(h2) == pytest.approx(1.0, abs=1e-9)
        assert all(0 < p < 1 for p in h1 + h2)

    def test_correction_pulls_the_half_draw_down_in_the_mid_range(self):
        """The measured bias: raw Poisson puts ~1.3pp too much on the H1 draw
        and ~1.8pp on the H2 draw for a typical FT draw price (~0.25-0.30).
        The correction must move in that direction, or it is not the curve
        that was fitted."""
        from src.soccer_model import (fit_lambdas, half_matrices, result_probs,
                                      half_result_probs)
        ft = (0.45, 0.27, 0.28)
        lh, la, _ = fit_lambdas(*ft)
        m1, m2 = half_matrices(lh, la)
        raw1, raw2 = result_probs(m1), result_probs(m2)
        c1, c2 = half_result_probs(lh, la, ft)
        assert c1[1] < raw1[1] and c2[1] < raw2[1]          # draws come down
        assert c1[0] > raw1[0]                               # H1 favourite goes up

    def test_correction_is_bounded(self):
        """Every learned correction is under 3pp in magnitude. A table entry
        outside that is a fitting or transcription error, not a finding."""
        from src.soccer_model import HALF_BIAS
        for key, (cx, cy) in HALF_BIAS.items():
            assert len(cx) == len(cy) == 20, key
            assert cx == sorted(cx), key
            assert all(abs(y) < 0.03 for y in cy), key
