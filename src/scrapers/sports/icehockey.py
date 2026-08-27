"""
Ice-hockey configuration for CrystalBet scraping (Phase 3.3).

Fifth sport on the dashboard, after basketball, soccer, tennis and American
football. Chosen because it is the best-covered sport that CrystalBet and
Pinnacle BOTH price and neither already carries: 322 CB games and 47 Pinnacle
matchups on 2026-08-27, against table tennis's 893 CB games and **zero**
Pinnacle matchups.

Everything below was measured against the whole live board (349 CB events,
every Pinnacle hockey league), not a sample.

═══════════════════════════════════════════════════════════════════════════
THE ONE THING TO GET RIGHT: REGULATION IS NOT FULL TIME
═══════════════════════════════════════════════════════════════════════════

Hockey is the first sport here where a book prices the same market twice — once
for the 60 minutes of regulation and once for the game as it settles, overtime
and shootout included — and where BOTH are high-volume on both books.

    CrystalBet    "Total Goals*"                                  regulation
                  "Total Goals(incl. overtime and penalties)"     incl-OT
                  "Main result"                    3-way          regulation
                  "Winner (incl. overtime and penalties)"  2-way  incl-OT

    Pinnacle      period 6   3-way ML, spread, total, team_total  regulation
                  period 0   2-way ML only                        incl-OT
                  period 1   1st period (no period 2 or 3 exists)

Getting this backwards does not drop rows or raise anything. It silently
matches a 60-minute total against a full-game total on EVERY event and reports
the overtime goals as edge. So it was verified three ways before a line of this
file was written:

1. Pinnacle's own two moneylines must satisfy the overtime box — you cannot win
   in overtime without first drawing in regulation:

       P(win reg)  <=  P(win incl OT)  <=  P(win reg) + P(tie)

   Across 64 (event, side) pairs: **0 violations in either direction**, and the
   implied P(win in OT | tied after 60) had median **0.502**. A coin flip is
   what that conditional has to be; a wrong period assignment could not produce
   it by accident.

2. CrystalBet's own pair, same test, 170 (event, side) pairs: 0 impossible,
   median 0.500 (p10 0.477, p90 0.524).

3. The two goal ladders compared at IDENTICAL lines, which removes the 0.5-goal
   ladder granularity: incl-OT devigged P(over) ran **+0.024** above regulation
   overall and +0.03..+0.06 mid-ladder, positive on 60 % and negative on none.
   Team totals: **+0.017, positive on 868 of 888 rungs**.

Hence `Period` gained "REG". "FT" keeps its meaning everywhere — the game as it
settles — and "REG" is the 60-minute market.

THE PUCK LINE IS EXEMPT, AND NOT BY LUCK
----------------------------------------
CB serves "Handicap" and "Handicap (incl. overtime and penalties)*" with
byte-identical prices. That is not duplication, it is arithmetic: overtime is
sudden death and is only ever reached from a tie, so the overtime goal always
produces a ONE-goal win. Winning by two or more including overtime is the same
event as winning by two or more in regulation.

Measured: identical on **300 of 300** rungs, at every |line| CB offers
(1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5). CB offers no ±0.5 handicap, which is the
only line where the two would diverge. So both ladders are emitted at REG and
either may be matched against Pinnacle's period-6 spread.

═══════════════════════════════════════════════════════════════════════════
LIST VIEW — a 12-column layout that is neither basketball's nor soccer's
═══════════════════════════════════════════════════════════════════════════

All 322 containers shipped Format B (col-divs) and **zero** shipped
`data-loadinfo`. Verified layout (Toronto Maple Leafs - Montreal Canadiens):

    col0  col1  col2    1X2 regulation      2.30 / 3.85 / 2.35
    col3  col4  col5    Double Chance       1.45 / 1.20 / 1.45   SKIP
    col6  col7  col8    Handicap home / line "-0.5/+0.5" / away
    col9  col10 col11   Total under / line "6.0" / over

Basketball has 8 cols and no draw; soccer has 13 and no handicap. Hockey has
both a draw and a handicap, so neither parser can be delegated to — this module
carries its own. `parse_loadinfo` is a documented no-op: the format does not
appear on this board, and writing a parser against a shape never observed would
be inventing a layout rather than reading one.

The list-view 1X2 is the REGULATION result (it matches the detail page's "Main
result" cell for cell), so the list parser emits period "REG", not "FT".

═══════════════════════════════════════════════════════════════════════════
DETAIL PAGE — whole-board title census (349 events)
═══════════════════════════════════════════════════════════════════════════

    ev   title                                            ->
   129   Main result                            3-way     moneyline REG
   129   Draw No Bet*                           2-way     skip (no counterpart)
   129   Double chance                                    skip
    89   Total Goals*                          11 rungs   total REG
    89   Handicap                               7 rungs   spread REG
    89   Handicap(1X2)*                         6 rungs   skip strict / permissive
    89   Home Team Total, Away Team total*      7 rungs   team_total REG
    89   Correct Score, Winning margin, Odd/Even*,        skip
          Both Teams to Score*, Matchbet and Total goals*
    85   Winner (incl. overtime and penalties)  2-way     moneyline FT
    53   1st/2nd/3rd Period - 1X2*              3-way     permissive only
    53   1/2/3 Period - Draw No Bet*            2-way     moneyline P1/P2/P3
    53   Nth Period - Handicap*                           spread P1/P2/P3
    51   Nth Period - Total Goals*                        total P1/P2/P3
    51   N period - Home/Away Team total        5 rungs   team_total P1/P2/P3
    49   Total Goals(incl. overtime and penalties)        total FT
    49   Handicap (incl. overtime and penalties)*         spread REG  (see above)
    49   Home/Away Team total (incl. overtime ...)        team_total FT
    49   Will there be overtime*, Goals Even/Odd (...)    skip
    43   1 goal*, Last Goal*, Nth Period - 1st Goal       skip
    32   Winner / Runner UP / Miss Playoffs / ...         skip — season outrights

Three traps, all live on the same page:

**(a) The period prefix is spelled three ways.** A single NHL detail page
carries `1st Period - 1X2*`, `1 Period - Draw No Bet*` and `1 period - Home
Team total` — ordinal-with-suffix, bare-digit-capitalised, and bare-digit-
lowercase. This is American football's bare-digit quarter trap again, except
here all three spellings coexist on one page rather than across leagues. A
pattern anchored on `1st period` alone drops two thirds of the period markets,
and worse, they then fall through to a period-less default and interleave their
~1.5-goal ladders with the ~5.5-goal full-game one.

**(b) CB's own typo is load-bearing.** The third-period scoring markets ship as
`2rd Period - Last Team To Score*` — sitting inside the 3rd-period block, named
"2rd". Both are skip-markets so nothing downstream cares, but a permissive
period deriver that trusts the digit assigns 3rd-period prices to P2. The
deriver here requires the ordinal SUFFIX to agree with the digit before it
trusts either.

**(c) `Draw No Bet` is the 2-way period moneyline.** Pinnacle's period-1 hockey
moneyline is 2-way (33 rows on the board, not one with a draw designation),
while CB's `Nth Period - 1X2*` is 3-way. Emitting the 3-way as the matched
moneyline would score a three-outcome price against a two-outcome one. Same
resolution basketball uses for H1: DNB is the strict 2-way, the 1X2 goes out on
the permissive path for CB-internal ladder checks only.

CB sport_id: 18. Pinnacle sport_id: 19 ("Hockey").
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

from src.models import Odds
from src.scrapers.cb_detail import MarketClassification
from src.scrapers.sports.basketball import _make_odds, _parse_float, _safe_float

log = logging.getLogger(__name__)

SPORT_ID = 18
SPORT_NAME = "icehockey"


# ── List view ─────────────────────────────────────────────────────────────────

def parse_loadinfo(raw, event_id, home, away, league, start_time, fetched_at):
    """No-op: CrystalBet ships no `data-loadinfo` for ice hockey.

    Checked against the whole live board — 322 containers, zero with the
    attribute. Every other sport serves both formats, so the hook stays for
    shape-compatibility with `_SPORT_MODULES`, but returning [] is the honest
    answer. Writing a positional parser for a layout that has never been
    observed would be inventing one; if CB starts serving loadinfo here, the
    Format-B parser below already documents which column means what.
    """
    return []


def parse_div_odds(
    container,
    event_id: str,
    home: str,
    away: str,
    league: Optional[str],
    start_time: Optional[datetime],
    fetched_at: datetime,
    sport_name: str = SPORT_NAME,
) -> list[Odds]:
    """Parse the 12-column Format-B layout (see module docstring).

        col0/1/2    1X2 regulation (home / draw / away)
        col3/4/5    Double Chance                        SKIP
        col6/7/8    handicap home / line "-0.5/+0.5" / away
        col9/10/11  total under / line "6.0" / over

    Everything here is REGULATION — the list-view 1X2 matches the detail page's
    "Main result" cell for cell, and neither the handicap nor the total carries
    CB's "(incl. overtime and penalties)" marking. The handicap is emitted at
    REG rather than FT even though the two are arithmetically identical at the
    lines CB offers, so that its period says what the market is rather than
    what it happens to equal.
    """
    col_map: dict[int, str] = {}
    for div in container.select(
        "div.x_loop_res.Snatch, div.x_loop_h_res.HandicapSnatch"
    ):
        for cls in (div.get("class") or []):
            if cls.startswith("col") and cls[3:].isdigit():
                col_map[int(cls[3:])] = div.text.strip()

    results: list[Odds] = []

    # 1X2 — 3-way, regulation. Unlike basketball/tennis there IS a draw.
    if {0, 1, 2} <= col_map.keys():
        ml = _make_odds(
            home=home, away=away, market_type="moneyline", period="REG",
            selections={"home": _safe_float(col_map[0]),
                        "draw": _safe_float(col_map[1]),
                        "away": _safe_float(col_map[2])},
            fetched_at=fetched_at, event_id=event_id,
            league=league, start_time=start_time, sport_name=sport_name,
        )
        if ml:
            results.append(ml)

    # Handicap — col7 text is "-0.5/+0.5", home line first.
    if {6, 7, 8} <= col_map.keys() and col_map[7]:
        try:
            ah_line: Optional[float] = float(col_map[7].split("/")[0])
        except ValueError:
            ah_line = None
        if ah_line is not None:
            ah = _make_odds(
                home=home, away=away, market_type="spread", period="REG",
                selections={"home": _safe_float(col_map[6]),
                            "away": _safe_float(col_map[8])},
                line=ah_line,
                fetched_at=fetched_at, event_id=event_id,
                league=league, start_time=start_time, sport_name=sport_name,
            )
            if ah:
                results.append(ah)

    # Total — col10 text is "6.0".
    if {9, 10, 11} <= col_map.keys() and col_map[10]:
        ou_line = _parse_float(col_map[10])
        if ou_line is not None:
            ou = _make_odds(
                home=home, away=away, market_type="total", period="REG",
                selections={"over": _safe_float(col_map[11]),
                            "under": _safe_float(col_map[9])},
                line=ou_line,
                fetched_at=fetched_at, event_id=event_id,
                league=league, start_time=start_time, sport_name=sport_name,
            )
            if ou:
                results.append(ou)

    return results


# ── Detail-page classifier ────────────────────────────────────────────────────

# CB writes the period prefix three ways on a single page — "1st Period - ",
# "1 Period - " and "1 period - ". The digit and the ordinal suffix must AGREE
# where both are present, so that CB's real "2rd Period" typo (which sits in
# the 3rd-period block) is refused rather than filed under P2.
_PERIOD_PREFIX = re.compile(
    r"^(?P<n>[123])\s*(?P<suffix>st|nd|rd|th)?\s*period\s*[-–]\s*",
)
_ORDINAL_FOR = {"1": "st", "2": "nd", "3": "rd"}

# "(incl. overtime and penalties)" — with or without the space before the paren
# and with or without CB's trailing asterisk.
_INCL_OT = r"[\s(]+incl(?:uding)?\.?\s*overtime"


def _split(t: str) -> tuple[Optional[str], str]:
    """('P1', 'total goals') for '1st period - total goals'; (None, t) if none.

    Refuses a digit/suffix disagreement. CB really does ship
    `2rd Period - Last Team To Score*` inside its 3rd-period block; trusting
    either half of that name alone files third-period prices under the wrong
    period. Both skip-markets today, but the deriver should not be the reason
    a future one lands wrong.
    """
    m = _PERIOD_PREFIX.match(t)
    if not m:
        return None, t
    n, suffix = m.group("n"), m.group("suffix")
    if suffix and suffix != _ORDINAL_FOR[n]:
        log.debug("icehockey: refusing inconsistent period prefix %r", t[:40])
        return None, t
    return f"P{n}", t[m.end():].strip()


def classify_market_title(title: str) -> Optional[MarketClassification]:
    """CB hockey detail-page title → canonical MarketClassification.

    Periods: "REG" = 60 minutes, "FT" = incl. overtime and shootout,
    "P1".."P3" = the three regulation periods. See the module docstring for why
    REG and FT must not be merged, and for the whole-board census behind each
    rule below.

    Returns None for the ~40 % of titles that are real markets with no
    representation in `Odds` — correct score, winning margin, both-teams-to-
    score, odd/even, "will there be overtime", first/last goal, the
    Matchbet-and-Total combo, and the NHL season outrights.
    """
    t = re.sub(r"\s+", " ", (title or "")).strip().lower().rstrip("*").strip()
    if not t:
        return None

    period, rest = _split(t)

    # ── period markets (P1..P3) ─────────────────────────────────────────────
    if period is not None:
        # Draw No Bet is the 2-way period moneyline — Pinnacle's period-1 ML is
        # 2-way, so the 3-way "1X2" must NOT become the matched moneyline.
        if rest.startswith("draw no bet"):
            return MarketClassification(market_type="moneyline",
                                        period=period, n_way=2)
        if rest.startswith("handicap"):
            return MarketClassification(market_type="spread", period=period)
        if rest.startswith("total goals"):
            return MarketClassification(market_type="total", period=period)
        if rest.startswith("home team total"):
            return MarketClassification(market_type="team_total",
                                        period=period, team_side="home")
        if rest.startswith("away team total"):
            return MarketClassification(market_type="team_total",
                                        period=period, team_side="away")
        # 1X2 / Double Chance / Both Teams To Score / Goals Even-Odd /
        # 1st Goal / Last Team To Score — see classify_market_title_permissive.
        return None

    incl_ot = re.search(_INCL_OT, t) is not None

    # ── moneyline ───────────────────────────────────────────────────────────
    # Two different markets, two different periods, and the only reason they
    # are distinguishable is CB's parenthetical.
    if t.startswith("winner") and incl_ot:
        return MarketClassification(market_type="moneyline", period="FT", n_way=2)
    if t == "main result":
        return MarketClassification(market_type="moneyline", period="REG", n_way=3)

    # ── totals ──────────────────────────────────────────────────────────────
    if t.startswith("total goals"):
        return MarketClassification(market_type="total",
                                    period="FT" if incl_ot else "REG")

    # ── team totals ─────────────────────────────────────────────────────────
    for prefix, side in (("home team total", "home"), ("away team total", "away")):
        if t.startswith(prefix):
            # "Home Team clean sheet (incl. overtime and penalties)" also starts
            # with neither prefix, so no guard is needed for it — but "home team
            # total" must not swallow a future "home team total odd/even".
            if "odd" in t or "even" in t:
                return None
            return MarketClassification(market_type="team_total",
                                        period="FT" if incl_ot else "REG",
                                        team_side=side)

    # ── handicap ────────────────────────────────────────────────────────────
    # "Handicap(1X2)*" is 3-way and has no Pinnacle counterpart — permissive
    # path only. Checked BEFORE the plain-handicap rule, which would match it.
    if t.startswith("handicap") and "1x2" in t:
        return None
    if t.startswith("handicap"):
        # Both ladders are emitted at REG: they are the same bet at every line
        # CB offers (300/300 rungs identical, |line| >= 1.5), because overtime
        # is sudden death and can only ever produce a one-goal win.
        return MarketClassification(market_type="spread", period="REG")

    return None


# ── Permissive classifier (anomaly scan only) ────────────────────────────────
# Everything the strict classifier refuses because Pinnacle has no counterpart,
# but which is still a well-formed ladder or n-way market CB must keep
# internally consistent. Never matched against Pinnacle; feeds the CB-internal
# consistency checks only.

def classify_market_title_permissive(title: str) -> Optional[MarketClassification]:
    """Strict classification, plus the CB-only shapes worth checking.

    Adds:
      * "Main result" stays 3-way REG (already strict) and the per-period
        "Nth Period - 1X2*" joins it at 3-way P1..P3, which is what lets the
        regulation-vs-incl-OT box run per period.
      * "Handicap(1X2)*" — the 3-way handicap ladder.
      * "Draw No Bet*" at full time, which the strict path skips because
        Pinnacle prices nothing against it.
    """
    strict = classify_market_title(title)
    if strict is not None:
        return strict

    t = re.sub(r"\s+", " ", (title or "")).strip().lower().rstrip("*").strip()
    if not t:
        return None
    period, rest = _split(t)

    if period is not None:
        if rest.startswith("1x2"):
            return MarketClassification(market_type="moneyline",
                                        period=period, n_way=3)
        return None

    if t.startswith("draw no bet"):
        # Regulation with the tie voided, not the incl-OT moneyline said twice.
        # Both hypotheses are plausible from the label, so it was measured: the
        # quoted DNB tracks P_reg(home)/(P_reg(home)+P_reg(away)) to a median
        # 0.77pp (n=125), against 1.87pp (n=85) for the incl-OT price. Hence
        # period REG. The selection count keeps it from colliding with the
        # 3-way "Main result" that shares that period.
        return MarketClassification(market_type="moneyline", period="REG", n_way=2)
    if t.startswith("handicap") and "1x2" in t:
        return MarketClassification(market_type="spread", period="REG", n_way=3)
    return None
