"""
Soccer fair-pricing model — one calibrated object prices every derivative.

Everything reduces to (lambda_home, lambda_away) — equivalently supremacy
S = lh - la and total T = lh + la. From those we build a Dixon-Coles-corrected
Poisson score matrix and, by splitting the goal rate across the two halves, a
pair of half matrices. EVERY soccer market (1X2, HT/FT, Asian handicaps, totals,
BTTS, correct score, result-&-total combos, …) is then a pure functional of
those matrices — one calibration, the whole sheet. Because the combos come off
the joint score matrix (not a product of marginals) they carry the correlation
the books throw away when they multiply legs.

numpy only. The two root-finders (power-devig exponent, the 2-D lambda fit) are
hand-rolled bisection / damped-Newton — no scipy. See docs/anomalies-catalog.md
family E for how this slots in.

Key convention: score matrix `F[i, j]` = P(home scores i, away scores j).
With `dc_rho = 0` the model is a pure independent Poisson, so the convolution of
the two half matrices equals the full-time matrix EXACTLY (sum of independent
Poissons) — every cross-half identity then holds to numerical zero. The DC
correction (default rho = -0.08) perturbs that by a sub-percent amount.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

# ── config (owner-tunable) ────────────────────────────────────────────────────
DEFAULT_N = 12          # score matrix is N×N (goals 0..N-1); ample for T<~6
DC_RHO = -0.08          # Dixon-Coles low-score dependency (draw-boosting)
DEFAULT_SPLIT = 0.44    # fraction of goals scored in H1 (~44% H1 / 56% H2)
# Per-league H1 goal share; falls back to DEFAULT_SPLIT.
LEAGUE_SPLIT: dict[str, float] = {}


def split_for_league(league: Optional[str]) -> float:
    if not league:
        return DEFAULT_SPLIT
    return LEAGUE_SPLIT.get(league.strip().lower(), DEFAULT_SPLIT)


# ══════════════════════════════════════════════════════════════════════════════
# Devig
# ══════════════════════════════════════════════════════════════════════════════
def _implied(odds: Sequence[float]) -> np.ndarray:
    q = np.array([1.0 / o for o in odds], dtype=float)
    return q


def _devig_proportional(odds: Sequence[float]) -> np.ndarray:
    q = _implied(odds)
    return q / q.sum()


def _devig_power(odds: Sequence[float]) -> np.ndarray:
    """Find k with sum(p_i^k) = 1 where p_i = 1/o_i (raw implied). k>1 for an
    over-round book; shrinks longshots more than a flat proportional scale."""
    p = _implied(odds)
    booksum = p.sum()
    if booksum <= 1.0:                       # no (or negative) vig → nothing to solve
        return p / booksum
    lo, hi = 1.0, 1.0
    # bracket: raise hi until sum(p^hi) <= 1
    while np.power(p, hi).sum() > 1.0 and hi < 1e4:
        hi *= 2.0
    for _ in range(200):                     # bisection on k
        mid = 0.5 * (lo + hi)
        s = np.power(p, mid).sum()
        if abs(s - 1.0) < 1e-12:
            break
        if s > 1.0:
            lo = mid
        else:
            hi = mid
    k = 0.5 * (lo + hi)
    out = np.power(p, k)
    return out / out.sum()                    # tidy any residual


def _devig_shin(odds: Sequence[float]) -> np.ndarray:
    """Shin's method: prices reflect a fraction z of insider money. Solve z so
    the recovered true probabilities sum to 1. Standard closed-form per outcome,
    z by bisection."""
    pi = _implied(odds)
    booksum = pi.sum()
    if booksum <= 1.0:
        return pi / booksum

    def probs(z: float) -> np.ndarray:
        # p_i = [sqrt(z^2 + 4(1-z) pi_i^2 / booksum) - z] / (2(1-z))
        root = np.sqrt(z * z + 4.0 * (1.0 - z) * pi * pi / booksum)
        return (root - z) / (2.0 * (1.0 - z))

    lo, hi = 0.0, 0.99                        # sum(probs) decreases in z
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        s = probs(mid).sum()
        if abs(s - 1.0) < 1e-12:
            break
        if s > 1.0:
            lo = mid
        else:
            hi = mid
    z = 0.5 * (lo + hi)
    out = probs(z)
    return out / out.sum()


def devig(odds: Sequence[float], method: str = "auto") -> list[float]:
    """Devig one market's decimal odds → fair probabilities summing to 1.

    method: "proportional" (baseline only), "power", "shin", or "auto"
    (shin for 3+ outcomes, power for 2). Default sharp policy is "auto"; the
    favourite-longshot correction (power/shin) matters most at lopsided prices
    like 1.18 vs 8.90 where proportional over-weights the longshot.
    """
    if method == "auto":
        method = "shin" if len(odds) >= 3 else "power"
    if method == "proportional":
        out = _devig_proportional(odds)
    elif method == "power":
        out = _devig_power(odds)
    elif method == "shin":
        out = _devig_shin(odds)
    else:
        raise ValueError(f"unknown devig method {method!r}")
    return out.tolist()


# ══════════════════════════════════════════════════════════════════════════════
# Score matrices
# ══════════════════════════════════════════════════════════════════════════════
def _poisson_pmf(n: int, lam: float) -> np.ndarray:
    k = np.arange(n)
    fact = np.array([math.factorial(int(i)) for i in k], dtype=float)
    lam = max(lam, 1e-9)
    return np.exp(-lam) * np.power(lam, k) / fact


def _dc_correct(F: np.ndarray, lh: float, la: float, rho: float) -> np.ndarray:
    """Dixon-Coles tau on the four low-score cells, then renormalise."""
    if rho:
        F = F.copy()
        F[0, 0] *= 1.0 - lh * la * rho
        F[0, 1] *= 1.0 + lh * rho
        F[1, 0] *= 1.0 + la * rho
        F[1, 1] *= 1.0 - rho
        F = np.clip(F, 0.0, None)
    return F / F.sum()                         # always sum to 1 (also fixes tail truncation)


def score_matrix(lh: float, la: float, *, n: int = DEFAULT_N,
                 dc_rho: float = DC_RHO) -> np.ndarray:
    """N×N matrix, F[i, j] = P(home=i, away=j). Independent Poisson with a
    Dixon-Coles correction on the 0-0/1-0/0-1/1-1 cells."""
    F = np.outer(_poisson_pmf(n, lh), _poisson_pmf(n, la))
    return _dc_correct(F, lh, la, dc_rho)


def half_matrices(lh: float, la: float, *, split: float = DEFAULT_SPLIT,
                  n: int = DEFAULT_N, dc_rho: float = DC_RHO):
    """(H1, H2) score matrices splitting the goal rate: H1 gets `split` of each
    side's lambda, H2 the rest. Empirically ~44% of goals land in H1."""
    h1 = score_matrix(lh * split, la * split, n=n, dc_rho=dc_rho)
    h2 = score_matrix(lh * (1.0 - split), la * (1.0 - split), n=n, dc_rho=dc_rho)
    return h1, h2


