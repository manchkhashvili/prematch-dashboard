"""
Basketball-specific configuration for CrystalBet scraping.

Two responsibilities:

1. **List-view loadinfo parser** — parses the per-game JSON CB ships on the
   Sports.aspx list page. Basketball uses 2-way moneyline + alt handicap +
   alt total in a fixed positional layout (see _parse_loadinfo docstring).
   Was originally inside crystalbet.py; lives here so that adding soccer (which
   has 3-way ML and different list-view shape) means writing a peer module
   instead of forking the scraper.

2. **Detail-page market classifier** — given a market title string from the
   expanded `table.game-details` block, returns the (MarketType, Period,
   variant_rank) tuple we want to extract, or None to skip.

   Variant rank: 0 = preferred (incl. overtime for FT/H2 where CB serves both
   variants). 1 = fallback (regular time only). The caller resolves
   conflicts by keeping the lower rank when two markets land on the same
   (period, market_type) key.

Markets in scope (21 combinations):
  Periods: FT, H1, H2, Q1, Q2, Q3, Q4
  Market types: moneyline (2-way), spread (handicap, alt-lines), total (O/U, alt-lines)

For H1/H2/Q1-Q4 the ML 2-way is "Draw No Bet" since CB's "1x2" variant is
3-way (Pinnacle's H1 ML on basketball is also effectively 2-way with push-on-tie).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Optional

from src.models import Odds
# MarketClassification used to live here. Moved to src/scrapers/cb_detail.py
# in Phase 2 so soccer.py and any future sport modules import it from a
# neutral location instead of importing across sport-module boundaries.
# Re-exported below for any external code that still does
# `from src.scrapers.sports.basketball import MarketClassification`.
from src.scrapers.cb_detail import MarketClassification  # noqa: F401

log = logging.getLogger(__name__)

# ── Sport config ──────────────────────────────────────────────────────────────
SPORT_ID = 17           # CB's DoSportTypePostBack id for basketball
SPORT_NAME = "basketball"


# ── List-view loadinfo parser ─────────────────────────────────────────────────
# This was originally in crystalbet.py. Verified shape: see the docstring of
# _parse_loadinfo below. Format-B (col-positional divs) handled by
# _parse_div_odds. Both routes emit FT-only Odds — the detail page is the
# source of truth once we have it; list-view exists for first-time-seen games.

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
    sport_name: str = SPORT_NAME,
) -> Optional[Odds]:
    """Build an Odds object; return None if any selection value is missing.

    `sport_name` defaults to basketball for back-compat with all existing
    callers + tests. Phase 3.1: tennis reuses these parsers by passing
    sport_name="tennis" (tennis's list-view shape is identical to basketball's
    — 2-way ML + AH + OU, 8-entry loadinfo + same 8-col Format-B layout).
    """
    if any(v is None for v in selections.values()):
        return None
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
        )
    except ValueError as exc:
        log.debug("Odds rejected: %s", exc)
        return None


def _identify_loadinfo_roles(
    items: list[dict],
    ah_idx: Optional[int],
    ou_idx: Optional[int],
) -> dict[str, Optional[int]]:
    """
    Map each market-side role to its index in `items`, or None if missing.

    Each side is identified by both NAME and POSITION (relative to the
    landmarks). When CB locks a side, it removes the entry from the JSON
    entirely — so the verification by name prevents us from accepting a
    neighbor that happened to shift into the expected slot.
    """
    out: dict[str, Optional[int]] = {
        "ml_home": None, "ml_away": None,
        "ah_home": None, "ah_away": None,
        "ou_under": None, "ou_over": None,
    }

    # AH home: must be at ah_idx-1 with name == "1" and handicap == "".
    if ah_idx is not None and ah_idx >= 1:
        e = items[ah_idx - 1]
        if e.get("handicap") == "" and (e.get("name") or "").strip() == "1":
            out["ah_home"] = ah_idx - 1

    # AH away: must be at ah_idx+1 with name == "2" (no leading space) and
    # handicap == "". The leading-space distinguishes ML away (" 2") from
    # AH away ("2") in CB's JSON.
    if ah_idx is not None and ah_idx + 1 < len(items):
        e = items[ah_idx + 1]
        if e.get("handicap") == "" and (e.get("name") or "") == "2":
            out["ah_away"] = ah_idx + 1

    # OU under: at ou_idx-1, name in ("Und", "Under"), handicap == "".
    if ou_idx is not None and ou_idx >= 1:
        e = items[ou_idx - 1]
        name = (e.get("name") or "").strip().lower()
        if e.get("handicap") == "" and name in ("und", "under"):
            out["ou_under"] = ou_idx - 1

    # OU over: at ou_idx+1, name in ("Over", "Ov"), handicap == "".
    if ou_idx is not None and ou_idx + 1 < len(items):
        e = items[ou_idx + 1]
        name = (e.get("name") or "").strip().lower()
        if e.get("handicap") == "" and name in ("over", "ov"):
            out["ou_over"] = ou_idx + 1

    # ML home/away: entries with handicap == "" appearing BEFORE the AH
    # home slot (or before the AH landmark if AH home is also locked).
    # Identified by name. The leading space on ML away (" 2") is what
    # distinguishes it from AH away in CB's JSON.
    if out["ah_home"] is not None:
        ml_cutoff = out["ah_home"]
    elif ah_idx is not None:
        ml_cutoff = ah_idx
    else:
        ml_cutoff = len(items)

    for i, e in enumerate(items):
        if i >= ml_cutoff:
            break
        if e.get("handicap") != "":
            continue
        name = (e.get("name") or "")
        stripped = name.strip()
        if stripped == "1" and out["ml_home"] is None:
            out["ml_home"] = i
        elif stripped == "2" and out["ml_away"] is None:
            out["ml_away"] = i

    return out


def parse_loadinfo(
    raw: str,
    event_id: str,
    home: str,
    away: str,
    league: Optional[str],
    start_time: Optional[datetime],
    fetched_at: datetime,
    sport_name: str = SPORT_NAME,
) -> list[Odds]:
    """
    Parse the data-loadinfo JSON attribute (Format A).

    Verified shape (all 38 JSON-format games in the 2026-05-24 sample) — when
    no entries are locked:

      [{name:"1",   bet:ML_home,         handicap:""        },
       {name:" 2",  bet:ML_away,         handicap:""        },
       {name:"1",   bet:AH_home,         handicap:""        },
       {name:"Handicap", bet:"-3.0 +3.0", handicap:"handicap"},  ← AH-line landmark
       {name:"2",   bet:AH_away,         handicap:""        },
       {name:"Und", bet:OU_under,        handicap:""        },
       {name:"Point",bet:"220.0",        handicap:"total"   },  ← OU-line landmark
       {name:"Over",bet:OU_over,         handicap:""        }]

    CB OMITS entries for locked/suspended sides — they don't render a
    placeholder, the entry just disappears. See _identify_loadinfo_roles for
    how we defend against position shift.
    """
    raw = re.sub(r"[\x00-\x1f]", " ", raw)
    raw = re.sub(r",\s*\]", "]", raw)
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("loadinfo JSON error for %s vs %s: %s", home, away, exc)
        return []

    ah_idx = next(
        (i for i, e in enumerate(items) if e.get("handicap") == "handicap"),
        None,
    )
    ou_idx = next(
        (i for i, e in enumerate(items) if e.get("handicap") == "total"),
        None,
    )

    role_idx = _identify_loadinfo_roles(items, ah_idx, ou_idx)

    results: list[Odds] = []

    # ── Moneyline ──
    if role_idx["ml_home"] is not None and role_idx["ml_away"] is not None:
        ml = _make_odds(
            home=home, away=away, market_type="moneyline",
            selections={
                "home": _safe_float(items[role_idx["ml_home"]]["bet"]),
                "away": _safe_float(items[role_idx["ml_away"]]["bet"]),
            },
            fetched_at=fetched_at, event_id=event_id,
            league=league, start_time=start_time,
            sport_name=sport_name,
        )
        if ml:
            results.append(ml)

    # ── Spread ──
    if (ah_idx is not None
            and role_idx["ah_home"] is not None
            and role_idx["ah_away"] is not None):
        try:
            ah_line: Optional[float] = float(items[ah_idx]["bet"].split()[0])
        except (ValueError, IndexError, KeyError):
            ah_line = None
        if ah_line is not None:
            ah = _make_odds(
                home=home, away=away, market_type="spread",
                selections={
                    "home": _safe_float(items[role_idx["ah_home"]]["bet"]),
                    "away": _safe_float(items[role_idx["ah_away"]]["bet"]),
                },
                line=ah_line,
                fetched_at=fetched_at, event_id=event_id,
                league=league, start_time=start_time,
                sport_name=sport_name,
            )
            if ah:
                results.append(ah)

    # ── Total ──
    if (ou_idx is not None
            and role_idx["ou_under"] is not None
            and role_idx["ou_over"] is not None):
        ou_line = _parse_float(items[ou_idx]["bet"])
        if ou_line is not None:
            ou = _make_odds(
                home=home, away=away, market_type="total",
                selections={
                    "over":  _safe_float(items[role_idx["ou_over"]]["bet"]),
                    "under": _safe_float(items[role_idx["ou_under"]]["bet"]),
                },
                line=ou_line,
                fetched_at=fetched_at, event_id=event_id,
                league=league, start_time=start_time,
                sport_name=sport_name,
            )
            if ou:
                results.append(ou)

    return results


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
    """
    Parse positional column divs (Format B).

    col0=ML_home  col1=ML_away
    col2=AH_home  col3=AH_line("-11.5/+11.5")  col4=AH_away
    col5=OU_under  col6=OU_line("161.0")  col7=OU_over
    """
    col_map: dict[int, str] = {}
    for div in container.select(
        "div.x_loop_res.Snatch, div.x_loop_h_res.HandicapSnatch"
    ):
        for cls in (div.get("class") or []):
            if cls.startswith("col") and cls[3:].isdigit():
                col_map[int(cls[3:])] = div.text.strip()

    results: list[Odds] = []

    # Moneyline
    if 0 in col_map and 1 in col_map:
        ml = _make_odds(
            home=home, away=away, market_type="moneyline",
            selections={"home": _safe_float(col_map[0]),
                        "away": _safe_float(col_map[1])},
            fetched_at=fetched_at, event_id=event_id,
            league=league, start_time=start_time,
            sport_name=sport_name,
        )
        if ml:
            results.append(ml)

    # Spread — col3 text: "-11.5/+11.5" (home line first, slash-separated)
    if 2 in col_map and 3 in col_map and 4 in col_map and col_map[3]:
        try:
            ah_line: Optional[float] = float(col_map[3].split("/")[0])
        except ValueError:
            ah_line = None

        if ah_line is not None:
            ah = _make_odds(
                home=home, away=away, market_type="spread",
                selections={"home": _safe_float(col_map[2]),
                            "away": _safe_float(col_map[4])},
                line=ah_line,
                fetched_at=fetched_at, event_id=event_id,
                league=league, start_time=start_time,
                sport_name=sport_name,
            )
            if ah:
                results.append(ah)

    # Total — col6 text: "161.0"
    if 5 in col_map and 6 in col_map and 7 in col_map and col_map[6]:
        ou_line = _parse_float(col_map[6])
        if ou_line is not None:
            ou = _make_odds(
                home=home, away=away, market_type="total",
                selections={"over": _safe_float(col_map[7]),
                            "under": _safe_float(col_map[5])},
                line=ou_line,
                fetched_at=fetched_at, event_id=event_id,
                league=league, start_time=start_time,
                sport_name=sport_name,
            )
            if ou:
                results.append(ou)

    return results


# ── Detail-page market-name classifier ────────────────────────────────────────
# MarketClassification is imported at the top of this file (re-exported from
# cb_detail) for back-compat with any external callers that import it from here.

# Patterns matched against a lowercased + whitespace-collapsed title.
# Order matters: more-specific patterns first, fallbacks later.
# variant_rank 0 = preferred (caller keeps this when conflict)
#              1 = fallback (used only if rank-0 absent for that key)
_RULES: list[tuple[re.Pattern[str], str, str, int]] = [
    # ── FT (incl. OT preferred) ──
    # CB uses two naming formats depending on the league:
    #   "Handicap (incl. overtime)*"         → normalize → "handicap (incl. overtime)"
    #   "Asian Handicap Including Overtime"  → normalize → "asian handicap including overtime"
    # Both must be caught at rank=0. The pattern handles "asian " prefix, open-paren
    # or space after "handicap", and "incl."/"including" spelling variants.
    (re.compile(r"^winner\s*\(incl\.?\s*overtime\)"),                                    "moneyline", "FT", 0),
    (re.compile(r"^total points[\s(]+incl(?:uding)?\.?\s*overtime"),                     "total",     "FT", 0),
    (re.compile(r"^(?:asian\s+)?handicap[\s(]+incl(?:uding)?\.?\s*overtime"),            "spread",    "FT", 0),

    # ── FT (regular time fallback) ──
    (re.compile(r"^total points\s*\(regular time\)$"),                                   "total",     "FT", 1),
    # No 2-way ML or 2-way spread regular-time variant on CB — these only
    # come as "incl. overtime" (above) or as 3-way "Full Time Result (1X2)"
    # / "Handicap (1X2)" which are out of scope.

    # ── H1 ──
    # Use "Draw No Bet" as our 2-way ML (Pinnacle H1 ML is also effectively 2-way).
    (re.compile(r"^1st half\s*-\s*draw no bet"),                                         "moneyline", "H1", 0),
    (re.compile(r"^1st\.?\s*half\s*-\s*(?:asian\s+)?handicap\s*$"),                     "spread",    "H1", 0),
    (re.compile(r"^1st half\s*-\s*total"),                                               "total",     "H1", 0),

    # ── H2 (incl OT preferred) ──
    (re.compile(r"^2nd half\s*-\s*draw no bet\s*\(ot\)"),                                "moneyline", "H2", 0),
    (re.compile(r"^2nd half\s*-\s*(?:asian\s+)?handicap[\s(]+incl(?:uding)?\.?\s*overtime"), "spread", "H2", 0),
    (re.compile(r"^2nd half\s*-\s*total\s*\(incl\.?\s*overtime\)"),                     "total",     "H2", 0),

    # ── H2 (regular time fallback) ──
    (re.compile(r"^2nd half\s*-\s*draw no bet\s*$"),                                     "moneyline", "H2", 1),
    (re.compile(r"^2nd\s*half\s*-\s*(?:asian\s+)?handicap\s*$"),                        "spread",    "H2", 1),
    (re.compile(r"^2nd half\s*-\s*total\s*$"),                                           "total",     "H2", 1),

    # ── Q1 ──
    (re.compile(r"^1st\s*quarter\s*-\s*draw no bet"),                                    "moneyline", "Q1", 0),
    (re.compile(r"^1st\.?\s*quarter\s*-\s*(?:asian\s+)?handicap\s*$"),                  "spread",    "Q1", 0),
    (re.compile(r"^1st\s*quarter\s*-\s*total\s*points"),                                 "total",     "Q1", 0),

    # ── Q2 ──
    (re.compile(r"^2nd\s*quarter\s*-\s*draw no bet"),                                    "moneyline", "Q2", 0),
    (re.compile(r"^2nd\s*quarter\s*-\s*(?:asian\s+)?handicap\s*$"),                     "spread",    "Q2", 0),
    (re.compile(r"^2nd\s*quarter\s*-\s*total\s*points"),                                 "total",     "Q2", 0),

    # ── Q3 ──
    (re.compile(r"^3rd\s*quarter\s*-\s*draw no bet"),                                    "moneyline", "Q3", 0),
    (re.compile(r"^3rd\s*quarter\s*-\s*(?:asian\s+)?handicap\s*$"),                     "spread",    "Q3", 0),
    (re.compile(r"^3rd\s*quarter\s*-\s*total\s*points"),                                 "total",     "Q3", 0),

    # ── Q4 (CB has typos: "4rt." instead of "4th.") ──
    (re.compile(r"^4(?:th|rt)\.?\s*quarter\s*-\s*draw no bet"),                          "moneyline", "Q4", 0),
    (re.compile(r"^4(?:th|rt)\.?\s*quarter\s*-\s*(?:asian\s+)?handicap\s*$"),           "spread",    "Q4", 0),
    (re.compile(r"^4(?:th|rt)\.?\s*quarter\s*-\s*total\s*points"),                      "total",     "Q4", 0),
]

# Hard-skip patterns: titles matching these are explicitly out of scope, no
# need to even try the rule list. Keeps the dashboard from logging warnings
# every cycle for known unsupported markets.
_SKIP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"1x2"),                       # 3-way ML / 3-way handicap
    re.compile(r"over-exact-under"),          # 3-way total
    re.compile(r"odd[/\s]?even"),
    re.compile(r"home\s*team\s*total"),
    re.compile(r"away\s*team\s*total"),
    re.compile(r"hometeam\s*total"),
    re.compile(r"awayteam\s*total"),
    re.compile(r"\bplayer\b"),
    re.compile(r"assists\b"),
    re.compile(r"rebounds\b"),
    re.compile(r"\bsteals\b"),
    re.compile(r"\bblocks\b"),
    re.compile(r"3-point field goals"),
    re.compile(r"double double"),
    re.compile(r"triple double"),
    re.compile(r"first point scorer"),
    re.compile(r"race to \d+"),
    re.compile(r"lead by \d+"),
    re.compile(r"competitor [12] to lead"),
    re.compile(r"winning margin"),
    re.compile(r"halftime/fulltime"),
    re.compile(r"halftime\s*/\s*fulltime"),
    re.compile(r"will there be overtime"),
    re.compile(r"highest scoring quarter"),
    re.compile(r"lowest scoring quarter"),
    re.compile(r"win all quarters"),
    re.compile(r"win both halves"),
    re.compile(r"to win 1st (?:half|quarter) and"),
    # Bare "Draw No Bet" (no period qualifier) is the FT regulation-time
    # derivative — skip since Pinnacle FT is incl-OT. Period-prefixed
    # variants like "1st Half - Draw No Bet" pass through to the rules.
    re.compile(r"^draw no bet$"),
    re.compile(r"\&\s*(?:total|handicap|1st (?:half|quarter) total|1x2)"),  # combo bets
    re.compile(r"last point"),
    re.compile(r"first free throw"),
    re.compile(r"point range"),
    re.compile(r"max(?:imum)? consecutive points"),
    re.compile(r"\{?\$competitor"),           # CB template-rendering glitch
)


def _normalize_title(title: str) -> str:
    """Lowercase, collapse whitespace, drop trailing '*' annotations."""
    t = title.lower().strip()
    t = re.sub(r"\s+", " ", t)
    t = t.replace("*", "")
    t = t.strip()
    return t


def classify_market_title(title: str) -> Optional[MarketClassification]:
    """
    Classify a detail-page market title.

    Returns a MarketClassification if the title maps to one of the 21
    in-scope (market_type, period) combinations. Returns None for any
    out-of-scope market (3-way variants, team totals, props, specialty bets).

    Variant rank: 0 means this is the preferred variant for its
    (period, market_type) key. 1 means it's a fallback (caller should
    prefer rank-0 if both exist for the same key).
    """
    if not title:
        return None
    norm = _normalize_title(title)

    # Hard-skip first — saves walking the rule list on known noise.
    for pat in _SKIP_PATTERNS:
        if pat.search(norm):
            return None

    for pat, market_type, period, rank in _RULES:
        if pat.search(norm):
            return MarketClassification(
                market_type=market_type, period=period, variant_rank=rank,
            )
    return None


# ── Permissive classifier (anomaly scanner only) ──────────────────────────────
# The strict classify_market_title above is an allowlist of exact title
# phrasings — right for the +EV pipeline (we only want markets we can confidently
# map + match to Pinnacle) but it silently DROPS any 2-way handicap/total ladder
# whose title isn't enumerated, e.g. "Asian Handicap 1st Period". For the CB-only
# anomaly scan we don't need a Pinnacle-grade mapping; we just need to see every
# 2-way ladder. This permissive variant:
#   1. runs the SAME _SKIP_PATTERNS first, so all the prop/3-way/exotic noise
#      (player totals, team totals, over-exact-under, 1x2, max-consecutive, …)
#      stays excluded;
#   2. then accepts ANY remaining title containing "handicap" → spread, or
#      "total" → total, with the period inferred from the text.
# Period is only used to GROUP rungs into ladders, so an approximate mapping is
# fine. "regular time" titles get variant_rank=1 so the incl-OT variant wins on
# the cb_detail dedup (same convention as the strict path).
_PERIOD_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(4(?:th|rt)|fourth)\b.*\bquarter\b|\bq4\b"), "Q4"),
    (re.compile(r"\b(3rd|third)\b.*\bquarter\b|\bq3\b"),         "Q3"),
    (re.compile(r"\b(2nd|second)\b.*\bquarter\b|\bq2\b"),        "Q2"),
    (re.compile(r"\b(1st|first|1)\b.*\bquarter\b|\bq1\b"),       "Q1"),
    (re.compile(r"\b(2nd|second)\b.*\b(half|period)\b"),         "H2"),
    (re.compile(r"\b(1st|first|1)\b.*\b(half|period)\b"),        "H1"),
    # Some leagues period-qualify with a bare "Halftime" instead of "1st half"
    # ("1X2 Halftime", "Asian Handicap Halftime", "Under/Over Halftime", …).
    # Without this they all silently default to FT — corrupting the FT view with
    # a second result market and leaving the H1 view empty. (The Halftime/Fulltime
    # COMBO is matched as htft before _derive_period runs, so it's unaffected.)
    (re.compile(r"\bhalf ?time\b"),                             "H1"),
]


def _derive_period(norm: str) -> str:
    """Best-effort period token from a title; defaults to FT. Quarters checked
    before halves, 2nd before 1st. 'period' is treated as a half (some CB
    leagues say '1st Period' for the first half). Punctuation is flattened to
    spaces first so "1st. Quarter" (the dot) still matches the quarter rule."""
    t = re.sub(r"[^a-z0-9]+", " ", norm)
    for pat, period in _PERIOD_PATTERNS:
        if pat.search(t):
            return period
    return "FT"


# Halftime/Fulltime combo. Leagues vary: "Halftime/Fulltime", "Half Time / Full
# Time", and the abbreviated "HT/FT Including Overtime" (La Liga Federal). Matched
# as a prefix so an "Including Overtime"/"(OT)" suffix still classifies — but NOT
# the multi-market combos ("Halftime/Fulltime and Total", "... Correct Score"),
# which are unmatchable and must stay out (excluded below).
_RE_HTFT_TITLE = re.compile(r"^(?:half\s*time\s*/\s*full\s*time|ht\s*/\s*ft)\b")
_RE_HTFT_EXCLUDE = re.compile(r"\band\b|&|correct|exact|\btotal\b|score")
# Plain N-way result markets phrased with "1x2": "Full Time Result(1X2)",
# "1st half - 1x2", "2nd Quarter - 1x2", ... Excludes the 3-way HANDICAP
# ("Handicap(1X2)") and any combo ("... & 1x2", "... and 1st half total").
_RE_PLAIN_1X2 = re.compile(r"1x2")
_RE_1X2_EXCLUDE = re.compile(r"handicap|&|\band\b|correct|total")


def classify_market_title_permissive(title: str) -> Optional[MarketClassification]:
    """Permissive 2-way ladder classifier for the anomaly scanner (see the
    block comment above). Returns spread/total for any non-skipped
    handicap/total title; None otherwise."""
    if not title:
        return None
    norm = _normalize_title(title)

    # Anomaly-scan extras, matched BEFORE the skip list (which excludes both
    # on purpose for the strict/+EV path):
    #   - the 9-way Halftime/Fulltime combo — input to the htft_combo
    #     consistency check (1/1 and 2/2 vs their own legs);
    #   - plain 1x2 result markets — the REGULATION-time 3-way moneylines that
    #     are those legs (HT/FT is settled on regulation, so the bounds must
    #     use regulation legs, not the incl-OT 2-way winner).
    if _RE_HTFT_TITLE.match(norm) and not _RE_HTFT_EXCLUDE.search(norm):
        return MarketClassification(market_type="htft", period="FT")
    if _RE_PLAIN_1X2.search(norm) and not _RE_1X2_EXCLUDE.search(norm):
        return MarketClassification(
            market_type="moneyline", period=_derive_period(norm), n_way=3,
        )

    for pat in _SKIP_PATTERNS:
        if pat.search(norm):
            return None
    rank = 1 if "regular time" in norm else 0
    period = _derive_period(norm)
    if "handicap" in norm:
        return MarketClassification(market_type="spread", period=period, variant_rank=rank)
    if "total" in norm:
        return MarketClassification(market_type="total", period=period, variant_rank=rank)
    # 2-way moneyline — captured so the consistency engine can compare ML win-prob
    # to the spread ladder per period. "winner" (incl-OT), "draw no bet" (the
    # 2-way derivative on sub-periods), or "moneyline". 3-way "1x2" is already
    # excluded by _SKIP_PATTERNS above. n_way=2 → cb_detail parses {home, away}.
    if "winner" in norm or "draw no bet" in norm or "moneyline" in norm:
        return MarketClassification(
            market_type="moneyline", period=period, variant_rank=rank, n_way=2,
        )
    return None
