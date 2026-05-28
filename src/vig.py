"""
Odds conversion and vig removal.

All dashboard math runs on DECIMAL odds. Pinnacle's API returns AMERICAN, so
we convert at ingest with `american_to_decimal`. Books' posted prices contain
vig; the fair price for edge calculations is the no-vig price returned by
`devig_2way` / `devig_3way` — which default to **Shin's method** as of
2026-05-27 (Phase 3.7).

Why Shin instead of proportional?
---------------------------------
Proportional devig (`fair_i = imp_i / sum(imp)`) assumes the book takes equal-
proportional rake from every side. Pinnacle (and most books) take much less
rake from the favorite because almost nobody bets a heavy dog at offered
prices — so they inflate the dog side to compensate for information asymmetry.

On balanced markets (1.91/1.91) proportional and Shin agree to 6+ decimals.
On skewed markets the difference grows fast. Example (1.023/7.31, 11.43% vig):

   method        home fair    away fair    away true prob
   proportional  1.140        8.146        12.28%   ← OVERSTATES the dog
   Shin          1.087       12.556         7.96%   ← matches empirical Pinnacle

If your dashboard used proportional for that game, CrystalBet offering the
dog at 9.00 would flash a +10.5% edge — Shin says the bet is actually -28%.
Shin's method has been validated against Pinnacle football moneylines by
Joseph Buchdahl and is the canonical answer in the sports-betting literature.

Public API
----------
  american_to_decimal(p)        American → decimal odds
  decimal_to_implied_prob(d)    decimal → implied prob (still vigged)
  devig_2way(d1, d2)            ALIAS for devig_2way_shin
  devig_3way(d1, d2, d3)        ALIAS for devig_3way_shin
  devig_2way_shin(d1, d2)       Shin's method, numerically solved
  devig_3way_shin(d1, d2, d3)   3-way Shin
  devig_2way_proportional(...)  legacy proportional — kept for comparison + tests
  devig_3way_proportional(...)  same, 3-way
  fair_decimal(prob)            prob → fair decimal odds
  vig_pct(d1, d2[, d3])         book's overround percentage

Note on extreme skew
--------------------
Shin's numerical solver is stable for typical Pinnacle ranges (vig ≤ 15%,
odds 1.1–50). At very extreme skew (one side >0.99 implied), the solver may
converge slowly or hit numerical edge cases — we fall back to proportional
+ log a warning rather than fail. User policy 2026-05-27: stay in 1.2–5.0
odds range where Shin's assumptions are well-validated empirically.
"""
from __future__ import annotations

import logging
import math
from typing import Sequence

log = logging.getLogger(__name__)


# ── Decimal / implied prob primitives ────────────────────────────────────────
def american_to_decimal(p: float) -> float:
    """
    Convert American moneyline odds to decimal.

    Examples
    --------
    >>> american_to_decimal(-110)
    1.9090909090909092
    >>> american_to_decimal(+150)
    2.5
    """
    if p == 0:
        raise ValueError("American odds of 0 are undefined")
    if p < 0:
        return (100 / -p) + 1
    return (p / 100) + 1


def decimal_to_implied_prob(d: float) -> float:
    """Decimal odds → implied probability (still contains vig if from a book)."""
    if d <= 1.0:
        raise ValueError(f"decimal odds {d!r} must be > 1.0")
    return 1 / d


# ── Proportional devig (legacy / comparison) ─────────────────────────────────
def devig_2way_proportional(d1: float, d2: float) -> tuple[float, float]:
    """
    Proportional devig — each side's implied prob divided by total overround.

    Simpler than Shin but biased on skewed markets: overstates the dog's true
    probability. Kept here so tests and the calc UI can compare methods.

    Examples
    --------
    >>> devig_2way_proportional(1.91, 1.91)
    (0.5, 0.5)
    """
    if d1 <= 1.0 or d2 <= 1.0:
        raise ValueError(f"decimal odds must be > 1.0; got {d1}, {d2}")
    imp1, imp2 = 1 / d1, 1 / d2
    total = imp1 + imp2
    return imp1 / total, imp2 / total


def devig_3way_proportional(d1: float, d2: float, d3: float) -> tuple[float, float, float]:
    """3-way proportional devig. Same bias as 2-way on skewed lines."""
    if d1 <= 1.0 or d2 <= 1.0 or d3 <= 1.0:
        raise ValueError(f"decimal odds must be > 1.0; got {d1}, {d2}, {d3}")
    imp1, imp2, imp3 = 1 / d1, 1 / d2, 1 / d3
    total = imp1 + imp2 + imp3
    return imp1 / total, imp2 / total, imp3 / total


# ── Shin's method (default) ──────────────────────────────────────────────────
def _shin_probs(z: float, imps: Sequence[float], overround: float) -> list[float]:
    """Per-side Shin fair probability given insider proportion z."""
    out: list[float] = []
    for pi in imps:
        # p_i = (sqrt(z² + 4·(1-z)·π_i²/Π) − z) / (2·(1-z))
        inner = z * z + 4 * (1 - z) * pi * pi / overround
        p = (math.sqrt(inner) - z) / (2 * (1 - z))
        out.append(p)
    return out


