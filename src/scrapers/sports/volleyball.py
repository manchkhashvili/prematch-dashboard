"""
Volleyball on CrystalBet — an LSport-only sport (2026-09-21).

Added for the New inconsistencies cycle, not the price pipeline: Pinnacle
prices ~12 volleyball matchups, so there is no reference to price against,
but 75 % of CB's volleyball board comes from the LSport feed — the largest
LSport share of any sport — and a best-of-5 match is over-determined the way
tennis is: the correct score, the sets handicap, the total sets, the per-set
winners and the match winner are all functions of the same six outcomes.

List view: IDENTICAL layout to basketball/tennis (8-entry loadinfo = ML / points
handicap / points total; Format-B cols the same), verified live on 22 games, so
the list parsers delegate exactly as tennis does.

Detail page, LSport vocabulary (the other feed's titles are accepted where they
are unambiguous, but they are 25 % of the board and none of the checks were
calibrated on them):

    Main result / Winner                  1/2         -> moneyline FT
    1st Period Winner / 1st Set - Winner  1/2         -> moneyline H1
    2nd Period Winner / 2nd Set - Winner  1/2         -> moneyline H2
    Correct Score                         3:0 .. 0:3  -> correct_score FT (6 cells)
    Asian Handicap Sets / Set Handicap    1(-1.5)     -> spread FT, submarket "sets"
    Total sets                            Under 3.5   -> total FT,  submarket "sets"
    total points / Total Points           Under 174.5 -> total FT
    Point handicap                        1(-11.5)    -> spread FT
    Total hometeam / Home Team total      Under 94.5  -> team_total FT home
    Away Team / Away Team total           Under 83.5  -> team_total FT away
    1 Set - Total Points                  Under 44.5  -> total H1
    1 set - Point Handicap                1(-2.5)     -> spread H1
    1st Period - Home Team / - Away Team  Under 22.5  -> team_total H1
    2 set - total points / point handicap             -> total / spread H2
    2nd Period - Home Team / - Away Team              -> team_total H2

The SETS markets carry submarket "sets" so they never share a ladder or a
period view with the POINTS markets that sit on the same (period, market_type)
— a −1.5 sets rung and a −11.5 points rung are different ladders. The
consistency engine reads them back across the submarket boundary explicitly
(see the volleyball block in src/consistency.py). Sets 3–5, odd/even, "race to"
and the yes/no props are left unclassified: the props are 2-way projections
of the correct-score partition, and the later sets' winners are derived from
the match price on this feed (measured: the 1st-set winner matches an IID
inversion of the match price to a median 0.0pp, max 1.3pp, over 13 games).

MEASURED before building (2026-09-21, 15 LSport games, docs/volleyball.md):
the correct-score board is generated from the match price by a fixed
set-to-set correlation — a latent-strength fit at sigma^2 = 0.032 reproduces
all six cells to 1.14pp RMS (IID: 4.07pp) — and every posted leg was 9–21 %
under the model fair. One independent price pair on the whole board: the
sets handicap against the correct-score cell it equals (22 % apart on one
game). Expect roughly a row a day, not a firehose.

CB sport_id: 21. No Pinnacle mapping (not fetched).
"""
from __future__ import annotations

import re

from src.scrapers.cb_detail import MarketClassification
from src.scrapers.sports import basketball

SPORT_ID = 21
SPORT_NAME = "volleyball"

# The sets markets live on their own submarket (see module docstring).
SETS = "sets"


def parse_loadinfo(raw, event_id, home, away, league, start_time, fetched_at):
    return basketball.parse_loadinfo(
        raw, event_id, home, away, league, start_time, fetched_at,
        sport_name=SPORT_NAME,
    )


def parse_div_odds(container, event_id, home, away, league, start_time, fetched_at):
    return basketball.parse_div_odds(
        container, event_id, home, away, league, start_time, fetched_at,
        sport_name=SPORT_NAME,
    )


_RE_SET_WINNER = re.compile(r"^(1st|2nd)\s+(period|set)\s*[-–]?\s*winner")
_RE_SET_N = re.compile(r"^(1|2)(?:st|nd)?\s+(?:set|period)\s*[-–]?\s*(.*)$")


def classify_market_title(title):
    """Volleyball detail-page titles → MarketClassification (see module doc)."""
    t = re.sub(r"\s+", " ", (title or "").strip().lower())
    if not t:
        return None

    # ── match winner ────────────────────────────────────────────────────────
    if t in ("main result", "winner", "match winner"):
        return MarketClassification(market_type="moneyline", period="FT", n_way=2)

    # ── set winners (sets 1 and 2 only; H1/H2 by the tennis convention) ────
    m = _RE_SET_WINNER.match(t)
    if m:
        return MarketClassification(market_type="moneyline",
                                    period="H1" if m.group(1) == "1st" else "H2",
                                    n_way=2)

    # ── the exact set score: EXACT titles only (a per-set correct score is a
    #    partition of something else and must not be folded in) ─────────────
    if t in ("correct score", "correct score sets"):
        return MarketClassification(market_type="correct_score",
                                    period="FT", n_way=6)

    # ── sets markets, on their own submarket ────────────────────────────────
    if t in ("asian handicap sets", "set handicap", "sets handicap"):
        return MarketClassification(market_type="spread", period="FT", submarket=SETS)
    if t in ("total sets", "under/over sets"):
        return MarketClassification(market_type="total", period="FT", submarket=SETS)

    # ── match points ────────────────────────────────────────────────────────
    if t in ("total points", "total", "under/over"):
        return MarketClassification(market_type="total", period="FT")
    if t in ("point handicap", "points handicap", "asian handicap", "handicap"):
        return MarketClassification(market_type="spread", period="FT")
    if t in ("total hometeam", "home team total", "under/over - home team", "home team"):
        return MarketClassification(market_type="team_total", period="FT", team_side="home")
    if t in ("total awayteam", "away team total", "under/over - away team", "away team"):
        return MarketClassification(market_type="team_total", period="FT", team_side="away")

    # ── per-set points (sets 1 and 2) ───────────────────────────────────────
    m = _RE_SET_N.match(t)
    if m:
        per = "H1" if m.group(1) == "1" else "H2"
        rest = m.group(2).strip()
        if rest in ("total points", "total", "- total points", "under/over"):
            return MarketClassification(market_type="total", period=per)
        if rest in ("point handicap", "points handicap", "handicap", "- point handicap"):
            return MarketClassification(market_type="spread", period=per)
        if rest in ("home team", "- home team", "home team total"):
            return MarketClassification(market_type="team_total", period=per, team_side="home")
        if rest in ("away team", "- away team", "away team total"):
            return MarketClassification(market_type="team_total", period=per, team_side="away")
    return None


# The LSport cycle parses in per-section ladder mode with the permissive
# classifier; volleyball has one classifier, so both names point at it.
classify_market_title_permissive = classify_market_title
