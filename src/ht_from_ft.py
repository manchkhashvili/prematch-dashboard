"""
Half-time fair values derived from full-time 1X2 + a total line.

Owner's module, dropped in 2026-08-12 and moved under src/ here. The model and
every assumption in it are unchanged — see "Model" below. What changed is the
numerics: the original imported `scipy.optimize.brentq`, `scipy.optimize.
least_squares` and `scipy.stats.poisson`, and this project is deliberately
numpy-only (scipy is a ~50 MB dependency the rest of the stack does not need).
All three had a numpy-only equivalent already living in `src/soccer_model.py`:

    poisson.pmf   -> soccer_model._poisson_pmf
    brentq        -> the bisection inside _devig_power / _devig_shin
    least_squares -> a local damped Gauss-Newton, the same shape as
                     soccer_model.fit_lambdas (log-space, box-clipped)

Parity against the scipy original was checked over a grid of fixtures before
this replaced it — see tests/test_ht_from_ft.py, which pins the reference
outputs the scipy version produced.

This is a SCREENING tool, not a pricing engine. The dominant error is not
Poisson misspecification — it is (a) the devig assumption and (b) the fact that
FT 1X2 plus one total line is 3 constraints against 2 goal-rate parameters. So
rather than emit a point estimate that hides both, it sweeps the assumption
space and returns a BAND.

Decision rule: bet only when the book price beats the WORST corner of the band
by your required margin. Anything inside the band is model noise.

Model
-----
Goals are Poisson with rates lambda_h, lambda_a. A share `rho` of the match
lambda falls in H1. In H2 the rates are conditioned on the half-time scoreline:
a lead of L multiplies the leader's rate by exp(-kappa*L) and the trailer's by
exp(+kappa*L). kappa=0 is naive independent halves (which overstates HT/FT
'hold the lead' outcomes because it ignores the leader easing off).

Crucially, lambda is fitted THROUGH this construction, so whatever rho and
kappa are set to, the model still reproduces the FT prices you fed it. The FT
anchor is the one thing actually known; it should not drift when a nuisance
parameter is nudged.

Assumptions swept
-----------------
  devig     : shin | multiplicative | additive | power
  rho       : share of FT goals scored in H1 (empirically ~0.44-0.46)
  fit_mode  : which of the 3 over-determined FT constraints to favour
  kappa     : H2 score-dependence strength

Usage
-----
  python3 -m src.ht_from_ft --ft 1.28 6.00 6.25 --total 2.5 1.28 3.50
  python3 -m src.ht_from_ft --ft 1.28 6.00 6.25 --total 2.5 1.28 3.50 \
                            --book ht_1=2.10 htft_1/1=2.30 --min-edge 0.05

Served to the Calc tab by POST /api/ht_from_ft (see src/app.py). A full sweep
is ~5 s and the fast one ~1.4 s, so the endpoint is button-triggered and
memoised, never called per keystroke.
"""

from __future__ import annotations

import argparse
import itertools
from typing import Optional, Sequence

import numpy as np

from src.soccer_model import (
    _devig_power, _devig_proportional, _devig_shin, _poisson_pmf,
)

MAXG = 14          # goals grid per team per half
LEAD_CAP = 3       # cap the scoreline effect on H2 rates
G = np.arange(MAXG + 1)


# ==========================================================================
# devigging
# ==========================================================================

def devig(odds: Sequence[float], method: str = "shin") -> np.ndarray:
    """Decimal odds -> fair probabilities.

    shin / power / multiplicative delegate to soccer_model's numpy-only
    implementations (identical formulas, bisection instead of brentq) so the
    whole dashboard devigs one way. `additive` lives here — it is a corner of
    this module's sweep and nothing else in the project uses it.
    """
    q = np.array([1.0 / o for o in odds], dtype=float)
    Q, n = q.sum(), len(q)

    if Q <= 1.0:                       # no (or negative) vig
        return q / Q

    if method == "multiplicative":
        return _devig_proportional(odds)
    if method == "power":
        return _devig_power(odds)
    if method == "shin":
        return _devig_shin(odds)
    if method == "additive":
        p = np.clip(q - (Q - 1.0) / n, 1e-9, None)
        return p / p.sum()

    raise ValueError(f"unknown devig method: {method}")


