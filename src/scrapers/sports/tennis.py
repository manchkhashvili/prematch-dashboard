"""
Tennis-specific configuration for CrystalBet scraping (Phase 3.1).

Tennis on CB shares the IDENTICAL list-view structure as basketball — same
8-entry loadinfo positional layout and same Format-B col layout. Verified
against 575-container live sample (2026-05-26):

  Format A (loadinfo, 8 entries):
    [0] '1'        handicap=''         → ML home
    [1] '\\t2'      handicap=''         → ML away (tab/space prefix; same quirk as basketball/soccer)
    [2] '1'        handicap=''         → AH home
    [3] 'Handicap' handicap='handicap' → AH line landmark (e.g. '+2.5 -2.5')
    [4] '2'        handicap=''         → AH away (no whitespace prefix — disambiguates from ML away)
    [5] 'Und'      handicap=''         → OU under
    [6] 'Game'     handicap='total'    → OU line landmark  (basketball uses 'Point' here; only the `handicap='total'` flag matters)
    [7] 'Over'     handicap=''         → OU over

  Format B (col-divs):
    col0/1: ML home/away
    col2/3/4: AH home / AH line ('+2.5/-2.5') / AH away
    col5/6/7: OU under / OU line / OU over

  Differences from basketball: ONLY the sport name. No draw (2-way ML),
  no quarters, no Q1-Q4 markets. List-view shape is otherwise identical.

This module delegates to basketball's parsers with sport_name="tennis"
override. No tennis-specific detail-page classifier — tennis runs in
list-only mode for v1 (set `SPORTS=tennis:list` to enable). The dashboard's
detail-expansion path checks SKIP_DETAIL_SPORTS at cycle time and bypasses
the per-game ExpandDetail loop entirely.

CB sport_id: 22 (per reference/cb_scraping.md §5).
Pinnacle sport_id: 33 (verify on first live run; if wrong, adjust in pinnacle.py).
"""
from __future__ import annotations

from src.scrapers.sports import basketball

SPORT_ID = 22
SPORT_NAME = "tennis"


def parse_loadinfo(raw, event_id, home, away, league, start_time, fetched_at):
    """Delegate to basketball.parse_loadinfo with sport_name="tennis"."""
    return basketball.parse_loadinfo(
        raw, event_id, home, away, league, start_time, fetched_at,
        sport_name=SPORT_NAME,
    )


def parse_div_odds(container, event_id, home, away, league, start_time, fetched_at):
    """Delegate to basketball.parse_div_odds with sport_name="tennis"."""
    return basketball.parse_div_odds(
        container, event_id, home, away, league, start_time, fetched_at,
        sport_name=SPORT_NAME,
    )


def classify_market_title(title):
    """List-only mode — no detail-page expansion for tennis in v1.

    Returns None for every title so any accidental call into
    cb_detail.parse_detail_page emits no Odds. Phase 3.x can revisit if
    tennis ever needs full-mode detail expansion (the per-game work would
    be heavier than basketball given 500+ tennis matches/day).
    """
    return None
