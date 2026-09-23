"""Lider-Bet combo markets tested against the primitives they are built from.

A combo ("Team 2 win and Total Over 1.5", "1st Half Result or Match Result") is
a bet on a *set* of outcomes. So is every primitive Lider posts — the 1X2, the
double chance, each rung of the totals ladder. Express both as bitmasks over a
small atom space and two exact, model-free tests fall out:

  CONTAINMENT   if set A sits inside set B, A can never pay more than B.
                Odds are directly comparable; no devig, no margin assumption,
                no correlation model, no second book. A violation is a logical
                impossibility, not an opinion.

  COVER         buy a group of bets whose sets union to everything. Every
                outcome pays, so the outlay sum(1/odds) < 1 is locked profit.
                Solved exactly by a DP over the 2^n masks.

Two spaces are scanned:

  TOTAL  (FT result) x (Under/Over @ line)   6 atoms, one space per line
  HTFT   (H1 result) x (FT result)           9 atoms

WHY THIS IS WORTH RUNNING (measured 2026-08-19 on the whole 1380-match board):
Lider's combo markets split cleanly into two behaviours when a leg moves —

    family                      moves when the 1X2 moves
    mt:16:1080 1X2/Total                 97%
    mt:16:2307 DC/Total                 100%
    mt:16:3396 Total/HT-FT              100%     <- derived, never stale
    mt:16:1095 1X2/BTTS                 100%
    ------------------------------------------
    mt:16:1716-1719 not-lose & total     10%     (13.5% baseline: no signal)
    mt:16:3910/4793 H1 win & total        6%
    mt:16:3976/3977 win & BTTS            5%     <- independently maintained

The 6-way grids are computed from their legs and reprice in the same tick, so
they carry nothing (best cover across 30 000 grid cells: 1.0333, zero arbs).
The 2-way Yes/No families are on their own schedule and a leg moving carries
NO information about whether they follow — and every arb found was in that
group. Both are scanned because the grids are free once the payload is parsed
and they make good hedge legs, but the yield is in the 2-way families.

Reference: docs/anomalies-catalog.md, "Lider combo bounds".
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from itertools import product

log = logging.getLogger(__name__)

# ── knobs (env seeds runtime_config, same as every other scan) ───────────────
# Horizon. NOT the 24 h used for the odds detail tier: the arbs measured on
# 2026-08-19 sat 3 days out, because a match nobody is betting yet is exactly
# where a hand-maintained combo block goes unattended. 0 = whole board.
COMBO_HOURS = float(os.environ.get("LIDER_COMBO_HOURS", "96"))
# Minimum locked edge (%) before a cover is worth a row on the tab.
MIN_EDGE_PCT = float(os.environ.get("LIDER_COMBO_MIN_EDGE", "0.5"))
# Minimum free-upgrade (%) before a containment violation is worth a row. Small
# ones are real but not worth acting on, and the odds ladder is coarse enough
# that adjacent rungs routinely differ by 2-3%.
MIN_DOM_PCT = float(os.environ.get("LIDER_COMBO_MIN_DOM", "3.0"))
# Minimum gap (%) between two prices for the SAME outcome set before it is
# worth a row. Exact and model-free, so this can be low.
MIN_DUP_PCT = float(os.environ.get("LIDER_COMBO_MIN_DUP", "8.0"))
# Minimum model EV (%) for a combo_fair row. Higher than the exact tests
# because a fitted distribution carries real error and this is the only check
# here that can be wrong about the world rather than about arithmetic.
MIN_EV_PCT = float(os.environ.get("LIDER_COMBO_MIN_EV", "25.0"))
# Reject the fit outright if it cannot reproduce the posted totals ladder to
# this many probability points on every rung.
MODEL_MAX_LADDER_ERR = float(os.environ.get("LIDER_COMBO_MAX_LADDER_ERR", "0.03"))
_MATCHES_PER_DETAIL = 20
_HTTP_TIMEOUT = 60.0

R = ("1", "X", "2")
DC_MEMBERS = {"1X": ("1", "X"), "X2": ("X", "2"), "12": ("1", "2")}

# ── TOTAL space: (FT result) x (Under/Over) at one line ─────────────────────
T_ATOMS = [(r, s) for r in R for s in ("u", "o")]
TIX = {a: i for i, a in enumerate(T_ATOMS)}
T_FULL = (1 << 6) - 1

# ── HTFT space: (H1 result) x (FT result) ───────────────────────────────────
H_ATOMS = [(h, f) for h in R for f in R]
HIX = {a: i for i, a in enumerate(H_ATOMS)}
H_FULL = (1 << 9) - 1

X12_T = {"FT": "mt:16:500", "H1": "mt:16:602", "H2": "mt:16:619"}
TOT_T = {"FT": "mt:16:502", "H1": "mt:16:600", "H2": "mt:16:622"}
DC_T = "mt:16:503"
X12_OT = {"ot:16:2": "1", "ot:16:1": "X", "ot:16:3": "2"}
TOT_OT = {"ot:16:6": "u", "ot:16:7": "o"}
HTFT_OT = {"ot:16:197": ("1", "1"), "ot:16:192": ("1", "X"), "ot:16:194": ("1", "2"),
           "ot:16:198": ("X", "1"), "ot:16:196": ("X", "X"), "ot:16:191": ("X", "2"),
           "ot:16:193": ("2", "1"), "ot:16:195": ("2", "X"), "ot:16:190": ("2", "2")}

# Grid cells: outcomeId -> (result leg, total side). Read by typeId + outcomeId
# ONLY, never by name — mt:16:3982 and mt:16:3985 carry the identical name
# 'Team 1 win and number of goals: 3-5', so one is mislabelled at source, and
# mt:16:501/1079 differ only by a double space. Same rule as _DETAIL_TYPES.
GRID_1080 = {"ot:16:1527": ("1", "u"), "ot:16:1531": ("1", "o"),
             "ot:16:1530": ("X", "u"), "ot:16:1532": ("X", "o"),
             "ot:16:1528": ("2", "u"), "ot:16:1529": ("2", "o")}
GRID_2307 = {"ot:16:5365": ("1X", "u"), "ot:16:5367": ("1X", "o"),
             "ot:16:5362": ("X2", "u"), "ot:16:5363": ("X2", "o"),
             "ot:16:5366": ("12", "u"), "ot:16:5364": ("12", "o")}
# 2-way Yes/No: typeId -> (period, result leg, total side)
TWOWAY = {
    "mt:16:1716": ("FT", "1X", "u"), "mt:16:1718": ("FT", "1X", "o"),
    "mt:16:1717": ("FT", "X2", "u"), "mt:16:1719": ("FT", "X2", "o"),
    "mt:16:1917": ("FT", "X", "u"),  "mt:16:1918": ("FT", "X", "o"),
    "mt:16:3910": ("H1", "1", "u"),  "mt:16:4793": ("H1", "1", "o"),
    "mt:16:3911": ("H1", "2", "u"),  "mt:16:4794": ("H1", "2", "o"),
    "mt:16:4792": ("H1", "X", "o"),
    "mt:16:4797": ("H2", "1", "u"),  "mt:16:4795": ("H2", "1", "o"),
    "mt:16:4798": ("H2", "2", "u"),  "mt:16:4796": ("H2", "2", "o"),
    "mt:16:4799": ("H2", "X", "o"),
}
# "<result> OR <total side>" unions
UNION = {"mt:16:4134": ("FT", "X", "u"), "mt:16:4133": ("FT", "X", "o"),
         "mt:16:4132": ("FT", "1", "u"), "mt:16:4131": ("FT", "1", "o"),
         "mt:16:4136": ("FT", "2", "u"), "mt:16:4135": ("FT", "2", "o")}
# "Team A Win and score more than 1.5 goals" is EXACTLY the grid cell
# (A, Over 1.5): given A wins, A scored more than B, so A>=2 <=> A+B>=2 <=>
# Over 1.5 (A=1 forces 1-0, total 1, Under). Both directions hold, so this is
# an identity and the book is pricing one event in two places.
WINSCORE = {"mt:16:2679": ("FT", "1", 1.5), "mt:16:2680": ("FT", "2", 1.5)}
# 'H1 result is C OR FT result is C' — the market on the owner's bet slip.
UNION_5132 = {"ot:16:70033": "1", "ot:16:70032": "X", "ot:16:70034": "2"}
# 'Team A wins the 1st half and does NOT win the match'
H1_NOT_FT = {"mt:16:2676": "1", "mt:16:2677": "2"}


def _f(x):
    try:
        v = float(x)
        return v if v > 1.0 else None
    except (TypeError, ValueError):
        return None


def _line(spec):
    if not isinstance(spec, dict):
        return None
    for k in ("total", "special"):
        if k in spec:
            try:
                return float(spec[k])
            except (TypeError, ValueError):
                pass
    return None


def _yes_no(mkt, mts):
    names = {o["id"]: (o.get("name") or "").strip().lower()
             for o in (mts.get(mkt.get("typeId")) or {}).get("outcomeTypes") or []}
    yes = no = None
    for oid, oc in (mkt.get("outcomes") or {}).items():
        v = _f(oc.get("value"))
        if v is None:
            continue
        if names.get(oid) == "yes":
            yes = v
        elif names.get(oid) == "no":
            no = v
    return yes, no


def t_mask(res, side):
    """Atoms of '<result leg> and <total side>' in the TOTAL space."""
    return sum(1 << TIX[(r, side)] for r in DC_MEMBERS.get(res, (res,)))


def h_row(code):
    """H1 result in <code> — a whole row (two rows for a double chance)."""
    return sum(1 << HIX[(h, f)] for h in DC_MEMBERS.get(code, (code,)) for f in R)


def h_col(code):
    """FT result in <code> — a whole column."""
    return sum(1 << HIX[(h, f)] for h in R for f in DC_MEMBERS.get(code, (code,)))


# ── parsing ─────────────────────────────────────────────────────────────────
# ── the book's own wording ───────────────────────────────────────────────────
# Internal shorthand like "1X&O4.5 Yes" is unusable for finding the position on
# the site. Lider's marketType names are the exact strings its search box
# matches, so carry them through: substitute the {1}/{total} placeholder with
# the real line and strip the trailing circled glyphs the UI decorates with.
_DECOR_RE = re.compile(r"[\u2460-\u24ff\u2070-\u209f\u2150-\u218f]")


def _disp(mts, tid, oid=None, line=None):
    """'<market name with the line filled in> - <outcome name>', as shown on
    the site. Falls back to the typeId if the dictionary has no entry."""
    mt = mts.get(tid) or {}
    name = _DECOR_RE.sub("", mt.get("name") or tid).strip()
    name = re.sub(r"\s{2,}", " ", name)
    if line is not None:
        ls = f"{line:g}"
        name = name.replace("{1}", ls).replace("{total}", ls)
        if "{" not in name and ls not in name:
            name = f"{name} {ls}"
    name = re.sub(r"\{[^}]*\}", "", name).strip()
    if oid is None:
        return name
    out = ""
    for o in mt.get("outcomeTypes") or []:
        if o.get("id") == oid:
            out = _DECOR_RE.sub("", o.get("name") or "").strip()
            break
    return f"{name} - {out}" if out else name


def parse_match(match: dict, mts: dict) -> dict:
    """Pull every market on one match that lands in either atom space."""
    from collections import defaultdict
    g = {"x12": defaultdict(dict), "tot": defaultdict(lambda: defaultdict(dict)),
         "dc": {}, "cells": defaultdict(list), "htft": [],
         # book wording for the primitives, so a hedge leg is findable too
         "x12lab": defaultdict(dict), "totlab": defaultdict(lambda: defaultdict(dict)),
         "dclab": {}}
    dcn = {o["id"]: (o.get("name") or "").replace(" ", "").upper()
           for o in (mts.get(DC_T) or {}).get("outcomeTypes") or []}
    for mkt in (match.get("markets") or {}).values():
        tid, ocs = mkt.get("typeId"), mkt.get("outcomes") or {}
        ln = _line(mkt.get("specifier"))
        for per, t in X12_T.items():
            if tid == t:
                for oid, oc in ocs.items():
                    if oid in X12_OT and (v := _f(oc.get("value"))):
                        g["x12"][per][X12_OT[oid]] = v
                        g["x12lab"][per][X12_OT[oid]] = _disp(mts, t, oid)
        for per, t in TOT_T.items():
            if tid == t and ln is not None:
                for oid, oc in ocs.items():
                    if oid in TOT_OT and (v := _f(oc.get("value"))):
                        g["tot"][per][ln][TOT_OT[oid]] = v
                        g["totlab"][per][ln][TOT_OT[oid]] = _disp(mts, t, oid, ln)
        if tid == DC_T:
            for oid, oc in ocs.items():
                if dcn.get(oid) in DC_MEMBERS and (v := _f(oc.get("value"))):
                    g["dc"][dcn[oid]] = v
                    g["dclab"][dcn[oid]] = _disp(mts, DC_T, oid)
        elif tid in ("mt:16:1080", "mt:16:2307") and ln is not None:
            table = GRID_1080 if tid == "mt:16:1080" else GRID_2307
            for oid, oc in ocs.items():
                hit = table.get(oid)
                if hit and (v := _f(oc.get("value"))):
                    g["cells"][("FT", ln)].append(
                        (t_mask(*hit), v, _disp(mts, tid, oid, ln), True))
        elif tid in WINSCORE:
            per, res, fixed = WINSCORE[tid]
            yes, no = _yes_no(mkt, mts)
            m = t_mask(res, "o")
            if yes:
                g["cells"][(per, fixed)].append(
                    (m, yes, f"{_disp(mts, tid)} - Yes", True))
            if no:
                g["cells"][(per, fixed)].append(
                    (T_FULL & ~m, no, f"{_disp(mts, tid)} - No", True))
        elif tid in TWOWAY and ln is not None:
            per, res, side = TWOWAY[tid]
            m = t_mask(res, side)
            lab = _disp(mts, tid, line=ln)
            yes, no = _yes_no(mkt, mts)
            if yes:
                g["cells"][(per, ln)].append((m, yes, f"{lab} - Yes", True))
            if no:
                g["cells"][(per, ln)].append((T_FULL & ~m, no, f"{lab} - No", True))
        elif tid in UNION and ln is not None:
            per, res, side = UNION[tid]
            m = t_mask(res, "u") | t_mask(res, "o")
            m |= sum(1 << TIX[(r, side)] for r in R)
            lab = _disp(mts, tid, line=ln)
            yes, no = _yes_no(mkt, mts)
            if yes:
                g["cells"][(per, ln)].append((m, yes, f"{lab} - Yes", True))
            if no:
                g["cells"][(per, ln)].append((T_FULL & ~m, no, f"{lab} - No", True))
        # ── HTFT space ──
        elif tid == "mt:16:573":
            for oid, oc in ocs.items():
                if oid in HTFT_OT and (v := _f(oc.get("value"))):
                    h, f_ = HTFT_OT[oid]
                    g["htft"].append((1 << HIX[(h, f_)], v, _disp(mts, tid, oid), True))
        elif tid == "mt:16:5132":
            for oid, oc in ocs.items():
                if oid in UNION_5132 and (v := _f(oc.get("value"))):
                    c = UNION_5132[oid]
                    g["htft"].append((h_row(c) | h_col(c), v, _disp(mts, tid, oid), True))
        elif tid in H1_NOT_FT:
            c = H1_NOT_FT[tid]
            other = [x for x in R if x != c]
            yes, _ = _yes_no(mkt, mts)
            if yes:
                m = sum(1 << HIX[(c, f_)] for f_ in other)
                g["htft"].append((m, yes, f"{_disp(mts, tid)} - Yes", True))
    return g


def duplicates(bets, min_gap_pct=0.0):
    """The same outcome set priced twice, at different odds.

    Lider prices one event in several places — "Team 1 Win and score more than
    1.5 goals" is the SAME set as the 1X2/Total grid cell "Over / 1" at 1.5,
    and the double-chance families overlap the DC/Total grid. When the two
    prices disagree the book is contradicting itself with no ambiguity at all:
    the shorter price is its own opinion, so the longer one is an overlay of
    exactly that ratio. Measured on Iwata vs Tokushima, 2026-08-19:

        (1, Over 1.5)   6.90  vs  3.10     +123%
        (2, Over 1.5)   4.10  vs  2.60      +58%
        (X2, Under 1.5) 4.40  vs  3.35      +31%

    This was invisible until now because _dedup collapsed each set to its best
    price before anything looked at it — the detector was deleting its own
    strongest signal.
    """
    by_mask: dict[int, list] = {}
    for m, v, lab, _ex in bets:
        if m:
            by_mask.setdefault(m, []).append((v, lab))
    out = []
    for m, prices in by_mask.items():
        if len(prices) < 2:
            continue
        prices.sort()
        (lo_v, lo_lab), (hi_v, hi_lab) = prices[0], prices[-1]
        gap = (hi_v / lo_v - 1.0) * 100.0
        if gap >= min_gap_pct:
            out.append((hi_lab, hi_v, lo_lab, lo_v, gap))
    return out


# Lider's refusal-to-quote value. NOT a price, and the board says so plainly:
# over 25 743 priced outcomes on 40 live matches, the two MOST COMMON prices
# anywhere were 100.0 (854, 3.3 %) and 101.0 (617, 2.4 %), above 50 there are
# only seven distinct values at all (50/60/70/80/90/100/101), and 101.0 is the
# maximum price on the board. A genuine distribution of beliefs does not have
# its mode at its own ceiling.
#
# Why this matters here rather than being cosmetic: a leg at 101 contributes
# 1/101 = 0.0099 to a cover's outlay — almost nothing — while being the leg
# that COMPLETES it. So a ceiling leg buys the arb its last corner for free,
# and the position only exists if the book will actually take that bet. Three
# of the four combo_cover flags on the live board were completed this way,
# including Barcelona v Paris FC at a reported +10.3 %.
#
# This project has now been caught by the same shape twice before: CrystalBet's
# 100.0 on 88 552 positions ("grid opportunities"), and the pinned longshot
# rungs in the hockey pass. Third time, so it gets a named constant.
CEILING_ODDS = 100.0


def _dedup(bets):
    """One bet per distinct outcome set — keep the longest odds.

    Bets are (mask, odds, label, exact). `exact` means the mask IS the bet's
    winning set inside this space; False means the mask is merely a subset of
    it — the bet also wins on outcomes the mask does not claim.

    Call `duplicates()` BEFORE this if you care about a set priced twice: this
    throws the losing price away.

    Also the single choke point where CEILING_ODDS legs are dropped: both
    bet lists exit through here, so one filter covers the cover DP, the
    containment test and the duplicate scan rather than three.
    """
    best = {}
    for m, v, lab, exact in bets:
        if not m or v >= CEILING_ODDS:
            continue
        if m not in best or v > best[m][0]:
            best[m] = (v, lab, exact)
        elif exact and not best[m][2]:
            # same set, same-or-worse price, but an exact reading — keep the
            # exactness so containment can still use this mask.
            best[m] = (best[m][0], best[m][1], True)
    return [(m, v, lab, ex) for m, (v, lab, ex) in best.items()]


def total_bets(g, per, line):
    """Every bet covering a subset of this (period, line)'s six atoms."""
    out = list(g["cells"].get((per, line), []))
    for r, v in (g["x12"].get(per) or {}).items():
        out.append((t_mask(r, "u") | t_mask(r, "o"), v,
                    (g.get("x12lab", {}).get(per) or {}).get(r) or f"{per} {r}", True))
    if per == "FT":
        for dc, v in g["dc"].items():
            out.append((t_mask(dc, "u") | t_mask(dc, "o"), v,
                    g.get("dclab", {}).get(dc) or f"DC {dc}", True))
    # Ladder monotonicity, and the DIRECTION is what makes a shifted rung a
    # valid hedge: Under M implies Under L whenever M <= L, so a rung at or
    # above the line covers the three Under atoms. Over is the mirror.
    #
    # EXACTNESS, and this is the subtle part. A rung at M != L is a valid COVER
    # leg but its mask is only a SUBSET of what it actually wins: "Over 2.5"
    # inside the 4.5 space is given the three Over-4.5 atoms, yet it also wins
    # on 3 and 4 goals, which live in the Under-4.5 atoms. Understating a
    # cover leg is safe (the position still pays everywhere). Understating a
    # CONTAINMENT operand is not — it manufactures subset relations that do not
    # hold, and it did: the first live run reported 98 dominance rows, of which
    # the top ones were all "FT Over 2.5 is contained in <combo> No" and every
    # one was an artefact of exactly this. Only M == L is exact.
    for M, sides in (g["tot"].get(per) or {}).items():
        if "u" in sides and M >= line:
            out.append((sum(1 << TIX[(r, "u")] for r in R), sides["u"],
                        ((g.get("totlab", {}).get(per) or {}).get(M) or {}).get("u")
                        or f"{per} Under {M:g}", M == line))
        if "o" in sides and M <= line:
            out.append((sum(1 << TIX[(r, "o")] for r in R), sides["o"],
                        ((g.get("totlab", {}).get(per) or {}).get(M) or {}).get("o")
                        or f"{per} Over {M:g}", M == line))
    return _dedup(out)


