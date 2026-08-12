"""Half-time values derived from the full-time market (2026-08-12).

The owner dropped `ht_from_ft.py` into the repo root using scipy
(`brentq`, `least_squares`, `stats.poisson`). It now lives at
`src/ht_from_ft.py` with those three replaced by numpy-only equivalents, since
the rest of this stack has no scipy dependency and every one of them already
had a numpy implementation in `src/soccer_model.py`.

**SCIPY_REFERENCE below is the output of the ORIGINAL scipy module**, captured
before it was replaced (16-corner FAST sweep, six fixtures spanning heavy
favourite / balanced / away-favourite / high and low totals). It is the whole
point of this file: a port that silently drifts is worse than no port, because
every number it produces still looks plausible.

Measured at capture time: worst probability difference 2.9e-7, worst lambda
difference 2.0e-8.
"""
from __future__ import annotations

import math

import pytest

from src import ht_from_ft as H

# ── captured from the scipy original ────────────────────────────────────────
SCIPY_REFERENCE = [
    ((1.55, 3.6, 5.25), 2.5, 2.15, 1.55, {
        'lam': (1.653510924, 0.683812364),
        'ht_1': 0.425963847, 'ht_X': 0.433986055, 'ht_2': 0.140050098,
        'ht_over_0.5': 0.650689993, 'ht_over_1.5': 0.283287307,
        'htft_1/1': 0.370693393, 'htft_X/X': 0.164688803, 'htft_2/2': 0.073015010,
    }),
    ((1.28, 6.0, 6.25), 2.5, 1.28, 3.5, {
        'lam': (2.884787950, 1.055747321),
        'ht_1': 0.572590195, 'ht_X': 0.291787058, 'ht_2': 0.135622747,
        'ht_over_0.5': 0.830218145, 'ht_over_1.5': 0.529154020,
        'htft_1/1': 0.512048352, 'htft_X/X': 0.072040055, 'htft_2/2': 0.050867193,
    }),
    ((2.1, 3.4, 3.4), 2.5, 1.9, 1.9, {
        'lam': (1.522183912, 1.147753473),
        'ht_1': 0.344915219, 'ht_X': 0.416941926, 'ht_2': 0.238142855,
        'ht_over_0.5': 0.699248767, 'ht_over_1.5': 0.337904634,
        'htft_1/1': 0.263577684, 'htft_X/X': 0.153866330, 'htft_2/2': 0.150809570,
    }),
    ((1.1, 9.0, 26.0), 3.5, 1.85, 1.95, {
        'lam': (3.294025480, 0.573990737),
        'ht_1': 0.688936045, 'ht_X': 0.249284139, 'ht_2': 0.061779816,
        'ht_over_0.5': 0.824586160, 'ht_over_1.5': 0.519259550,
        'htft_1/1': 0.662012592, 'htft_X/X': 0.049277635, 'htft_2/2': 0.014702723,
    }),
    ((4.5, 3.8, 1.75), 2.0, 2.05, 1.8, {
        'lam': (0.903151200, 1.640674382),
        'ht_1': 0.183045029, 'ht_X': 0.421233670, 'ht_2': 0.395721301,
        'ht_over_0.5': 0.681687448, 'ht_over_1.5': 0.317308223,
        'htft_1/1': 0.103602301, 'htft_X/X': 0.156060178, 'htft_2/2': 0.326866523,
    }),
    ((3.0, 3.2, 2.45), 2.5, 1.95, 1.85, {
        'lam': (1.208566292, 1.403246536),
        'ht_1': 0.259900212, 'ht_X': 0.424201065, 'ht_2': 0.315898723,
        'ht_over_0.5': 0.691278522, 'ht_over_1.5': 0.328433299,
        'htft_1/1': 0.173628117, 'htft_X/X': 0.159625849, 'htft_2/2': 0.232762220,
    }),
]

# Generous next to the 2.9e-7 actually measured, but tight enough that a real
# change to the optimiser or the devig would fail rather than slide through.
PROB_TOL = 1e-5
LAM_TOL = 1e-4


@pytest.mark.parametrize("ft,line,over,under,expect", SCIPY_REFERENCE)
def test_matches_the_scipy_original(ft, line, over, under, expect):
    central, lo, hi, band = H.sweep(ft, line, over, under, **H.FAST)
    lh, la = band[2]
    assert lh == pytest.approx(expect["lam"][0], abs=LAM_TOL)
    assert la == pytest.approx(expect["lam"][1], abs=LAM_TOL)
    for key, want in expect.items():
        if key == "lam":
            continue
        assert central[key] == pytest.approx(want, abs=PROB_TOL), key


