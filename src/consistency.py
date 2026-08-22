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
  4d. ot_vs_regulation  — American football only. CB posts BOTH a regulation
                          3-way ("Main result", with a real tie leg) and an
                          incl-overtime 2-way ("Winner (incl. overtime)") on the
                          same period. P(win incl OT) must lie between P(win in
                          regulation) and P(win) + P(tie); outside that box the
                          pair is arithmetically impossible, inside it the
                          coin-flip point estimate still has to roughly hold.
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
from src.vig import devig_2way, devig_3way  # FAIR PROBABILITIES (devigged)


# ── Thresholds (tunable; defaults sit well above measured normal variation) ────
ML_SPREAD_GAP_PP = 5.0     # ml vs spread win-prob gap (clean game: <=0.5pp)
PICKEM_LOCK_PCT = 0.3      # min locked % for a pickem_arb row
_PICKEM_MAX_SKEW_SEC = 120.0  # both legs must come from the same fetch
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
# Bettable-range gate (owner 2026-06-12): HT/FT flags only fire when CB's
# POSTED price sits inside this range — shorter than 1.15 isn't worth betting,
# longer than 4.5 is longshot territory where flags were noise.
HTFT_ODDS_MIN = 1.15
HTFT_ODDS_MAX = 4.5
# Sports the consistency engine evaluates. Basketball (its original home),
# soccer (1X2 + HT/FT), and tennis (set winners vs match winner — see
# set_vs_match below; tennis detail pages carry 15 markets that are all
# functions of the same four best-of-3 outcomes).
CONSISTENCY_SPORTS = ("basketball", "soccer", "tennis", "americanfootball")

# ot_vs_regulation (American football): CB prices the SAME game twice at full
# time — a 3-way REGULATION result ("Main result": 1 / X / 2, the tie leg
# around 13.2) and a 2-way INCLUDING-OVERTIME winner ("Winner (incl.
# overtime)"). The two are linked by an identity with no model in it:
#
#     P(home incl OT) = P(home reg) + P(tie reg) * P(home wins OT | tie)
#
# Since the conditional lives in [0, 1], the incl-OT probability is BOXED:
#     P(home reg)  <=  P(home incl OT)  <=  P(home reg) + P(tie reg)
# Stepping outside that box is not an aggressive price, it is an impossible
# one: below the floor says overtime makes a team LESS likely to win; above
# the ceiling says it wins more often than "win in regulation or tie" allows.
#
# OT_BOX_PP is how far outside the box a price must sit before flagging —
# pure devig slack, since the two markets carry independent vig (the 3-way's
# longshot tie leg is the noisier of the pair).
#
# Calibrated on the WHOLE live CB board, 2026-08-12: 177 games, of which 50
# (event, period) pairs post both markets. Residual distributions, where a
# POSITIVE value means "outside the box":
#     floor (p_reg − q_ot)        p50 −2.39   p90 +0.24   max +4.26
#     ceiling (q_ot − p_reg−p_tie) p50 −1.23  p90 +0.95   max +5.08
# So the ordinary case sits comfortably INSIDE a box only ~3.6pp wide, and
# 3.0pp puts the trigger past the 90th percentile in both directions. That
# flagged 3 of the 50 pairs; all three were inspected by hand and are real
# (e.g. Jacksonville-Cleveland: regulation 1.35/12.8/3.15 → 68 % + 5 % tie,
# but incl-OT 1.19/3.55 → 78 %, which no overtime record can produce).
#
# CAVEAT worth knowing before retuning: the size of these residuals depends on
# the devig model. src.vig uses a power devig, which pushes more vig onto the
# longshot tie leg than a proportional one would, so proportional devigging
# shrinks the same three cases by ~1-2pp. 3.0 is calibrated against the power
# devig the rest of the system uses; it is not a model-free constant.
OT_BOX_PP = 3.0
# Soft check: the point estimate. Treating overtime as a coin flip gives
# P(home incl OT) ~ P(home reg) + P(tie reg)/2. Real NFL overtime is close to
# even (possession rules cut the coin-toss edge), and the tie leg is small
# (~5 % of the book on NFL, matching the ~6-7 % of games that actually reach
# overtime), so the whole correction is a couple of pp. Measured |gap| on the
# same board: p90 3.29, max 7.36 — so 8.0 fires on nothing ordinary and exists
# to catch a pair that stays inside the box while still disagreeing wildly.
OT_COINFLIP_PP = 8.0