def htft_bets(g):
    out = list(g["htft"])
    for r, v in (g["x12"].get("H1") or {}).items():
        out.append((h_row(r), v, (g.get("x12lab", {}).get("H1") or {}).get(r) or f"H1 {r}", True))
    for r, v in (g["x12"].get("FT") or {}).items():
        out.append((h_col(r), v, (g.get("x12lab", {}).get("FT") or {}).get(r) or f"FT {r}", True))
    for dc, v in g["dc"].items():
        out.append((h_col(dc), v, g.get("dclab", {}).get(dc) or f"FT DC {dc}", True))
    return _dedup(out)


# ── the two tests ───────────────────────────────────────────────────────────
def min_cover(bets, full, nbits):
    """Cheapest set of bets whose outcomes union to everything.

    Exact DP over 2^nbits masks. Cost is sum(1/odds): for a true partition that
    is the exact outlay, and where the chosen sets overlap the position simply
    returns MORE than 1 on the overlap. So this can never invent an arb — at
    worst it misses a cheaper one.
    """
    INF = float("inf")
    size = 1 << nbits
    dp = [INF] * size
    pick: list = [None] * size
    dp[0] = 0.0
    for mask in range(size):
        if dp[mask] == INF:
            continue
        base = dp[mask]
        for i, (m, v, _lab, _ex) in enumerate(bets):
            nm = mask | m
            if nm == mask:
                continue
            c = base + 1.0 / v
            if c < dp[nm]:
                dp[nm] = c
                pick[nm] = (mask, i)
    if dp[full] == INF:
        return None
    legs, cur = [], full
    seen = 0
    while cur and pick[cur] is not None and seen < nbits + 2:
        prev, i = pick[cur]
        legs.append((bets[i][2], bets[i][1]))
        cur = prev
        seen += 1
    return dp[full], legs[::-1]