# ==========================================================================
# model
# ==========================================================================

def _sm(lh: float, la: float) -> np.ndarray:
    """Score matrix P(i home, j away)."""
    return np.outer(_poisson_pmf(MAXG + 1, lh), _poisson_pmf(MAXG + 1, la))


def _diff_total(m: np.ndarray):
    """Collapse a score matrix into (diff_dist, total_dist, joint_total_diff)."""
    d = G[:, None] - G[None, :]
    t = G[:, None] + G[None, :]
    n = MAXG
    diff = np.zeros(2 * n + 1)
    tot = np.zeros(2 * n + 1)
    joint = np.zeros((2 * n + 1, 2 * n + 1))   # [total, diff+n]
    np.add.at(diff, d.ravel() + n, m.ravel())
    np.add.at(tot, t.ravel(), m.ravel())
    np.add.at(joint, (t.ravel(), d.ravel() + n), m.ravel())
    return diff, tot, joint


def solve(lh, la, rho, kappa, ft_line=2.5, ht_lines=(0.5, 1.5, 2.5)) -> dict:
    """Full model. Returns dict of market probabilities (HT, HT/FT, and FT check)."""
    n = MAXG
    m1 = _sm(rho * lh, rho * la)
    d1, t1, j1 = _diff_total(m1)

    bh, ba = (1 - rho) * lh, (1 - rho) * la
    leads = np.arange(-n, n + 1)
    h2_diff = np.zeros((len(leads), 2 * n + 1))
    h2_tot = np.zeros((len(leads), 2 * n + 1))
    cache: dict[int, tuple] = {}
    for li, L in enumerate(leads):
        Lc = int(np.clip(L, -LEAD_CAP, LEAD_CAP))
        if Lc not in cache:
            m2 = _sm(bh * np.exp(-kappa * Lc), ba * np.exp(kappa * Lc))
            cache[Lc] = _diff_total(m2)[:2]
        h2_diff[li], h2_tot[li] = cache[Lc]

    out: dict[str, float] = {}

    # ---- HT 1X2 / totals -------------------------------------------------
    out["ht_1"] = d1[n + 1:].sum()
    out["ht_X"] = d1[n]
    out["ht_2"] = d1[:n].sum()
    tvals = np.arange(len(t1))
    for ln in ht_lines:
        out[f"ht_over_{ln}"] = t1[tvals > ln].sum()
        out[f"ht_under_{ln}"] = t1[tvals < ln].sum()

    # ---- HT/FT 3x3 -------------------------------------------------------
    lab = {1: "1", 0: "X", -1: "2"}
    grid = {f"htft_{lab[a]}/{lab[b]}": 0.0 for a in (1, 0, -1) for b in (1, 0, -1)}
    d2_vals = np.arange(-n, n + 1)
    for li, L in enumerate(leads):
        p1 = d1[li]
        if p1 < 1e-14:
            continue
        ht = lab[int(np.sign(L))]
        ftsign = np.sign(L + d2_vals)
        for s, name in ((1, "1"), (0, "X"), (-1, "2")):
            grid[f"htft_{ht}/{name}"] += p1 * h2_diff[li][ftsign == s].sum()
    out.update(grid)

    # ---- FT reconstruction (the anchor the fit targets) ------------------
    ft_diff = np.zeros(4 * n + 1)
    for li in range(len(leads)):
        if d1[li] < 1e-14:
            continue
        ft_diff[li:li + 2 * n + 1] += d1[li] * h2_diff[li]
    off = 2 * n
    out["_ft_1"] = ft_diff[off + 1:].sum()
    out["_ft_X"] = ft_diff[off]
    out["_ft_2"] = ft_diff[:off].sum()

    ft_tot = np.zeros(4 * n + 1)
    for li in range(len(leads)):
        col = j1[:, li]
        if col.sum() < 1e-14:
            continue
        for tt in np.nonzero(col > 1e-14)[0]:
            ft_tot[tt:tt + 2 * n + 1] += col[tt] * h2_tot[li]
    out["_ft_over"] = ft_tot[np.arange(len(ft_tot)) > ft_line].sum()

    return out