# set_vs_match: how far the per-set win probability implied by the MATCH price
# may sit from the one the book posts on the FIRST SET, in percentage points.
# Both describe the same player's per-set strength, so a real gap is a genuine
# contradiction rather than a modelling choice.
#
# Calibrated over ALL 525 checkable live tennis matches (2026-08-11), not a
# sample — an early 130-match sample only covered CB's tighter naming scheme and
# suggested 8.0, but the full board runs looser: p50 2.09pp, p90 6.13, p99 8.48,
# max 9.32. 10.0 therefore sits just above every observation on a full board
# (zero soft flags) while the case that prompted the check measured 21-26pp.
SET_MATCH_PP = 10.0

# The HARD order rule — P(match) must not sit below P(set 1) — only carries
# information when the player is a CLEAR favourite. At p_set = 0.50 the match
# probability is also 0.50, so there is no cushion and a one-tick pricing
# difference flips which player is "favourite" between the two markets. That
# produced 15 bogus violations on the full board, all near-even matches such as
# match 1.70/1.80 against set 1.80/1.70 (a 2.4pp "impossibility").
# At p_set = 0.60 the model gives a 4.8pp cushion, so require both a real
# favourite and a violation big enough to clear devig noise.
SET_MATCH_HARD_MIN_FAV = 0.60
SET_MATCH_HARD_MIN_PP = 3.0


def _match_prob_from_set(p: float) -> float:
    """P(win a best-of-3) given a constant per-set win probability p.

    Win 2-0 (p^2) or 2-1 (two orderings of one loss): p^2 + 2*p^2*(1-p)
    = p^2 * (3 - 2p). Sets are treated as independent and identically
    distributed — the standard first-order tennis model. It is an approximation
    (serve order and momentum matter), which is exactly why the tolerance is a
    generous 8pp rather than something tight.
    """
    return p * p * (3.0 - 2.0 * p)


def _set_prob_from_match(p_match: float) -> Optional[float]:
    """Invert _match_prob_from_set by bisection — it is strictly increasing on
    [0,1], so the root is unique. Returns None outside the open interval."""
    if not 0.0 < p_match < 1.0:
        return None
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if _match_prob_from_set(mid) < p_match:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


@dataclass(frozen=True)
class ConsistencyFlag:
    sport: str
    league: Optional[str]
    home: str
    away: str
    start_time: Optional[datetime]
    event_id: Optional[str]
    kind: str           # ml_vs_spread | favourite_flip | total_additivity | quarter_ml_extreme | htft_*
    periods: str        # which period(s) involved, e.g. "FT" or "H1 vs FT"
    detail: str         # human-readable description
    severity: float     # bigger = weirder (pp or points); used for sort/filter
    # HT/FT checks: the outcome label ("1/1", "2/2", ...). Gives the flag a
    # stable identity across scans even though `detail` carries live odds —
    # app.py keys first_seen carry-over on (kind, event, periods, outcome).
    outcome: Optional[str] = None

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
    ml_phome: Optional[float] = None          # P(home win) from the 2-way moneyline
    ml_phome3: Optional[float] = None          # P(home win) from a 3-way 1X2 (soccer)
    ml_pdraw3: Optional[float] = None          # P(draw/tie) from that same 3-way
    spread_pwin: Optional[float] = None        # P(home win) from spread @ line 0
    spread_center: Optional[float] = None      # home_line where P(cover)=0.5
    total_center: Optional[float] = None       # total where P(over)=0.5

    @property
    def home_winprob(self) -> Optional[float]:
        """Devigged P(home) — the 2-way ML where present, else the 3-way 1X2
        (home / (home+away+draw)). Lets HT-vs-FT compare across sports."""
        return self.ml_phome if self.ml_phome is not None else self.ml_phome3

    @property
    def ml_pwin_nodraw(self) -> Optional[float]:
        """P(home wins | the game is not drawn) — the basis a pick'em rung and a
        Draw-No-Bet actually settle on.

        A 2-way ML is already draw-free, so it passes through. A 3-way 1X2 has
        to have the draw taken OUT before it can be compared to a line-0 rung;
        comparing the raw 3-way P(home) against a pick'em is a category error,
        because the pick'em voids the draw rather than losing to it.

        This is why the check was silent on every soccer game: `ml_phome` is
        only ever filled from a 2-way moneyline, soccer's main result is 3-way,
        so `ml_phome` stayed None and the comparison was skipped outright.
        Measured on CrystalBet, Lomza II v Ruch Wysokie (2026-08-23): the 1X2
        implies P(home | no draw) = 30.3% while the posted Asian Handicap 0.0
        implies 13.2% — a 17.0pp disagreement that produced zero flags.
        """
        if self.ml_phome is not None:
            return self.ml_phome
        if self.ml_phome3 is not None and self.ml_pdraw3 is not None:
            den = 1.0 - self.ml_pdraw3
            if den > 1e-9:
                p = self.ml_phome3 / den
                if 0.0 < p < 1.0:
                    return p
        return None


