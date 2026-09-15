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
from typing import Any, Iterable, Optional

from src import htft_model
from src.models import Odds
from src.vig import devig_2way, devig_3way, devig_nway_shin  # FAIR PROBABILITIES (devigged)


# ── Thresholds (tunable; defaults sit well above measured normal variation) ────
ML_SPREAD_GAP_PP = 5.0     # ml vs spread win-prob gap (clean game: <=0.5pp)
PICKEM_LOCK_PCT = 0.3      # min locked % for a pickem_arb row
_PICKEM_MAX_SKEW_SEC = 120.0  # both legs must come from the same fetch
TOTAL_ADD_PTS = 5.0        # period totals vs parent sum (clean game: ~0.5pt)
# ...and 5.0 is a BASKETBALL number: five points on a ~220-point total is 2 %.
# The same constant on ice hockey would be five goals against a ~5.5-goal
# regulation total, i.e. the sum of the three periods would have to be roughly
# double or roughly zero before it flagged. A threshold in the units of the
# sport's own total has to move with the sport.
#
# HONEST LABEL: the hockey number below is NOT calibrated against observed
# violations, because the check has never had a chance to run. CrystalBet
# publishes exactly ONE rung on each per-period goal ladder ("1st Period -
# Total Goals*" and friends), and a ladder centre needs two rungs bracketing
# 50 % — so across the whole collected history, 0 of 349 events priced all
# four ladders. 1.0 goal is scaled off the board's own line spacing (period
# rungs sit at 1.5, the regulation ladder at 5.5) and is deliberately loose;
# it exists so the check is correct if CB ever widens those ladders, not
# because it has been tuned on anything.
#
# Soccer sits in the same trap and is deliberately LEFT ALONE here: its FT
# total is ~2.5 goals, so H1+H2-vs-FT has been effectively muted by the 5.0
# basketball constant since it shipped. Fixing that changes what an existing
# sport puts on the board and belongs in its own pass, not in a hockey one.
TOTAL_ADD_PTS_BY_SPORT: dict[str, float] = {
    "icehockey": 1.0,
}
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
# longer than the cap is longshot territory where flags were noise.
#
# The cap was 4.5 and is now 15.0 (2026-08-29). It was hiding well-founded
# flags while filtering almost no noise, which the board says plainly. Residual
# of the correlation-fair model, (posted - fair)/fair, bucketed by posted price
# over 2 936 (event, cell) pairs:
#
#     posted      n     median     p10      p90    p10-p90 spread
#     1.15-2.5   441    -12.0%   -15.1    -3.9         11.2
#     2.5-4.5   1264    -21.1%   -25.9   -16.0          9.9
#     4.5-8      846    -31.3%   -37.4   -27.2         10.2
#     8-15       297    -44.9%   -51.3   -40.0         11.3
#     >15         86    -58.6%   -72.6   -53.5         19.2   <- model degrades
#
# The DISPERSION is flat at ~10-11 % all the way to 15 and only widens past it,
# so the model holds fine at 5.90; it is the MEDIAN that drifts with price. And
# because the median drifts DOWN, the +2 % bar gets HARDER to clear as the price
# lengthens, not easier — a long-price flag is a bigger outlier than a
# short-price one, which is the opposite of what a longshot gate assumes.
#
# What the old cap actually cost, counting cells clearing the +2 % bar across
# the whole collected board: cap 4.5 -> 30 flags, cap 8 -> 30, cap 15 -> 31,
# no cap -> 31. It was blocking one historical flag and, on the live board, a
# 1/1 @5.90 sitting 12.4 % above fair while its own bucket's p90 is -27.2 %.
#
# Noise filtering by PRICE now belongs to the odds band on the Anomalies tab and
# the alert vetoes — visible, per-table and adjustable, where this constant
# makes a flag never exist at all.
HTFT_ODDS_MIN = 1.15
HTFT_ODDS_MAX = 15.0
# ...and htft_fair keeps the OLD 4.5, because the measurement above is not
# about it. `HTFT_ODDS_MAX` had two consumers and only one was calibrated:
#
#   htft_combo  soccer, cells 1/1 and 2/2, a CORRELATION-fair approximation
#               (h1 x (1 + (ft-1)/2)). That is what the residual table above
#               measures, and it is what earned the 15.0.
#   htft_fair   BASKETBALL, all NINE cells, a bivariate-normal model. A
#               different check, a different sport, a different model, with no
#               calibration behind widening it at all.
#
# Widening the shared constant put 17 htft_fair flags on the live board with
# severities to 123.3 — every one of them a "market shape off vs model" on a
# REVERSAL cell (1/2, 2/1), which is exactly where a bivariate normal is least
# trustworthy: the lowest-probability corner of the grid, where a small
# absolute error is a huge ratio. They swamped a tab whose next-largest check
# tops out an order of magnitude below.
#
# So this one stays where it was measured to behave. Raising it is a separate
# piece of work needing its own residual-by-price table for the basketball
# model, not a side effect of a soccer measurement.
HTFT_FAIR_ODDS_MAX = 4.5
# Sports the consistency engine evaluates. Basketball (its original home),
# soccer (1X2 + HT/FT), and tennis (set winners vs match winner — see
# set_vs_match below; tennis detail pages carry 15 markets that are all
# functions of the same four best-of-3 outcomes).
CONSISTENCY_SPORTS = ("basketball", "soccer", "tennis", "americanfootball",
                      "icehockey")

# ── what ice hockey does and does not contribute here ────────────────────────
# Hockey is the cleanest board on the dashboard, and that is a finding rather
# than a gap. Measured over the whole collected CrystalBet history (349 events
# with full detail pages, plus the live board):
#
#     ladder monotonicity        0 violations in 4 866 rungs / 86 events
#     overtime box (below/above) 0 violations in  85 events pricing both MLs
#     incl-OT ladder dominance   0 violations in 924 shared rungs / 49 events
#     coin-flip point estimate   max 1.81pp against an 8.0pp threshold
#
# The reason is visible in that last number: CrystalBet does not price its
# incl-OT hockey moneyline independently at all. It derives it from the
# regulation 1X2 with a flat even overtime — implied P(win in OT | tied after
# 60) came out median 0.500, p10 0.477, p90 0.524 across 170 (event, side)
# pairs. Two markets computed from one number cannot contradict each other, so
# no CB-internal check can find anything in them.
#
# The checks below are still wired, for two reasons. They are exact identities
# that cost a comparison each, and the same class of check DOES fire on a
# comparable board — American football's ot_vs_regulation flagged 3 of 50 pairs
# on its first live run. Hockey's board is also 30-of-47 preseason friendlies
# today and multiplies when the NHL season opens.
#
# Where hockey actually pays is the cross-book grid, not here: seven books now
# price its regulation market, verified against Pinnacle at 0.27-1.79pp median
# devigged gap. Pinnacle, unlike CrystalBet, DOES adjust the overtime
# conditional for team strength (p10 0.433, p90 0.567), so CB's flat coin flip
# is systematically wrong on mismatched teams — which the ordinary +EV path
# picks up now that the sport is wired, without needing a detector of its own.

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
# ot_monotone (ice hockey): how far the regulation goal ladder may sit ABOVE
# the incl-overtime one at the same line before flagging. The true bound is
# zero — overtime cannot remove goals — so anything positive is a contradiction
# and the threshold is pure devig slack, the two ladders carrying independent
# vig. Measured over 924 shared rungs on 49 events: not one had the regulation
# side higher, and the incl-OT side ran +2.4pp above it on median (+5-6pp mid-
# ladder), which is what ~0.2 of an overtime goal looks like. 3.0pp therefore
# sits well past anything the board does when it is behaving.
OT_MONOTONE_PP = 3.0

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

