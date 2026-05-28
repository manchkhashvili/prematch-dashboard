"""
Tests for prematch.src.vig — odds conversion + vig removal.

Run from the prematch/ directory:
    python -m pytest tests/ -v
"""
from __future__ import annotations

import math

import pytest

# Test imports as `src.vig` so the tests are runnable from the prematch/ root.
# Adding the prematch root to sys.path here keeps the tests independent of
# whether the caller set PYTHONPATH.
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.vig import (  # noqa: E402
    american_to_decimal,
    decimal_to_implied_prob,
    devig_2way,
    devig_2way_proportional,
    devig_2way_shin,
    devig_3way,
    devig_3way_proportional,
    devig_3way_shin,
    fair_decimal,
    vig_pct,
)


# ── american_to_decimal ────────────────────────────────────────────────────────
class TestAmericanToDecimal:
    def test_pickem_negative(self):
        # -100 is pick'em on the favorite side
        assert american_to_decimal(-100) == pytest.approx(2.0)

    def test_pickem_positive(self):
        # +100 is pick'em on the underdog side
        assert american_to_decimal(+100) == pytest.approx(2.0)

    def test_standard_favorite(self):
        # -110 is the classic "vig'd 50/50" line
        assert american_to_decimal(-110) == pytest.approx(1.9090909, abs=1e-6)

    def test_heavy_favorite(self):
        # -200 ⇒ 1.5
        assert american_to_decimal(-200) == pytest.approx(1.5)

    def test_standard_underdog(self):
        # +150 ⇒ 2.5
        assert american_to_decimal(+150) == pytest.approx(2.5)

    def test_heavy_underdog(self):
        # +400 ⇒ 5.0
        assert american_to_decimal(+400) == pytest.approx(5.0)

    def test_rejects_zero(self):
        with pytest.raises(ValueError):
            american_to_decimal(0)

    def test_round_trip_via_devig_balanced(self):
        # -110 / -110 (balanced book) should devig to almost exactly 0.5/0.5
        d = american_to_decimal(-110)
        p1, p2 = devig_2way(d, d)
        assert p1 == pytest.approx(0.5, abs=1e-12)
        assert p2 == pytest.approx(0.5, abs=1e-12)


# ── devig_2way ─────────────────────────────────────────────────────────────────
class TestDevig2Way:
    def test_balanced_market(self):
        p1, p2 = devig_2way(1.91, 1.91)
        assert p1 == pytest.approx(0.5)
        assert p2 == pytest.approx(0.5)

    def test_skewed_market_sums_to_one(self):
        # -200 favorite vs +160 dog (decimal 1.5 / 2.6)
        p1, p2 = devig_2way(1.5, 2.6)
        assert p1 + p2 == pytest.approx(1.0, abs=1e-12)

    def test_skewed_market_favorite_higher_prob(self):
        # The favorite (lower decimal odds) should have higher fair probability
        p_fav, p_dog = devig_2way(1.5, 2.6)
        assert p_fav > p_dog

    def test_extreme_favorite(self):
        # 1.05 / 11.0 — heavy favorite. Sanity: fair_fav > 0.9
        p_fav, p_dog = devig_2way(1.05, 11.0)
        assert p_fav > 0.9
        assert p_fav + p_dog == pytest.approx(1.0, abs=1e-12)

    def test_realistic_pinnacle_basketball(self):
        # Pinnacle NBA spread typically -106/-104. Decimal: 1.9434 / 1.9615.
        # Vig ~2.1%, fair should be close to 50/50 with slight tilt.
        d1 = american_to_decimal(-106)
        d2 = american_to_decimal(-104)
        p1, p2 = devig_2way(d1, d2)
        assert p1 + p2 == pytest.approx(1.0)
        # both within ~5pp of 50/50
        assert abs(p1 - 0.5) < 0.05

    def test_rejects_invalid_odds_lower(self):
        with pytest.raises(ValueError):
            devig_2way(1.0, 2.0)

    def test_rejects_invalid_odds_upper(self):
        with pytest.raises(ValueError):
            devig_2way(2.0, 0.5)

    def test_no_vig_when_odds_complement(self):
        # 2.0 / 2.0 means total = 1.0 already — no vig. Devig should be no-op.
        p1, p2 = devig_2way(2.0, 2.0)
        assert p1 == pytest.approx(0.5)
        assert p2 == pytest.approx(0.5)