# ── the model's own invariants ──────────────────────────────────────────────

@pytest.mark.parametrize("ft,line,over,under,_e", SCIPY_REFERENCE)
def test_the_ft_anchor_is_reproduced(ft, line, over, under, _e):
    """The headline property of this construction: lambda is fitted THROUGH the
    two-half model, so whatever rho and kappa are, the FT prices you fed in come
    back out. If they don't, everything downstream is built on sand — which is
    why `shaky` is surfaced to the UI rather than only printed."""
    r = H.analyze(ft, line, over, under, fast=True)
    fc = r["ft_check"]
    assert fc["max_resid"] < 0.05
    assert fc["shaky"] == (fc["max_resid"] > H.FT_RESID_WARN)


@pytest.mark.parametrize("ft,line,over,under,_e", SCIPY_REFERENCE)
def test_ht_and_htft_are_proper_distributions(ft, line, over, under, _e):
    c = H.sweep(ft, line, over, under, **H.FAST)[0]
    assert c["ht_1"] + c["ht_X"] + c["ht_2"] == pytest.approx(1.0, abs=1e-6)
    grid = sum(v for k, v in c.items() if k.startswith("htft_"))
    assert grid == pytest.approx(1.0, abs=1e-6)
    for ln in (0.5, 1.5, 2.5):
        assert (c[f"ht_over_{ln}"] + c[f"ht_under_{ln}"]
                == pytest.approx(1.0, abs=1e-6))


def test_htft_rows_and_columns_agree_with_the_ht_and_ft_legs():
    """Each HT/FT row must sum to its half-time result, and each column to the
    full-time one. This is the identity the Anomalies tab checks on posted
    prices, so the model had better satisfy it exactly."""
    ft, line, over, under = (1.55, 3.6, 5.25), 2.5, 2.15, 1.55
    c = H.sweep(ft, line, over, under, **H.FAST)[0]
    for ht in "1X2":
        row = sum(c[f"htft_{ht}/{f}"] for f in "1X2")
        assert row == pytest.approx(c[f"ht_{ht}"], abs=1e-9), f"row {ht}"
    for ftr, key in (("1", "_ft_1"), ("X", "_ft_X"), ("2", "_ft_2")):
        col = sum(c[f"htft_{h}/{ftr}"] for h in "1X2")
        assert col == pytest.approx(c[key], abs=1e-9), f"column {ftr}"


def test_a_bigger_favourite_leads_at_half_more_often():
    a = H.sweep((3.00, 3.20, 2.45), 2.5, 1.95, 1.85, **H.FAST)[0]
    b = H.sweep((1.10, 9.00, 26.0), 2.5, 1.95, 1.85, **H.FAST)[0]
    assert b["ht_1"] > a["ht_1"]
    assert b["htft_1/1"] > a["htft_1/1"]


def test_the_band_brackets_the_central_estimate():
    ft, line, over, under = (1.55, 3.6, 5.25), 2.5, 2.15, 1.55
    central, lo, hi, _ = H.sweep(ft, line, over, under, **H.FAST)
    for k in H.MARKET_ORDER:
        assert lo[k] <= central[k] + 1e-9, k
        assert hi[k] >= central[k] - 1e-9, k


def test_full_sweep_band_is_at_least_as_wide_as_the_fast_one():
    """The fast sweep exists to be quick, not to look confident. If it ever
    reported a WIDER band than the full one, the corners would be mis-chosen."""
    ft, line, over, under = (1.55, 3.6, 5.25), 2.5, 2.15, 1.55
    _, flo, fhi, _ = H.sweep(ft, line, over, under, **H.FAST)
    _, slo, shi, _ = H.sweep(ft, line, over, under)
    for k in ("ht_1", "ht_X", "htft_1/1", "htft_2/2"):
        assert slo[k] <= flo[k] + 1e-9, k
        assert shi[k] >= fhi[k] - 1e-9, k


# ── devig methods ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method", ["shin", "multiplicative", "additive", "power"])
def test_every_devig_corner_normalises(method):
    p = H.devig([1.55, 3.60, 5.25], method)
    assert sum(p) == pytest.approx(1.0, abs=1e-9)
    assert all(0.0 < x < 1.0 for x in p)


