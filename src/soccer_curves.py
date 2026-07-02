"""
Curve-residual checks — the model upgrade of family A (ladder monotonicity).

Family A flags a handicap/total ladder rung that moves the WRONG way versus its
neighbour. That misses a rung that is monotone but the wrong MAGNITUDE. Here we
fit the theoretical curve the whole ladder must lie on and flag rungs that leave
it:

- Asian handicap ladder ⇒ the goal-difference is Skellam(lh, la) (difference of
  two independent Poissons). We fit (lh, la) to the devigged cover probabilities
  and flag any rung off the fitted survival curve by >= 2pp.
- Totals ladder ⇒ the goal-sum is ~Poisson(T). We fit T and flag off-curve rungs.
  (Dixon-Coles only reshuffles the 0/1/2-goal cells and preserves the mean, so
  its effect on the aggregate total curve is second order — a plain Poisson(T)
  fit is used.)

- TWO-ANCHOR CROSS-FIT. Supremacy S and total T can be read two ways: from the
  1X2 + main total (the "headline" anchor) and from the full AH + totals ladders.
  If they disagree by more than a threshold, one family is stale — flagged as a
  diagnostic, with a timestamp-based guess at which side moved last.

numpy only; the fits are hand-rolled Gauss-Newton / golden-section. Rungs are
devigged internally with the power method (each rung is a 2-way market).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

from src import soccer_model as sm

CURVE_RESID_PP = 2.0       # flag a rung this many implied pp off the fitted curve
CROSS_S_GOALS = 0.15       # cross-fit supremacy divergence threshold (goals)
CROSS_T_GOALS = 0.25       # cross-fit total divergence threshold (goals)


@dataclass(frozen=True)
class CurveFlag:
    kind: str              # "ah_curve" | "total_curve" | "cross_fit"
    detail: str
    severity: float        # pp off-curve, or goals of divergence ×100
    line: Optional[float] = None
    tier: str = "structural"


# ── small solvers (numpy only) ────────────────────────────────────────────────
def _gauss_newton(resid_fn, x0, *, iters: int = 80):
    x = np.array(x0, dtype=float)
    r = resid_fn(x)
    for _ in range(iters):
        eps = 1e-5
        J = np.zeros((len(r), len(x)))
        for c in range(len(x)):
            xp = x.copy(); xp[c] += eps
            J[:, c] = (resid_fn(xp) - r) / eps
        JTJ = J.T @ J + 1e-9 * np.eye(len(x))
        try:
            dx = np.linalg.solve(JTJ, -J.T @ r)
        except np.linalg.LinAlgError:
            break
        step, base = 1.0, float(r @ r)
        for _ in range(30):
            rn = resid_fn(x + step * dx)
            if float(rn @ rn) < base:
                break
            step *= 0.5
        x = x + step * dx
        r = resid_fn(x)
        if float(r @ r) < 1e-16:
            break
    return x, r


def _poisson_over(line: float, T: float, n: int = 24) -> float:
    pmf = sm._poisson_pmf(n, T)
    k = np.arange(n)
    return float(pmf[k > line].sum())


# ── ladder devig ──────────────────────────────────────────────────────────────
def _devig_rungs(ladder: dict) -> dict:
    """{line: (side_a_odds, side_b_odds)} → {line: p_side_a} via power devig."""
    out = {}
    for line, pair in ladder.items():
        try:
            oa, ob = float(pair[0]), float(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        if oa > 1.0 and ob > 1.0:
            out[float(line)] = sm.devig([oa, ob], "power")[0]
    return out


# ── AH curve (Skellam) ────────────────────────────────────────────────────────
def fit_ah_ladder(ah_ladder: dict, *, n: int = sm.DEFAULT_N, dc_rho: float = sm.DC_RHO):
    """Fit (lh, la) to a devigged AH ladder {home_line: p_home_cover}. Returns
    (lh, la, {line: residual}) where residual = observed − fitted."""
    lines = sorted(ah_ladder)
    obs = np.array([ah_ladder[l] for l in lines])

    def resid(x):
        lh, la = math.exp(x[0]), math.exp(x[1])
        F = sm.score_matrix(lh, la, n=n, dc_rho=dc_rho)
        return np.array([sm._ah_home_prob(F, l) for l in lines]) - obs

    x, _ = _gauss_newton(resid, [math.log(1.3), math.log(1.3)])
    lh, la = math.exp(x[0]), math.exp(x[1])
    F = sm.score_matrix(lh, la, n=n, dc_rho=dc_rho)
    return lh, la, {l: float(obs[i] - sm._ah_home_prob(F, l)) for i, l in enumerate(lines)}


def ah_curve_flags(ah_raw: dict, *, thresh_pp: float = CURVE_RESID_PP,
                   n: int = sm.DEFAULT_N, dc_rho: float = sm.DC_RHO) -> list[CurveFlag]:
    """Flag AH rungs off the fitted Skellam survival curve. ah_raw =
    {home_line: (home_odds, away_odds)}."""
    obs = _devig_rungs(ah_raw)
    if len(obs) < 3:
        return []
    lines = sorted(obs)
    y = np.array([obs[l] for l in lines])

    def resid(x):
        lh, la = math.exp(x[0]), math.exp(x[1])
        F = sm.score_matrix(lh, la, n=n, dc_rho=dc_rho)
        return np.array([sm._ah_home_prob(F, l) for l in lines]) - y

    x, _ = _gauss_newton(resid, [math.log(1.3), math.log(1.3)])
    lh, la = math.exp(x[0]), math.exp(x[1])
    F = sm.score_matrix(lh, la, n=n, dc_rho=dc_rho)
    flags = []
    for l in lines:
        model = sm._ah_home_prob(F, l)
        dev = (obs[l] - model) * 100.0
        if abs(dev) >= thresh_pp:
            flags.append(CurveFlag(
                kind="ah_curve", line=l, severity=round(abs(dev), 2),
                detail=(f"AH home {l:+g}: devigged {obs[l]*100:.1f}% vs Skellam-fit "
                        f"{model*100:.1f}% (lh={lh:.2f}, la={la:.2f}) — {dev:+.1f}pp "
                        f"off curve"),
            ))
    flags.sort(key=lambda f: f.severity, reverse=True)
    return flags


# ── totals curve (Poisson) ────────────────────────────────────────────────────
def fit_total_ladder(totals: dict) -> tuple[float, dict]:
    """Fit T to a devigged totals ladder {line: p_over}. Returns (T, residuals)."""
    lines = sorted(totals)
    y = np.array([totals[l] for l in lines])

    def resid(x):
        T = math.exp(x[0])
        return np.array([_poisson_over(l, T) for l in lines]) - y

    x, _ = _gauss_newton(resid, [math.log(2.6)])
    T = math.exp(x[0])
    return T, {l: totals[l] - _poisson_over(l, T) for l in lines}


def total_curve_flags(totals_raw: dict, *, thresh_pp: float = CURVE_RESID_PP) -> list[CurveFlag]:
    """Flag totals rungs off the fitted Poisson(T) curve. totals_raw =
    {line: (over_odds, under_odds)}."""
    obs = _devig_rungs(totals_raw)
    if len(obs) < 3:
        return []
    T, resid = fit_total_ladder(obs)
    flags = []
    for l, res in resid.items():
        dev = res * 100.0
        if abs(dev) >= thresh_pp:
            flags.append(CurveFlag(
                kind="total_curve", line=l, severity=round(abs(dev), 2),
                detail=(f"Over {l:g}: devigged {obs[l]*100:.1f}% vs Poisson-fit "
                        f"{(obs[l]-res)*100:.1f}% (T={T:.2f}) — {dev:+.1f}pp off curve"),
            ))
    flags.sort(key=lambda f: f.severity, reverse=True)
    return flags


# ── two-anchor cross-fit ──────────────────────────────────────────────────────
def cross_fit_divergence(
    anchor_S: float, anchor_T: float,
    ah_raw: Optional[dict] = None, totals_raw: Optional[dict] = None,
    *, s_thresh: float = CROSS_S_GOALS, t_thresh: float = CROSS_T_GOALS,
    anchor_ts=None, ladder_ts=None, n: int = sm.DEFAULT_N, dc_rho: float = sm.DC_RHO,
) -> Optional[CurveFlag]:
    """Compare (S, T) from the 1X2+total anchor against (S, T) implied by the AH
    + totals ladders. Flag if they diverge; name the side that moved last."""
    ah = _devig_rungs(ah_raw or {})
    tot = _devig_rungs(totals_raw or {})
    if len(ah) < 3 and len(tot) < 3:
        return None

    lines_a = sorted(ah)
    lines_t = sorted(tot)
    ya = np.array([ah[l] for l in lines_a])
    yt = np.array([tot[l] for l in lines_t])

    def resid(x):
        lh, la = math.exp(x[0]), math.exp(x[1])
        F = sm.score_matrix(lh, la, n=n, dc_rho=dc_rho)
        ra = np.array([sm._ah_home_prob(F, l) for l in lines_a]) - ya if len(ah) >= 3 else np.array([])
        rt = np.array([_poisson_over(l, lh + la) for l in lines_t]) - yt if len(tot) >= 3 else np.array([])
        return np.concatenate([ra, rt])

    x, _ = _gauss_newton(resid, [math.log(1.3), math.log(1.3)])
    lh, la = math.exp(x[0]), math.exp(x[1])
    S_l, T_l = lh - la, lh + la
    dS, dT = abs(anchor_S - S_l), abs(anchor_T - T_l)
    if dS < s_thresh and dT < t_thresh:
        return None
    moved = ""
    if anchor_ts is not None and ladder_ts is not None:
        moved = (" (ladders moved last)" if ladder_ts > anchor_ts
                 else " (headline 1X2/total moved last)")
    return CurveFlag(
        kind="cross_fit", severity=round(max(dS, dT) * 100, 2),
        detail=(f"cross-fit divergence: headline S={anchor_S:+.2f}/T={anchor_T:.2f} vs "
                f"ladder S={S_l:+.2f}/T={T_l:.2f} (ΔS={dS:.2f}, ΔT={dT:.2f}) — one "
                f"family is stale{moved}"),
        tier="diagnostic",
    )