# ── devig_3way ─────────────────────────────────────────────────────────────────
class TestDevig3Way:
    """
    Soccer 1X2 markets have a draw — three sides instead of two. Same
    proportional devig method as devig_2way. Not used by basketball today
    but added now so soccer's edge math just works when we add it.
    """

    def test_balanced_3way_sums_to_one(self):
        # Roughly balanced soccer market
        p1, p2, p3 = devig_3way(2.10, 3.40, 3.40)
        assert p1 + p2 + p3 == pytest.approx(1.0, abs=1e-12)

    def test_skewed_3way_favorite_has_highest_prob(self):
        # Heavy home favorite, away dog
        p_home, p_draw, p_away = devig_3way(1.50, 4.50, 6.00)
        assert p_home > p_draw
        assert p_home > p_away
        assert p_home + p_draw + p_away == pytest.approx(1.0, abs=1e-12)

    def test_no_vig_three_way_passthrough(self):
        # Three sides at exactly 3.0 — total implied is exactly 1.0, no vig
        p1, p2, p3 = devig_3way(3.0, 3.0, 3.0)
        assert p1 == pytest.approx(1/3)
        assert p2 == pytest.approx(1/3)
        assert p3 == pytest.approx(1/3)

    def test_rejects_invalid_odds_any_side(self):
        # Any side ≤ 1.0 → reject
        with pytest.raises(ValueError):
            devig_3way(1.0, 3.0, 3.0)
        with pytest.raises(ValueError):
            devig_3way(3.0, 0.5, 3.0)
        with pytest.raises(ValueError):
            devig_3way(3.0, 3.0, 1.0)

    def test_realistic_soccer_market_devig_sane(self):
        # Pinnacle EPL-typical 1X2: 2.05 / 3.60 / 3.80. Overround ~2.9%
        # (Pinnacle is the sharpest book; their soccer vig is low).
        # Just verify that devig produces a clean probability distribution.
        d1, d2, d3 = 2.05, 3.60, 3.80
        raw_sum = 1/d1 + 1/d2 + 1/d3
        assert 1.01 < raw_sum < 1.10   # 1-10% overround band
        p1, p2, p3 = devig_3way(d1, d2, d3)
        assert p1 + p2 + p3 == pytest.approx(1.0, abs=1e-12)
        # Home is favorite → highest probability.
        assert p1 > p2
        assert p1 > p3


# ── helpers ────────────────────────────────────────────────────────────────────
class TestHelpers:
    def test_decimal_to_implied_prob(self):
        assert decimal_to_implied_prob(2.0) == pytest.approx(0.5)
        assert decimal_to_implied_prob(4.0) == pytest.approx(0.25)

    def test_decimal_to_implied_prob_rejects_low(self):
        with pytest.raises(ValueError):
            decimal_to_implied_prob(1.0)

    def test_fair_decimal_round_trips(self):
        # fair_decimal(1/d) ≈ d
        for d in (1.5, 1.91, 2.5, 5.0):
            assert fair_decimal(1 / d) == pytest.approx(d)

    def test_fair_decimal_rejects_boundary(self):
        with pytest.raises(ValueError):
            fair_decimal(0.0)
        with pytest.raises(ValueError):
            fair_decimal(1.0)

    def test_vig_pct_balanced(self):
        # 1.91 / 1.91 ⇒ overround ≈ 4.7%
        v = vig_pct(1.91, 1.91)
        assert 4.0 < v < 5.5

    def test_vig_pct_no_vig(self):
        # 2.0 / 2.0 is a fair line ⇒ ~0% vig
        assert vig_pct(2.0, 2.0) == pytest.approx(0.0, abs=1e-12)


