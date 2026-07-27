"""
Sport-agnostic walker for CrystalBet's expanded match detail page.

After we trigger `DoGamesPostBack('ExpandDetail:<id>')` on a game, CB injects
a `<table class="game-details">` block inline. That table holds the full
market menu for that one game, laid out as a 2-column grid:

  <tr>
    <td class="sport_more_td1">MARKET TITLE A</td>
    <td class="sport_more_td2">
      <div class="sport_more_td_div">
        <div class="sport_more_bt DetailSnatch">
          <div class="sport_more_bt1">LABEL</div>
          <div class="sport_more_bt2">ODDS</div>
        </div>
        ... more DetailSnatch cells ...
      </div>
    </td>
    <td class="sport_more_td3">MARKET TITLE B</td>
    <td class="sport_more_td4"> ... selections ... </td>
  </tr>

This module knows nothing about basketball or any specific sport. The caller
passes:
  - `classify`  — a callable that maps a market title string to a
                  MarketClassification (or None to skip)
  - `sport_name` — the string stored on emitted Odds.sport

We handle 5 selection-cell shapes (dispatched by classification fields):
  - moneyline 2-way    — labels "1" / "2",                 → {home, away}
  - moneyline 3-way    — labels "1" / "X" / "2",           → {home, draw, away}  (soccer)
  - spread (alt-lines) — labels "1 (<line>)" / "2 (<line>)" → {home, away} per line
  - total (alt-lines)  — labels "Und <line>" / "over <line>" → {over, under} per line
  - team_total         — same labels as total, but classification.team_side
                         picks which team's total this is (soccer Home/Away
                         Team Total). One Odds per (team_side, line) — the
                         team_side gets stamped onto the emitted Odds.

After parsing, we merge variants: when two classifications land on the same
(period, market_type, line, submarket, team_side) key, the lower variant_rank
wins (i.e., the incl-OT variant beats the regular-time fallback).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from src.models import Odds

log = logging.getLogger(__name__)


# ── Sport-agnostic classification protocol ────────────────────────────────────
# Lives here (not in any sport module) so soccer.py and basketball.py both
# import it from a neutral location. Sport modules return one of these from
# their classify_market_title(); cb_detail dispatches based on the fields.
@dataclass(frozen=True)
class MarketClassification:
    """Result of classifying a detail-page market title."""
    market_type: str           # "moneyline" | "spread" | "total" | "team_total"
    period: str                # "FT" | "H1" | "H2" | "Q1".."Q4"
    variant_rank: int = 0      # 0 = preferred (e.g. incl-OT), 1 = fallback
    # Phase 2 (soccer) additions — both default None so basketball
    # classifications work unchanged.
    n_way: int = 2             # 2 = home/away (basketball ML, soccer AH/total/team_total)
                               # 3 = home/draw/away (soccer 1X2 moneyline)
    submarket: Optional[str] = None   # None | "corners" | "bookings"
    team_side: Optional[str] = None   # None | "home" | "away" (required for team_total)


def _safe_float(s: str) -> Optional[float]:
    """Parse odds string; return None if non-numeric or <= 1.0 (suspended)."""
    try:
        v = float(str(s).strip())
        return v if v > 1.0 else None
    except (ValueError, TypeError):
        return None


# ── Selection-cell parsers ────────────────────────────────────────────────────
# Each selection lives in <div class="sport_more_bt DetailSnatch">
#   with sub-divs sport_more_bt1 (label) and sport_more_bt2 (odds).
# Three shapes — one per market_type.

_RE_SPREAD_LABEL = re.compile(r"^([12])\s*\(([+-]?\d+(?:\.\d+)?)\)$")
_RE_TOTAL_LABEL = re.compile(
    r"^(und(?:er)?|over|ov)\s+(\d+(?:\.\d+)?)$",
    re.IGNORECASE,
)


def _bt_pair(snatch) -> Optional[tuple[str, str]]:
    """Extract (label, odds_text) from a DetailSnatch cell. None if shape wrong."""
    bt1 = snatch.select_one(".sport_more_bt1")
    bt2 = snatch.select_one(".sport_more_bt2")
    if bt1 is None or bt2 is None:
        return None
    return bt1.get_text(strip=True), bt2.get_text(strip=True)


def _parse_ml(snatches) -> Optional[dict[str, float]]:
    """
    Parse 2-way ML selections. Returns {"home": d, "away": d} or None if either
    side missing. Used for basketball and any 2-way moneyline shape.
    """
    sels: dict[str, float] = {}
    for snatch in snatches:
        pair = _bt_pair(snatch)
        if pair is None:
            continue
        label, odds_text = pair
        # Strip whitespace from label to handle soccer's "\t2" / " 2" quirk
        # for 1X2 away (loadinfo has these inconsistently across games).
        stripped = label.strip()
        if stripped == "1":
            side = "home"
        elif stripped == "2":
            side = "away"
        else:
            continue
        odds = _safe_float(odds_text)
        if odds is None:
            continue
        sels[side] = odds
    return sels if {"home", "away"} <= sels.keys() else None


def _parse_ml_3way(snatches) -> Optional[dict[str, float]]:
    """
    Parse 3-way ML selections (soccer 1X2). Returns {"home": d, "draw": d,
    "away": d} or None if any side missing.

    Labels: "1" (home), "X" (draw), "2" (away). Whitespace stripped on label
    for the "\\t2" / " 2" quirk we saw in CB's loadinfo + detail samples.
    """
    sels: dict[str, float] = {}
    for snatch in snatches:
        pair = _bt_pair(snatch)
        if pair is None:
            continue
        label, odds_text = pair
        stripped = label.strip()
        if stripped == "1":
            side = "home"
        elif stripped.lower() == "x":
            side = "draw"
        elif stripped == "2":
            side = "away"
        else:
            continue
        odds = _safe_float(odds_text)
        if odds is None:
            continue
        sels[side] = odds
    return sels if {"home", "draw", "away"} <= sels.keys() else None


_RE_HTFT_LABEL = re.compile(r"^([12Xx])\s*/\s*([12Xx])$")


def _parse_htft(snatches) -> Optional[dict[str, float]]:
    """
    Parse the 9-way Halftime/Fulltime combo: labels "1/1", "1/X", ..., "2/2"
    (half result / full-time result). Returns {label: odds} with labels
    normalized to uppercase-X form. Suspended (<=1.0) selections are dropped
    by _safe_float; we only require the corner outcomes the consistency check
    needs ("1/1" or "2/2") to be present.
    """
    sels: dict[str, float] = {}
    for snatch in snatches:
        pair = _bt_pair(snatch)
        if pair is None:
            continue
        label, odds_text = pair
        m = _RE_HTFT_LABEL.match(label.strip())
        if not m:
            continue
        odds = _safe_float(odds_text)
        if odds is None:
            continue
        sels[f"{m.group(1).upper()}/{m.group(2).upper()}"] = odds
    return sels if ("1/1" in sels or "2/2" in sels) else None


def _parse_spread(snatches) -> list[tuple[float, dict[str, float]]]:
    """
    Parse alt-line spread. Returns list of (home_line, {"home": d, "away": d})
    — one entry per line where BOTH sides are present.

    CB encodes labels as "1 (-3.0)" for home side (home gets -3.0) and
    "2 (+3.0)" for away side (away gets +3.0). We normalize to the
    home-line convention (the line for the home team), so the pair above
    becomes home_line=-3.0 with home_odds and away_odds joined.
    """
    by_home_line: dict[float, dict[str, float]] = {}
    for snatch in snatches:
        pair = _bt_pair(snatch)
        if pair is None:
            continue
        label, odds_text = pair
        m = _RE_SPREAD_LABEL.match(label)
        if not m:
            continue
        side_digit, line_str = m.group(1), m.group(2)
        odds = _safe_float(odds_text)
        if odds is None:
            continue
        line = float(line_str)
        if side_digit == "1":
            home_line = line
            side = "home"
        else:  # "2" — away side, line is from away's perspective; flip to home's.
            home_line = -line
            side = "away"
        by_home_line.setdefault(home_line, {})[side] = odds
    return [
        (line, sels) for line, sels in by_home_line.items()
        if {"home", "away"} <= sels.keys()
    ]


def _parse_total(snatches) -> list[tuple[float, dict[str, float]]]:
    """
    Parse alt-line total. Returns list of (line, {"over": d, "under": d}) —
    one entry per line where BOTH over and under are present.
    """
    by_line: dict[float, dict[str, float]] = {}
    for snatch in snatches:
        pair = _bt_pair(snatch)
        if pair is None:
            continue
        label, odds_text = pair
        m = _RE_TOTAL_LABEL.match(label)
        if not m:
            continue
        side_word, line_str = m.group(1).lower(), m.group(2)
        odds = _safe_float(odds_text)
        if odds is None:
            continue
        line = float(line_str)
        side = "under" if side_word.startswith("und") else "over"
        by_line.setdefault(line, {})[side] = odds
    return [
        (line, sels) for line, sels in by_line.items()
        if {"over", "under"} <= sels.keys()
    ]


# ── Main entry point ──────────────────────────────────────────────────────────
def parse_detail_page(
    html: str,
    *,
    event_id: str,
    home: str,
    away: str,
    league: Optional[str],
    start_time: Optional[datetime],
    fetched_at: datetime,
    sport_name: str,
    classify: Callable[[str], Optional[MarketClassification]],
    scope_to_event: bool = False,
    per_section: bool = False,
) -> list[Odds]:
    """
    Parse the expanded detail block for one game.

    Returns a flat list of Odds. Out-of-scope markets are silently skipped
    (the classifier returns None). When CB offers both regular-time and
    incl-OT variants of the same (period, market_type, line), the variant
    with the lower variant_rank wins (incl-OT preferred).

    When `scope_to_event` is True, only looks at the table.game-details
    INSIDE this event's GContainerList[data-id=event_id]. Use this when
    multiple games have been expanded on the same page (serial scraping
    leaves prior expansions in the DOM). When False (default), parses the
    first table.game-details found anywhere on the page — convenient for
    one-off captures.
    """
    # `html` may be a string (Playwright transport, saved fixtures) or an
    # already-parsed html5lib soup (browser-free transport — see
    # cb_http.normalize_soup). Re-parsing a soup we already built was 33 % of
    # CB's HTML processing for zero change in output.
    from src.scrapers.cb_http import as_soup

    soup = as_soup(html)
    if scope_to_event:
        # Scope: find this game's container, then the detail table within it.
        container = soup.select_one(
            f"div.GContainerList[data-id='{event_id}']"
        )
        if container is None:
            return []
        det = container.select_one("table.game-details")
    else:
        det = soup.select_one("table.game-details")
    if det is None:
        return []

    # Walk every (title, selections) pair across BOTH columns (td1+td2 = left,
    # td3+td4 = right). Some TRs have only one column populated; some have both.
    pairs: list[tuple[str, list]] = []
    for tr in det.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        i = 0
        while i < len(tds):
            cls = tds[i].get("class") or []
            if "sport_more_td1" in cls or "sport_more_td3" in cls:
                # Title cell — selections live in the next sibling td.
                if i + 1 >= len(tds):
                    break
                next_cls = tds[i + 1].get("class") or []
                if "sport_more_td2" in next_cls or "sport_more_td4" in next_cls:
                    title = tds[i].get_text(" ", strip=True)
                    snatches = tds[i + 1].select(
                        ".sport_more_bt.DetailSnatch"
                    )
                    if title:
                        pairs.append((title, snatches))
                    i += 2
                    continue
            i += 1

    # Classify each title and route to the right selection parser.
    # Track (rank, odds) so we can prefer lower-rank variants on conflict.
    ranked: list[tuple[int, Odds]] = []
    for title, snatches in pairs:
        cls = classify(title)
        if cls is None:
            continue

        if cls.market_type == "moneyline":
            # 2-way or 3-way based on classification.n_way
            # (basketball: 2 = {home, away}; soccer 1X2: 3 = {home, draw, away})
            if cls.n_way == 3:
                sels = _parse_ml_3way(snatches)
            else:
                sels = _parse_ml(snatches)
            if sels is None:
                continue
            odds = _build_odds(
                home=home, away=away,
                sport_name=sport_name,
                market_type="moneyline", period=cls.period,
                selections=sels, line=None,
                fetched_at=fetched_at, event_id=event_id,
                league=league, start_time=start_time,
                submarket=cls.submarket, team_side=None,
                section=title,
            )
            if odds is not None:
                ranked.append((cls.variant_rank, odds))

        elif cls.market_type == "htft":
            # 9-way Halftime/Fulltime combo — one row, selections keyed by
            # combo label ("1/1" ... "2/2"). period is always FT (the market
            # spans the whole game); line None.
            sels = _parse_htft(snatches)
            if sels is None:
                continue
            odds = _build_odds(
                home=home, away=away,
                sport_name=sport_name,
                market_type="htft", period="FT",
                selections=sels, line=None,
                fetched_at=fetched_at, event_id=event_id,
                league=league, start_time=start_time,
                submarket=cls.submarket, team_side=None,
                section=title,
            )
            if odds is not None:
                ranked.append((cls.variant_rank, odds))

        elif cls.market_type == "spread":
            for line, sels in _parse_spread(snatches):
                odds = _build_odds(
                    home=home, away=away,
                    sport_name=sport_name,
                    market_type="spread", period=cls.period,
                    selections=sels, line=line,
                    fetched_at=fetched_at, event_id=event_id,
                    league=league, start_time=start_time,
                    submarket=cls.submarket, team_side=None,
                    section=title,
                )
                if odds is not None:
                    ranked.append((cls.variant_rank, odds))

        elif cls.market_type == "total":
            for line, sels in _parse_total(snatches):
                odds = _build_odds(
                    home=home, away=away,
                    sport_name=sport_name,
                    market_type="total", period=cls.period,
                    selections=sels, line=line,
                    fetched_at=fetched_at, event_id=event_id,
                    league=league, start_time=start_time,
                    submarket=cls.submarket, team_side=None,
                    section=title,
                )
                if odds is not None:
                    ranked.append((cls.variant_rank, odds))

        elif cls.market_type == "team_total":
            # Soccer Home/Away Team Total — same label shape as total (Und/over),
            # but classification.team_side picks which team's total this is and
            # gets stamped onto the emitted Odds so the matcher can distinguish
            # home_team_total 1.5 from away_team_total 1.5 at the same line.
            if cls.team_side is None:
                log.warning(
                    "team_total classification missing team_side for title %r — skipping",
                    title,
                )
                continue
            for line, sels in _parse_total(snatches):
                odds = _build_odds(
                    home=home, away=away,
                    sport_name=sport_name,
                    market_type="team_total", period=cls.period,
                    selections=sels, line=line,
                    fetched_at=fetched_at, event_id=event_id,
                    league=league, start_time=start_time,
                    submarket=cls.submarket, team_side=cls.team_side,
                    section=title,
                )
                if odds is not None:
                    ranked.append((cls.variant_rank, odds))

    # Per-section mode (anomaly scanner): return EVERY section's rungs as-is,
    # each Odds carrying its source `section`, with NO cross-section merge or
    # rank suppression. The anomaly detector then analyses each section as an
    # independent ladder, so distinct sections that share (period, market_type)
    # — incl-OT vs regular-time, or a mis-derived period — never interleave into
    # one ladder and fabricate violations. (Within a section, _parse_spread /
    # _parse_total already key by line, so there are no intra-section dups.)
    if per_section:
        return [odds for _, odds in ranked]

    # Merge: keep lowest-rank variant per
    # (period, market_type, line, submarket, team_side) key. Submarket and
    # team_side stop corners-total-9.5 from accidentally colliding with
    # goals-total-9.5, and home-team-total-1.5 from colliding with
    # away-team-total-1.5. For basketball (submarket=None, team_side=None
    # everywhere) the key reduces to the prior 3-tuple — no behavior change.
    # Tied ranks (shouldn't happen in practice) → first wins.
    keyed: dict[
        tuple[str, str, Optional[float], Optional[str], Optional[str]],
        tuple[int, Odds],
    ] = {}
    for rank, odds in ranked:
        key = (odds.period, odds.market_type, odds.line,
               odds.submarket, odds.team_side)
        existing = keyed.get(key)
        if existing is None or rank < existing[0]:
            keyed[key] = (rank, odds)

    # Drop all fallback (rank>0) rows for any (period, market_type, submarket,
    # team_side) group where a preferred (rank=0) row exists. Without this,
    # "2nd Half - Total (incl. OT)" (.5 lines, rank=0) and "2nd Half - Total*"
    # (.0 lines, rank=1) survive as separate line values and get interleaved
    # into a single ladder, producing spurious monotonicity violations.
    preferred_groups: set[tuple[str, str, Optional[str], Optional[str]]] = set()
    for rank, odds in keyed.values():
        if rank == 0:
            preferred_groups.add(
                (odds.period, odds.market_type, odds.submarket, odds.team_side)
            )

    return [
        o for rank, o in keyed.values()
        if rank == 0
        or (o.period, o.market_type, o.submarket, o.team_side) not in preferred_groups
    ]


def _build_odds(
    *,
    home: str, away: str, sport_name: str,
    market_type: str, period: str,
    selections: dict[str, float], line: Optional[float],
    fetched_at: datetime, event_id: str,
    league: Optional[str], start_time: Optional[datetime],
    submarket: Optional[str] = None,
    team_side: Optional[str] = None,
    section: Optional[str] = None,
) -> Optional[Odds]:
    """Construct an Odds; return None if the model rejects (suspended side, etc.)."""
    try:
        return Odds(
            source="crystalbet",
            sport=sport_name,
            home=home,
            away=away,
            market_type=market_type,  # type: ignore[arg-type]
            period=period,  # type: ignore[arg-type]
            selections=selections,
            fetched_at=fetched_at,
            line=line,
            start_time=start_time,
            league=league,
            raw_event_id=event_id,
            submarket=submarket,  # type: ignore[arg-type]
            team_side=team_side,  # type: ignore[arg-type]
            section=section,
        )
    except ValueError as exc:
        log.debug("Odds rejected: %s", exc)
        return None
