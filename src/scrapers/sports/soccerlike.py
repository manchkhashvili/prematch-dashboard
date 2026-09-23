"""
Soccer-shaped LSport-only sports — futsal and handball (2026-09-22).

Both are two-half, draw-possible games whose CrystalBet detail page is the
LSport soccer template: a 3-way result, Draw No Bet, an Asian handicap ladder
with a 0.0 rung, half results, half handicaps and totals, team totals and an
HT/FT grid. That is exactly the market set the identity checks read —
`pickem_duplicate` / `pickem_dominance` (DNB vs the 0.0 rung vs the 1X2),
`htft_combo` (the grid vs its own legs), `total_additivity` (halves vs the
match), `favourite_flip`, and the ladder detector — none of which knows what
sport it is looking at. The soccer GOAL MODEL (`half_result_vs_ft`, the E
family) stays soccer-only: it is calibrated on ~2.7 goals a game, and a
futsal match has six, a handball match fifty-five.

So each sport is a thin module: CB's sport id, the soccer classifier for the
titles it already reads, and a prelude for the LSport period vocabulary the
soccer classifier does not (`1st Period Winner`, `Asian Handicap 1st Period`,
`Under/Over - Home Team`, `2nd Half 3 Way`, …). Double Chance, BTTS, race-to
and odd/even stay unclassified: Odds has no shape for them, and Double Chance
was measured derived from the 1X2 on 74 LSport soccer games the same week.

List view: soccer's loadinfo layout reads the 1X2 correctly on both boards
but misreads the second block on futsal (a handicap read as a total at 1.7),
so only the 3-way moneyline is kept from the list view. The LSport cycle
expands every game anyway; list rows are the fallback for a failed expansion.

Measured before building (2026-09-21/22): 4-8 LSport futsal games and 3-4
LSport handball games on the board (Russian Superliga, Thai women's league;
Brazil women's league, Luxembourg), and the soccer engine produced 0 flags on
them with partial classification. Expect little; the point is that the checks
that fire on LSport soccer now run wherever LSport posts the same shape.
"""
from __future__ import annotations

import re
from typing import Optional

from src.scrapers.cb_detail import MarketClassification
from src.scrapers.sports import soccer

# LSport period vocabulary the soccer classifier does not read. Exact,
# lowercase, whitespace-collapsed titles → classification.
_LSPORT_TITLES: dict[str, MarketClassification] = {
    "1st period winner": MarketClassification(market_type="moneyline", period="H1", n_way=3),
    "2nd period winner": MarketClassification(market_type="moneyline", period="H2", n_way=3),
    "2nd half 3 way": MarketClassification(market_type="moneyline", period="H2", n_way=3),
    "asian handicap 1st period": MarketClassification(market_type="spread", period="H1"),
    "asian handicap 2nd period": MarketClassification(market_type="spread", period="H2"),
    "under/over - home team": MarketClassification(market_type="team_total", period="FT", team_side="home"),
    "under/over - away team": MarketClassification(market_type="team_total", period="FT", team_side="away"),
    "under/over 1st period - home team": MarketClassification(market_type="team_total", period="H1", team_side="home"),
    "under/over 1st period - away team": MarketClassification(market_type="team_total", period="H1", team_side="away"),
    "under/over 2nd period - home team": MarketClassification(market_type="team_total", period="H2", team_side="home"),
    "under/over 2nd period - away team": MarketClassification(market_type="team_total", period="H2", team_side="away"),
    "1 period - home team total": MarketClassification(market_type="team_total", period="H1", team_side="home"),
    "1 period - away team total": MarketClassification(market_type="team_total", period="H1", team_side="away"),
    "2 period - home team total": MarketClassification(market_type="team_total", period="H2", team_side="home"),
    "2 period - away team total": MarketClassification(market_type="team_total", period="H2", team_side="away"),
}


def classify(title: str) -> Optional[MarketClassification]:
    t = re.sub(r"\s+", " ", (title or "").strip().lower())
    if not t:
        return None
    hit = _LSPORT_TITLES.get(t)
    if hit is not None:
        return hit
    # Soccer's per-period correct scores and the sport-specific stat boards
    # (corners, bookings) do not exist here; the soccer classifier would not
    # match them anyway, and what it does match is the shared vocabulary.
    return soccer.classify_market_title_permissive(title)


def _only_moneyline(rows):
    return [o for o in rows if o.market_type == "moneyline"]


def make_parsers(sport_name: str):
    """List-view parsers: soccer's, keeping only the 3-way moneyline."""
    def parse_loadinfo(raw, event_id, home, away, league, start_time, fetched_at):
        rows = soccer.parse_loadinfo(raw, event_id, home, away, league, start_time, fetched_at)
        out = _only_moneyline(rows)
        for o in out:
            o.sport = sport_name
        return out

    def parse_div_odds(container, event_id, home, away, league, start_time, fetched_at):
        rows = soccer.parse_div_odds(container, event_id, home, away, league, start_time, fetched_at)
        out = _only_moneyline(rows)
        for o in out:
            o.sport = sport_name
        return out

    return parse_loadinfo, parse_div_odds