def _conv2d(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """2-D discrete convolution — the joint score of two independent halves."""
    n, m = a.shape[0], b.shape[0]
    out = np.zeros((n + m - 1, n + m - 1))
    for i in range(m):
        for j in range(m):
            w = b[i, j]
            if w:
                out[i:i + n, j:j + n] += w * a
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Result / total helpers (pure functionals of a matrix)
# ══════════════════════════════════════════════════════════════════════════════
def _masks(shape):
    n0, n1 = shape
    ii = np.arange(n0)[:, None]
    jj = np.arange(n1)[None, :]
    return ii, jj


def result_probs(F: np.ndarray) -> tuple[float, float, float]:
    """(home, draw, away) win probabilities from a score matrix."""
    ii, jj = _masks(F.shape)
    return float(F[ii > jj].sum()), float(F[ii == jj].sum()), float(F[ii < jj].sum())


def _total_dist(F: np.ndarray) -> np.ndarray:
    """Distribution over the combined total goals 0..(n0+n1-2)."""
    n0, n1 = F.shape
    out = np.zeros(n0 + n1 - 1)
    ii, jj = _masks(F.shape)
    tot = (ii + jj).ravel()
    np.add.at(out, tot, F.ravel())
    return out


def _over_prob(F: np.ndarray, line: float) -> float:
    td = _total_dist(F)
    k = np.arange(len(td))
    return float(td[k > line].sum())


def _ah_home_prob(F: np.ndarray, hline: float) -> float:
    """Push-adjusted P(home covers the Asian line `hline`). Line 0 == DNB."""
    ii, jj = _masks(F.shape)
    diff = (ii - jj)
    thr = -hline
    win = float(F[diff > thr].sum())
    push = float(F[diff == thr].sum()) if float(thr).is_integer() else 0.0
    lose = 1.0 - win - push
    denom = win + lose
    return win / denom if denom > 0 else 0.0


# ══════════════════════════════════════════════════════════════════════════════
# Fit (market-implied lambdas)
# ══════════════════════════════════════════════════════════════════════════════
def _model_targets(lh, la, n, dc_rho, total_line):
    F = score_matrix(lh, la, n=n, dc_rho=dc_rho)
    h, d, a = result_probs(F)
    ov = _over_prob(F, total_line) if total_line is not None else None
    return h, d, a, ov


def fit_lambdas(
    p_home: float, p_draw: float, p_away: float,
    total_line: Optional[float] = None, p_over: Optional[float] = None,
    *, n: int = DEFAULT_N, dc_rho: float = DC_RHO,
) -> tuple[float, float, dict]:
    """Recover (lh, la) from devigged market probabilities.

    Two anchors when a main total is supplied — match home-win AND over probs.
    Otherwise match the 1X2 shape — home-win AND draw probs. Damped Newton in
    log-lambda space (keeps lambdas positive); numerical Jacobian.
    """
    use_total = total_line is not None and p_over is not None

    def resid(x):
        lh, la = math.exp(x[0]), math.exp(x[1])
        h, d, a, ov = _model_targets(lh, la, n, dc_rho, total_line if use_total else None)
        if use_total:
            return np.array([h - p_home, ov - p_over])
        return np.array([h - p_home, d - p_draw])

    x = np.array([math.log(1.2), math.log(1.2)])   # start ~1.2 each
    r = resid(x)
    converged = False
    iters = 0
    for iters in range(1, 101):
        # numerical Jacobian
        J = np.zeros((2, 2))
        eps = 1e-5
        for c in range(2):
            xp = x.copy(); xp[c] += eps
            J[:, c] = (resid(xp) - r) / eps
        try:
            dx = np.linalg.solve(J, -r)
        except np.linalg.LinAlgError:
            break
        # backtracking on residual norm
        step = 1.0
        rn = np.linalg.norm(r)
        for _ in range(30):
            xn = x + step * dx
            rn_new = np.linalg.norm(resid(xn))
            if rn_new < rn:
                break
            step *= 0.5
        x = x + step * dx
        r = resid(x)
        if np.linalg.norm(r) < 1e-11:
            converged = True
            break

    lh, la = math.exp(x[0]), math.exp(x[1])
    diag = {
        "supremacy": lh - la,
        "total": lh + la,
        "residual": float(np.linalg.norm(r)),
        "converged": converged,
        "iterations": iters,
        "anchor": "1x2+total" if use_total else "1x2",
    }
    return lh, la, diag


# ══════════════════════════════════════════════════════════════════════════════
# The model + its price sheet
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class SoccerModel:
    lh: float
    la: float
    split: float
    n: int
    dc_rho: float
    ft: np.ndarray                 # full-time score matrix (independent Poisson)
    h1: np.ndarray                 # first-half score matrix
    h2: np.ndarray                 # second-half score matrix
    full_from_halves: np.ndarray   # convolution of the halves (HT/FT-consistent FT)
    htft: np.ndarray               # 3×3 grid, rows=HT[1,X,2], cols=FT[1,X,2]
    prices: dict[str, dict] = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)

    def prob(self, key: str) -> Optional[float]:
        e = self.prices.get(key)
        return e["prob"] if e else None

    def odds(self, key: str) -> Optional[float]:
        e = self.prices.get(key)
        return e["odds"] if e else None


