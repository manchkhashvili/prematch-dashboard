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
def parse_match(match: dict, mts: dict) -> dict:
    """Pull every market on one match that lands in either atom space."""
    from collections import defaultdict
    g = {"x12": defaultdict(dict), "tot": defaultdict(lambda: defaultdict(dict)),
         "dc": {}, "cells": defaultdict(list), "htft": []}
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
        for per, t in TOT_T.items():
            if tid == t and ln is not None:
                for oid, oc in ocs.items():
                    if oid in TOT_OT and (v := _f(oc.get("value"))):
                        g["tot"][per][ln][TOT_OT[oid]] = v
        if tid == DC_T:
            for oid, oc in ocs.items():
                if dcn.get(oid) in DC_MEMBERS and (v := _f(oc.get("value"))):
                    g["dc"][dcn[oid]] = v
        elif tid in ("mt:16:1080", "mt:16:2307") and ln is not None:
            table = GRID_1080 if tid == "mt:16:1080" else GRID_2307
            for oid, oc in ocs.items():
                hit = table.get(oid)
                if hit and (v := _f(oc.get("value"))):
                    g["cells"][("FT", ln)].append(
                        (t_mask(*hit), v,
                         f"{hit[0]}&{'U' if hit[1]=='u' else 'O'}{ln:g}", True))
        elif tid in WINSCORE:
            per, res, fixed = WINSCORE[tid]
            yes, no = _yes_no(mkt, mts)
            m = t_mask(res, "o")
            if yes:
                g["cells"][(per, fixed)].append((m, yes, f"{res} win & score 2+", True))
            if no:
                g["cells"][(per, fixed)].append((T_FULL & ~m, no, f"not({res} win & score 2+)", True))
        elif tid in TWOWAY and ln is not None:
            per, res, side = TWOWAY[tid]
            m = t_mask(res, side)
            lab = f"{res}&{'U' if side=='u' else 'O'}{ln:g}"
            yes, no = _yes_no(mkt, mts)
            if yes:
                g["cells"][(per, ln)].append((m, yes, f"{lab} Yes", True))
            if no:
                g["cells"][(per, ln)].append((T_FULL & ~m, no, f"{lab} No", True))
        elif tid in UNION and ln is not None:
            per, res, side = UNION[tid]
            m = t_mask(res, "u") | t_mask(res, "o")
            m |= sum(1 << TIX[(r, side)] for r in R)
            lab = f"{res} or {'U' if side=='u' else 'O'}{ln:g}"
            yes, no = _yes_no(mkt, mts)
            if yes:
                g["cells"][(per, ln)].append((m, yes, f"{lab} Yes", True))
            if no:
                g["cells"][(per, ln)].append((T_FULL & ~m, no, f"{lab} No", True))
        # ── HTFT space ──
        elif tid == "mt:16:573":
            for oid, oc in ocs.items():
                if oid in HTFT_OT and (v := _f(oc.get("value"))):
                    h, f_ = HTFT_OT[oid]
                    g["htft"].append((1 << HIX[(h, f_)], v, f"HT/FT {h}/{f_}", True))
        elif tid == "mt:16:5132":
            for oid, oc in ocs.items():
                if oid in UNION_5132 and (v := _f(oc.get("value"))):
                    c = UNION_5132[oid]
                    g["htft"].append((h_row(c) | h_col(c), v, f"{c} in H1 or in match", True))
        elif tid in H1_NOT_FT:
            c = H1_NOT_FT[tid]
            other = [x for x in R if x != c]
            yes, _ = _yes_no(mkt, mts)
            if yes:
                m = sum(1 << HIX[(c, f_)] for f_ in other)
                g["htft"].append((m, yes, f"{c} wins H1, not the match", True))
    return g


