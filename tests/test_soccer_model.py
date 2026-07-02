"""Tests for src.soccer_model — devig, lambda fit, and the fair-price sheet.

The Nepean-Mounties fixture was validated by hand; the property tests assert the
internal algebra the family-E identity checks rely on (partition identities,
handicap equivalences, monotone ladders) all hold exactly on model output.
"""
import numpy as np
import pytest

from src import soccer_model as sm


# ── Nepean-Mounties fixture (hand-validated) ──────────────────────────────────
NEPEAN_1X2 = [8.90, 6.15, 1.18]     # home / draw / away


def test_power_devig_matches_hand_numbers():
    p = sm.devig(NEPEAN_1X2, "power")
    assert p == pytest.approx([0.0708, 0.1108, 0.8184], abs=1e-3)
    assert sum(p) == pytest.approx(1.0, abs=1e-9)


def test_longshot_shrunk_more_than_proportional():
    prop = sm.devig(NEPEAN_1X2, "proportional")
    powr = sm.devig(NEPEAN_1X2, "power")
    shin = sm.devig(NEPEAN_1X2, "shin")
    # the longshot is the home side (index 0); power & shin both shrink it below
    # the flat proportional estimate (favourite-longshot correction).
    assert powr[0] < prop[0]
    assert shin[0] < prop[0]


def test_fit_lambdas_1x2_only():
    ph, pd, pa = sm.devig(NEPEAN_1X2, "power")
    lh, la, diag = sm.fit_lambdas(ph, pd, pa, dc_rho=0.0, n=12)
    assert lh == pytest.approx(0.921, abs=0.01)
    assert la == pytest.approx(3.239, abs=0.01)
    assert diag["total"] == pytest.approx(4.16, abs=0.02)
    assert diag["converged"]
    assert diag["residual"] < 1e-6


def test_nepean_htft_fair_prices():
    m = sm.model_from_market(NEPEAN_1X2, devig_method="power", split=0.45, dc_rho=0.0)
    assert m.odds("ht_1x2:2") == pytest.approx(1.575, abs=0.02)   # posted 1.55
    assert m.odds("htft:2/2") == pytest.approx(1.66, abs=0.02)    # posted 1.80
    assert m.odds("htft:X/2") == pytest.approx(5.58, abs=0.05)
    assert m.odds("htft:2/1") == pytest.approx(147.0, rel=0.05)
    # 2/2 posted @1.80 is the ~+8% edge the fixture flags
    assert 1.80 * m.prob("htft:2/2") - 1.0 == pytest.approx(0.082, abs=0.01)


# ── property tests (dc_rho=0 → pure Poisson → identities exact) ────────────────
@pytest.fixture
def model():
    return sm.build_model(0.921, 3.239, split=0.45, dc_rho=0.0, n=12)


def test_matrices_sum_to_one(model):
    assert model.ft.sum() == pytest.approx(1.0, abs=1e-9)
    assert model.h1.sum() == pytest.approx(1.0, abs=1e-9)
    assert model.h2.sum() == pytest.approx(1.0, abs=1e-9)
    assert model.htft.sum() == pytest.approx(1.0, abs=1e-9)


def test_htft_columns_equal_full_from_halves(model):
    col = model.htft.sum(axis=0)                       # sum over HT → FT marginal
    assert np.allclose(col, sm.result_probs(model.full_from_halves), atol=1e-9)


def test_within_matrix_partitions_exact(model):
    p = model.prob
    assert p("dc:1X") == pytest.approx(p("ft_1x2:1") + p("ft_1x2:X"), abs=1e-12)
    assert p("multigoal:0-1") == pytest.approx(p("exact:0") + p("exact:1"), abs=1e-12)
    assert p("btts:yes") + p("btts:no") == pytest.approx(1.0, abs=1e-12)
    assert p("ft_total:over_2.5") + p("ft_total:under_2.5") == pytest.approx(1.0, abs=1e-12)


def test_handicap_equivalences_exact(model):
    p = model.prob
    assert p("ah:home_0") == pytest.approx(p("dnb:1"), abs=1e-12)          # AH0 == DNB
    assert p("ah:home_-0.5") == pytest.approx(p("ft_1x2:1"), abs=1e-12)    # AH-0.5 == 1X2 home
    assert p("ah:home_0.5") == pytest.approx(p("dc:1X"), abs=1e-12)        # AH+0.5 == DC 1X
    quarter = p("ah:home_-0.25")
    assert quarter == pytest.approx(0.5 * (p("ah:home_0") + p("ah:home_-0.5")), abs=1e-12)


def test_ah_ladder_monotone(model):
    lines = np.arange(-4.0, 4.0 + 0.1, 0.5)
    probs = [model.prob(f"ah:home_{h:g}") for h in lines]
    assert all(probs[i] <= probs[i + 1] + 1e-12 for i in range(len(probs) - 1))


@pytest.mark.parametrize("method", ["proportional", "power", "shin"])
def test_devig_sums_to_one(method):
    assert sum(sm.devig([2.10, 3.40, 3.50], method)) == pytest.approx(1.0, abs=1e-9)
    assert sum(sm.devig([1.85, 1.95], method)) == pytest.approx(1.0, abs=1e-9)


def test_auto_devig_picks_shin_then_power():
    assert sm.devig([2.1, 3.4, 3.5], "auto") == sm.devig([2.1, 3.4, 3.5], "shin")
    assert sm.devig([1.85, 1.95], "auto") == sm.devig([1.85, 1.95], "power")