# tennis_correct_score: the minimum EDGE, posted odds against the model's fair
# price, before a leg is worth naming. This is the whole test now — the earlier
# version also gated on a percentage-point gap against the devigged
# correct-score market, which measured shape rather than money and came apart
# from it badly (a 10pp move on a 1.18 leg is +5%, 7.7pp on a 4.45 leg is +18%).
#
# Calibrated on the live board 2026-09-08, 1000 legs over 250 events. The edge
# distribution is p50 -13.95%, p90 -0.89%, p99 +7.49%:
#      6% -> 18 legs (1.8%)     10% ->  1 leg
#      8% ->  5 legs (0.5%)     15% ->  0
# 8% sits just above p99, which is where this repo puts a bar it wants the tail
# of rather than the body. 10% was the first choice and left a single event on
# a 250-event board — too tight to learn anything from.
CS_MIN_EDGE = 0.08

# Longshot guard. A small absolute probability error becomes a huge RELATIVE
# edge on a long price, so without this the list is led by legs nobody would
# bet: the calibration board's top finding was 0-2 @ 20.60 at "+18.6%", where
# the model and the book differ by well under a point of probability. 8.0 sits
# above every realistic bo3 correct-score leg while removing that class.
CS_MAX_LEG_ODDS = 8.0

# Both moneyline legs must be real prices before anything is derived from them.
# src/vig.py's own policy is "stay in 1.2-5.0 odds range where Shin's
# assumptions are well-validated". The calibration board had Goncalo M. vs
# Nunez L. priced 1.01/9.00 — 1.01 is the ">0.99 implied" solver edge case and
# is not really a price; it produced a spurious +5.1%.
#
# The bar is on IMPLIED PROBABILITY (1.05 ~ 95%), not on favouritism: vig.py's
# literal 1.20 floor drops 30% of the board and takes ordinary prices with it
# (Figl M. vs Nagel A is 3.85/1.15, a normal 87% favourite).
CS_MIN_ML_ODDS = 1.05

# duplicate_fixture: the same match listed TWICE by one book, as two separate
# events. Every other check here compares two markets inside one event; this one
# compares two events, so it is the only cross-event rule in the module.
#
# The rule is deliberately the strict one the owner asked for: identical kickoff
# AND the same two teams. Nothing fuzzier — a wider time window immediately
# starts reporting real fixtures as duplicates.
#
# LEAGUE PLAYS NO PART, by owner instruction: the same match is routinely
# listed under two different league names, which is precisely the case worth
# catching. League appears only in the flag's detail text, never in the test.
#
# Measured 2026-09-09 on the live boards, CrystalBet 1677 events and Lider-Bet
# 1506: ZERO duplicate pairs. The check is expected to sit silent, which is the
# point — when it does fire, the same outcome is being quoted at two prices by
# one book and that is bettable with no model.
#
# Measured against 3 months of the tick store rather than argued: 158 real
# duplicates on the two books that matter (CrystalBet 126, Lider-Bet 32), about
# 1.8 a day. They come in three shapes, all of which this catches — the same
# match under two league names ("Club Friendly Games" vs "Clubs"), the same
# match with the sides swapped ("Rostov v CSKA" vs "CSKA v Rostov"), and the
# same match spelled two ways ("Dane Sweeny" vs "Sweeny D.").
#
# Simulated and e-sports leagues must be excluded by the caller or they drown
# it: those fixtures legitimately repeat every few minutes, and they accounted
# for more than 16k events in the same scan.
DUP_NAME_SCORE = 80.0

# half_result_vs_ft: how far a HALF's 1X2 may sit below what the full-time 1X2
# implies for it, in percentage points on the most generous outcome.
#
# The halves are BIAS-CORRECTED Poisson (soccer_model.HALF_BIAS): raw Poisson
# overstates half-draws by 1-2pp on every book measured.
#
# The bar is deliberately HIGH, by owner instruction 2026-09-14: this is not a
# precision instrument, it is a tripwire for a half priced absurdly against
# its own full-time market — "ml has 1.50 on one and ht ml has 3.50 on one".
# That example measures 18.8pp. The screenshot that prompted the check
# (Belarus U19 v Gomel Region, a second half at 25% away against 6% for the
# match) measured 22.4pp. And in three months of closing history the LARGEST
# gap ordinary pricing ever produced was 12.6pp on CrystalBet H1 (3667
# events) and 6.1pp on Lider-Bet H2 (2827):
#     bar   CB-H1 fires   Lider-H2 fires
#      8        13             0
#     10         5             0
#     12         2             0
#     15         0             0
# 15.0 sits in the gap between everything normal and the cases worth an
# alert. An earlier bar of 6.0 (just past p99) was correct as statistics and
# wrong as a product: it fired the H1 draw of lopsided national-team matches,
# which nobody wants to be woken up for.
HALF_RESULT_PP = 15.0

# Both halves, on, alerting. A brief spell OFF (same day) while the bar was
# being re-thought — the calibration and correction underneath did not change.
HALF_RESULT_ENABLED = True