# ── Shin's method specifically ───────────────────────────────────────────────
# Phase 3.7 (2026-05-27) — Shin replaced proportional as the default devig.
# These tests anchor Shin's specific behavior so a regression to the old
# proportional values would be caught immediately.
class TestShin2Way:
    def test_default_alias_is_shin(self):
        # devig_2way is currently aliased to devig_2way_shin. If that ever
        # flips to proportional, this test fails loudly.
        a = devig_2way(1.50, 2.75)
        b = devig_2way_shin(1.50, 2.75)
        assert a == b

    def test_balanced_market_matches_proportional(self):
        # On 1.91/1.91 (or any equal-probs input) Shin == proportional
        # to many decimals — there's no skew for Shin to correct.
        s = devig_2way_shin(1.91, 1.91)
        p = devig_2way_proportional(1.91, 1.91)
        assert s[0] == pytest.approx(p[0], abs=1e-9)
        assert s[1] == pytest.approx(p[1], abs=1e-9)
        assert s[0] + s[1] == pytest.approx(1.0, abs=1e-12)

    def test_sum_to_one_invariant_skewed(self):
        for d1, d2 in [(1.10, 8.00), (1.30, 4.50), (1.50, 2.75), (2.20, 1.75)]:
            p1, p2 = devig_2way_shin(d1, d2)
            assert p1 + p2 == pytest.approx(1.0, abs=1e-9), f"({d1},{d2})"

    def test_users_canonical_skewed_example(self):
        # 1.023 / 7.31 — the example that prompted Phase 3.7. The user's
        # screenshot showed the OLD (proportional) values 1.140 / 8.146.
        # Shin should produce fair odds approximately 1.087 / 12.56.
        p_fav, p_dog = devig_2way_shin(1.023, 7.31)
        assert 1 / p_fav == pytest.approx(1.087, abs=0.005)
        assert 1 / p_dog == pytest.approx(12.56, abs=0.1)
        # Confirm direction vs proportional: Shin pushes the favorite UP
        # (higher implied prob, lower fair odds) and the dog DOWN (lower
        # implied prob, higher fair odds).
        pp_fav, pp_dog = devig_2way_proportional(1.023, 7.31)
        assert p_fav > pp_fav      # Shin favorite higher prob
        assert p_dog < pp_dog      # Shin dog lower prob
        assert 1 / p_fav < 1 / pp_fav   # Shin favorite SHORTER fair odds
        assert 1 / p_dog > 1 / pp_dog   # Shin dog LONGER fair odds

    def test_no_vig_input_passthrough(self):
        # 2.0 / 2.0 — overround is exactly 1.0. Solver early-returns proportional
        # which is also 0.5/0.5 here.
        p1, p2 = devig_2way_shin(2.0, 2.0)
        assert p1 == pytest.approx(0.5, abs=1e-9)
        assert p2 == pytest.approx(0.5, abs=1e-9)

    def test_rejects_invalid_odds(self):
        with pytest.raises(ValueError):
            devig_2way_shin(1.0, 2.0)
        with pytest.raises(ValueError):
            devig_2way_shin(2.0, 0.5)


class TestShin3Way:
    def test_default_alias_is_shin(self):
        a = devig_3way(2.10, 3.40, 3.40)
        b = devig_3way_shin(2.10, 3.40, 3.40)
        assert a == b

    def test_balanced_3way_matches_proportional(self):
        # Equal-prob sides: 3.0/3.0/3.0 (exactly fair) → Shin = proportional = 1/3 each.
        s = devig_3way_shin(3.0, 3.0, 3.0)
        for p in s:
            assert p == pytest.approx(1 / 3, abs=1e-9)

    def test_sum_to_one_invariant_skewed(self):
        for d1, d2, d3 in [(1.50, 4.50, 6.00), (2.10, 3.40, 3.40), (1.40, 5.00, 9.00)]:
            p1, p2, p3 = devig_3way_shin(d1, d2, d3)
            assert p1 + p2 + p3 == pytest.approx(1.0, abs=1e-9), f"({d1},{d2},{d3})"

    def test_skewed_3way_direction_vs_proportional(self):
        # Heavy home favorite: Shin should push the home prob UP and the dog DOWN.
        d1, d2, d3 = 1.40, 5.00, 9.00
        s_h, s_d, s_a = devig_3way_shin(d1, d2, d3)
        p_h, p_d, p_a = devig_3way_proportional(d1, d2, d3)
        assert s_h >= p_h   # favorite up
        assert s_a <= p_a   # heavy underdog down


class TestShinNumericGuardrails:
    """Confirm Shin's fallback behaviour on edge inputs doesn't crash."""

    def test_very_high_vig_still_returns_valid_probs(self):
        # 1.05 / 5.00 with absurd 25%+ vig (synthetic CB-like). Shin should
        # converge or fall back without raising.
        p1, p2 = devig_2way_shin(1.05, 5.00)
        assert 0 < p1 < 1
        assert 0 < p2 < 1
        assert p1 + p2 == pytest.approx(1.0, abs=1e-6)

    def test_low_vig_close_to_proportional(self):
        # Pinnacle-realistic 2% vig: 1.94/1.95. Shin and proportional should
        # agree to about 3 decimal places — proves we're not "fixing" a market
        # that doesn't need fixing.
        s1, s2 = devig_2way_shin(1.94, 1.95)
        p1, p2 = devig_2way_proportional(1.94, 1.95)
        assert s1 == pytest.approx(p1, abs=5e-4)
        assert s2 == pytest.approx(p2, abs=5e-4)
