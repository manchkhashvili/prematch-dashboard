"""Tests for src.soccer_curves — Skellam/Poisson curve residuals + cross-fit."""
import pytest

from src import soccer_model as sm
from src import soccer_curves as sc


def _fair_ah_ladder(lh, la, lines):
    """Fair (vig-free) raw AH odds from a model, keyed by home line. Uses the
    fitter's default dc_rho so the round-trip is exact."""
    F = sm.score_matrix(lh, la, dc_rho=sm.DC_RHO)
    out = {}
    for l in lines:
        p = sm._ah_home_prob(F, l)
        if 0.0 < p < 1.0:
            out[l] = (1.0 / p, 1.0 / (1.0 - p))
    return out


def _fair_total_ladder(T, lines):
    out = {}
    for l in lines:
        p = sc._poisson_over(l, T)
        if 0.0 < p < 1.0:
            out[l] = (1.0 / p, 1.0 / (1.0 - p))
    return out


AH_LINES = [-2.0, -1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0]
TOTAL_LINES = [0.5, 1.5, 2.5, 3.5, 4.5]


def test_ah_fit_recovers_lambdas_zero_residual():
    ladder = _fair_ah_ladder(1.6, 1.0, AH_LINES)
    lh, la, resid = sc.fit_ah_ladder(sc._devig_rungs(ladder))
    assert lh == pytest.approx(1.6, abs=0.03)
    assert la == pytest.approx(1.0, abs=0.03)
    assert max(abs(v) for v in resid.values()) < 5e-3


def test_ah_ladder_no_false_flags_on_fair_curve():
    ladder = _fair_ah_ladder(1.6, 1.0, AH_LINES)
    assert sc.ah_curve_flags(ladder) == []


def test_ah_planted_off_curve_rung_flagged():
    ladder = _fair_ah_ladder(1.6, 1.0, AH_LINES)
    # make the -0.5 rung generous for home: shorten... lengthen home price a lot
    ladder[-0.5] = (ladder[-0.5][0] * 1.25, ladder[-0.5][1])
    flags = sc.ah_curve_flags(ladder)
    assert any(f.line == -0.5 for f in flags)


def test_total_curve_no_false_flags_and_planted_flag():
    ladder = _fair_total_ladder(2.7, TOTAL_LINES)
    assert sc.total_curve_flags(ladder) == []
    ladder[2.5] = (ladder[2.5][0] * 1.2, ladder[2.5][1])     # generous over 2.5
    flags = sc.total_curve_flags(ladder)
    assert any(f.line == 2.5 for f in flags)


def test_cross_fit_consistent_no_flag():
    # anchor S/T matches the ladders' S/T → no divergence.
    ah = _fair_ah_ladder(1.6, 1.0, AH_LINES)
    tot = _fair_total_ladder(2.6, TOTAL_LINES)
    assert sc.cross_fit_divergence(0.6, 2.6, ah, tot) is None


def test_cross_fit_divergence_flagged():
    # ladders say S≈0.6/T≈2.6 but the headline anchor claims S=1.4/T=3.2 → stale.
    ah = _fair_ah_ladder(1.6, 1.0, AH_LINES)
    tot = _fair_total_ladder(2.6, TOTAL_LINES)
    f = sc.cross_fit_divergence(1.4, 3.2, ah, tot, anchor_ts=1, ladder_ts=2)
    assert f is not None and f.kind == "cross_fit"
    assert "ladders moved last" in f.detail