def _dedup(bets):
    """One bet per distinct outcome set — keep the longest odds.

    Bets are (mask, odds, label, exact). `exact` means the mask IS the bet's
    winning set inside this space; False means the mask is merely a subset of
    it — the bet also wins on outcomes the mask does not claim.
    """
    best = {}
    for m, v, lab, exact in bets:
        if not m:
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
        out.append((t_mask(r, "u") | t_mask(r, "o"), v, f"{per} {r}", True))
    if per == "FT":
        for dc, v in g["dc"].items():
            out.append((t_mask(dc, "u") | t_mask(dc, "o"), v, f"DC {dc}", True))
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
                        f"{per} Under {M:g}", M == line))
        if "o" in sides and M <= line:
            out.append((sum(1 << TIX[(r, "o")] for r in R), sides["o"],
                        f"{per} Over {M:g}", M == line))
    return _dedup(out)


def htft_bets(g):
    out = list(g["htft"])
    for r, v in (g["x12"].get("H1") or {}).items():
        out.append((h_row(r), v, f"H1 {r}", True))
    for r, v in (g["x12"].get("FT") or {}).items():
        out.append((h_col(r), v, f"FT {r}", True))
    for dc, v in g["dc"].items():
        out.append((h_col(dc), v, f"FT DC {dc}", True))
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


# ── flags ───────────────────────────────────────────────────────────────────
def _flag(kind, home, away, league, eid, detail, severity, start=None, periods="FT"):
    return {
        "book": "liderbet", "sport": "soccer", "kind": kind,
        "match_label": f"{home} — {away}", "home": home, "away": away,
        "league": league, "cb_event_id": None, "book_event_id": str(eid),
        "start_time": start.isoformat() if start else None,
        "periods": periods,
        "detail": f"[liderbet] {detail}", "severity": round(severity, 1),
        "outcome": None,
    }


def analyse_match(g, home, away, league, eid, start=None,
                  min_edge=None, min_dom=None) -> list[dict]:
    """Both tests on one parsed match → flag rows."""
    min_edge = MIN_EDGE_PCT if min_edge is None else min_edge
    min_dom = MIN_DOM_PCT if min_dom is None else min_dom
    out: list[dict] = []
    for (per, line) in list(g["cells"]):
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
                    f"locked {edge:.2f}%", edge, start, periods=per))
        for la, oa, lb, ob, gain in containment(bl):
            if gain >= min_dom:
                out.append(_flag(
                    "combo_dominance", home, away, league, eid,
                    f"{per} line {line:g}: '{la}' @ {oa:g} is contained in "
                    f"'{lb}' @ {ob:g} — the superset pays {gain:.2f}% more",
                    gain, start, periods=per))
    hb = htft_bets(g)
    if len(hb) >= 4:
        got = min_cover(hb, H_FULL, 9)
        if got and got[0] < 1.0:
            edge = (1.0 / got[0] - 1.0) * 100.0
            if edge >= min_edge:
                legs = "  +  ".join(f"{l} @ {o:g}" for l, o in got[1])
                out.append(_flag("combo_cover", home, away, league, eid,
                                 f"HT/FT grid: {legs} — outlay {got[0]:.4f}, "
                                 f"locked {edge:.2f}%", edge, start, periods="HT/FT"))
        for la, oa, lb, ob, gain in containment(hb):
            if gain >= min_dom:
                out.append(_flag("combo_dominance", home, away, league, eid,
                                 f"HT/FT: '{la}' @ {oa:g} is contained in '{lb}' "
                                 f"@ {ob:g} — the superset pays {gain:.2f}% more",
                                 gain, start, periods="HT/FT"))
    return out


# ── the sweep ───────────────────────────────────────────────────────────────
def scan(hours: float | None = None, min_edge: float | None = None,
         min_dom: float | None = None) -> list[dict]:
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
                                           _start_time(src), min_edge, min_dom))
            except Exception:
                log.exception("lider_combos: match %s failed", mid)

    log.info("lider_combos: %d flags from %d matches (%.0fh horizon)",
             len(flags), len(mids), hours)
    return flags
