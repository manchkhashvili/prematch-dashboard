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

import re

from src.scrapers.cb_detail import MarketClassification
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
    """Tennis detail-page market titles → canonical MarketClassification.

    Tennis used to return None for everything (list-only mode), so ML / games
    spread / games total were the only tennis markets that ever existed. But a CB
    tennis detail page carries **15 markets on every match** (surveyed live
    2026-08-11, present on 8/8 sampled matches), and they are all functions of the
    SAME four best-of-3 outcomes (2:0, 2:1, 1:2, 0:2). That over-determination is
    what makes tennis worth classifying: the markets must satisfy exact arithmetic
    identities, so contradictions are provable without any model.

    IMPORTANT — CB serves tennis under **two different naming schemes**, almost
    certainly two upstream feeds. Surveyed across ALL 546 live matches
    (2026-08-11), not a sample: an early 30-match sample saw only scheme B and
    missed the dominant one entirely, which left match-winner coverage at 43
    events against 218 with a first-set winner.

      scheme A — 71% of matches (the one in the live board screenshot)
        Winner                          1/2      -> moneyline FT
        1st Period Winner Home/Away     1/2      -> moneyline H1
        2nd Period Winner Home/Away     1/2      -> moneyline H2
        Under/Over Sets                 Und/Over -> total FT
        Number Of Sets                  2/3 sets -> same market, different words
        Correct Score                   2-0 ...  -> correct_score FT
        To win 1st set & win the match  1/2      -> skipped; this is P(1/1), the
                                                    combo leg, worth wiring next
        Asian Handicap Sets/Games/1st Period, Under/Over Games, Odd/Even, ...

      scheme B — 29% of matches
        Which player will win the match 1/2      -> moneyline FT
        1st Set - Winner                1/2      -> moneyline H1
        2nd Set - Winner                1/2      -> moneyline H2
        1st Set / Match*                1/1..2/2 -> htft FT  (structurally identical
                                                    to soccer's HT/FT combo, so the
                                                    existing htft_combo dominance +
                                                    correlation bounds apply as-is)
        Total sets                      Und/over -> total FT, line 2.5
        Exact Sets                      2/3 sets -> same market as "Total sets"
        Set Handicap                    1(-1.5)  -> skipped (see note below)
        Home/Away Team To Win a Set     Yes/No   -> skipped
        Home/Away to win exactly 1 set  Yes/No   -> skipped
        Correct score                   2:0 ...  -> correct_score FT
        Match to end 2:0 / 0:2          Yes/No   -> skipped
        Odd/even games                  odd/even -> skipped

    Matching "winner" must be EXACT — a substring test would also swallow
    "Winner & total", "1 set - winner & total" and the per-set winners.

    The skipped ones are real and useful — every one is a linear function of the
    four correct-score probabilities. They are catalogued here so the next pass
    knows exactly what is on the table.

    CORRECTION (2026-09-08): this paragraph used to claim wiring them "needs a
    schema change", because Odds had no representation for a 4-way market. That
    was wrong and it cost the correct-score market a month on the bench —
    `selections` is a free-form dict and htft has carried NINE keys through it
    since soccer. Widening the MarketType Literal was the whole change. The
    remaining skips (the Yes/No shapes, "To win 1st set & win the match") are
    still genuinely unrepresented, but for a different reason: they are
    2-way projections OF the correct-score partition, so they want the same
    market row, not a new market type.

    NOTE the spread here is a SET handicap (±1.5 sets), while the list-view
    spread for tennis is a GAMES handicap. Both land on market_type "spread",
    period "FT" — so the detail one is deliberately NOT emitted as spread to
    avoid colliding with (and overwriting) the list-view games line. It is left
    unclassified until Odds can carry the distinction.
    """
    t = (title or "").strip().lower()
    if not t:
        return None

    # ── match winner ────────────────────────────────────────────────────────
    # Exact "winner" only. Substring matching would swallow "Winner & total",
    # "1 set - winner & total" and the per-set winners.
    if t == "winner":
        return MarketClassification(market_type="moneyline", period="FT", n_way=2)
    if "win the match" in t and "set" not in t and "&" not in t:
        return MarketClassification(market_type="moneyline", period="FT", n_way=2)

    # ── per-set winners ─────────────────────────────────────────────────────
    # "1st Set - Winner" (scheme B) and "1st Period Winner Home/Away" (scheme A).
    m = re.match(r"^(1st|2nd)\s+(set|period)\s*[-–]?\s*winner", t)
    if m:
        return MarketClassification(market_type="moneyline",
                                    period="H1" if m.group(1) == "1st" else "H2",
                                    n_way=2)

    # ── set/match combo — same 4-cell shape as soccer HT/FT ─────────────────
    if re.match(r"^1st\s+set\s*/\s*match", t):
        return MarketClassification(market_type="htft", period="FT")

    # ── total SETS (the 2.5 line on best-of-3) ──────────────────────────────
    # Three different wordings for the same market across the two schemes;
    # "Number Of Sets" and "Exact Sets" are the same thing said differently,
    # which is itself a consistency opportunity once Odds can hold both.
    if t.startswith("total sets") or t.startswith("under/over sets"):
        return MarketClassification(market_type="total", period="FT", n_way=2)

    # ── correct score (the exact set score) ─────────────────────────────────
    # Wired 2026-09-08. EXACT titles only, deliberately: this market is an
    # exhaustive partition of the match, and the checks built on it devig
    # across the whole set of legs. A substring test would also match a
    # per-set correct score ("Correct score 1st set" — games within one set),
    # whose legs are a partition of something else entirely; folding those in
    # would devig two different markets together and invent edge out of it.
    # Both CB naming schemes reduce to the same lowercase string; the list is
    # here so an unrecognised third wording stays unclassified rather than
    # being guessed at.
    if t in ("correct score", "correct score sets"):
        return MarketClassification(market_type="correct_score",
                                    period="FT", n_way=4)
    return None