# ==========================================================================
# fitting: lambdas are solved THROUGH the two-half model
# ==========================================================================

# 3 FT constraints (home, draw, over) vs 2 free rates -> over-determined.
# The totals line is always weighted hard: it is the only clean anchor on
# SCALE, and dropping it leaves the fit unidentified (kappa inflates draws,
# and an unconstrained fit answers by cranking lambda_total to absurd levels).
# What genuinely varies is how the home/away SPLIT is pinned.
WEIGHTS = {
    "ls":   (1.0, 1.0, 5.0),   # compromise between the home and draw legs
    "home": (1.0, 0.0, 5.0),   # pin the split off the home leg
    "draw": (0.0, 1.0, 5.0),   # pin the split off the draw leg
}

_LO = np.log([0.05, 0.05])     # same box the scipy version used
_HI = np.log([6.0, 6.0])


def _least_squares_2d(resid, x0: np.ndarray, *, max_iter: int = 200,
                      tol: float = 1e-12) -> np.ndarray:
    """Damped Gauss-Newton (Levenberg-Marquardt) for a 2-parameter, box-bounded
    least-squares fit — the numpy-only stand-in for scipy's
    `least_squares(..., method="trf", bounds=...)`.

    Same shape as soccer_model.fit_lambdas: numerical Jacobian, log-space
    parameters, damping raised on a rejected step. Bounds are enforced by
    clipping, which is legitimate here because the box only exists to stop the
    optimiser wandering into lambdas where the truncated goal grid loses mass —
    the solution is never near an edge for a real fixture.
    """
    x = np.clip(np.asarray(x0, dtype=float), _LO, _HI)
    r = resid(x)
    cost = float(r @ r)
    lam = 1e-3
    eps = 1e-6
    for _ in range(max_iter):
        # numerical Jacobian (3 residuals x 2 params)
        J = np.zeros((len(r), 2))
        for c in range(2):
            xp = x.copy()
            step = eps * max(1.0, abs(xp[c]))
            xp[c] = min(xp[c] + step, _HI[c])
            J[:, c] = (resid(xp) - r) / (xp[c] - x[c] if xp[c] != x[c] else step)
        JTJ = J.T @ J
        JTr = J.T @ r
        improved = False
        for _ in range(30):                      # raise damping until we improve
            try:
                dx = np.linalg.solve(JTJ + lam * np.eye(2), -JTr)
            except np.linalg.LinAlgError:
                lam *= 10.0
                continue
            xn = np.clip(x + dx, _LO, _HI)
            rn = resid(xn)
            cn = float(rn @ rn)
            if cn < cost:
                x, r, cost = xn, rn, cn
                lam = max(lam * 0.3, 1e-12)
                improved = True
                break
            lam *= 10.0
        if not improved or cost < tol:
            break
    return x


def fit(p_h, p_d, p_over, line, rho, kappa, fit_mode="ls") -> tuple[float, float]:
    w = WEIGHTS[fit_mode]

    def resid(x):
        lh, la = np.exp(x)
        m = solve(lh, la, rho, kappa, ft_line=line, ht_lines=())
        return np.array([
            w[0] * (m["_ft_1"] - p_h),
            w[1] * (m["_ft_X"] - p_d),
            w[2] * (m["_ft_over"] - p_over),
        ])

    x = _least_squares_2d(resid, np.log([1.4, 1.1]))
    return float(np.exp(x[0])), float(np.exp(x[1]))


# ==========================================================================
# sweep
# ==========================================================================

DEVIGS = ("shin", "multiplicative", "additive", "power")
RHOS = (0.43, 0.45, 0.47)
FITS = ("ls", "home", "draw")
KAPPAS = (0.0, 0.10, 0.20)

# reduced sweep for scanning many fixtures (16 corners, ~1.4 s)
FAST = dict(devigs=("shin", "multiplicative"), rhos=(0.44, 0.46),
            fits=("ls", "home"), kappas=(0.05, 0.15))