def _duplicate_fixture_flags(events, meta) -> list["ConsistencyFlag"]:
    """The same match, listed twice by one book, as two separate events.

    Returns one flag per duplicated PAIR, attached to the event whose id sorts
    first and naming its twin in `detail` — ConsistencyFlag is keyed to a single
    event, so the pair is reported from one side rather than twice.

    Severity is a FLAT 100 for every duplicate, by owner instruction: a book
    listing one match twice is always worth being told about, so the flag must
    clear whatever threshold the panel is set to instead of competing on size.
    Nothing is lost by it — the price disagreement and the best-of-both cover,
    including whether that cover is actually locked, are both in `detail`.

    Two earlier versions were wrong about this. Scoring the locked edge put an
    identically-priced pair at -5.3 (the book's own overround), which could
    never clear a threshold; scoring the disagreement put it at 0, which still
    could not clear a positive one. Both silenced exactly the duplicates worth
    hearing about.
    """
    from rapidfuzz import fuzz
    from src.normalize import normalize_team, normalize_tennis_name

    def _norm(name: str, sport: str) -> str:
        name = str(name or "").strip()
        if not name:
            return ""
        try:
            return (normalize_tennis_name(name) if sport == "tennis"
                    else normalize_team(name))
        except Exception:
            return name.lower()

    def _ml(key) -> dict[str, float]:
        """The full-time moneyline of one listing, as {outcome: odds}."""
        for o in events.get(key, {}).get("FT", {}).get("moneyline", []):
            s = o.selections
            if "home" in s and "away" in s:
                return {k: v for k, v in s.items() if v and v > 1.0}
        return {}

    # Only the main board — a corners submarket is not a separate fixture.
    rows = [(k, o) for k, o in meta.items()
            if k[1] is None and o.start_time and o.sport in CONSISTENCY_SPORTS]
    buckets: dict[tuple, list] = {}
    for k, o in rows:
        buckets.setdefault((o.sport, o.start_time), []).append((k, o))

    out: list[ConsistencyFlag] = []
    for (sport, _start), group in buckets.items():
        if len(group) < 2:
            continue
        for i, (ka, a) in enumerate(sorted(group, key=lambda x: str(x[0][0]))):
            for kb, b in sorted(group, key=lambda x: str(x[0][0]))[i + 1:]:
                if ka[0] == kb[0]:
                    continue
                # NO GUARDS, and no league test. The rule is the whole rule:
                # same kickoff, same two teams. Measured over 3 months of the
                # tick store (238k events, simulated/e-sports leagues excluded)
                # a youth/senior guard blocked 11 pairs board-wide, 3 of them on
                # CrystalBet and Lider-Bet — and those three are the mislabelling
                # this check exists to catch, not false alarms:
                #     FCI Tallinn II       v Tabasalu Ulasabat
                #     Fci Levadia Tallinn U19 v Tabasalu
                # one side tagged "II", the other "U19", same club. A women's
                # guard blocked 2 more. Two genuinely different fixtures between
                # the same two teams do not kick off in the same minute, so the
                # tags carry no information the kickoff has not already given.
                ah, aa = _norm(a.home, sport), _norm(a.away, sport)
                bh, ba = _norm(b.home, sport), _norm(b.away, sport)
                if not (ah and aa and bh and ba):
                    continue
                direct = min(fuzz.token_set_ratio(ah, bh), fuzz.token_set_ratio(aa, ba))
                swap = min(fuzz.token_set_ratio(ah, ba), fuzz.token_set_ratio(aa, bh))
                if max(direct, swap) < DUP_NAME_SCORE:
                    continue
                flipped = swap > direct
                ml_a, ml_b = _ml(ka), _ml(kb)
                # Align the twin onto this listing's orientation before
                # comparing. A swapped duplicate ("Rostov v CSKA" against
                # "CSKA v Rostov") is not a lesser case — measured on the tick
                # store it is a large share of the real ones, and it is the most
                # bettable, since backing the same team on both listings is one
                # bet at two prices. An earlier version gated the cover behind
                # `not flipped` and reported every one of them as "prices not
                # comparable" with no edge at all.
                if flipped:
                    ml_b = {"home": ml_b.get("away"), "away": ml_b.get("home"),
                            **({"draw": ml_b["draw"]} if "draw" in ml_b else {})}
                    ml_b = {k: v for k, v in ml_b.items() if v}
                note = "prices not comparable"
                if ml_a and ml_b and set(ml_a) == set(ml_b):
                    best = {k: max(ml_a[k], ml_b[k]) for k in ml_a}
                    cover = sum(1.0 / v for v in best.values())
                    gap = max(abs(ml_a[k] / ml_b[k] - 1.0) * 100.0 for k in ml_a)
                    note = (f"they disagree by up to {gap:.1f}%, best-of-both "
                            f"cover {cover:.4f}"
                            + (f" — LOCKED {(1.0 - cover) * 100:.2f}%"
                               if cover < 1.0 else ""))
                out.append(ConsistencyFlag(
                    sport=a.sport, league=a.league, home=a.home, away=a.away,
                    start_time=a.start_time, event_id=ka[0],
                    kind="duplicate_fixture", periods="FT",
                    detail=(f"same match listed twice at the same kickoff — "
                            f"event {ka[0]} ({a.league}) and event {kb[0]} "
                            f"({b.league}){' with sides flipped' if flipped else ''}; "
                            f"{note}"),
                    severity=100.0, outcome=None, odds=None,
                    submarket=None))
    return out


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


# ── Tennis correct score: the latent-strength (Beta) model ───────────────────
#
# _match_prob_from_set above assumes a CONSTANT per-set probability, i.e. that
# set outcomes are independent. They are not, and the owner put the mechanism
# better than the textbook does: "when someone wins first it most likely lower
# odds to win next one too". Winning set 1 is evidence you are the better
# player, so it shortens set 2.
#
# The honest way to say that is NOT a correlation fudge factor bolted onto the
# product. Sets ARE independent — given the player's true per-set strength.
# What we lack is that strength. So make it a random variable:
#
#     P ~ Beta(mu*nu, (1-mu)*nu)
#
# and every correct-score leg is a raw moment of P:
#
#     P(fav 2-0) = E[P^2]                       = mu^2 + sigma^2
#     P(fav 2-1) = 2(E[P^2] - E[P^3])
#     P(dog 2-1) = 2(mu - 2E[P^2] + E[P^3])
#     P(dog 2-0) = 1 - 2mu + E[P^2]
#     P(match)   = 3E[P^2] - 2E[P^3]
#
# The correlation IS the variance: straight sets get lifted by exactly sigma^2
# over the independent p^2, and nu -> infinity recovers _match_prob_from_set
# exactly. Nothing is fitted by eye.
#
# The reason this is worth having at all is that CB OVER-DETERMINES it. The
# 1st-set price gives mu; the match price then pins nu; and the four
# correct-score legs follow with NO free parameter left. A board that posts all
# three is making a checkable arithmetic claim, not three opinions.
#
# CAVEAT, stated because it bounds what the check can ever say: solving nu from
# the match price forces the model to reproduce that price. So this tests the
# SHAPE across the four legs, never the level. If CB's match price is wrong,
# the model inherits the error and reports nothing.

def _cs_moments(mu: float, nu: float) -> tuple[float, float]:
    """E[P^2], E[P^3] for P ~ Beta(mu*nu, (1-mu)*nu)."""
    a, b = mu * nu, (1.0 - mu) * nu
    n = a + b
    return (a * (a + 1) / (n * (n + 1)),
            a * (a + 1) * (a + 2) / (n * (n + 1) * (n + 2)))


def _cs_match_prob(mu: float, nu: float) -> float:
    """P(win a best-of-3) under the latent-strength model."""
    m2, m3 = _cs_moments(mu, nu)
    return 3.0 * m2 - 2.0 * m3


