"""
Soccer-specific configuration for CrystalBet scraping (Phase 2).

Peer of `basketball.py` — same two responsibilities, different sport:

1. **List-view parsers** — Soccer ships TWO formats inline on Sports.aspx
   like basketball, with the same `div.GContainerList` envelope:
     A. `div.game_loading[data-loadinfo]` — JSON array (parse_loadinfo)
     B. `div.x_loop_res.Snatch col{N}` — positional col-divs (parse_div_odds)

   Verified shape (PSG-Arsenal sample, 2026-05-26):

     Loadinfo positions for soccer:
       [0] name="1"      → 1X2 home
       [1] name="X"      → 1X2 draw
       [2] name="\\t2"    → 1X2 away  (tab/space prefix INCONSISTENT — strip)
       [3..5]            → Double Chance 1X / 12 / X2          (SKIP)
       [6..7]            → Draw No Bet (0)1 / (0)2             (SKIP)
       [8]  name="Und "  → Total under (trailing space)
       [9]  name="Goal"  → Total LINE landmark (handicap="total", bet="2.5")
       [10] name="over " → Total over (trailing space)
       [11..12]          → BTTS Yes / no                       (SKIP)

     Format-B col positions map 1:1 to loadinfo positions:
       col0 col1 col2  →  1X2 (home, draw, away)
       col3 col4 col5  →  DC               (SKIP)
       col6 col7       →  DNB              (SKIP)
       col8            →  Total under
       col9            →  Total line (HandicapSnatch total)
       col10           →  Total over
       col11 col12     →  BTTS             (SKIP)

   NO HANDICAP IN EITHER LIST-VIEW FORMAT. Asian Handicap is detail-page-only
   for soccer. That's fine — list view is just the fallback when expansion
   fails; production runs almost always serve from the detail page.

2. **Detail-page market classifier** — given a market-title string from the
   expanded `table.game-details` block, returns a `MarketClassification`
   (with `n_way`/`submarket`/`team_side` populated as needed) or `None` to
   skip.

   Mirrors Pinnacle exactly (user-confirmed scope 2026-05-26):
     PARENT match (no submarket):
       moneyline FT/H1   (3-way: 1X2)
       spread    FT/H1   (2-way Asian Handicap)
       total     FT/H1   (over/under goals)
       team_total FT/H1  (Home/Away Team Total, over/under)
     CORNERS (submarket="corners", on Pinnacle these come as a CHILD matchup
       in a separate "... Corners" league but in CB they're inline on the
       same event's detail page):
       total     FT/H1
       spread    FT/H1

   Out of scope for v1 (skipped):
     H2 entirely (Pinnacle ships no H2 soccer markets)
     Handicap(1X2) 3-way (Pinnacle's spread is 2-way only)
     Double Chance, BTTS, Draw No Bet, Correct Score, Halftime/Fulltime,
       all combo markets, all player props, all booking markets
       (Pinnacle ships these as type=special — unstructured, not matchable)
     Home/Away Team Corner Range (multi-outcome ranges, not over/under —
       shape mismatch with Pinnacle's team_total corners)
     First/Last Corner, Corner Matchbet (CB-only / different shape)

CRITICAL: asterisk handling differs from basketball.

  Basketball normalization strips all `*` — they were decorative.

  Soccer normalization PRESERVES asterisks because CB uses them
  semantically:
    `"Total corners*"`              one `*`  → corners-submarket marker
    `"1st Half - Handicap"`         none      → 2-way AH (in scope)
    `"1st Half - Handicap***"`      three `*` → 3-way scoreline handicap
                                                 (out of scope — labels are
                                                 `"1 (2:0)"` not `"1 (-1.5)"`)

  Without preserving asterisks the two H1 handicap variants would both
  normalize to `"1st half - handicap"` and the 3-way variant would
  mis-classify as 2-way AH, producing nonsensical Odds.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Optional

from src.models import Odds
from src.scrapers.cb_detail import MarketClassification

log = logging.getLogger(__name__)


# ── Sport config ──────────────────────────────────────────────────────────────
SPORT_ID = 16            # CB's DoSportTypePostBack id for soccer (reference §5)
SPORT_NAME = "soccer"


# ── Shared parsing helpers (mirror basketball's) ──────────────────────────────

def _safe_float(s: str) -> Optional[float]:
    """Parse odds string; return None if non-numeric or <= 1.0 (suspended)."""
    try:
        v = float(str(s).strip())
        return v if v > 1.0 else None
    except (ValueError, TypeError):
        return None


def _parse_float(s: str) -> Optional[float]:
    """Parse a numeric string with no range constraint (for line values)."""
    try:
        return float(str(s).strip())
    except (ValueError, TypeError):
        return None


def _make_odds(
    *,
    home: str,
    away: str,
    market_type: str,
    selections: dict,
    fetched_at: datetime,
    event_id: str,
    period: str = "FT",
    line: Optional[float] = None,
    league: Optional[str] = None,
    start_time: Optional[datetime] = None,
    submarket: Optional[str] = None,
    team_side: Optional[str] = None,
) -> Optional[Odds]:
    """Build an Odds object; return None if any selection value is missing."""
    if any(v is None for v in selections.values()):
        return None
    try:
        return Odds(
            source="crystalbet",
            sport=SPORT_NAME,
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
        )
    except ValueError as exc:
        log.debug("Odds rejected: %s", exc)
        return None


# ── List-view loadinfo parser (Format A) ──────────────────────────────────────
#
# Soccer loadinfo is shorter and simpler than basketball's because Asian
# Handicap is NOT shipped inline — it lives in the detail page only.
# We extract 1X2 (positions 0-2) + Total (positions 8-10 around the
# handicap=="total" landmark) and skip everything else.

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
    Parse the data-loadinfo JSON attribute (Format A) for soccer.

    Emits at most 2 Odds objects per game:
      - moneyline FT 3-way    selections={home, draw, away}
      - total     FT          selections={over, under} with line

    Locked sides cause that market to be skipped (CB OMITS locked entries
    from the JSON entirely). 1X2 requires ALL THREE sides; total requires
    both over+under sides plus the line landmark.
    """
    # Same defensive cleanup as basketball: strip control chars and trailing
    # comma-before-closing-bracket that CB sometimes emits.
    raw = re.sub(r"[\x00-\x1f]", " ", raw)
    raw = re.sub(r",\s*\]", "]", raw)
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("loadinfo JSON error for %s vs %s: %s", home, away, exc)
        return []

    results: list[Odds] = []

    # ── 1X2 moneyline (3-way) ──
    # Strategy: find three entries with handicap=="" whose stripped names
    # are "1", "X", "2" — positionally these are 0, 1, 2 in the canonical
    # layout but CB sometimes locks/reorders, so we scan rather than indexing.
    ml: dict[str, float] = {}
    for it in items:
        if it.get("handicap") != "":
            continue
        name = (it.get("name") or "").strip()
        if name == "1" and "home" not in ml:
            v = _safe_float(it.get("bet", ""))
            if v is not None:
                ml["home"] = v
        elif name.lower() == "x" and "draw" not in ml:
            v = _safe_float(it.get("bet", ""))
            if v is not None:
                ml["draw"] = v
        elif name == "2" and "away" not in ml:
            v = _safe_float(it.get("bet", ""))
            if v is not None:
                ml["away"] = v
        if {"home", "draw", "away"} <= ml.keys():
            break

    if {"home", "draw", "away"} <= ml.keys():
        odds_ml = _make_odds(
            home=home, away=away, market_type="moneyline",
            selections=ml,
            fetched_at=fetched_at, event_id=event_id,
            league=league, start_time=start_time,
        )
        if odds_ml:
            results.append(odds_ml)

    # ── Total goals ──
    # Landmark: entry with handicap=="total" carries the LINE in `bet`.
    # Sandwich: an "Und"/"under" entry before it and an "over"/"ov" entry
    # after it, both with handicap=="".
    total_idx = next(
        (i for i, e in enumerate(items) if e.get("handicap") == "total"),
        None,
    )
    if total_idx is not None:
        line = _parse_float(items[total_idx].get("bet", ""))
        if line is not None:
            ou: dict[str, float] = {}
            # Walk left for under, right for over, taking the nearest matching
            # name with handicap=="". Defensive in case CB inserts entries.
            for offset, target_side in ((-1, "under"), (1, "over")):
                idx = total_idx + offset
                if 0 <= idx < len(items):
                    e = items[idx]
                    name = (e.get("name") or "").strip().lower()
                    if e.get("handicap") == "" and (
                        (target_side == "under" and name.startswith("und"))
                        or (target_side == "over" and name.startswith("ov"))
                    ):
                        v = _safe_float(e.get("bet", ""))
                        if v is not None:
                            ou[target_side] = v
            if {"over", "under"} <= ou.keys():
                odds_tot = _make_odds(
                    home=home, away=away, market_type="total",
                    selections=ou, line=line,
                    fetched_at=fetched_at, event_id=event_id,
                    league=league, start_time=start_time,
                )
                if odds_tot:
                    results.append(odds_tot)

    return results