_HTFT_LABELS = ("1", "X", "2")


def _htft_grid(h1: np.ndarray, h2: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """3×3 HT/FT grid + the FT matrix implied by the halves' convolution.
    Row = HT result (from H1), Col = FT result (from the full-game convolution)."""
    ii, jj = _masks(h1.shape)
    ht_masks = [(ii > jj), (ii == jj), (ii < jj)]   # HT home / draw / away
    grid = np.zeros((3, 3))
    full = _conv2d(h1, h2)
    for r, mask in enumerate(ht_masks):
        fb = _conv2d(h1 * mask, h2)                 # joint conditioned on HT bucket
        grid[r, 0], grid[r, 1], grid[r, 2] = result_probs(fb)
    return grid, full


def build_model(lh: float, la: float, *, split: float = DEFAULT_SPLIT,
                n: int = DEFAULT_N, dc_rho: float = DC_RHO,
                league: Optional[str] = None,
                diagnostics: Optional[dict] = None) -> SoccerModel:
    """Build all matrices and the full fair-price sheet from (lh, la)."""
    if league is not None:
        split = split_for_league(league)
    ft = score_matrix(lh, la, n=n, dc_rho=dc_rho)
    h1, h2 = half_matrices(lh, la, split=split, n=n, dc_rho=dc_rho)
    grid, full = _htft_grid(h1, h2)
    m = SoccerModel(lh=lh, la=la, split=split, n=n, dc_rho=dc_rho,
                    ft=ft, h1=h1, h2=h2, full_from_halves=full, htft=grid,
                    diagnostics=diagnostics or {})
    _price_sheet(m)
    return m


def _add(prices: dict, key: str, prob: float) -> None:
    prob = float(max(min(prob, 1.0), 0.0))
    prices[key] = {"prob": prob, "odds": (1.0 / prob if prob > 1e-12 else math.inf)}


def _price_sheet(m: SoccerModel) -> None:
    p = m.prices
    ft, h1, h2, grid, full = m.ft, m.h1, m.h2, m.htft, m.full_from_halves

    # FT 1X2 (from the independent FT matrix)
    fh, fd, fa = result_probs(ft)
    _add(p, "ft_1x2:1", fh); _add(p, "ft_1x2:X", fd); _add(p, "ft_1x2:2", fa)
    # Double chance / DNB
    _add(p, "dc:1X", fh + fd); _add(p, "dc:12", fh + fa); _add(p, "dc:X2", fd + fa)
    if fh + fa > 0:
        _add(p, "dnb:1", fh / (fh + fa)); _add(p, "dnb:2", fa / (fh + fa))

    # HT / FT (9 cells) — from the half convolution
    for r, hl in enumerate(_HTFT_LABELS):
        for c, fl in enumerate(_HTFT_LABELS):
            _add(p, f"htft:{hl}/{fl}", grid[r, c])
    # HT 1X2, 2nd-half 1X2
    for lab, prob in zip(_HTFT_LABELS, result_probs(h1)):
        _add(p, f"ht_1x2:{lab}", prob)
    for lab, prob in zip(_HTFT_LABELS, result_probs(h2)):
        _add(p, f"h2_1x2:{lab}", prob)

    # Totals ladders (FT, H1, H2)
    for line in np.arange(0.5, 6.5 + 0.1, 1.0):
        ov = _over_prob(ft, line)
        _add(p, f"ft_total:over_{line:g}", ov)
        _add(p, f"ft_total:under_{line:g}", 1.0 - ov)
    for tag, M in (("h1", h1), ("h2", h2)):
        for line in np.arange(0.5, 3.5 + 0.1, 1.0):
            ov = _over_prob(M, line)
            _add(p, f"{tag}_total:over_{line:g}", ov)
            _add(p, f"{tag}_total:under_{line:g}", 1.0 - ov)

    # Asian handicap ladder incl. quarter lines (quarter = mean of neighbours)
    half_lines = np.arange(-4.0, 4.0 + 0.1, 0.5)
    ah_prob = {round(h, 2): _ah_home_prob(ft, h) for h in half_lines}
    for h in half_lines:
        _add(p, f"ah:home_{h:g}", ah_prob[round(h, 2)])
        _add(p, f"ah:away_{-h:g}", 1.0 - ah_prob[round(h, 2)])
    for h in np.arange(-3.75, 3.75 + 0.1, 0.5):     # quarter lines: -3.75,-3.25,...
        lo, hi = round(h - 0.25, 2), round(h + 0.25, 2)
        if lo in ah_prob and hi in ah_prob:
            q = 0.5 * (ah_prob[lo] + ah_prob[hi])
            _add(p, f"ah:home_{h:g}", q); _add(p, f"ah:away_{-h:g}", 1.0 - q)

    # European 3-way handicap (integer lines applied to home)
    ii, jj = _masks(ft.shape)
    for k in (-3, -2, -1, 1, 2, 3):
        d = (ii + k) - jj
        _add(p, f"eh:{k:+d}:1", float(ft[d > 0].sum()))
        _add(p, f"eh:{k:+d}:X", float(ft[d == 0].sum()))
        _add(p, f"eh:{k:+d}:2", float(ft[d < 0].sum()))

    # Team totals
    home_marg, away_marg = ft.sum(axis=1), ft.sum(axis=0)
    for line in np.arange(0.5, 3.5 + 0.1, 1.0):
        kk = np.arange(len(home_marg))
        _add(p, f"tt_home:over_{line:g}", float(home_marg[kk > line].sum()))
        _add(p, f"tt_home:under_{line:g}", float(home_marg[kk < line].sum()))
        _add(p, f"tt_away:over_{line:g}", float(away_marg[kk > line].sum()))
        _add(p, f"tt_away:under_{line:g}", float(away_marg[kk < line].sum()))

    # BTTS
    p_h0 = float(ft[0, :].sum()); p_a0 = float(ft[:, 0].sum()); p_00 = float(ft[0, 0])
    btts_yes = 1.0 - p_h0 - p_a0 + p_00
    _add(p, "btts:yes", btts_yes); _add(p, "btts:no", 1.0 - btts_yes)

    # Result & total combos (from the matrix, NOT leg products) — main lines
    tot = (ii + jj)
    for line in (1.5, 2.5, 3.5):
        for lab, rmask in (("1", ii > jj), ("X", ii == jj), ("2", ii < jj)):
            over = float(ft[rmask & (tot > line)].sum())
            under = float(ft[rmask & (tot < line)].sum())
            _add(p, f"res_tot:{lab}&over_{line:g}", over)
            _add(p, f"res_tot:{lab}&under_{line:g}", under)
    # BTTS & result combos
    btts_mask = (ii >= 1) & (jj >= 1)
    for lab, rmask in (("1", ii > jj), ("X", ii == jj), ("2", ii < jj)):
        _add(p, f"btts_res:yes&{lab}", float(ft[btts_mask & rmask].sum()))
        _add(p, f"btts_res:no&{lab}", float(ft[(~btts_mask) & rmask].sum()))

    # Correct score grid (0..5 each) + any-other
    acc = 0.0
    for i in range(6):
        for j in range(6):
            v = float(ft[i, j]); acc += v
            _add(p, f"cs:{i}-{j}", v)
    _add(p, "cs:other", max(0.0, 1.0 - acc))

    # Win to nil / clean sheet
    _add(p, "wtn:home", float(ft[1:, 0].sum()))
    _add(p, "wtn:away", float(ft[0, 1:].sum()))
    _add(p, "cleansheet:home", p_a0)         # home keeps a clean sheet ⇔ away=0
    _add(p, "cleansheet:away", p_h0)

    # Totals: multigoals bands, exact goals, odd/even
    td = _total_dist(ft)
    kk = np.arange(len(td))
    for a, b in ((0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6),
                 (1, 3), (2, 4), (0, 2), (0, 3), (2, 5), (1, 4), (4, 6)):
        _add(p, f"multigoal:{a}-{b}", float(td[(kk >= a) & (kk <= b)].sum()))
    for g in range(7):
        _add(p, f"exact:{g}", float(td[g]) if g < len(td) else 0.0)
    _add(p, "exact:7+", float(td[kk >= 7].sum()))
    _add(p, "parity:odd", float(td[kk % 2 == 1].sum()))
    _add(p, "parity:even", float(td[kk % 2 == 0].sum()))

    # Highest scoring half + goal in both halves (independent halves)
    t1, t2 = _total_dist(h1), _total_dist(h2)
    j1 = np.outer(t1, t2)
    a1, b1 = _masks(j1.shape)
    _add(p, "high_half:h1", float(j1[a1 > b1].sum()))
    _add(p, "high_half:h2", float(j1[a1 < b1].sum()))
    _add(p, "high_half:equal", float(j1[a1 == b1].sum()))
    _add(p, "both_halves_goal:yes", float((1.0 - t1[0]) * (1.0 - t2[0])))
    _add(p, "both_halves_goal:no", float(1.0 - (1.0 - t1[0]) * (1.0 - t2[0])))


# ══════════════════════════════════════════════════════════════════════════════
# Convenience: straight from posted 1X2 (+ optional main total)
# ══════════════════════════════════════════════════════════════════════════════
def model_from_market(
    odds_1x2: Sequence[float],
    *, total_line: Optional[float] = None, odds_over_under: Optional[Sequence[float]] = None,
    devig_method: str = "auto", split: float = DEFAULT_SPLIT,
    n: int = DEFAULT_N, dc_rho: float = DC_RHO, league: Optional[str] = None,
) -> SoccerModel:
    """Devig a posted 1X2 (and optional main total), fit lambdas, build the sheet."""
    ph, pd, pa = devig(odds_1x2, devig_method)
    p_over = None
    if total_line is not None and odds_over_under is not None:
        p_over = devig(odds_over_under, "power")[0]
    lh, la, diag = fit_lambdas(ph, pd, pa, total_line, p_over, n=n, dc_rho=dc_rho)
    diag["devig_method"] = devig_method
    return build_model(lh, la, split=split, n=n, dc_rho=dc_rho, league=league,
                       diagnostics=diag)