def _cs_solve_nu(mu: float, p_match: float) -> Optional[float]:
    """Concentration implied by a (1st-set, match) price pair.

    Variance costs a favourite match equity, so P(match) is increasing in nu —
    bisect geometrically, since nu spans orders of magnitude. Returns None when
    NO nu reproduces the observed match price: that pair is already
    contradictory before correct score is consulted, and tennis_set_match
    above owns that case.
    """
    if not 0.0 < mu < 1.0 or not 0.0 < p_match < 1.0:
        return None
    # Solve on the FAVOURITE. P(match) is monotone in nu but the DIRECTION
    # flips at mu = 0.5: variance costs a favourite match equity and gives it
    # to a dog. Bisecting without this reversed the comparison on every event
    # where the away player was the underdog, and the range check then rejected
    # it — measured 2026-09-08 on the live board as a 55% "unfittable" rate that
    # was entirely this bug. Orienting fixes it because nu = a + b is symmetric
    # under P <-> 1-P: Beta(a,b) and Beta(b,a) have the same concentration, so
    # the nu solved for the favourite is the one the dog has too.
    if mu < 0.5:
        mu, p_match = 1.0 - mu, 1.0 - p_match
    lo, hi = 1e-6, 1e9
    if not (_cs_match_prob(mu, lo) <= p_match <= _cs_match_prob(mu, hi)):
        return None
    for _ in range(200):
        mid = (lo * hi) ** 0.5
        if _cs_match_prob(mu, mid) < p_match:
            lo = mid
        else:
            hi = mid
    return (lo * hi) ** 0.5


def _cs_partition(mu: float, nu: float) -> dict[str, float]:
    """The four best-of-3 correct-score probabilities, keyed HOME-AWAY.

    `mu` is the AWAY player's per-set strength, matching the "0-2" label
    convention (away wins 2-0). Sums to exactly 1 by construction.
    """
    m2, m3 = _cs_moments(mu, nu)
    return {
        "0-2": m2,
        "1-2": 2.0 * (m2 - m3),
        "2-1": 2.0 * (mu - 2.0 * m2 + m3),
        "2-0": 1.0 - 2.0 * mu + m2,
    }


# The set-to-set variance, as ONE constant for the whole board rather than a
# parameter fitted per match.
#
# Fitting nu per match was the first version and it was wrong. There is exactly
# one observation (the gap between the 1st-set price and the match price) per
# free parameter, and the gap's leverage on nu is 3*sigma^2*(1-2mu) — which goes
# to ZERO at an even match. So near mu = 0.5 the parameter is unidentified and
# the solver converts CB's ordinary pricing noise into whatever variance closes
# the gap. Measured on the live board 2026-09-08: an identical -2pp gap implies
# sigma^2 = 0.116 at mu = 0.55 but 0.012 at mu = 0.80, and one real match
# (Filar K. vs Spierle J., a -2.94pp gap at mu = 0.573) solved to sigma^2 =
# 0.121 — 49% of the maximum variance mathematically possible, i.e. a
# near-uniform Beta asserting we know nothing about the player. That single
# artefact was the largest "finding" the first version produced: +18.9% on a
# leg this model prices at -8.2%.
#
# Pooled by least squares on P(match) instead — one parameter for the whole
# board, fitted over all 363 events carrying both moneylines, residual RMS
# 1.60pp. Deliberately NOT conditioned on having a correct-score market: this
# constant describes how CB's set and match prices relate, which is a property
# of the board, not of the subset we happen to be checking. Fitting it on the
# 250 correct-score events gave 0.0335 and the wider sample gives 0.0403.
#
# HONEST LIMIT: this is fitted to make CB's own two markets agree, so it
# absorbs whatever systematic gap sits between them — and CB alone cannot
# distinguish "sets are correlated" from "these two markets are priced
# inconsistently". Settling that needs a sharp reference (Pinnacle prices
# tennis; src/scrapers/pinnacle.py already reaches it). Until then, treat
# 0.0335 as a description of CB, not of tennis.
CS_SIGMA2 = 0.04026


def _cs_fair_from_match(p_match: float, sigma2: float = CS_SIGMA2) -> dict[str, float]:
    """Fair correct-score partition from the devigged MATCH price alone.

    This is the whole model in one call: devig the match, invert it to a per-set
    strength at the board's fixed variance, and read off the four legs. The
    1st-set price is deliberately NOT an input — it was what the discredited
    per-match fit consumed, and holding it out leaves it free to be used as a
    check on the model instead of a feedstock for it.

    Stated on the AWAY player ("0-2" = away wins 2-0). Below 0.5 it recurses on
    the favourite and mirrors, because the inversion is only monotone above it.
    """
    if p_match < 0.5:
        f = _cs_fair_from_match(1.0 - p_match, sigma2)
        return {"2-0": f["0-2"], "2-1": f["1-2"], "1-2": f["2-1"], "0-2": f["2-0"]}
    lo, hi = 0.5, 0.999
    for _ in range(200):
        mid = (lo + hi) / 2.0
        nu = mid * (1.0 - mid) / sigma2 - 1.0
        if nu <= 0.0:
            lo = mid
            continue
        if _cs_match_prob(mid, nu) < p_match:
            lo = mid
        else:
            hi = mid
    mu = (lo + hi) / 2.0
    return _cs_partition(mu, mu * (1.0 - mu) / sigma2 - 1.0)


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
    # The PRICE this flag is about — the leg you would actually back. Added
    # 2026-08-29 so the odds band on the Anomalies tab can reach consistency
    # rows at all: the band shipped reading `odds`, and ConsistencyFlag had no
    # such field, so every one of these rows was exempt from a filter the panel
    # showed as applying to them.
    #
    # None where the check names no single bettable leg — ml_vs_spread and
    # total_additivity describe a relationship between markets, not a bet. Rows
    # with no price are never filtered out, because muting them would turn a
    # noise filter into a coverage hole.
    odds: Optional[float] = None
    # Which board this finding is on: None for the main goals markets,
    # "corners" for the corner markets, and so on. Every check runs
    # INDEPENDENTLY per submarket — see the grouping in find_consistency_flags.
    submarket: Optional[str] = None

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