# ── List-view col-div parser (Format B) ───────────────────────────────────────
#
# When a game has no loadinfo (~28% of soccer games — many of these are
# outrights and get filtered earlier; non-outright Format-B examples exist
# for smaller-league regular matches), CB ships the same data as positional
# col-divs. Cols verified against three non-outright Format-B containers
# (Ishoej-VSK, Hellerup-Vendsyssel, Varnamo-Nordic United, 2026-05-26):
#   col0=1X2 home   col1=1X2 draw   col2=1X2 away
#   col3=DC 1X      col4=DC 12      col5=DC X2          (SKIP)
#   col6=DNB (0)1   col7=DNB (0)2                       (SKIP)
#   col8=Total under
#   col9=Total LINE landmark (class includes 'HandicapSnatch total')
#   col10=Total over
#   col11=BTTS yes  col12=BTTS no                       (SKIP)

def parse_div_odds(
    container,
    event_id: str,
    home: str,
    away: str,
    league: Optional[str],
    start_time: Optional[datetime],
    fetched_at: datetime,
) -> list[Odds]:
    """Parse positional col-divs (Format B) for soccer. 1X2 + Total only."""
    col_map: dict[int, str] = {}
    # Both Snatch (odds cells) and HandicapSnatch (line landmark) populate
    # the col_map. EmptySnatch (locked) yields empty text → _safe_float fails.
    for div in container.select(
        "div.x_loop_res, div.x_loop_h_res"
    ):
        for cls in (div.get("class") or []):
            if cls.startswith("col") and cls[3:].isdigit():
                col_map[int(cls[3:])] = div.text.strip()

    results: list[Odds] = []

    # ── 1X2 (3-way moneyline) ──
    if 0 in col_map and 1 in col_map and 2 in col_map:
        sels = {
            "home": _safe_float(col_map[0]),
            "draw": _safe_float(col_map[1]),
            "away": _safe_float(col_map[2]),
        }
        ml = _make_odds(
            home=home, away=away, market_type="moneyline",
            selections=sels,
            fetched_at=fetched_at, event_id=event_id,
            league=league, start_time=start_time,
        )
        if ml:
            results.append(ml)

    # ── Total goals ──
    # col9 carries the line (e.g. "2.5"); col8 is under, col10 is over.
    if 8 in col_map and 9 in col_map and 10 in col_map and col_map[9]:
        line = _parse_float(col_map[9])
        if line is not None:
            ou = {
                "over": _safe_float(col_map[10]),
                "under": _safe_float(col_map[8]),
            }
            tot = _make_odds(
                home=home, away=away, market_type="total",
                selections=ou, line=line,
                fetched_at=fetched_at, event_id=event_id,
                league=league, start_time=start_time,
            )
            if tot:
                results.append(tot)

    return results