def containment(bets):
    """Subsets priced longer than a superset they sit inside — impossible.

    Only EXACT bets take part. A bet whose mask understates what it wins would
    otherwise appear to be a subset of things it is not, and every one of the
    top false positives in the first live run came from that.
    """
    exact = [(m, v, lab) for m, v, lab, ex in bets if ex]
    hits = []
    for (ma, oa, la), (mb, ob, lb) in product(exact, exact):
        if ma == mb or (ma & mb) != ma:
            continue                    # need ma strictly inside mb
        if oa < ob:                     # subset pays LESS than its superset
            hits.append((la, oa, lb, ob, (ob / oa - 1.0) * 100.0))
    return hits


# ── model-based fair pricing ────────────────────────────────────────────────
# Containment and cover only catch what is logically IMPOSSIBLE, and that is a
# very low bar: on Iwata the market the owner was actually pointing at,
# "Team 1 Win and score more than 1.5 goals" @ 6.90, sits comfortably inside
# its Frechet box [0.062, 0.392] and violates nothing. Its fair price is near
# 3.4. To see that at all you need a distribution over scores, not a bound —
# so fit one to the book's OWN 1X2 and totals ladder, then price every combo
# exactly off it. src/soccer_model.py already does the fitting.
def fit_score_model(g, league=None, per="FT"):
    """Fit (lh, la) to this match's devigged 1X2 + main total, return the score
    matrix — or None if the fit does not reproduce the posted ladder.

    The guard matters: a fair price is only as good as the fit under it, and a
    model that cannot reproduce the ladder it was fitted to has no business
    calling another market wrong.
    """
    import numpy as np
    from src import soccer_model as sm

    x = g["x12"].get(per) or {}
    rungs = g["tot"].get(per) or {}
    if len(x) != 3:
        return None
    # HALF-LINES ONLY. Lider posts integer rungs too (Under 2, Under 3), and on
    # those a total of exactly L is a PUSH — the stake comes back, so the two
    # sides do not partition and a proportional devig of them is meaningless.
    # Measured on Iwata: the integer rungs miss the fitted model by -19.9pp and
    # -16.3pp while every half-line lands within 2.4pp. Feeding them to the fit
    # or to the guard throws the whole thing away.
    two_sided = {L: s for L, s in rungs.items()
                 if len(s) == 2 and abs(L - round(L)) > 1e-9}
    if not two_sided:
        return None
    ph, pd, pa = sm.devig([x["1"], x["X"], x["2"]])
    # anchor on the rung nearest 2.5 — the one the book actually balances
    anchor = min(two_sided, key=lambda L: abs(L - 2.5))
    au, ao = two_sided[anchor]["u"], two_sided[anchor]["o"]
    p_under, p_over = sm.devig([au, ao])
    try:
        lh, la, _info = sm.fit_lambdas(ph, pd, pa, anchor, p_over)
    except Exception:
        return None
    if not (0.05 < lh < 8 and 0.05 < la < 8):
        return None
    F = sm.score_matrix(lh, la)
    # ── the guard: reproduce every OTHER rung of the ladder ──
    ii, jj = np.indices(F.shape)
    tot = ii + jj
    worst = 0.0
    for L, sides in two_sided.items():
        pu, po = sm.devig([sides["u"], sides["o"]])
        worst = max(worst, abs(float(F[tot > L].sum()) - po))
    if worst > MODEL_MAX_LADDER_ERR:
        return None
    return F