def _fts_vs_ml(period_odds: dict, per: str,
               decisive: float = DECISIVE_PROB) -> list[tuple]:
    """First Team To Score against the 1X2: who is the stronger side?

    Owner asked for this after GKS Wikielec v Concordia Elblag, where the 1X2
    made the home side a 2.90 underdog and First Team To Score made it the
    2 @1.80 favourite to open the scoring.

    IT IS A CORRELATION, NOT AN IDENTITY, and the rest of this file is
    identities — so the trigger is chosen to match what the relationship can
    actually support. Measured over the 90 collected events pricing both,
    comparing P(home first | someone scores) against P(home | no draw):

        median -0.050   p10 -0.131   p90 +0.104   max |0.174|

    The median is the shape working as it should: scoring first is a SHRUNK
    version of winning, because a team can score first and still lose. But the
    deciles are +-0.12 wide, so the magnitude of the gap is worthless as a
    trigger — a threshold clearing that noise sits near 0.17 and fires on
    nothing, and anything lower fires on everything.

    What IS interpretable is a FAVOURITE FLIP: the 1X2 makes a side a clear
    underdog and FTS makes it favourite to score first. Rates over the same 90:

        bare flip (opposite sides of 0.500)          3   3.3%
        flip + 1X2 decisive by 0.06                  1   1.1%   <- this
        flip + BOTH decisive by 0.06                 0   0.0%
        |gap| >= 0.15 (magnitude only)               4   4.4%

    A bare flip is mostly two near-coin-flips landing either side of 0.500 and
    means nothing, so the 1X2 side must be a decisive favourite — the same
    0.06 bar `favourite_flip` already uses. Requiring BOTH sides decisive kills
    it: the FTS side is usually near even, which is the whole point (a clear
    underdog has no business being favoured to open the scoring at all).

    The one exact bound this pair has — P(nobody scores) is P(0-0) and cannot
    exceed P(draw) — is checked too. It was violated 0 times in 90 events.
    """
    ml3 = fts = None
    for o in period_odds.get("moneyline", []):
        if {"home", "draw", "away"} <= set(o.selections):
            ml3 = o.selections
            break
    for o in period_odds.get("fts", []):
        if {"home", "none", "away"} <= set(o.selections):
            fts = o.selections
            break
    if not ml3 or not fts:
        return []
    try:
        inv = [1.0 / ml3[k] for k in ("home", "draw", "away")]
        s_ = sum(inv)
        ph, pd, pa = (v / s_ for v in inv)
        jnv = [1.0 / fts[k] for k in ("home", "none", "away")]
        t_ = sum(jnv)
        fh, fn, fa = (v / t_ for v in jnv)
    except (ZeroDivisionError, KeyError):
        return []
    if ph + pa <= 0 or fh + fa <= 0:
        return []

    out: list[tuple] = []

    # The exact leg first: "nobody scores" IS 0-0, which is one way to draw.
    if fn - pd >= 0.03:
        out.append((
            f"{per}: nobody-scores is priced at {fn*100:.0f}% but the 1X2 makes "
            f"the whole DRAW only {pd*100:.0f}% — 0-0 is one way to draw, so it "
            f"cannot be the more likely of the two",
            (fn - pd) * 100.0, "none", fts["none"],
        ))

    x = ph / (ph + pa)              # 1X2: home strength, draw removed
    y = fh / (fh + fa)              # FTS: home strength, "nobody" removed
    if (x - 0.5) * (y - 0.5) < 0 and abs(x - 0.5) >= decisive:
        under, first = ("home", "home") if x < 0.5 else ("away", "away")
        side = "home" if y > 0.5 else "away"
        out.append((
            f"{per}: the 1X2 makes {under} the underdog at "
            f"{x*100:.0f}% to win (draw removed), but First Team To Score makes "
            f"it the {y*100:.0f}% favourite to open the scoring — the two "
            f"markets disagree about which side is stronger",
            abs(y - x) * 100.0, side, fts[side],
        ))
    return out


def _pickem_dominance(period_odds: dict, per: str,
                      min_pct: float = 0.3) -> list[tuple]:
    """A 0.0 rung can never be the LONGER price on the same side as the 1X2.

    The 0.0 handicap VOIDS the draw; the 1X2 LOSES to it. Same side, same win
    condition, and one of them hands your stake back where the other keeps it —
    so the 0.0 rung is a strictly better bet and its price must be strictly
    shorter. On fair prices the ratio is exact:

        AH0(side) / X12(side)  ==  1 - P(draw)

    A ratio at or above 1.0 is the book offering the better bet at the longer
    price. There is nothing to model and nothing to devig: this compares RAW
    prices you can actually take, so it is immune to the devig choice that
    every probability-space check here depends on.

    Applies to EVERY market that voids the draw — the 0.0 handicap rung and the
    Draw No Bet alike. Two live cases, and each was invisible to a check that
    looked at only one of them:

        Resovia Rzeszow v KKP Bydgoszcz W   0.0 rung away @3.50 vs 1X2 @3.10
                                            ratio 1.129 (expected 0.773)
        GKS Wikielec v Concordia Elblag     DNB away @1.95 vs 1X2 @1.90
                                            ratio 1.026, while its 0.0 rung was
                                            perfectly coherent at 0.793

    CALIBRATION, whole collected board.

      0.0 handicap vs 1X2 — 2 993 (event, period) pairs, 5 986 sides:
        ratio p10 0.558, median 0.688, p90 0.771, max 0.978; residual against
        each event's own 1 - P(draw) a median +0.003. ZERO sides reached 1.0.

      Draw No Bet vs 1X2 — 109 events, 218 sides:
        ratio p10 0.712, median 0.774, p90 0.840, max 1.127. ONE violation, and
        it is Cheltenham v Gwalia United — the same board that produced the
        only historical pickem_duplicate lock, caught here a second way.

    The DNB median of 0.774 lands on the theoretical 1 - P(draw) almost exactly,
    which is the relation stated as a number. The bound needs no slack: it is
    exact, and both live cases clear 1.0 outright.
    """
    ml3 = ml_at = None
    for o in period_odds.get("moneyline", []):
        if {"home", "draw", "away"} <= set(o.selections):
            ml3, ml_at = o.selections, o.fetched_at
            break
    if not ml3:
        return []

    # EVERY market that voids the draw, not just the handicap rung. Both are
    # the same bet and both carry the same bound; checking only one of them
    # missed GKS Wikielec v Concordia Elblag, where the 0.0 rung was coherent
    # (ratio 0.793) and the DRAW NO BET was not (away @1.95 against the 1X2's
    # @1.90, ratio 1.026).
    voiders: list[tuple[str, dict, Any]] = []
    for o in period_odds.get("spread", []):
        if (o.line is not None and abs(o.line) < 1e-6
                and {"home", "away"} <= set(o.selections)):
            voiders.append(("0.0 handicap", o.selections, o.fetched_at))
            break
    for o in period_odds.get("moneyline", []):
        if ("draw no bet" in (o.section or "").lower()
                and set(o.selections) == {"home", "away"}):
            voiders.append(("draw-no-bet", o.selections, o.fetched_at))
            break
    if not voiders:
        return []

    try:
        inv = sum(1.0 / ml3[k] for k in ("home", "draw", "away"))
        p_draw = (1.0 / ml3["draw"]) / inv
    except (ZeroDivisionError, KeyError):
        return []

    out: list[tuple] = []
    for label, quote, at in voiders:
        if ml_at is not None and at is not None:
            try:
                if abs((ml_at - at).total_seconds()) > _PICKEM_MAX_SKEW_SEC:
                    continue
            except (TypeError, AttributeError):
                pass
        for side in ("home", "away"):
            a, m = quote.get(side), ml3.get(side)
            if not a or not m or a <= 1.0 or m <= 1.0:
                continue
            ratio = a / m
            if ratio < 1.0:
                continue
            excess = (ratio - 1.0) * 100.0
            if excess < min_pct:
                continue
            out.append((
                f"{per}: the {label} {side} @{a:g} is LONGER than the 1X2 "
                f"{side} @{m:g} (ratio {ratio:.3f}), but the {label} voids the "
                f"draw where the 1X2 loses to it — the better bet cannot be the "
                f"longer price. The 1X2's own P(draw)={p_draw*100:.0f}% puts the "
                f"ratio at {1-p_draw:.3f}. Take the {label}: same win, stake back "
                f"on the draw, and {excess:.1f}% better odds",
                excess, side, a,
            ))
    return out


