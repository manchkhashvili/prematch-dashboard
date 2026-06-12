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
  6. htft_fair          — model-based fair pricing of EVERY HT/FT outcome via
                          a bivariate normal on the (halftime, full-game)
                          margins (src/htft_model.py — joint probability, not
                          a product of marginals). mu comes from CB's own
                          devigged handicap ladder (or ML), sigma per league,
                          rho ~ 0.70. Two signals per outcome:
                          * EDGE — CB's posted price exceeds model fair even
                            before vig (cb >= fair * 1.03): +EV candidate.
                            Soft books template a near-constant HT/FT
                            multiplier while the true one varies (~1.27
                            favorite-1/1 ... ~1.49 dog-2/2), so lopsided
                            lines are the structural sweet spots.
                          * SHAPE — the devigged CB probability disagrees
                            with the model by >= 1.5x on a meaningful
                            outcome: the market's internal shape is off.
  5. htft_combo         — the Halftime/Fulltime 1/1 (and 2/2) price violates a
                          bound implied by its own legs (the H1 and FT 1x2
                          moneylines, regulation time):
                          * dominance: P(1/1) <= min(P(H1=1), P(FT=1)), so
                            odds(1/1) must be >= each leg's odds. A combo
                            SHORTER than its own leg is logically impossible.
                          * correlation: leading at half and winning are
                            positively correlated, so P(1/1) >= P(H1=1)*P(FT=1)
                            and odds(1/1) must be <= odds(H1 1) * odds(FT 1).
                            A combo LONGER than the independent product is
                            priced too generously (the +EV direction). The raw
                            product carries both legs' vig, which only widens
                            the margin a true violation has to clear.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from src import htft_model
from src.models import Odds
from src.vig import devig_2way  # returns FAIR PROBABILITIES (p_home, p_away)


# ── Thresholds (tunable; defaults sit well above measured normal variation) ────
ML_SPREAD_GAP_PP = 5.0     # ml vs spread win-prob gap (clean game: <=0.5pp)
TOTAL_ADD_PTS = 5.0        # period totals vs parent sum (clean game: ~0.5pt)
DECISIVE_PROB = 0.06       # |P-0.5| past this = a "decisive" favourite (~1.8/2.05)
EXTREME_PP = 6.0           # quarter |P-0.5| exceeding FT's by this many pp
HTFT_GAP_PCT = 2.0         # htft combo bound violations smaller than this % are
                           # ignored (rounding / odds-step noise on either side)
HTFT_FAIR_EDGE_PCT = 3.0   # flag when CB's POSTED price beats model fair by
                           # this much — already net of vig, so a real edge;
                           # 3% clears the model's sigma/rho noise (~+-2-3%)
HTFT_SHAPE_RATIO = 1.5     # flag when devigged CB prob vs model prob differs
                           # by this factor on a meaningful outcome
HTFT_FAIR_MAX_ODDS = 20.0  # ignore outcomes the model prices longer than this
                           # (X-row longshots: tiny probs, model error explodes)
HTFT_SHAPE_MIN_PROB = 0.05  # shape check only on outcomes the model gives >=5%


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
            # 2-way rows only: the permissive classifier also captures
            # regulation 1x2 (3-way, has "draw") as htft_combo legs —
            # devig_2way on those would misread P(home).
            if "home" in s and "away" in s and "draw" not in s:
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
    htft_gap_pct: float = HTFT_GAP_PCT,
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

        # 5. HT/FT combo vs its own legs (1/1 and 2/2; raw odds, see module doc)
        combo = _first_htft(periods)
        if combo:
            h1_1x2 = _first_1x2(periods.get("H1", {}))
            ft_1x2 = _first_1x2(periods.get("FT", {}))
            if h1_1x2 and ft_1x2:
                for combo_key, leg_side, who in (
                    ("1/1", "home", "home"), ("2/2", "away", "away"),
                ):
                    c = combo.get(combo_key)
                    l_h1 = h1_1x2.get(leg_side)
                    l_ft = ft_1x2.get(leg_side)
                    if c is None or l_h1 is None or l_ft is None:
                        continue
                    # dominance: combo can't be SHORTER than either leg
                    max_leg = max(l_h1, l_ft)
                    short_pct = (max_leg - c) / c * 100.0
                    if short_pct >= htft_gap_pct:
                        mk("htft_combo", "H1+FT",
                           f"HT/FT {combo_key} @{c:g} is shorter than its own "
                           f"{who} leg (H1 @{l_h1:g} / FT @{l_ft:g}) — the combo "
                           f"can never be more likely than one leg alone "
                           f"(off by {short_pct:.0f}%)", short_pct)
                        continue
                    # correlation: combo can't beat the independent product
                    prod = l_h1 * l_ft
                    long_pct = (c - prod) / prod * 100.0
                    if long_pct >= htft_gap_pct:
                        mk("htft_combo", "H1+FT",
                           f"HT/FT {combo_key} @{c:g} exceeds H1 {who} @{l_h1:g} "
                           f"× FT {who} @{l_ft:g} = {prod:.2f} — legs are "
                           f"positively correlated so the combo should be "
                           f"SHORTER than the product, not {long_pct:.0f}% "
                           f"longer (too generous)", long_pct)

        # 6. HT/FT vs the bivariate-normal fair model (basketball only —
        #    sigma/rho are calibrated to basketball margins)
        if combo and m.sport == "basketball":
            for kind, periods_label, detail, severity in _htft_fair_signals(
                combo, views, m.league,
            ):
                mk(kind, periods_label, detail, severity)

    flags.sort(key=lambda f: f.severity, reverse=True)
    return flags