def _atom_probs(F, line):
    """P of each (result, total side) atom under the fitted score matrix."""
    import numpy as np
    ii, jj = np.indices(F.shape)
    tot = ii + jj
    res = {"1": ii > jj, "X": ii == jj, "2": ii < jj}
    side = {"u": tot < line, "o": tot > line}
    return {(r, s): float(F[res[r] & side[s]].sum()) for r in res for s in side}


def _mask_prob(mask, atom_p):
    return sum(p for a, p in atom_p.items() if mask & (1 << TIX[a]))


# ── flags ───────────────────────────────────────────────────────────────────
def _flag(kind, home, away, league, eid, detail, severity, start=None,
          periods="FT", odds=None):
    """One flag row.

    `odds` is the price of the leg a bettor would actually back — the long side
    of a duplicate, the superset of a containment, the cell being covered. It
    exists so the dashboard can filter on it: a flag whose bettable leg pays 40
    is a longshot, and longshots are where the book's margin is worst (measured
    on the movement store: 16 % margin above 40 % implied probability, 68 % at
    2-5 %). Filtering them out removes noise rather than opportunity.

    Kept as a NUMBER rather than left to be scraped back out of `detail`,
    because the detail is prose and a price is data.
    """
    return {
        "book": "liderbet", "sport": "soccer", "kind": kind,
        "match_label": f"{home} — {away}", "home": home, "away": away,
        "league": league, "cb_event_id": None, "book_event_id": str(eid),
        "start_time": start.isoformat() if start else None,
        "periods": periods,
        "detail": f"[liderbet] {detail}", "severity": round(severity, 1),
        "outcome": None,
        "odds": round(float(odds), 3) if odds is not None else None,
    }