def _pickem_duplicates(period_odds: dict, per: str,
                       min_pct: float = 0.3) -> list[tuple]:
    """Draw No Bet and an Asian Handicap 0.0 rung are THE SAME BET.

    Both void on the draw and settle on "who wins, given it is not drawn", so a
    book quoting both is quoting one market twice. Take the better price on each
    side and the two quotes cover the game between them; when that costs less
    than 1.0 it is a locked position with **no losing branch at all** — the draw
    voids both legs and returns the stake.

    Found by the owner on the live board (Resovia Rzeszow v KKP Bydgoszcz W,
    2026-08-29), where CrystalBet posted

        Draw no bet         1 @ 1.55   2 @ 2.10   -> P(home | no draw) 57.5 %
        Asian Handicap 0.0  1 @ 1.20   2 @ 3.50   -> P(home | no draw) 74.5 %

        best of each: 1/1.55 + 1/3.50 = 0.9309  ->  +7.4 % locked

    HOW OFTEN, measured over the whole collected board before this was written:
    1 414 events price both markets, and they normally agree to a **median
    0.1pp** (p90 0.4pp) — as two quotes of one bet should. Ten sat 5pp+ apart,
    one 10pp+, and exactly one locked, at +0.40 %.

    So it is rare, and that sample understates it in a way worth naming: it is
    built from each event's LAST seen prices, which skew late and settled, and
    it cannot contain the case above at all — a live mid-morning board on an
    obscure women's league, which is exactly where a book's automation is
    loosest. The owner found +7.4 % by eye on a market the scanner was not even
    reading. 1-in-1414 is a floor, not the rate.

    Unlike `ml_vs_spread`, which reports the same disagreement and names no bet,
    this returns the position: both prices, both sides, and the locked return.
    """
    quotes: list[tuple[str, dict, Any]] = []
    for o in period_odds.get("moneyline", []):
        # Only an explicit Draw No Bet. A plain 2-way moneyline is NOT the same
        # bet as a 0.0 rung in every sport — basketball has no draw to void, and
        # American football's 2-way winner includes overtime while its 0.0
        # spread pushes on a regulation tie. The section title is what makes
        # this specific rather than a shape coincidence.
        if "draw no bet" in (o.section or "").lower() and set(o.selections) == {"home", "away"}:
            quotes.append(("draw-no-bet", o.selections, o.fetched_at))
    for o in period_odds.get("spread", []):
        if (o.line is not None and abs(o.line) < 1e-6
                and {"home", "away"} <= set(o.selections)):
            quotes.append(("handicap 0.0", o.selections, o.fetched_at))
    if len(quotes) < 2:
        return []

    # SAME INSTANT ONLY, for the reason _pickem_locks documents at length: two
    # prices captured minutes apart are not a position you can take.
    stamps = [q[2] for q in quotes if q[2] is not None]
    if len(stamps) >= 2:
        try:
            if (max(stamps) - min(stamps)).total_seconds() > _PICKEM_MAX_SKEW_SEC:
                return []
        except (TypeError, AttributeError):
            pass

    out: list[tuple] = []
    for side, other in (("home", "away"), ("away", "home")):
        best = max(quotes, key=lambda q: q[1].get(side) or 0.0)
        best_other = max(quotes, key=lambda q: q[1].get(other) or 0.0)
        if best[0] == best_other[0]:
            continue                 # one market is better on both sides: no cross
        a, b = best[1].get(side), best_other[1].get(other)
        if not a or not b or a <= 1.0 or b <= 1.0:
            continue
        cost = 1.0 / a + 1.0 / b
        if cost >= 1.0:
            continue
        profit = (1.0 / cost - 1.0) * 100.0
        if profit < min_pct:
            continue
        out.append((
            f"{per}: {best[0]} {side} @{a:g} + {best_other[0]} {other} @{b:g} — "
            f"the same bet quoted twice (both void on the draw), and the two "
            f"quotes cover the game for {cost:.4f}: +{profit:.2f}% locked, with "
            f"the draw returning both stakes",
            profit, side, min(a, b),
        ))
        break                        # one cross per period; the mirror is the same position
    return out


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
            profit, f"pickem_{side}", P))
    return out