def _htft_fair_signals(
    combo: dict[str, float],
    views: dict[str, "_PeriodView"],
    league: Optional[str],
) -> list[tuple[str, str, str, float]]:
    """Model-based HT/FT signals for one event (check 6 — see module doc)."""
    ft = views.get("FT")
    if ft is None:
        return []
    sigma = htft_model.sigma_for_league(league)

    # mu: devigged handicap ladder center first (no sigma involved), devigged
    # moneyline second. Both already vig-free — required, or mu inherits
    # the book's overround.
    mu: Optional[float] = None
    if ft.spread_center is not None:
        mu = -ft.spread_center
    elif ft.ml_phome is not None:
        mu = htft_model.mu_from_moneyline(ft.ml_phome, sigma)
    if mu is None:
        return []
    h1 = views.get("H1")
    mu1 = -h1.spread_center if (h1 and h1.spread_center is not None) else None

    nine = htft_model.detect_nine_outcome(combo)
    fair = htft_model.htft_fair_probs(
        mu, mu1, sigma=sigma, rho=htft_model.RHO, nine_outcome=nine,
    )

    # Devig the posted 9-way (or 6-way) proportionally for the shape check.
    inv = {k: 1.0 / v for k, v in combo.items() if v and v > 1.0}
    overround = sum(inv.values())
    shape = (nine and len(inv) >= 7) or (not nine and len(inv) >= 5)

    out: list[tuple[str, str, str, float]] = []
    params = (f"mu={mu:+.1f}" + (f", mu1={mu1:+.1f}" if mu1 is not None else "")
              + f", sigma={sigma:g}, rho={htft_model.RHO:g}, "
              + ("9" if nine else "6") + "-outcome")
    for label, p_fair in fair.items():
        cb = combo.get(label)
        if cb is None or p_fair <= 0:
            continue
        fair_odds = 1.0 / p_fair
        if fair_odds > HTFT_FAIR_MAX_ODDS:
            continue
        # EDGE: posted price beats model fair (already net of vig).
        edge_pct = (cb / fair_odds - 1.0) * 100.0
        if edge_pct >= HTFT_FAIR_EDGE_PCT:
            out.append((
                "htft_fair", "HT/FT",
                f"HT/FT {label} @{cb:g} vs model fair {fair_odds:.2f} "
                f"(+{edge_pct:.0f}% over fair, before their vig — possible "
                f"+EV; {params})", round(edge_pct, 2),
            ))
            continue
        # SHAPE: devigged prob disagrees with the model by a big factor.
        if shape and overround > 0 and p_fair >= HTFT_SHAPE_MIN_PROB:
            p_cb = inv[label] / overround
            ratio = p_cb / p_fair
            if ratio >= HTFT_SHAPE_RATIO or ratio <= 1.0 / HTFT_SHAPE_RATIO:
                off_pct = abs(ratio - 1.0) * 100.0
                out.append((
                    "htft_fair", "HT/FT",
                    f"HT/FT {label} devigs to {p_cb*100:.0f}% but the model "
                    f"says {p_fair*100:.0f}% (x{ratio:.2f}) — market shape "
                    f"off vs model ({params})", round(off_pct, 2),
                ))
    return out


def _first_htft(periods: dict[str, dict[str, list[Odds]]]) -> Optional[dict[str, float]]:
    """Selections of the first htft row on the event (label -> odds)."""
    for rows in (periods.get("FT", {}).get("htft", []),):
        for o in rows:
            if o.selections:
                return o.selections
    return None


def _first_1x2(period_mkts: dict[str, list[Odds]]) -> Optional[dict[str, float]]:
    """Selections of the first REGULATION 3-way moneyline (has a draw price)
    in one period's markets — the legs HT/FT settles against."""
    for o in period_mkts.get("moneyline", []):
        s = o.selections
        if "draw" in s and "home" in s and "away" in s:
            return s
    return None