# ── Detail-page market-name classifier ────────────────────────────────────────
#
# Soccer normalization PRESERVES asterisks (semantic markers), unlike
# basketball's (decorative, stripped). See module docstring "CRITICAL".
#
# Order: hard-skip patterns first (suppress noise warnings on the ~600 noise
# titles per match — props, combos, exotics). Then rule list (longest /
# most specific patterns checked first to avoid greedy mis-matches).


def _normalize_title(title: str) -> str:
    """Lowercase, collapse internal whitespace, strip outer whitespace.
    Does NOT strip asterisks — they're semantic on soccer."""
    t = title.lower().strip()
    t = re.sub(r"\s+", " ", t)
    return t


# Hard-skip patterns. Matching here means "out of scope, suppress warning."
# Order doesn't matter (all are checked). Listed roughly by frequency in the
# 662-title PSG-Arsenal sample so common noise short-circuits quickly.
_SKIP_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 3-way derivatives of in-scope titles (CB tags with *** suffix)
    re.compile(r"\*\*\*$"),                  # any title ending in *** is 3-way derivative
    re.compile(r"^handicap\(1x2\)$"),        # 3-way handicap; Pin has 2-way only

    # H2 entirely (Pin ships no H2 soccer markets)
    re.compile(r"^2nd\s*half"),
    re.compile(r"^second\s*half"),
    re.compile(r"^2nd\.\s*half"),

    # Specials Pinnacle ships as type=special (unstructured, not matchable)
    re.compile(r"^draw no bet"),
    re.compile(r"^double chance"),
    re.compile(r"^both teams to score"),
    re.compile(r"^correct score"),
    re.compile(r"^halftime"),
    re.compile(r"halftime\s*/\s*fulltime"),
    re.compile(r"^matchbet"),

    # CB combo bets ("X and Y", "X / Y") — never matchable
    re.compile(r"\s&\s"),
    re.compile(r"\sand\s"),
    re.compile(r"\sor\s"),
    re.compile(r"\s/\s"),                    # most combos use slash separators

    # Player markets (huge volume — 80%+ of the 662 titles are these)
    re.compile(r"\b(?:1st|anytime|first|last|tournament top)\s+goalscorer"),
    re.compile(r"\bgoalscorer\b"),
    re.compile(r"^1\s+goalscorer"),
    re.compile(r"clean\s+sheet"),
    # Player stat words. NOTE: "goals" is NOT in this list — it would match
    # the in-scope "Total goals" and "1st. Half Total goals" parent-match
    # titles. Combo player-stat titles like "Player X Total Goals Over 2.5"
    # are caught by the player-name combo patterns (" & ", " / ") above.
    re.compile(r"\b(?:assists|shots|passes|tackles|saves|cards)\b"),

    # Bookings — deferred to Phase 2.5 (Pinnacle only ships on 2 leagues)
    re.compile(r"\bbooking[s]?\b"),

    # Exotics / multi-outcome ranges
    re.compile(r"\b(?:exact|odd[/\s]?even)\b"),
    re.compile(r"multigoals"),
    re.compile(r"\brange\b"),                # "Home Team Corner Range" etc.
    re.compile(r"who\s+scores"),
    re.compile(r"\d+\s*minutes"),            # "10 minutes - 1x2 from 1 to 10"

    # CB-only corner markets (different shape from Pinnacle)
    re.compile(r"^cornerbet"),
    re.compile(r"corner\s*matchbet"),
    re.compile(r"\bfirst\s*corner\b|\blast\s*corner\b"),
    re.compile(r"1st\.?\s*corner"),

    # Sundry CB tilde/template noise
    re.compile(r"\{?\$competitor"),
)