def analyse_match(g, home, away, league, eid, start=None,
                  min_edge=None, min_dom=None, min_dup=None, min_ev=None,
                  model=True) -> list[dict]:
    """All four tests on one parsed match → flag rows.

    Ordered by how hard they are to argue with: duplicate pricing and
    containment are arithmetic, the cover is arithmetic plus a hedge, and the
    model check is the only one that can be wrong about football rather than
    about the book.
    """
    min_edge = MIN_EDGE_PCT if min_edge is None else min_edge
    min_dom = MIN_DOM_PCT if min_dom is None else min_dom
    min_dup = MIN_DUP_PCT if min_dup is None else min_dup
    min_ev = MIN_EV_PCT if min_ev is None else min_ev
    out: list[dict] = []
    F = fit_score_model(g, league) if model else None
    for (per, line) in list(g["cells"]):
        raw = g["cells"].get((per, line), [])
        atom_p = _atom_probs(F, line) if (F is not None and per == "FT") else None
        # A duplicate says one of the two prices is wrong, NOT which. Framing
        # the gap as an overlay on the longer side was plain wrong: measured
        # board-wide it produced 3240 rows, and the biggest were cases like
        # 'X2&O2.5' @ 24 vs 7.8 on a 1.10 favourite, where 24 is the CORRECT
        # price and 7.8 is the bad one — and you cannot lay the bad one. So the
        # long side has to independently clear model fair before this is worth
        # a row; then it carries more evidence than either check alone, because
        # two unrelated methods agree. Without a model there is no way to tell
        # which side is the mistake, so nothing is emitted.
        covered: set[int] = set()
        if atom_p is not None:
            for hi_lab, hi_v, lo_lab, lo_v, gap in duplicates(raw, min_dup):
                m = next((mm for mm, vv, ll, _e in raw if ll == hi_lab and vv == hi_v), None)
                if m is None:
                    continue
                p = _mask_prob(m, atom_p)
                ev = (p * hi_v - 1.0) * 100.0
                if p <= 0.0 or ev <= 0.0:
                    continue
                covered.add(m)
                out.append(_flag(
                    "combo_duplicate", home, away, league, eid,
                    f"{per} line {line:g}: the same outcome is priced twice — "
                    f"'{hi_lab}' @ {hi_v:g} vs '{lo_lab}' @ {lo_v:g} ({gap:.1f}% apart); "
                    f"model fair {1.0 / p:.2f}, so the long side is EV {ev:+.1f}%",
                    ev, start, periods=per, odds=hi_v))
        if atom_p is not None:
            for m, v, lab, _ex in _dedup(raw):
                if m in covered:
                    continue      # a combo_duplicate row already carries this,
                                  # with the book's own second price as evidence
                p = _mask_prob(m, atom_p)
                if p <= 0.0:
                    continue
                ev = (p * v - 1.0) * 100.0
                if ev >= min_ev:
                    out.append(_flag(
                        "combo_fair", home, away, league, eid,
                        f"{per} line {line:g}: '{lab}' @ {v:g} vs model fair "
                        f"{1.0 / p:.2f} (P={p:.3f}) — EV {ev:+.1f}%",
                        ev, start, periods=per, odds=v))
        bl = total_bets(g, per, line)
        if len(bl) < 2:
            continue
        got = min_cover(bl, T_FULL, 6)
        if got and got[0] < 1.0:
            edge = (1.0 / got[0] - 1.0) * 100.0
            if edge >= min_edge:
                legs = "  +  ".join(f"{l} @ {o:g}" for l, o in got[1])
                out.append(_flag(
                    "combo_cover", home, away, league, eid,
                    f"{per} line {line:g}: {legs} — outlay {got[0]:.4f}, "
                    f"locked {edge:.2f}%", edge, start, periods=per,
                    odds=max(o for _l, o in got[1])))
        # A containment row invites you to back the SUPERSET, so the superset
        # has to be worth backing. Without that gate the check fires on pairs
        # where both prices are bad and the gap is just one of them being
        # awful: Eintracht Trier v RB Leipzig posted DC 1X @ 4.50 against a
        # fair 13.31, so "the superset pays 71% more" was true, useless, and
        # itself EV -27%. Same rule as combo_duplicate — no model, no row.
        for la, oa, lb, ob, gain in containment(bl):
            if gain < min_dom or atom_p is None:
                continue
            msup = next((mm for mm, vv, ll, _e in bl if ll == lb and vv == ob), None)
            if msup is None:
                continue
            p_sup = _mask_prob(msup, atom_p)
            ev_sup = (p_sup * ob - 1.0) * 100.0
            if p_sup <= 0.0 or ev_sup <= 0.0:
                continue
            out.append(_flag(
                "combo_dominance", home, away, league, eid,
                f"{per} line {line:g}: '{la}' @ {oa:g} is contained in "
                f"'{lb}' @ {ob:g} — the superset pays {gain:.2f}% more, and is "
                f"EV {ev_sup:+.1f}% against model fair {1.0 / p_sup:.2f}",
                ev_sup, start, periods=per, odds=ob))
    hb = htft_bets(g)
    if len(hb) >= 4:
        got = min_cover(hb, H_FULL, 9)
        if got and got[0] < 1.0:
            edge = (1.0 / got[0] - 1.0) * 100.0
            if edge >= min_edge:
                legs = "  +  ".join(f"{l} @ {o:g}" for l, o in got[1])
                out.append(_flag("combo_cover", home, away, league, eid,
                                 f"HT/FT grid: {legs} — outlay {got[0]:.4f}, "
                                 f"locked {edge:.2f}%", edge, start, periods="HT/FT",
                                 odds=max(o for _l, o in got[1])))
        for la, oa, lb, ob, gain in containment(hb):
            if gain >= min_dom:
                out.append(_flag("combo_dominance", home, away, league, eid,
                                 f"HT/FT: '{la}' @ {oa:g} is contained in '{lb}' "
                                 f"@ {ob:g} — the superset pays {gain:.2f}% more",
                                 gain, start, periods="HT/FT", odds=ob))
    return out