def sweep(ft_odds, line, over_odds, under_odds,
          devigs=DEVIGS, rhos=RHOS, fits=FITS, kappas=KAPPAS):
    rows, lams = [], []
    for dv in devigs:
        p_h, p_d, p_a = devig(list(ft_odds), dv)
        p_ov = devig([over_odds, under_odds], dv)[0]
        for fm, rho, kap in itertools.product(fits, rhos, kappas):
            lh, la = fit(p_h, p_d, p_ov, line, rho, kap, fm)
            lams.append((lh, la))
            rows.append(solve(lh, la, rho, kap, ft_line=line))

    keys = [k for k in rows[0] if not k.startswith("_")]
    arr = {k: np.array([r[k] for r in rows]) for k in keys}

    p_h, p_d, p_a = devig(list(ft_odds), "shin")
    p_ov = devig([over_odds, under_odds], "shin")[0]
    lh0, la0 = fit(p_h, p_d, p_ov, line, 0.45, 0.10, "ls")
    central = solve(lh0, la0, 0.45, 0.10, ft_line=line)

    band = ((min(l[0] for l in lams), max(l[0] for l in lams)),
            (min(l[1] for l in lams), max(l[1] for l in lams)),
            (lh0, la0))
    return central, {k: arr[k].min() for k in keys}, {k: arr[k].max() for k in keys}, band


def _o(p: float) -> float:
    """Probability -> decimal odds, always as a NATIVE python float.

    The float() is not cosmetic: everything upstream is numpy, and a
    numpy.float64/numpy.bool_ leaking into the API response makes pydantic
    raise `Unable to serialize unknown type` at render time — a 500 that only
    appears once a book price is supplied (without one the `bet` flag stays a
    python literal and the bug hides).
    """
    return float("inf") if p <= 1e-12 else float(1.0 / p)


# Order used by both the CLI report and the API payload.
MARKET_ORDER = (["ht_1", "ht_X", "ht_2"]
                + [f"ht_over_{l}" for l in (0.5, 1.5, 2.5)]
                + [f"ht_under_{l}" for l in (0.5, 1.5, 2.5)]
                + [f"htft_{a}/{b}" for a in "1X2" for b in "1X2"])

# The residual above which the FT prices do not sit on a clean Poisson surface,
# so everything derived off them is shaky. Surfaced to the UI, not just printed.
FT_RESID_WARN = 0.03


def analyze(ft_odds, line, over_odds, under_odds, *, book: Optional[dict] = None,
            min_edge: float = 0.05, fast: bool = False) -> dict:
    """Structured result — the API and the CLI both render this.

    Returns fair price, band, and (when a book price is supplied) the edge at
    the CENTRAL estimate and at the WORST corner. `worst_edge` is the one that
    matters: the module's whole decision rule is "beat the worst corner".
    """
    kw = FAST if fast else {}
    central, lo, hi, lam = sweep(ft_odds, line, over_odds, under_odds, **kw)
    (lh_lo, lh_hi), (la_lo, la_hi), (lh0, la0) = lam

    tgt = devig(list(ft_odds), "shin")
    resid = max(abs(central["_ft_1"] - tgt[0]), abs(central["_ft_X"] - tgt[1]),
                abs(central["_ft_2"] - tgt[2]))

    book = book or {}
    markets = []
    for k in MARKET_ORDER:
        c, l, h = central[k], lo[k], hi[k]
        row = {
            "key": k,
            "prob": float(c),
            "fair": _o(c),
            "band_lo": _o(h),          # shortest fair price across corners
            "band_hi": _o(l),          # longest
            "min_bet_odds": _o(l),     # worst-corner fair — the decision line
            "book": None, "edge": None, "worst_edge": None, "bet": False,
        }
        if k in book and book[k]:
            bk = float(book[k])
            row["book"] = bk
            row["edge"] = float(bk * c - 1.0)
            row["worst_edge"] = float(bk * l - 1.0)
            row["bet"] = bool(row["worst_edge"] >= min_edge)
        markets.append(row)

    g = lambda key, d: len(kw.get(key, d))
    corners = (g("devigs", DEVIGS) * g("fits", FITS)
               * g("rhos", RHOS) * g("kappas", KAPPAS))
    return {
        "lambda": {"home": float(lh0), "away": float(la0),
                   "match": float(lh0 + la0),
                   "home_lo": float(lh_lo), "home_hi": float(lh_hi),
                   "away_lo": float(la_lo), "away_hi": float(la_hi)},
        "ft_check": {"home": float(central["_ft_1"]), "draw": float(central["_ft_X"]),
                     "away": float(central["_ft_2"]),
                     "target_home": float(tgt[0]), "target_draw": float(tgt[1]),
                     "target_away": float(tgt[2]),
                     "max_resid": float(resid), "shaky": bool(resid > FT_RESID_WARN)},
        "markets": markets,
        "corners": corners,
        "min_edge": min_edge,
    }