# In-scope rules. Each entry is (pattern, market_type, period, n_way, submarket, team_side).
# Patterns matched against the NORMALIZED title (lowercase, collapsed whitespace,
# asterisks PRESERVED). Order matters: more-specific patterns first.
_RULES: list[tuple[re.Pattern[str], str, str, int, Optional[str], Optional[str]]] = [
    # ── PARENT match, FT ──
    (re.compile(r"^(?:1x2|main result)$"),                       "moneyline",  "FT", 3, None, None),
    (re.compile(r"^handicap$"),                                  "spread",     "FT", 2, None, None),
    (re.compile(r"^total goals$"),                               "total",      "FT", 2, None, None),
    (re.compile(r"^home team total$"),                           "team_total", "FT", 2, None, "home"),
    (re.compile(r"^away team total$"),                           "team_total", "FT", 2, None, "away"),

    # ── PARENT match, H1 ──
    (re.compile(r"^1st half result$"),                           "moneyline",  "H1", 3, None, None),
    (re.compile(r"^1st\s*half\s*-\s*handicap$"),                 "spread",     "H1", 2, None, None),
    # Detail-page sample showed exact title "1st. Half Total goals" (period after
    # "1st", capital H on Half, lowercase 'g' on goals). Be permissive on the
    # period and capitalization, strict on word order.
    (re.compile(r"^1st\.?\s*half\s+total\s+goals$"),             "total",      "H1", 2, None, None),
    (re.compile(r"^1st\s*half\s*-\s*home team total$"),          "team_total", "H1", 2, None, "home"),
    # Detail page used lowercase 'total' in "1st half - Away Team total" —
    # the normalization already handles case, just keep this in mind.
    (re.compile(r"^1st\s*half\s*-\s*away team total$"),          "team_total", "H1", 2, None, "away"),

    # ── CORNERS (submarket="corners"), FT — one `*` suffix is CB's corners marker ──
    (re.compile(r"^total corners\*$"),                           "total",      "FT", 2, "corners", None),
    (re.compile(r"^handicap of corner\*$"),                      "spread",     "FT", 2, "corners", None),

    # ── CORNERS, H1 ──
    (re.compile(r"^1st\s*half\s*-\s*total corners\*$"),          "total",      "H1", 2, "corners", None),
    # H1 corner spread has line embedded in title ("hcp=-0.5", etc.). The
    # inline cell labels still carry the line ("1 (-0.50)" / "2 (+0.50)"),
    # so we let _parse_spread extract the line from those — no need to
    # capture it here. Each title yields one Odds.
    (re.compile(r"^1st\s*half\s*-\s*corner\s*handicap\s+hcp=[+-]?\d+(?:\.\d+)?$"),
                                                                 "spread",     "H1", 2, "corners", None),
]