def _solve_shin_z(imps: Sequence[float]) -> tuple[float, list[float]]:
    """
    Solve for z (insider proportion ∈ [0, 1)) such that the per-side Shin fair
    probabilities sum to 1. Returns (z, [fair_probs]).

    Uses bisection over z ∈ [0, 0.5] — z values above 0.5 are nonsensical
    (would imply >50% insider trading) and never occur in practice.

    Raises ValueError if the solver doesn't converge to a valid set of probs.
    """
    overround = sum(imps)
    if overround <= 1.0:
        # No vig (or negative — broken input). Just normalize, no Shin needed.
        return 0.0, [p / overround for p in imps]

    def sum_at(z: float) -> float:
        return sum(_shin_probs(z, imps, overround))

    lo, hi = 0.0, 0.5
    # Quick check: at z=0 the formula collapses to proportional. If sum at z=0
    # is already ≤ 1 we've got a degenerate input.
    s_lo = sum_at(lo)
    if s_lo <= 1.0:
        # Shouldn't happen with valid book prices (overround > 1 → sum > 1 at z=0).
        # If it does, fall back to proportional.
        return 0.0, [p / overround for p in imps]

    # Bisection. 60 iterations is overkill but cheap.
    for _ in range(60):
        z = (lo + hi) / 2
        s = sum_at(z)
        if s > 1.0:
            lo = z
        else:
            hi = z
    z = (lo + hi) / 2
    probs = _shin_probs(z, imps, overround)
    s = sum(probs)
    # Renormalize tiny residual (always < 1e-10).
    probs = [p / s for p in probs]
    return z, probs


def devig_2way_shin(d1: float, d2: float) -> tuple[float, float]:
    """
    Shin devig for a 2-way market. Falls back to proportional if the solver
    can't produce a valid result (very extreme inputs).

    Examples
    --------
    >>> p1, p2 = devig_2way_shin(1.91, 1.91)
    >>> round(p1, 6), round(p2, 6)
    (0.5, 0.5)
    >>> p1, p2 = devig_2way_shin(1.023, 7.31)
    >>> round(p1 + p2, 10)
    1.0
    """
    if d1 <= 1.0 or d2 <= 1.0:
        raise ValueError(f"decimal odds must be > 1.0; got {d1}, {d2}")
    imps = [1 / d1, 1 / d2]
    try:
        _, probs = _solve_shin_z(imps)
        if not all(0 < p < 1 for p in probs):
            raise ValueError(f"Shin produced invalid probs {probs}")
        return probs[0], probs[1]
    except (ValueError, ZeroDivisionError) as e:
        log.warning("Shin devig failed for (%s, %s): %s — falling back to proportional", d1, d2, e)
        return devig_2way_proportional(d1, d2)


def devig_3way_shin(d1: float, d2: float, d3: float) -> tuple[float, float, float]:
    """
    Shin devig for a 3-way market (soccer 1X2). Same iterative solver as 2-way.
    Falls back to proportional on solver failure.

    Examples
    --------
    >>> p1, p2, p3 = devig_3way_shin(2.10, 3.40, 3.40)
    >>> round(p1 + p2 + p3, 10)
    1.0
    """
    if d1 <= 1.0 or d2 <= 1.0 or d3 <= 1.0:
        raise ValueError(f"decimal odds must be > 1.0; got {d1}, {d2}, {d3}")
    imps = [1 / d1, 1 / d2, 1 / d3]
    try:
        _, probs = _solve_shin_z(imps)
        if not all(0 < p < 1 for p in probs):
            raise ValueError(f"Shin produced invalid probs {probs}")
        return probs[0], probs[1], probs[2]
    except (ValueError, ZeroDivisionError) as e:
        log.warning("Shin devig failed for (%s, %s, %s): %s — falling back to proportional",
                    d1, d2, d3, e)
        return devig_3way_proportional(d1, d2, d3)


# ── Default-method aliases (the rest of the codebase uses these) ─────────────
# Switched 2026-05-27 (Phase 3.7) from proportional → Shin. Callers don't need
# to change anything; they get more accurate fair prices automatically.
def devig_2way(d1: float, d2: float) -> tuple[float, float]:
    """Default 2-way devig — Shin's method. See module docstring for rationale."""
    return devig_2way_shin(d1, d2)


def devig_3way(d1: float, d2: float, d3: float) -> tuple[float, float, float]:
    """Default 3-way devig — Shin's method. See module docstring for rationale."""
    return devig_3way_shin(d1, d2, d3)


# ── Misc helpers ─────────────────────────────────────────────────────────────
def fair_decimal(prob: float) -> float:
    """Convert a (no-vig) probability back to decimal odds — the 'fair line'."""
    if not (0 < prob < 1):
        raise ValueError(f"probability must be in (0, 1); got {prob}")
    return 1 / prob


def vig_pct(d1: float, d2: float, d3: float | None = None) -> float:
    """
    Total overround as a percentage above 100%. Diagnostic only — doesn't depend
    on devig method. 2-way: Pinnacle basketball ~2-4%, CB ~6-10%. 3-way: bigger
    because the book splits its margin across 3 sides.
    """
    odds = [d1, d2] + ([d3] if d3 is not None else [])
    if any(d <= 1.0 for d in odds):
        raise ValueError(f"decimal odds must be > 1.0; got {odds}")
    return (sum(1 / d for d in odds) - 1) * 100
