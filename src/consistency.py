"""
CB-internal consistency checks ("something looks wrong here").

Unlike src/anomalies.py (which finds bettable ladder violations), this module
finds CONTRADICTIONS between CB's own markets for the same game — across market
types (moneyline vs handicap) and across periods (halves/quarters vs full time).
No sharp book, no Pinnacle: everything here is derivable from CB data alone.

It is a DIAGNOSTIC ("this game's pricing contradicts itself → inspect"), not an
opportunity claim. By design it errs toward over-flagging genuine weirdness; the
thresholds are set well above mild, normal period-to-period variation so that
e.g. a 1st-half ML of 1.6/2.0 next to a full-time 1.55/2.1 does NOT flag.

Why the thresholds are where they are — measured on a clean NBA game (2026-05-31):
  - ML win-prob vs spread-ladder win-prob agreed to <= 0.5pp every period.
  - Period totals summed to the full total within 0.5 points.
  - Period handicaps do NOT simply add (favourite pulls away late): H1+H2 was
    ~0.75 short of FT — so we DON'T flag handicap additivity here (too noisy).
  - Quarter MLs compress toward 0.50 (more variance in a short period); a
    quarter MORE extreme than full time is the weird case.

Checks (all per game, CB-only):
  1. ml_vs_spread       — |P_home(ML) - P_home(spread@line0)| within a period.
  2. favourite_flip     — periods disagree on who's favoured, both decisively.
  3. total_additivity   — period totals don't sum to their parent (H1+H2 vs FT;
                          Q1+Q2 vs H1; Q3+Q4 vs H2; Q1..Q4 vs FT).
  4. quarter_ml_extreme — a quarter's win prob is FURTHER from 0.50 than FT's.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from src.models import Odds
from src.vig import devig_2way  # returns FAIR PROBABILITIES (p_home, p_away)


# ── Thresholds (tunable; defaults sit well above measured normal variation) ────
ML_SPREAD_GAP_PP = 5.0     # ml vs spread win-prob gap (clean game: <=0.5pp)
TOTAL_ADD_PTS = 5.0        # period totals vs parent sum (clean game: ~0.5pt)
DECISIVE_PROB = 0.06       # |P-0.5| past this = a "decisive" favourite (~1.8/2.05)
EXTREME_PP = 6.0           # quarter |P-0.5| exceeding FT's by this many pp


@dataclass(frozen=True)
class ConsistencyFlag:
    sport: str
    league: Optional[str]
    home: str
    away: str
    start_time: Optional[datetime]
    event_id: Optional[str]
    kind: str           # ml_vs_spread | favourite_flip | total_additivity | quarter_ml_extreme
    periods: str        # which period(s) involved, e.g. "FT" or "H1 vs FT"
    detail: str         # human-readable description
    severity: float     # bigger = weirder (pp or points); used for sort/filter

    @property
    def match_label(self) -> str:
        return f"{self.home} — {self.away}"


def _interp_at(points: list[tuple[float, float]], x: float) -> Optional[float]:
    """Linear-interpolate y at x over sorted (x, y) points; clamp at the ends."""
    if not points:
        return None
    pts = sorted(points)
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1 and x1 != x0:
            return y0 + (x - x0) * (y1 - y0) / (x1 - x0)
    return None


def _cross_50(points: list[tuple[float, float]]) -> Optional[float]:
    """x where y crosses 0.5 (the 'center' line of a ladder)."""
    pts = sorted(points)
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if (y0 - 0.5) * (y1 - 0.5) <= 0 and y1 != y0:
            return x0 + (0.5 - y0) * (x1 - x0) / (y1 - y0)
    return None


def _devig_home(home: float, away: float) -> Optional[float]:
    try:
        ph, _ = devig_2way(home, away)
        if 0.0 < ph < 1.0:
            return ph
    except (ValueError, ZeroDivisionError):
        pass
    return None


@dataclass
class _PeriodView:
    """Derived per-(event,period) summary used by the checks."""
    ml_phome: Optional[float] = None          # P(home win) from the moneyline
    spread_pwin: Optional[float] = None        # P(home win) from spread @ line 0
    spread_center: Optional[float] = None      # home_line where P(cover)=0.5
    total_center: Optional[float] = None       # total where P(over)=0.5


def _main_section(rows: list[Odds]) -> list[Odds]:
    """Pick the single section (by Odds.section) with the most rungs, so we never
    mix two sections (incl-OT vs regular, etc.) when deriving a period summary."""
    if not rows:
        return []
    by_section: dict[Optional[str], list[Odds]] = {}
    for o in rows:
        by_section.setdefault(o.section, []).append(o)
    return max(by_section.values(), key=len)


def _build_period_view(period_odds: dict[str, list[Odds]]) -> _PeriodView:
    v = _PeriodView()
    ml = period_odds.get("moneyline")
    if ml:
        for o in ml:
            s = o.selections
            if "home" in s and "away" in s:
                v.ml_phome = _devig_home(s["home"], s["away"])
                if v.ml_phome is not None:
                    break
    sp = _main_section(period_odds.get("spread", []))
    sprows = []
    zero_ph = None
    for o in sp:
        if o.line is None:
            continue
        ph = _devig_home(o.selections["home"], o.selections["away"])
        if ph is not None:
            sprows.append((o.line, ph))
            if abs(o.line) < 1e-6:           # the pick'em (line 0.0) rung
                zero_ph = ph
    if len(sprows) >= 2:
        v.spread_center = _cross_50(sprows)
    # P(home win) is read ONLY from a true pick'em (line 0.0) rung. Its
    # push-on-tie semantics match the Draw-No-Bet moneyline, so the two are
    # directly comparable. We deliberately do NOT interpolate/extrapolate to
    # line 0: extrapolating to a 0 outside a heavy-favourite's ladder (clamping)
    # fabricated large one-sided gaps, and interpolating across ±0.5 lines mixes
    # tie conventions (tie-as-loss vs tie-as-win), worst in tie-prone quarters/
    # halves. No pick'em rung → we can't read P(win) cleanly → skip this check.
    v.spread_pwin = zero_ph
    tt = _main_section(period_odds.get("total", []))
    ttrows = []
    for o in tt:
        if o.line is None:
            continue
        try:
            po, _ = devig_2way(o.selections["over"], o.selections["under"])
        except (ValueError, ZeroDivisionError):
            continue
        if 0.0 < po < 1.0:
            ttrows.append((o.line, po))
    if len(ttrows) >= 2:
        v.total_center = _cross_50(ttrows)
    return v


def find_consistency_flags(
    odds: Iterable[Odds],
    *,
    ml_spread_gap_pp: float = ML_SPREAD_GAP_PP,
    total_add_pts: float = TOTAL_ADD_PTS,
    decisive_prob: float = DECISIVE_PROB,
    extreme_pp: float = EXTREME_PP,
) -> list[ConsistencyFlag]:
    """Find CB-internal contradictions across markets/periods. See module docs."""
    # Group: event_id -> period -> market_type -> [Odds]
    events: dict[Optional[str], dict[str, dict[str, list[Odds]]]] = {}
    meta: dict[Optional[str], Odds] = {}
    for o in odds:
        if o.sport != "basketball":
            continue
        ev = events.setdefault(o.raw_event_id, {})
        ev.setdefault(o.period, {}).setdefault(o.market_type, []).append(o)
        meta.setdefault(o.raw_event_id, o)

    flags: list[ConsistencyFlag] = []
    for eid, periods in events.items():
        m = meta[eid]
        views = {per: _build_period_view(mkts) for per, mkts in periods.items()}

        def mk(kind, per_label, detail, severity):
            flags.append(ConsistencyFlag(
                sport="basketball", league=m.league, home=m.home, away=m.away,
                start_time=m.start_time, event_id=eid,
                kind=kind, periods=per_label, detail=detail,
                severity=round(severity, 2),
            ))

        # 1. ML vs spread win-prob (per period)
        for per, v in views.items():
            if v.ml_phome is not None and v.spread_pwin is not None:
                gap = abs(v.ml_phome - v.spread_pwin) * 100.0
                if gap >= ml_spread_gap_pp:
                    mk("ml_vs_spread", per,
                       f"{per}: ML says P(home)={v.ml_phome*100:.0f}% but the handicap "
                       f"ladder implies {v.spread_pwin*100:.0f}% (gap {gap:.0f}pp)", gap)

        # 2. favourite flip across periods (vs FT)
        ft = views.get("FT")
        if ft and ft.ml_phome is not None:
            ft_edge = ft.ml_phome - 0.5
            for per, v in views.items():
                if per == "FT" or v.ml_phome is None:
                    continue
                edge = v.ml_phome - 0.5
                if (ft_edge * edge < 0
                        and abs(ft_edge) >= decisive_prob
                        and abs(edge) >= decisive_prob):
                    mk("favourite_flip", f"{per} vs FT",
                       f"FT favours {'home' if ft_edge>0 else 'away'} "
                       f"(P_home={ft.ml_phome*100:.0f}%) but {per} favours "
                       f"{'home' if edge>0 else 'away'} (P_home={v.ml_phome*100:.0f}%)",
                       (abs(ft_edge) + abs(edge)) * 100.0)

        # 3. total additivity (parent vs sum of children)
        def tc(p):
            vv = views.get(p)
            return vv.total_center if vv else None
        for parent, kids in (("FT", ("H1", "H2")), ("H1", ("Q1", "Q2")),
                             ("H2", ("Q3", "Q4")), ("FT", ("Q1", "Q2", "Q3", "Q4"))):
            pv = tc(parent)
            kv = [tc(k) for k in kids]
            if pv is not None and all(x is not None for x in kv):
                diff = sum(kv) - pv
                if abs(diff) >= total_add_pts:
                    mk("total_additivity", f"{'+'.join(kids)} vs {parent}",
                       f"total: {'+'.join(kids)}={sum(kv):.1f} vs {parent}={pv:.1f} "
                       f"(off by {diff:+.1f} pts)", abs(diff))

        # 4. quarter ML more extreme than FT
        if ft and ft.ml_phome is not None:
            ft_dev = abs(ft.ml_phome - 0.5) * 100.0
            for per in ("Q1", "Q2", "Q3", "Q4"):
                v = views.get(per)
                if v and v.ml_phome is not None:
                    q_dev = abs(v.ml_phome - 0.5) * 100.0
                    if q_dev - ft_dev >= extreme_pp:
                        mk("quarter_ml_extreme", f"{per} vs FT",
                           f"{per} win-prob {v.ml_phome*100:.0f}% is more lopsided than "
                           f"FT {ft.ml_phome*100:.0f}% (a short period should be closer "
                           f"to 50%)", q_dev - ft_dev)

    flags.sort(key=lambda f: f.severity, reverse=True)
    return flags