def classify_market_title(title: str) -> Optional[MarketClassification]:
    """
    Classify a soccer detail-page market title.

    Returns a MarketClassification when the title is in-scope, None otherwise.
    See module docstring for the in-scope rule list and the asterisk-as-
    semantic-marker explanation.
    """
    if not title:
        return None
    norm = _normalize_title(title)

    # Hard-skip first to suppress warning noise on the ~600 non-actionable
    # titles a Champions League match's detail page ships.
    for pat in _SKIP_PATTERNS:
        if pat.search(norm):
            return None

    for pat, mt, period, n_way, submarket, team_side in _RULES:
        if pat.search(norm):
            return MarketClassification(
                market_type=mt,
                period=period,
                variant_rank=0,
                n_way=n_way,
                submarket=submarket,
                team_side=team_side,
            )
    return None


_RE_HTFT_TITLE = re.compile(r"^halftime\s*/\s*fulltime$")


def classify_market_title_permissive(title: str) -> Optional[MarketClassification]:
    """Strict soccer classification PLUS the Halftime/Fulltime combo, for the
    anomaly scanner's HT/FT consistency checks (htft_combo + ht_vs_ft_divergence).

    The strict/+EV path deliberately SKIPS Halftime/Fulltime (it's not matchable
    against Pinnacle); here we capture it as market_type 'htft' so the engine can
    compare the 1/1 (and 2/2) combo to its own FT and 1st-half 1X2 legs. Every
    other in-scope market (FT 1X2, 1st-half result, handicaps, totals) already
    comes from the strict rules, so we fall through to them."""
    if not title:
        return None
    if _RE_HTFT_TITLE.fullmatch(_normalize_title(title)):
        return MarketClassification(market_type="htft", period="FT")
    return classify_market_title(title)