def report(ft_odds, line, over_odds, under_odds, book=None, min_edge=0.05,
           fast=False):
    """CLI rendering of analyze() — unchanged output from the original module."""
    r = analyze(ft_odds, line, over_odds, under_odds, book=book,
                min_edge=min_edge, fast=fast)
    lm, fc = r["lambda"], r["ft_check"]

    print(f"\nFT in    1X2 {ft_odds[0]}/{ft_odds[1]}/{ft_odds[2]}    "
          f"O/U {line}  {over_odds}/{under_odds}")
    print(f"lambda   home {lm['home']:.2f} [{lm['home_lo']:.2f}-{lm['home_hi']:.2f}]   "
          f"away {lm['away']:.2f} [{lm['away_lo']:.2f}-{lm['away_hi']:.2f}]   "
          f"match {lm['match']:.2f}")
    print(f"FT check {fc['home']:.3f}/{fc['draw']:.3f}/{fc['away']:.3f}  vs input "
          f"{fc['target_home']:.3f}/{fc['target_draw']:.3f}/{fc['target_away']:.3f}"
          f"   max resid {fc['max_resid']:.3f}"
          + ("   <-- FT prices do not sit on a clean Poisson surface; "
             "derivations off them are shaky" if fc["shaky"] else ""))

    hdr = f"\n{'market':<14}{'fair':>8}{'band':>19}{'min bet odds':>14}"
    if book:
        hdr += f"{'book':>9}{'edge':>8}{'worst':>8}"
    print(hdr)
    print("-" * (55 + (25 if book else 0)))
    for m in r["markets"]:
        row = (f"{m['key']:<14}{m['fair']:>8.2f}"
               f"{f'{m['band_lo']:.2f} - {m['band_hi']:.2f}':>19}"
               f"{m['min_bet_odds']:>14.2f}")
        if book and m["book"] is not None:
            row += f"{m['book']:>9.2f}{m['edge']:>8.1%}{m['worst_edge']:>8.1%}"
            if m["bet"]:
                row += "  << BET"
        print(row)

    print(f"\nband = min/max over {r['corners']} assumption corners "
          f"(devig x fit x rho x kappa)")
    print("'min bet odds' = worst-corner fair price; below it you are betting noise")
    return r


def _parse_book(items):
    out = {}
    for it in items or []:
        k, v = it.split("=")
        out[k.strip()] = float(v)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ft", nargs=3, type=float, required=True,
                    metavar=("HOME", "DRAW", "AWAY"))
    ap.add_argument("--total", nargs=3, type=float, required=True,
                    metavar=("LINE", "OVER", "UNDER"))
    ap.add_argument("--book", nargs="*", default=None,
                    help="e.g. ht_1=2.10 htft_1/1=2.30")
    ap.add_argument("--min-edge", type=float, default=0.05)
    ap.add_argument("--fast", action="store_true",
                    help="16-corner sweep instead of 108, for bulk scanning")
    a = ap.parse_args()
    report(a.ft, a.total[0], a.total[1], a.total[2],
           _parse_book(a.book), a.min_edge, a.fast)
