"""
Fair HT/FT pricing for basketball — bivariate normal on the margins.

1/1 is a JOINT probability — P(team leads at HT AND wins FT). Halftime and
full-game margins are strongly correlated, so marginals can NOT be
multiplied. Model (owner's research notes, 2026-06-12):

    full-game margin  M  ~ Normal(mu,  sigma^2)
    halftime margin   M1 ~ Normal(mu1, (sigma*rho)^2)
    corr(M1, M) = rho

    P(outcome i/j) = P(M1 in I_i, M in I_j), continuity correction 0.5
    (integer scores): I_1 = (0.5, inf), I_X = (-0.5, 0.5), I_2 = (-inf, -0.5)

WHY rho = 1/sqrt(2): with i.i.d. halves each half carries half the variance,
so corr(M1, M) = sigma_half/sigma_full = 1/sqrt(2) ~ 0.707. Empirically rho
runs BELOW that (~0.65-0.70) — leaders coast, garbage time compresses 2H
variance — so the default sits at 0.70.

Getting mu (expected full-game margin, home - away):
    best:  mu  = -spread_center        (line where the devigged ladder
                                        crosses 50% — no sigma needed)
    else:  mu  = sigma * Phi^-1(p_home)  from the devigged moneyline
    best:  mu1 = -spread_center_H1     (halves aren't symmetric)
    else:  mu1 = mu * H1_SHARE (0.5)

MARKET SHAPE — MUST DETECT:
    9 outcomes = FT is regulation (X/X exists on the board)
    6 outcomes = FT includes OT (no FT draw; regulation-tie mass splits
                 ~50/50 into 1 and 2)
Getting this wrong shifts fair ~3% — i.e. fabricates an edge.

Anchors (50/50 game, sigma=11, rho=1/sqrt2): fair 1/1 ~ 2.76 with the
6-outcome convention, ~2.83 with the 9-outcome one; closed form without HT
ties is 1/4 + arcsin(rho)/(2*pi) = 3/8 -> 2.667. The HT/FT multiplier
(fair 1/1 over fair FT) is NOT constant: ~1.38 at 50/50, ~1.27 for a big
favorite, ~1.49 for a big dog — soft books template a near-constant
multiplier, so lopsided lines (favorite-1/1, dog-2/2) are the structural
sweet spots.

The bivariate normal CDF uses Plackett's identity (dPhi2/drho is the
bivariate density) integrated with fixed Gauss-Legendre nodes — pure
stdlib, accurate to ~1e-7 for |rho| <= 0.95, far beyond what sigma/rho
uncertainty justifies.
"""
from __future__ import annotations

import math
from statistics import NormalDist
from typing import Optional

_N = NormalDist()

# Defaults per the research notes. sigma = SD of the final margin.
RHO = 0.70           # theory 0.707; empirically 0.65-0.70
H1_SHARE = 0.5       # E[M1] = mu * H1_SHARE when no 1H spread is available
SIGMA_DEFAULT = 10.0  # Euro/other leagues ~9.5-10
_SIGMA_BY_LEAGUE = (  # substring match on the lowercased league name
    ("nba", 11.75),   # NBA ~11.5-12
    ("ncaa", 10.3),   # college ~10.3
    ("college", 10.3),
)

HTFT_LABELS_9 = ("1/1", "1/X", "1/2", "X/1", "X/X", "X/2", "2/1", "2/X", "2/2")
HTFT_LABELS_6 = ("1/1", "1/2", "X/1", "X/2", "2/1", "2/2")

# 20-point Gauss-Legendre nodes/weights on [-1, 1] (symmetric pairs).
_GL_X = (
    0.0765265211334973, 0.2277858511416451, 0.3737060887154195,
    0.5108670019508271, 0.6360536807265150, 0.7463319064601508,
    0.8391169718222188, 0.9122344282513259, 0.9639719272779138,
    0.9931285991850949,
)
_GL_W = (
    0.1527533871307258, 0.1491729864726037, 0.1420961093183820,
    0.1316886384491766, 0.1181945319615184, 0.1019301198172404,
    0.0832767415767048, 0.0626720483341091, 0.0406014298003869,
    0.0176140071391521,
)


def sigma_for_league(league: Optional[str]) -> float:
    low = (league or "").lower()
    for needle, s in _SIGMA_BY_LEAGUE:
        if needle in low:
            return s
    return SIGMA_DEFAULT