def _main_section(rows: list[Odds]) -> list[Odds]:
    """Pick the single section (by Odds.section) with the most rungs, so we never
    mix two sections (incl-OT vs regular, etc.) when deriving a period summary."""
    if not rows:
        return []
    by_section: dict[Optional[str], list[Odds]] = {}
    for o in rows:
        by_section.setdefault(o.section, []).append(o)
    return max(by_section.values(), key=len)



def _pickem_locks(period_odds: dict, per: str, min_pct: float = 0.3) -> list[tuple]:
    """Locked positions across a 3-way moneyline and a pick'em (line-0) rung.

    The pick'em VOIDS the draw, so backing it alongside the draw and the far
    side of the moneyline is a complete cover in which the draw branch pays the
    pick'em stake back on top of the draw's own return:

        stake x on pick'em home @ P    home  -> P*x
        stake y on the draw     @ D    draw  -> x + D*y      (x is returned)
        stake z on away ML      @ A    away  -> A*z

    Equalise all three to 1 and the outlay is x + y + z with y only needing to
    make up (1 - x), not the whole unit. Ignore the push and you compute
    sum(1/odds), overstate the cost, and miss the arb entirely.

    Returns (detail, profit_pct, outcome_label) per lockable side.
    """
    out: list[tuple] = []
    ml3 = ml_at = None
    for o in period_odds.get("moneyline", []):
        s = o.selections
        if {"home", "draw", "away"} <= set(s):
            ml3, ml_at = s, o.fetched_at
            break
    if not ml3:
        return out
    pick = pick_at = None
    for o in period_odds.get("spread", []):
        if o.line is not None and abs(o.line) < 1e-6 and {"home", "away"} <= set(o.selections):
            pick, pick_at = o.selections, o.fetched_at
            break
    if not pick:
        return out
    # SAME INSTANT ONLY. Two prices captured at different times are not a
    # position you can take, and pairing them manufactures arbs wholesale:
    # replaying this over the local tick store (whose `latest` keeps each
    # market's own last-seen tick) produced 21 "locks" of which ALL 21 had the
    # two legs more than 5 minutes apart — median 1.6 days, max 13. In
    # production the check runs on one poll's odds so they always match, but
    # a cached or carried-over rung would otherwise slip straight through.
    if ml_at is not None and pick_at is not None:
        try:
            if abs((ml_at - pick_at).total_seconds()) > _PICKEM_MAX_SKEW_SEC:
                return out
        except (TypeError, AttributeError):
            pass
    D = ml3.get("draw")
    if not D or D <= 1.0:
        return out

    for side, far in (("home", "away"), ("away", "home")):
        P, A = pick.get(side), ml3.get(far)
        if not P or not A or P <= 1.0 or A <= 1.0:
            continue
        x = 1.0 / P                      # pick'em stake, returns 1 on a win
        z = 1.0 / A                      # far side of the ML
        y = (1.0 - x) / D                # the draw only has to cover 1 - x
        if y < 0:
            continue
        outlay = x + y + z
        if outlay <= 0:
            continue
        profit = (1.0 / outlay - 1.0) * 100.0
        if profit < min_pct:
            continue
        naive = 1.0 / P + 1.0 / D + 1.0 / A
        out.append((
            f"{per}: pick'em {side} @ {P:g} + draw @ {D:g} + {far} @ {A:g} — "
            f"the pick'em pushes on the draw, so the outlay is {outlay:.4f} "
            f"(naive {naive:.4f}), locking {profit:.2f}%. "
            f"Stakes {x/outlay:.3f} / {y/outlay:.3f} / {z/outlay:.3f} per unit.",
            profit, f"pickem_{side}"))
    return out


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
        # 3-way 1X2 (soccer FT / 1st-half result): devigged P(home) over all
        # three outcomes — used by the HT-vs-FT divergence check.
        for o in ml:
            s = o.selections
            if {"home", "draw", "away"} <= set(s):
                try:
                    ph, pd, _ = devig_3way(s["home"], s["draw"], s["away"])
                    if 0.0 < ph < 1.0:
                        v.ml_phome3 = ph
                        v.ml_pdraw3 = pd if 0.0 <= pd < 1.0 else None
                        break
                except (ValueError, ZeroDivisionError):
                    pass
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
    pickem_lock_pct: float = PICKEM_LOCK_PCT,
    total_add_pts: float = TOTAL_ADD_PTS,
    decisive_prob: float = DECISIVE_PROB,
    extreme_pp: float = EXTREME_PP,
    htft_gap_pct: float = HTFT_GAP_PCT,
    ht_set_match_pp: float = SET_MATCH_PP,
    ot_box_pp: float = OT_BOX_PP,
    ot_coinflip_pp: float = OT_COINFLIP_PP,
) -> list[ConsistencyFlag]:
    """Find CB-internal contradictions across markets/periods. See module docs."""
    # Group: event_id -> period -> market_type -> [Odds]
    events: dict[Optional[str], dict[str, dict[str, list[Odds]]]] = {}
    meta: dict[Optional[str], Odds] = {}
    for o in odds:
        if o.sport not in CONSISTENCY_SPORTS:
            continue
        ev = events.setdefault(o.raw_event_id, {})
        ev.setdefault(o.period, {}).setdefault(o.market_type, []).append(o)
        meta.setdefault(o.raw_event_id, o)

    flags: list[ConsistencyFlag] = []
    for eid, periods in events.items():
        m = meta[eid]
        views = {per: _build_period_view(mkts) for per, mkts in periods.items()}

        def mk(kind, per_label, detail, severity, outcome=None):
            flags.append(ConsistencyFlag(
                sport=m.sport, league=m.league, home=m.home, away=m.away,
                start_time=m.start_time, event_id=eid,
                kind=kind, periods=per_label, detail=detail,
                severity=round(severity, 2), outcome=outcome,
            ))

        # 1. ML vs spread win-prob (per period)
        for per, v in views.items():
            pml = v.ml_pwin_nodraw
            if pml is not None and v.spread_pwin is not None:
                gap = abs(pml - v.spread_pwin) * 100.0
                if gap >= ml_spread_gap_pp:
                    basis = ("no-draw basis" if v.ml_phome is None else "2-way ML")
                    mk("ml_vs_spread", per,
                       f"{per}: ML says P(home)={pml*100:.0f}% ({basis}) but the "
                       f"pick'em rung implies {v.spread_pwin*100:.0f}% "
                       f"(gap {gap:.0f}pp)", gap)

        # 1b. the same disagreement, priced. A pick'em / Draw-No-Bet PUSHES on a
        #     draw, so a three-leg cover costs less than the naive sum of its
        #     inverse odds and an arb can hide behind that. On the CrystalBet
        #     game above the naive read was 1/5.25 + 1/5.45 + 1/1.55 = 1.0191,
        #     "no arb", while the push-aware outlay is 0.9842 = +1.61% locked.
        for per, mkts in periods.items():
            for flag in _pickem_locks(mkts, per, min_pct=pickem_lock_pct):
                mk("pickem_arb", per, flag[0], flag[1], outcome=flag[2])

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

        # (removed 2026-08-03) 4b. ht_vs_ft_divergence — flagged |P_home(H1) −
        # P_home(FT)| >= 13pp. Deleted because it tested no relation: a large
        # HT→FT gap is what a goal model REQUIRES, not a contradiction. Half-time
        # carries far more draw mass than full time, so a favourite's win-prob is
        # always compressed at the break. Measured over its own 1151 historical
        # flags: ZERO fired on a balanced match (FT 35-65%) — the FT distribution
        # was perfectly bimodal (all flags at FT <=29% or >=70%), i.e. it detected
        # "this game has a big favourite". Severity was the raw gap, so it sorted
        # the dashboard by favourite strength. It was also 26% of ALL consistency
        # flags (1151/4452), crowding out the real ones. The genuine HT<->FT
        # relationship is already modelled properly by htft_fair below (bivariate
        # normal on the two margins, rho 0.70, mu from CB's own devigged ladder) —
        # a raw percentage-point gap is the crude, wrong version of that.

        # 4c. TENNIS: the first-set price vs the match price.
        #
        # A best-of-3 match is won by taking 2 sets, so with per-set win
        # probability p the match probability is p^2*(3-2p) — strictly ABOVE p
        # whenever p > 0.5, because you may lose the opening set and still win.
        # That gives one hard structural rule and one quantitative one:
        #
        #   HARD  : a player favoured to win set 1 (p_set > 0.5) must be at least
        #           as likely to win the MATCH. P(match) < P(set 1) is impossible,
        #           not merely aggressive.
        #   SOFT  : invert the match price to a per-set probability and compare it
        #           with the posted first-set probability. Both are the same
        #           quantity, so a gap beyond SET_MATCH_PP is a contradiction.
        #
        # Real case that prompted this (CB, ITF Campos Do Jordao women, 2026-08-11):
        # match 1.40/2.35 -> P(win) 0.627, first set 1.11/4.30 -> P(win) 0.795.
        # The match was priced BELOW its own first set by 16.8pp, and the two
        # implied per-set numbers were 0.585 vs 0.795 — 21pp apart.
        if m.sport == "tennis":
            h1v, ftv = views.get("H1"), views.get("FT")
            if h1v and ftv:
                p_set, p_ft = h1v.home_winprob, ftv.home_winprob
                if p_set is not None and p_ft is not None:
                    # orient onto whichever player the SET market favours, so the
                    # rule is always stated about a favourite
                    ps, pm = (p_set, p_ft) if p_set >= 0.5 else (1 - p_set, 1 - p_ft)
                    if (pm < ps and ps >= SET_MATCH_HARD_MIN_FAV
                            and (ps - pm) * 100.0 >= SET_MATCH_HARD_MIN_PP):
                        mk("tennis_set_match", "H1 vs FT",
                           f"first-set favourite {ps*100:.0f}% is only {pm*100:.0f}% "
                           f"to win the MATCH — impossible in a best-of-3, where "
                           f"losing the opening set still leaves a route to victory",
                           (ps - pm) * 100.0)
                    else:
                        implied = _set_prob_from_match(pm)
                        if implied is not None:
                            gap = abs(implied - ps) * 100.0
                            if gap >= ht_set_match_pp:
                                mk("tennis_set_match", "H1 vs FT",
                                   f"match price implies a {implied*100:.0f}% per-set "
                                   f"favourite but the first set is priced at "
                                   f"{ps*100:.0f}% — {gap:.0f}pp apart on the same "
                                   f"quantity", gap)

        # 4d. AMERICAN FOOTBALL: the incl-OT winner vs the regulation 1X2.
        #
        # Both markets are posted on the same CB detail page for the same
        # period, and they are tied by an identity rather than a model:
        # winning "including overtime" means winning in regulation OR tying
        # and then winning the extra period. So the incl-OT probability can
        # never fall below the regulation win probability, and can never
        # exceed regulation-win-plus-tie. See OT_BOX_PP above.
        #
        # Applied per period, not just FT — CB ships "Main result" at FT and
        # "1st Half Result" at H1, each alongside its own 2-way price.
        if m.sport == "americanfootball":
            for per, v in views.items():
                q = v.ml_phome            # 2-way, includes overtime
                p = v.ml_phome3           # 3-way, regulation only
                d = v.ml_pdraw3
                if q is None or p is None or d is None:
                    continue
                floor_pp = (p - q) * 100.0          # >0 → below the floor
                ceil_pp = (q - (p + d)) * 100.0     # >0 → above the ceiling
                if floor_pp >= ot_box_pp:
                    mk("ot_vs_regulation", per,
                       f"{per}: home wins {p*100:.0f}% in regulation but only "
                       f"{q*100:.0f}% including overtime — overtime cannot take "
                       f"away a regulation win ({floor_pp:.0f}pp below the floor)",
                       floor_pp)
                elif ceil_pp >= ot_box_pp:
                    mk("ot_vs_regulation", per,
                       f"{per}: home wins {q*100:.0f}% including overtime, more "
                       f"than winning ({p*100:.0f}%) or tying ({d*100:.0f}%) in "
                       f"regulation combined — impossible even if it won every "
                       f"overtime ({ceil_pp:.0f}pp above the ceiling)", ceil_pp)
                else:
                    # inside the box — check the point estimate (OT ~ coin flip)
                    gap = abs(q - (p + d / 2.0)) * 100.0
                    if gap >= ot_coinflip_pp:
                        mk("ot_vs_regulation", per,
                           f"{per}: regulation 1X2 ({p*100:.0f}/{d*100:.0f}/"
                           f"{(1-p-d)*100:.0f}) implies {(p+d/2)*100:.0f}% "
                           f"including overtime if the extra period were even, "
                           f"but the incl-OT winner is priced at {q*100:.0f}% "
                           f"({gap:.0f}pp apart)", gap)

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
                    if not HTFT_ODDS_MIN <= c <= HTFT_ODDS_MAX:
                        continue  # outside the bettable range — not actionable
                    # dominance: combo can't be SHORTER than either leg
                    max_leg = max(l_h1, l_ft)
                    short_pct = (max_leg - c) / c * 100.0
                    if short_pct >= htft_gap_pct:
                        mk("htft_combo", "H1+FT",
                           f"HT/FT {combo_key} @{c:g} is shorter than its own "
                           f"{who} leg (H1 @{l_h1:g} / FT @{l_ft:g}) — the combo "
                           f"can never be more likely than one leg alone "
                           f"(off by {short_pct:.0f}%)", short_pct,
                           outcome=combo_key)
                        continue
                    # correlation-adjusted fair: the naive product (l_h1 × l_ft)
                    # over-states the fair odds — conditioning on the HT lead
                    # makes the FT leg SHORTER than its unconditional price, so the
                    # true combo is shorter than the product. Approximate the
                    # conditional FT leg by halving its margin over evens (owner's
                    # model 2026-06-21): fair = HT_leg × (1 + (FT_leg − 1)/2),
                    # clamped at the longest leg (can't beat dominance). A combo
                    # longer than that fair is over-generous (the +EV direction).
                    fair = max(l_h1 * (1.0 + (l_ft - 1.0) / 2.0), l_h1, l_ft)
                    long_pct = (c - fair) / fair * 100.0
                    if long_pct >= htft_gap_pct:
                        mk("htft_combo", "H1+FT",
                           f"HT/FT {combo_key} @{c:g} vs correlation-fair {fair:.2f} "
                           f"(H1 {who} @{l_h1:g} × half the FT {who} margin, FT "
                           f"@{l_ft:g}) — {long_pct:.0f}% too generous; conditioning "
                           f"on the HT lead shortens the FT leg, so fair sits below "
                           f"the {l_h1 * l_ft:.2f} independent product", long_pct,
                           outcome=combo_key)

        # 6. HT/FT vs the bivariate-normal fair model (basketball only —
        #    sigma/rho are calibrated to basketball margins)
        if combo and m.sport == "basketball":
            for kind, periods_label, detail, severity, outcome in _htft_fair_signals(
                combo, views, m.league,
            ):
                mk(kind, periods_label, detail, severity, outcome=outcome)

    flags.sort(key=lambda f: f.severity, reverse=True)
    return flags


def _htft_fair_signals(
    combo: dict[str, float],
    views: dict[str, "_PeriodView"],
    league: Optional[str],
) -> list[tuple[str, str, str, float, str]]:
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

    out: list[tuple[str, str, str, float, str]] = []
    params = (f"mu={mu:+.1f}" + (f", mu1={mu1:+.1f}" if mu1 is not None else "")
              + f", sigma={sigma:g}, rho={htft_model.RHO:g}, "
              + ("9" if nine else "6") + "-outcome")
    for label, p_fair in fair.items():
        cb = combo.get(label)
        if cb is None or p_fair <= 0:
            continue
        if not HTFT_ODDS_MIN <= cb <= HTFT_ODDS_MAX:
            continue  # outside the bettable range — not actionable
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
                f"+EV; {params})", round(edge_pct, 2), label,
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
                    f"off vs model ({params})", round(off_pct, 2), label,
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