def _ot_ladder(period_mkts: dict[str, list[Odds]], market_type: str,
               team_side: Optional[str]) -> dict[float, float]:
    """{line: devigged P(over)} for one goal ladder.

    Used by ice hockey's ot_monotone check to lay the regulation ladder against
    the incl-overtime one rung for rung. Devigged rather than raw, because the
    two ladders carry independent vig and a raw comparison would report the vig
    difference as a contradiction.

    Every rung is kept, not just the main section: the whole point is to compare
    the SAME line across two ladders, and dropping alt-lines would throw away
    most of the overlap.
    """
    out: dict[float, float] = {}
    for o in period_mkts.get(market_type, []):
        if o.line is None or o.team_side != team_side:
            continue
        if "over" not in o.selections or "under" not in o.selections:
            continue
        try:
            po, _ = devig_2way(o.selections["over"], o.selections["under"])
        except (ValueError, ZeroDivisionError):
            continue
        if 0.0 < po < 1.0:
            # Two rungs can share a line across sections; keep the first, which
            # is the page's own primary ordering.
            out.setdefault(o.line, po)
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
    ot_monotone_pp: float = OT_MONOTONE_PP,
) -> list[ConsistencyFlag]:
    """Find CB-internal contradictions across markets/periods. See module docs."""
    # Group: event_id -> period -> market_type -> [Odds]
    events: dict[Optional[str], dict[str, dict[str, list[Odds]]]] = {}
    meta: dict[Optional[str], Odds] = {}
    for o in odds:
        if o.sport not in CONSISTENCY_SPORTS:
            continue
        # Keyed by (event, SUBMARKET). Corners are a separate board that
        # happens to share an event id and a period vocabulary with goals: a
        # corners 1X2 and a goals 1X2 are both "moneyline FT", and a corners
        # 0.0 handicap and a goals one are both "spread FT line 0". Grouping on
        # the event alone would put them in one bucket and every check would
        # then compare a goals price against a corners price — ml_vs_spread
        # would read the goals 1X2 against the corners pick'em, pickem_duplicate
        # would "lock" a cover across two different things being counted.
        #
        # Everything that existed before this carries submarket=None, so the
        # grouping is unchanged for it.
        ev = events.setdefault((o.raw_event_id, o.submarket), {})
        ev.setdefault(o.period, {}).setdefault(o.market_type, []).append(o)
        meta.setdefault((o.raw_event_id, o.submarket), o)

    flags: list[ConsistencyFlag] = []
    for (eid, submarket), periods in events.items():
        m = meta[(eid, submarket)]
        views = {per: _build_period_view(mkts) for per, mkts in periods.items()}

        def mk(kind, per_label, detail, severity, outcome=None, odds=None,
               _sub=submarket):
            flags.append(ConsistencyFlag(
                sport=m.sport, league=m.league, home=m.home, away=m.away,
                start_time=m.start_time, event_id=eid,
                kind=kind, periods=per_label,
                detail=(f"[{_sub}] {detail}" if _sub else detail),
                severity=round(severity, 2), outcome=outcome,
                odds=round(float(odds), 3) if odds is not None else None,
                submarket=_sub,
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
                mk("pickem_arb", per, flag[0], flag[1],
                   outcome=flag[2], odds=flag[3])

        # 1c. the same bet quoted twice. Draw No Bet and an Asian Handicap 0.0
        #     rung settle identically, so the better side of each is a cover
        #     with no losing branch. See _pickem_duplicates.
        for per, mkts in periods.items():
            for flag in _pickem_duplicates(mkts, per, min_pct=pickem_lock_pct):
                mk("pickem_duplicate", per, flag[0], flag[1],
                   outcome=flag[2], odds=flag[3])

        # 1d. the 0.0 rung longer than the 1X2 on the same side — impossible,
        #     and checked on RAW prices so no devig choice can affect it.
        for per, mkts in periods.items():
            for flag in _pickem_dominance(mkts, per, min_pct=pickem_lock_pct):
                mk("pickem_dominance", per, flag[0], flag[1],
                   outcome=flag[2], odds=flag[3])

        # 1e. First Team To Score vs the 1X2. The only CORRELATION in this
        #     file — see _fts_vs_ml for why the trigger is a favourite flip
        #     rather than a size, and what the relationship measures at.
        for per, mkts in periods.items():
            for flag in _fts_vs_ml(mkts, per):
                mk("fts_vs_ml", per, flag[0], flag[1],
                   outcome=flag[2], odds=flag[3])

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
                             ("H2", ("Q3", "Q4")), ("FT", ("Q1", "Q2", "Q3", "Q4")),
                             # hockey: three periods make the regulation game.
                             # Not FT — the incl-OT total is a different number.
                             ("REG", ("P1", "P2", "P3"))):
            pv = tc(parent)
            kv = [tc(k) for k in kids]
            if pv is not None and all(x is not None for x in kv):
                diff = sum(kv) - pv
                if abs(diff) >= TOTAL_ADD_PTS_BY_SPORT.get(m.sport, total_add_pts):
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

        # 4c-ter. TENNIS: the correct-score market against the match price.
        #
        # Devig the match, turn it into a fair price for each exact set score,
        # and compare the board's correct-score legs to it. That is the whole
        # check, and it is deliberately the simple thing: the only market it
        # reads besides correct score is the match winner.
        #
        # The set score is a function of per-set strength, and the match price
        # pins that down — but NOT under independent sets, which is the owner's
        # point and it is right: "when someone wins first it most likely lower
        # odds to win next one too." That is carried by CS_SIGMA2, one constant
        # measured across the board, in _cs_fair_from_match.
        #
        # An earlier version fitted that variance PER MATCH from the 1st-set
        # price. It looked more principled and was much worse — see CS_SIGMA2
        # for why the fit is unidentified near an even match, and for the
        # +18.9% phantom it produced on a leg this model prices at -8.2%.
        # Holding the 1st-set price out also leaves it available as an
        # independent check on the model rather than an input to it, and lifts
        # coverage to every match with a correct-score board.
        #
        # Only the GENEROUS direction fires. The other sign is the book
        # charging too much, which is most of the board and is just the vig.
        if m.sport == "tennis":
            cs_rows = periods.get("FT", {}).get("correct_score") or []
            ftv = views.get("FT")
            if cs_rows and ftv is not None and ftv.ml_phome is not None:
                # Refuse to derive anything from a price the book isn't making.
                real_prices = all(
                    o2.selections.get(k, 0.0) >= CS_MIN_ML_ODDS
                    for o2 in periods.get("FT", {}).get("moneyline", [])
                    for k in ("home", "away"))
                # Model is stated on the AWAY player, matching "0-2".
                fair = _cs_fair_from_match(1.0 - ftv.ml_phome)
                for o in cs_rows:
                    if not real_prices:
                        break
                    sels = o.selections
                    # Best-of-5 boards parse, but the closed form is bo3.
                    if set(sels) != set(fair):
                        continue
                    for leg, price in sels.items():
                        edge = price * fair[leg] - 1.0
                        if edge < CS_MIN_EDGE or price > CS_MAX_LEG_ODDS:
                            continue
                        mk("tennis_correct_score", "FT",
                           f"correct score {leg} @ {price:.2f} against a fair "
                           f"{1.0 / fair[leg]:.2f} — the match price "
                           f"({(1.0 - ftv.ml_phome) * 100:.0f}% away, devigged) "
                           f"makes {leg} a {fair[leg] * 100:.1f}% shot, so the "
                           f"board is {edge * 100:+.1f}% generous on it",
                           edge * 100.0, outcome=leg, odds=price)

        # 4c-quater. SOCCER: each HALF's result against the full-time result.
        #
        # The full-time 1X2 fixes the goal rates; the halves are a fixed share
        # of those rates. So the first- and second-half 1X2 are not free
        # opinions — they are determined by the full-time market up to the
        # league's half split. A half priced far from that is the book
        # contradicting its own headline market.
        #
        # Found by the owner on the live board (Belarus U19 v Gomel Region,
        # 2026-09-11). Full-time 1.10/6.90/14.6, first half 1.45/2.85/11.8,
        # second half 1.90/3.20/3.50: the away side 25.4% to win the second
        # half against 6.1% to win the match. A goal model fitted ONLY to the
        # full-time 1X2 and total reproduced the first half to 0.0pp — never
        # having seen it — and put the second-half away at 7.2%: an 18.2pp
        # miss. When the same fit nails one half and misses the other by 3.5x,
        # the market is the outlier. The value sat on the other side: home in
        # the second half at 1.90 against a model 66.5% is +26% EV.
        #
        # The second half could not fire before this because CB's H2 was never
        # classified — skipped as unmatchable against Pinnacle, which is the
        # +EV pipeline's concern and not this one's. Third time that reasoning
        # has hidden a CB-internal contradiction; see the permissive
        # classifier's docstring for the other two.
        #
        # Fires on the GENEROUS direction: the outcome whose posted probability
        # sits furthest BELOW the model, i.e. the leg priced too long. The
        # opposite sign is the book charging too much, which is not a bet.
        if HALF_RESULT_ENABLED and m.sport == "soccer" and submarket is None:
            ftv = views.get("FT")
            if (ftv is not None and ftv.ml_phome3 is not None
                    and ftv.ml_pdraw3 is not None):
                ph, pd = ftv.ml_phome3, ftv.ml_pdraw3
                pa = 1.0 - ph - pd
                if 0.0 < ph < 1.0 and 0.0 < pd < 1.0 and 0.0 < pa < 1.0:
                    from src.soccer_model import (fit_lambdas, half_result_probs,
                                                  split_for_league)
                    try:
                        lh, la, info = fit_lambdas(ph, pd, pa)
                        ok = bool(info.get("converged", True))
                    except Exception:
                        ok = False
                    if ok:
                        # Bias-corrected halves — see HALF_BIAS in soccer_model
                        # for why raw Poisson halves are not used here.
                        h1p, h2p = half_result_probs(lh, la, (ph, pd, pa),
                                                     split=split_for_league(m.league))
                        for per, (mh, md, ma) in (("H1", h1p), ("H2", h2p)):
                            v = views.get(per)
                            if v is None or v.ml_phome3 is None or v.ml_pdraw3 is None:
                                continue
                            posted = {"1": v.ml_phome3, "X": v.ml_pdraw3,
                                      "2": 1.0 - v.ml_phome3 - v.ml_pdraw3}
                            model = {"1": mh, "X": md, "2": ma}
                            gen = max(posted, key=lambda k: model[k] - posted[k])
                            gap = (model[gen] - posted[gen]) * 100.0
                            if gap < HALF_RESULT_PP:
                                continue
                            # the posted price of that leg, for the odds band
                            price = None
                            for o3 in periods.get(per, {}).get("moneyline", []):
                                s3 = o3.selections
                                if {"home", "draw", "away"} <= set(s3):
                                    price = s3[{"1": "home", "X": "draw", "2": "away"}[gen]]
                                    break
                            ev = (price * model[gen] - 1.0) * 100.0 if price else None
                            mk("half_result_vs_ft", f"{per} vs FT",
                               f"{per} result prices {gen} at {posted[gen]*100:.1f}% but the "
                               f"full-time 1X2 ({ph*100:.0f}/{pd*100:.0f}/{pa*100:.0f}) "
                               f"implies {model[gen]*100:.1f}% for that half — {gap:.1f}pp "
                               f"generous"
                               + (f", {gen} @ {price:.2f} is {ev:+.0f}% EV" if price else ""),
                               gap, outcome=gen, odds=price)

        # 4c-bis. ICE HOCKEY: the same overtime identity, one period apart.
        #
        # Hockey posts its two full-game moneylines under DIFFERENT periods,
        # not the same one: the 3-way regulation result is REG (60 minutes,
        # where a tie is a real outcome) and the 2-way winner is FT (the game
        # as it settles, overtime and shootout included). So the AF loop below,
        # which reads both shapes out of a single period view, finds nothing
        # here — the pair has to be assembled across REG and FT.
        #
        # Same arithmetic, and it is arithmetic rather than a model: you cannot
        # win in overtime without first drawing in regulation.
        if m.sport == "icehockey":
            reg, ftv = views.get("REG"), views.get("FT")
            if reg is not None and ftv is not None:
                p, d, q = reg.ml_phome3, reg.ml_pdraw3, ftv.ml_phome
                if p is not None and d is not None and q is not None:
                    floor_pp = (p - q) * 100.0
                    ceil_pp = (q - (p + d)) * 100.0
                    if floor_pp >= ot_box_pp:
                        mk("ot_vs_regulation", "REG vs FT",
                           f"home wins {p*100:.0f}% in regulation but only "
                           f"{q*100:.0f}% including overtime and the shootout — "
                           f"overtime cannot take away a regulation win "
                           f"({floor_pp:.0f}pp below the floor)", floor_pp)
                    elif ceil_pp >= ot_box_pp:
                        mk("ot_vs_regulation", "REG vs FT",
                           f"home wins {q*100:.0f}% including overtime, more than "
                           f"winning ({p*100:.0f}%) or drawing ({d*100:.0f}%) in "
                           f"regulation combined — impossible even winning every "
                           f"overtime ({ceil_pp:.0f}pp above the ceiling)", ceil_pp)
                    else:
                        gap = abs(q - (p + d / 2.0)) * 100.0
                        if gap >= ot_coinflip_pp:
                            mk("ot_vs_regulation", "REG vs FT",
                               f"regulation 1X2 ({p*100:.0f}/{d*100:.0f}/"
                               f"{(1-p-d)*100:.0f}) implies {(p+d/2)*100:.0f}% "
                               f"including overtime if the extra period were even, "
                               f"but the incl-OT winner is priced at {q*100:.0f}% "
                               f"({gap:.0f}pp apart)", gap)

            # 4c-ter. The incl-OT goal ladder must DOMINATE the regulation one.
            #
            # Overtime only ever adds goals, so at every line
            #     P(over N incl OT)  >=  P(over N regulation)
            # exactly, with no tolerance for a model. The book publishes both
            # ladders on the same page — "Total Goals*" and "Total Goals(incl.
            # overtime and penalties)" — so this is its own arithmetic checked
            # against itself, at up to 11 rungs and both team totals per event.
            #
            # The puck line is deliberately NOT checked this way: overtime is
            # sudden death and is only reached from a tie, so the overtime goal
            # always makes a ONE-goal win and winning by 2+ including overtime
            # is the same event as winning by 2+ in regulation. CB prices those
            # two ladders identically on 300 of 300 rungs, correctly.
            for mtype in ("total", "team_total"):
                for team in (None, "home", "away"):
                    reg_l = _ot_ladder(periods.get("REG", {}), mtype, team)
                    ot_l = _ot_ladder(periods.get("FT", {}), mtype, team)
                    for line in sorted(set(reg_l) & set(ot_l)):
                        gap = (reg_l[line] - ot_l[line]) * 100.0
                        if gap >= ot_monotone_pp:
                            what = (f"{team} team total" if team else "total")
                            mk("ot_monotone", "REG vs FT",
                               f"{what} {line:g}: regulation P(over)="
                               f"{reg_l[line]*100:.0f}% but including overtime "
                               f"only {ot_l[line]*100:.0f}% — overtime can only "
                               f"ADD goals, so the incl-OT price can never be "
                               f"the shorter of the two ({gap:.0f}pp backwards)",
                               gap)

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
                           outcome=combo_key, odds=c)
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
                           outcome=combo_key, odds=c)

        # 6. HT/FT vs the bivariate-normal fair model (basketball only —
        #    sigma/rho are calibrated to basketball margins)
        if combo and m.sport == "basketball":
            for kind, periods_label, detail, severity, outcome in _htft_fair_signals(
                combo, views, m.league,
            ):
                mk(kind, periods_label, detail, severity, outcome=outcome)

    flags.sort(key=lambda f: f.severity, reverse=True)
    # 7. DUPLICATE FIXTURE — the only CROSS-event rule here. Runs after the
    #    per-event loop because it needs every event at once.
    flags.extend(_duplicate_fixture_flags(events, meta))

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
        if not HTFT_ODDS_MIN <= cb <= HTFT_FAIR_ODDS_MAX:
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