# ── the sweep ───────────────────────────────────────────────────────────────
def scan(hours: float | None = None, min_edge: float | None = None,
         min_dom: float | None = None, min_dup: float | None = None,
         min_ev: float | None = None) -> list[dict]:
    """Fetch Lider's soccer detail payloads and run both tests. Sync — call it
    from a thread, like every other scanner here."""
    from curl_cffi.requests import Session
    from src.scrapers.liderbet import (
        HEADERS, IMPERSONATE, MENU_URL, MATCHDATA_URL, SECTION,
        _menu_nodes, _start_time,
    )
    from src.normalize import is_simulated_league

    hours = COMBO_HOURS if hours is None else hours
    now = datetime.now(tz=timezone.utc)
    cut = now + timedelta(hours=hours) if hours and hours > 0 else None

    s = Session(impersonate=IMPERSONATE)
    menu = s.get(f"{MENU_URL}?lang=en&marketFilter=true",
                 headers=HEADERS, timeout=_HTTP_TIMEOUT).json()["menu"]
    nodes = _menu_nodes(menu)
    name_by_id = {n["id"]: (n.get("name") or "") for n in nodes if n.get("id")}
    parent_of: dict[str, str] = {}
    for parent_id, children in menu.items():
        if isinstance(children, list):
            for n in children:
                if isinstance(n, dict) and n.get("id"):
                    parent_of[n["id"]] = parent_id
    tours = [
        n["id"] for n in nodes
        if n.get("id", "").startswith("t:") and n.get("sectionId") == SECTION["soccer"]
        and n.get("cnt", 0) > 0
        and not is_simulated_league(n.get("name"))
        and not is_simulated_league(name_by_id.get(parent_of.get(n["id"], "")))
    ]
    if not tours:
        return []

    mids: list[str] = []
    meta: dict[str, dict] = {}
    anc: dict = {}
    for i in range(0, len(tours), 40):
        chunk = ",".join(tours[i:i + 40])
        try:
            d = s.get(f"{MATCHDATA_URL}?tourIds={chunk}&lang=en&marketFilter=true",
                      headers=HEADERS, timeout=_HTTP_TIMEOUT).json()["data"]
        except Exception as exc:
            log.warning("lider_combos: matchData chunk failed: %s", exc)
            continue
        anc.update(d.get("ancestors") or {})
        for mid, m in (d.get("matches") or {}).items():
            st = _start_time(m)
            if st is not None and st < now:
                continue
            if cut is not None and (st is None or st > cut):
                continue
            mids.append(mid)
            meta[mid] = m

    flags: list[dict] = []
    for i in range(0, len(mids), _MATCHES_PER_DETAIL):
        batch = mids[i:i + _MATCHES_PER_DETAIL]
        try:
            d = s.get(f"{MATCHDATA_URL}/details?matchIds={','.join(batch)}&lang=en",
                      headers=HEADERS, timeout=_HTTP_TIMEOUT * 2).json()["data"]
        except Exception as exc:
            log.warning("lider_combos: details batch failed: %s", exc)
            continue
        mts = d.get("marketTypes") or {}
        matches = d.get("matches") or {}
        for mid in batch:
            m = matches.get(mid)
            if not m:
                continue
            src = meta.get(mid) or {}
            home = (anc.get(src.get("homeId")) or {}).get("name") or "?"
            away = (anc.get(src.get("awayId")) or {}).get("name") or "?"
            league = (anc.get(src.get("tourId")) or {}).get("name") or None
            try:
                g = parse_match(m, mts)
                flags.extend(analyse_match(g, home, away, league, mid,
                                           _start_time(src), min_edge, min_dom,
                                           min_dup, min_ev))
            except Exception:
                log.exception("lider_combos: match %s failed", mid)

    log.info("lider_combos: %d flags from %d matches (%.0fh horizon)",
             len(flags), len(mids), hours)
    return flags