def test_unknown_devig_is_rejected_rather_than_silently_defaulted():
    with pytest.raises(ValueError):
        H.devig([1.9, 1.9], "made-up")


def test_devig_delegates_to_the_project_implementation():
    """shin/power/multiplicative must be the SAME numbers the rest of the
    dashboard devigs with — a second, subtly different devig living in this
    module is exactly the drift the port was meant to avoid."""
    from src import soccer_model as sm
    odds = [1.55, 3.60, 5.25]
    for method, fn in (("shin", sm._devig_shin), ("power", sm._devig_power),
                       ("multiplicative", sm._devig_proportional)):
        assert list(H.devig(odds, method)) == pytest.approx(list(fn(odds)))


# ── the API-facing shape ─────────────────────────────────────────────────────

def test_analyze_returns_native_python_types():
    """A numpy.float64 or numpy.bool_ leaking into the response makes pydantic
    raise `Unable to serialize unknown type` — and it only bites once a book
    price is supplied, because without one the `bet` flag stays a literal False.
    That is exactly how it got shipped and caught here."""
    r = H.analyze((1.55, 3.6, 5.25), 2.5, 2.15, 1.55,
                  book={"ht_1": 3.05, "htft_1/1": 4.30}, fast=True)
    import json
    json.dumps(r)                       # raises TypeError on any numpy scalar
    for m in r["markets"]:
        assert type(m["fair"]) is float
        assert type(m["bet"]) is bool


def test_book_prices_are_optional_market_by_market():
    """The half-time price is frequently not posted at all — that is the normal
    case this tool exists for, not an error."""
    r = H.analyze((1.55, 3.6, 5.25), 2.5, 2.15, 1.55, fast=True)
    assert r["markets"]
    assert all(m["book"] is None and m["edge"] is None and m["bet"] is False
               for m in r["markets"])


def test_bet_flag_uses_the_worst_corner_not_the_central_estimate():
    """The module's stated decision rule. A price that beats the central fair
    but not the worst corner must NOT be marked bettable — that gap is model
    noise, and calling it a bet is the one error this design exists to prevent."""
    r = H.analyze((1.55, 3.6, 5.25), 2.5, 2.15, 1.55, fast=True)
    row = next(m for m in r["markets"] if m["key"] == "ht_1")
    between = (row["fair"] + row["min_bet_odds"]) / 2.0
    assert row["fair"] < between < row["min_bet_odds"]

    r2 = H.analyze((1.55, 3.6, 5.25), 2.5, 2.15, 1.55,
                   book={"ht_1": between}, min_edge=0.0, fast=True)
    row2 = next(m for m in r2["markets"] if m["key"] == "ht_1")
    assert row2["edge"] > 0            # beats the central fair
    assert row2["worst_edge"] < 0      # but not the worst corner
    assert row2["bet"] is False


def test_min_bet_odds_is_the_longest_corner():
    r = H.analyze((2.1, 3.4, 3.4), 2.5, 1.9, 1.9, fast=True)
    for m in r["markets"]:
        assert m["min_bet_odds"] == pytest.approx(m["band_hi"])
        assert m["band_lo"] <= m["fair"] <= m["band_hi"] + 1e-9


def test_the_two_headline_cells_are_present():
    """1/1 and 2/2 are the cells the Calc tab emphasises."""
    r = H.analyze((1.55, 3.6, 5.25), 2.5, 2.15, 1.55, fast=True)
    keys = {m["key"] for m in r["markets"]}
    assert {"htft_1/1", "htft_2/2"} <= keys
    assert {"ht_1", "ht_X", "ht_2"} <= keys


def test_no_scipy_import_sneaks_back_in():
    """The port exists so the dashboard keeps a numpy-only dependency set.

    Checked on the parsed IMPORT statements, not on the text: the module
    docstring names scipy several times explaining what was replaced, and a
    substring search would fail on its own documentation.
    """
    import ast
    from pathlib import Path
    tree = ast.parse((Path(__file__).resolve().parents[1]
                      / "src" / "ht_from_ft.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "scipy" not in imported, f"scipy import reintroduced (imports: {sorted(imported)})"


def test_requirements_still_have_no_scipy():
    from pathlib import Path
    req = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text()
    assert "scipy" not in req.lower()