def phi2(a: float, b: float, rho: float) -> float:
    """Standard bivariate normal CDF P(X <= a, Y <= b), corr rho.

    Plackett: Phi2(a,b,rho) = Phi(a)Phi(b)
        + 1/(2pi) * \\int_0^rho exp(-(a^2 - 2 t a b + b^2)/(2(1-t^2)))
                      / sqrt(1-t^2) dt
    """
    if a == math.inf:
        return _N.cdf(b)
    if b == math.inf:
        return _N.cdf(a)
    if a == -math.inf or b == -math.inf:
        return 0.0
    base = _N.cdf(a) * _N.cdf(b)
    if rho == 0.0:
        return base
    half = rho / 2.0
    acc = 0.0
    for x, w in zip(_GL_X, _GL_W):
        for t in (half + half * x, half - half * x):
            omt2 = 1.0 - t * t
            acc += w * math.exp(
                -(a * a - 2.0 * t * a * b + b * b) / (2.0 * omt2)
            ) / math.sqrt(omt2)
    return base + acc * half / (2.0 * math.pi)


def _rect(x0: float, x1: float, y0: float, y1: float, rho: float) -> float:
    """P(X in (x0,x1), Y in (y0,y1)) for standard bivariate normal."""
    return (phi2(x1, y1, rho) - phi2(x0, y1, rho)
            - phi2(x1, y0, rho) + phi2(x0, y0, rho))


def htft_fair_probs(
    mu: float,
    mu1: Optional[float] = None,
    *,
    sigma: float = SIGMA_DEFAULT,
    rho: float = RHO,
    nine_outcome: bool = True,
) -> dict[str, float]:
    """Fair probabilities per HT/FT label, summing to 1.0.

    mu/mu1 in points (home - away). nine_outcome=True means the FT leg is
    regulation time (X/X is a real outcome); False means FT includes OT —
    regulation-tie mass splits 50/50 onto the 1 and 2 columns.
    """
    if mu1 is None:
        mu1 = mu * H1_SHARE
    sigma1 = sigma * rho

    # Standardized band edges (continuity correction +-0.5 around 0).
    h_lo, h_hi = (-0.5 - mu1) / sigma1, (0.5 - mu1) / sigma1   # halftime bands
    f_lo, f_hi = (-0.5 - mu) / sigma, (0.5 - mu) / sigma       # full-time bands
    h_bands = {"1": (h_hi, math.inf), "X": (h_lo, h_hi), "2": (-math.inf, h_lo)}
    f_bands = {"1": (f_hi, math.inf), "X": (f_lo, f_hi), "2": (-math.inf, f_lo)}

    grid = {
        (hi, fj): _rect(*h_bands[hi], *f_bands[fj], rho)
        for hi in ("1", "X", "2") for fj in ("1", "X", "2")
    }
    if nine_outcome:
        probs = {f"{h}/{f}": p for (h, f), p in grid.items()}
    else:
        # FT incl OT: no FT draw; ties in regulation resolve ~50/50.
        probs = {}
        for h in ("1", "X", "2"):
            probs[f"{h}/1"] = grid[(h, "1")] + 0.5 * grid[(h, "X")]
            probs[f"{h}/2"] = grid[(h, "2")] + 0.5 * grid[(h, "X")]
    total = sum(probs.values())
    return {k: v / total for k, v in probs.items()}


def detect_nine_outcome(selections: dict[str, float]) -> bool:
    """9-outcome (FT = regulation) iff the board prices an FT draw column."""
    return any(k in selections for k in ("1/X", "X/X", "2/X"))


def mu_from_moneyline(p_home: float, sigma: float) -> Optional[float]:
    """mu = sigma * Phi^-1(p_home) — devig the ML first or mu inherits vig."""
    if not 0.0 < p_home < 1.0:
        return None
    return sigma * _N.inv_cdf(p_home)


def ft_win_prob(mu: float, sigma: float, *, regulation: bool) -> float:
    """P(home wins FT) under the model — regulation excludes the tie band,
    incl-OT splits it 50/50 (used for the multiplier diagnostics)."""
    p_win = 1.0 - _N.cdf((0.5 - mu) / sigma)
    if regulation:
        return p_win
    p_tie = _N.cdf((0.5 - mu) / sigma) - _N.cdf((-0.5 - mu) / sigma)
    return p_win + 0.5 * p_tie
