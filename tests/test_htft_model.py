"""
Tests for src/htft_model.py — bivariate-normal fair HT/FT pricing.

Validation requirements from the research notes (2026-06-12):
  - all outcome probs sum to 1.0
  - 50/50 anchor lands at ~2.76 (6-outcome) / ~2.83 (9-outcome)
  - multiplier monotonic: favorite 1/1 < 1.38 < dog 2/2
  - market-shape detection (6 vs 9 outcomes) from the priced labels
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.htft_model import (  # noqa: E402
    detect_nine_outcome,
    ft_win_prob,
    htft_fair_probs,
    mu_from_moneyline,
    phi2,
    sigma_for_league,
)

R = 1 / math.sqrt(2)


class TestPhi2:
    def test_closed_form_at_origin(self):
        # Phi2(0,0,rho) = 1/4 + arcsin(rho)/(2*pi)
        for rho in (0.0, 0.3, 0.65, R):
            want = 0.25 + math.asin(rho) / (2 * math.pi)
            assert abs(phi2(0, 0, rho) - want) < 1e-7

    def test_infinite_limits(self):
        assert phi2(math.inf, math.inf, 0.5) == 1.0
        assert phi2(-math.inf, 1.0, 0.5) == 0.0
        assert abs(phi2(math.inf, 0.0, 0.5) - 0.5) < 1e-12


class TestFairProbs:
    def test_probs_sum_to_one(self):
        for mu in (-9.5, 0.0, 4.0, 12.0):
            for nine in (True, False):
                p = htft_fair_probs(mu, sigma=11, rho=0.68, nine_outcome=nine)
                assert abs(sum(p.values()) - 1.0) < 1e-9
                assert len(p) == (9 if nine else 6)

    def test_5050_anchors(self):
        # sigma=11, rho=1/sqrt2 — the worked check in the notes.
        p9 = htft_fair_probs(0.0, sigma=11, rho=R, nine_outcome=True)
        p6 = htft_fair_probs(0.0, sigma=11, rho=R, nine_outcome=False)
        assert abs(1 / p9["1/1"] - 2.83) < 0.02
        assert abs(1 / p6["1/1"] - 2.76) < 0.02
        assert abs(1 / p9["1/2"] - 9.59) < 0.10
        assert abs(1 / p6["1/2"] - 8.87) < 0.10
        # symmetry at 50/50
        assert abs(p9["1/1"] - p9["2/2"]) < 1e-9
        assert abs(p9["X/1"] - p9["X/2"]) < 1e-9

    def test_multiplier_monotonic(self):
        # favorite 1/1 multiplier < 1.38 (50/50) < dog 2/2 multiplier
        p_even = htft_fair_probs(0.0, sigma=11, rho=R, nine_outcome=False)
        pf_even = ft_win_prob(0.0, 11, regulation=False)
        m_even = (1 / p_even["1/1"]) / (1 / pf_even)
        assert abs(m_even - 1.38) < 0.02

        p_fav = htft_fair_probs(8.0, sigma=11, rho=R, nine_outcome=False)
        pf = ft_win_prob(8.0, 11, regulation=False)
        m_fav = (1 / p_fav["1/1"]) / (1 / pf)
        m_dog = (1 / p_fav["2/2"]) / (1 / (1 - pf))
        assert m_fav < m_even < m_dog
        assert abs(m_fav - 1.27) < 0.03    # research: big favorite ~x1.27
        assert abs(m_dog - 1.49) < 0.03    # research: big dog ~x1.49

    def test_robustness_band(self):
        # sigma 10 -> ~2.78, sigma 12 -> ~2.76, rho .67 -> ~2.83 (6-outcome)
        assert abs(1 / htft_fair_probs(0, sigma=10, rho=R, nine_outcome=False)["1/1"] - 2.78) < 0.03
        assert abs(1 / htft_fair_probs(0, sigma=12, rho=R, nine_outcome=False)["1/1"] - 2.76) < 0.03
        assert abs(1 / htft_fair_probs(0, sigma=11, rho=0.67, nine_outcome=False)["1/1"] - 2.83) < 0.04

    def test_mu1_override_used(self):
        # a stronger first half than mu*0.5 must raise P(1/1)
        base = htft_fair_probs(4.0, sigma=11, rho=0.7)
        strong_h1 = htft_fair_probs(4.0, 3.5, sigma=11, rho=0.7)
        assert strong_h1["1/1"] > base["1/1"]


class TestHelpers:
    def test_detect_nine_outcome(self):
        nine = {"1/1": 1.8, "1/X": 26.0, "X/X": 100.0, "2/2": 3.3}
        six = {"1/1": 1.7, "1/2": 8.0, "X/1": 30.0, "X/2": 35.0, "2/1": 6.0, "2/2": 3.1}
        assert detect_nine_outcome(nine) is True
        assert detect_nine_outcome(six) is False

    def test_mu_from_moneyline_roundtrip(self):
        mu = mu_from_moneyline(0.70, 11.0)
        # P(M > 0) should recover ~0.70
        from statistics import NormalDist
        assert abs((1 - NormalDist(mu, 11).cdf(0)) - 0.70) < 1e-9
        assert mu_from_moneyline(1.5, 11.0) is None

    def test_sigma_for_league(self):
        assert sigma_for_league("USA, NBA. Playoffs") == 11.75
        assert sigma_for_league("NCAA Division I") == 10.3
        assert sigma_for_league("Lithuania, LKL") == 10.0
        assert sigma_for_league(None) == 10.0
