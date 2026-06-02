"""
MMA-specific configuration for CrystalBet scraping (Phase 4.3, 2026-05-27).

MMA on CB has a NARROWER list-view structure than basketball/tennis — no
handicap section at all (you can't spread a fight). Verified against the
58-container live sample captured 2026-05-27 (CC discovery — see
notes/cc_mma_findings.md).

  Format A (loadinfo, 5 entries):
    [0] name='1'     handicap=''       → ML home
    [1] name='\\t2'   handicap=''       → ML away  (same \\t whitespace quirk as soccer/tennis)
    [2] name='Und'   handicap='total'  → OU under odds  ← BOTH this AND [3] are flagged 'total'
    [3] name='Tot'   handicap='total'  → OU line value  ← unlike basketball where only the line entry is flagged
    [4] name='Over'  handicap=''       → OU over odds

  Format B (col-divs, 5 cols populated):
    col0 → ML home odds
    col1 → ML away odds
    col2 → EmptySnatch  (no AH home — MMA has no handicap)
    col3 → '' (total line, available only on detail page)
    col4 → EmptySnatch  (no AH away)

**Why we can't reuse basketball.parse_loadinfo directly:** the basketball
parser searches for the FIRST entry with `handicap='total'` and treats it
as the OU LINE landmark, with under at line-1 and over at line+1. MMA has
TWO entries flagged 'total' (Und + Tot) and `Tot` is the second one — the
basketball parser would put `ou_idx=2` (Und) and then under-check items[1]
(ML away, name='\\t2' which strips to '2', does NOT match 'und'), correctly
emitting only ML. We'd lose every OU row without a custom parser.

**Format B fallback:** delegate to basketball.parse_div_odds with
sport_name="mma". Basketball gracefully rejects missing AH/OU cols
(EmptySnatch values fail _safe_float's >1.0 check), so we get ML-only
output for Format B containers — which is what we want.

CB sport_id: 69
Pinnacle sport_id: 22 — Mixed Martial Arts (UFC, LFA, Road to the UFC)

List-only mode; no detail-page expansion needed for v1 (`SPORTS=mma:list`).
Add the per-fight method-of-victory / round-betting expander as a Phase 4.x
follow-up if you start placing prop bets.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Optional

from src.models import Odds
from src.scrapers.sports import basketball

log = logging.getLogger(__name__)

SPORT_ID = 69
SPORT_NAME = "mma"


def parse_loadinfo(
    raw: str,
    event_id: str,
    home: str,
    away: str,
    league: Optional[str],
    start_time: Optional[datetime],
    fetched_at: datetime,
) -> list[Odds]:
    """
    Parse MMA's 5-entry loadinfo. Emits up to 2 Odds rows per fight:
    moneyline FT, total FT.

    Total emission requires all three of (Und, Tot, Over) to have parseable
    values. Per discovery, 38/53 sample games have blank Over — those emit
    ML only, which is the intended behaviour.
    """
    if not raw:
        return []

    # Clean control chars + trailing-comma noise the same way basketball does.
    raw = re.sub(r"[\x00-\x1f]", " ", raw)
    raw = re.sub(r",\s*\]", "]", raw)
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("MMA loadinfo JSON error for %s vs %s: %s", home, away, exc)
        return []

    if not isinstance(items, list):
        return []

    # ── Index identification ────────────────────────────────────────────────
    # ML home/away: handicap='' AND stripped name is '1' / '2'. Take FIRST
    # match for each — same convention as basketball/tennis (the leading
    # whitespace on ML away "\t2" / " 2" does not affect .strip()).
    ml_home_idx: Optional[int] = None
    ml_away_idx: Optional[int] = None
    for i, e in enumerate(items):
        if not isinstance(e, dict):
            continue
        if e.get("handicap") != "":
            continue
        stripped = (e.get("name") or "").strip()
        if stripped == "1" and ml_home_idx is None:
            ml_home_idx = i
        elif stripped == "2" and ml_away_idx is None:
            ml_away_idx = i

    # OU: identify by NAME only (the 5-entry MMA layout has unique names —
    # 'Und' / 'Tot' / 'Over' don't collide with anything else). The handicap
    # flag is unstable across populated vs blank state:
    #
    #   When OU is BLANK (no live odds — 38/53 sample games):
    #     Und  → handicap='total'  bet=' '
    #     Tot  → handicap='total'  bet=' '
    #     Over → handicap='total'  bet=' '
    #
    #   When OU is LIVE (15/53 sample games):
    #     Und  → handicap='total'  bet=<under odds>
    #     Tot  → handicap='total'  bet=<line value>
    #     Over → handicap=''       bet=<over odds>   ← flag flips!
    #
    # Our initial discovery report had the populated case's Over handicap
    # right but missed the blank case's handicap='total'. Filtering by name
    # only sidesteps both forms cleanly.
    und_idx: Optional[int] = None
    tot_idx: Optional[int] = None
    over_idx: Optional[int] = None
    for i, e in enumerate(items):
        if not isinstance(e, dict):
            continue
        name = (e.get("name") or "").strip().lower()
        if name in ("und", "under") and und_idx is None:
            und_idx = i
        elif name in ("tot", "total") and tot_idx is None:
            tot_idx = i
        elif name in ("over", "ov") and over_idx is None:
            over_idx = i

    results: list[Odds] = []

    # ── Moneyline ──────────────────────────────────────────────────────────
    if ml_home_idx is not None and ml_away_idx is not None:
        ml = basketball._make_odds(
            home=home, away=away, market_type="moneyline",
            selections={
                "home": basketball._safe_float(items[ml_home_idx]["bet"]),
                "away": basketball._safe_float(items[ml_away_idx]["bet"]),
            },
            fetched_at=fetched_at, event_id=event_id,
            league=league, start_time=start_time,
            sport_name=SPORT_NAME,
        )
        if ml:
            results.append(ml)

    # ── Total rounds ───────────────────────────────────────────────────────
    if (und_idx is not None and tot_idx is not None and over_idx is not None):
        line = basketball._parse_float(items[tot_idx].get("bet", ""))
        if line is not None:
            ou = basketball._make_odds(
                home=home, away=away, market_type="total",
                selections={
                    "under": basketball._safe_float(items[und_idx]["bet"]),
                    "over":  basketball._safe_float(items[over_idx]["bet"]),
                },
                line=line,
                fetched_at=fetched_at, event_id=event_id,
                league=league, start_time=start_time,
                sport_name=SPORT_NAME,
            )
            if ou:
                results.append(ou)

    return results


def parse_div_odds(container, event_id, home, away, league, start_time, fetched_at):
    """Delegate to basketball.parse_div_odds with sport_name="mma".

    MMA Format-B has only col0/col1 populated (ML home/away). col2/3/4 are
    `EmptySnatch` (no AH, total line behind detail page). basketball's
    parse_div_odds rejects missing AH/OU cols via _safe_float's >1.0 check,
    so the only output is the ML row — which is what we want.
    """
    return basketball.parse_div_odds(
        container, event_id, home, away, league, start_time, fetched_at,
        sport_name=SPORT_NAME,
    )


def classify_market_title(title):
    """List-only mode — no detail-page expansion for MMA in v1.

    Returns None for every title so any accidental call into
    cb_detail.parse_detail_page emits no Odds. Phase 4.x can revisit if
    you start placing prop bets on Method of Victory / round betting.
    """
    return None
