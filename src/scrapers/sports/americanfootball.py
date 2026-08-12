"""
American-football-specific configuration for CrystalBet scraping (Phase 3.2).

Peer of `basketball.py` / `soccer.py` / `tennis.py`. Fourth sport on the
dashboard; CB sport_id 27 ("American Football" in the sports nav, 183 events
when surveyed 2026-08-12).

Surveyed against the WHOLE live board — all 177 in-scope games, every one of
their detail pages, not a sample (the tennis lesson: a 30-match sample missed
CB's dominant naming scheme entirely). 39 distinct market titles.

1. LIST VIEW — byte-identical to basketball
   -----------------------------------------
   All 177 containers shipped Format B with exactly 8 cols, in basketball's
   order, and the loadinfo carries basketball's 8-entry layout:

     [0] '1'         handicap=''          → ML home
     [1] '2'         handicap=''          → ML away
     [2] '1'         handicap=''          → AH home
     [3] 'Handicap'  handicap='handicap'  → AH line landmark ('-4.0 +4.0')
     [4] '2'         handicap=''          → AH away
     [5] 'Und'       handicap=''          → OU under
     [6] 'Tot'       handicap='total'     → OU line landmark ('44.0')
     [7] 'Over'      handicap=''          → OU over

   Two cosmetic differences from basketball, neither of which the parser
   cares about:
     * entry [1] is a bare '2' — basketball/soccer ship '\\t2' with a
       tab/space prefix. `_identify_loadinfo_roles` resolves ML-vs-AH away by
       POSITION relative to the AH landmark (everything before `ah_home` is
       moneyline), so the missing prefix changes nothing. Verified on the
       full board.
     * the OU landmark is named 'Tot' (basketball 'Point', tennis 'Game').
       Only the `handicap='total'` flag is read.

   The list-view moneyline is the **incl-OT 2-way** price — the same number
   the detail page serves as "Winner (incl. overtime)" (checked cell-for-cell
   on Seattle-New England: list 1.45/2.40, detail 1.45/2.40). The 3-way
   regulation "Main result" never appears on the list view.

   So this module delegates both list parsers to basketball, exactly as
   tennis does.

2. DETAIL PAGE — a real classifier
   --------------------------------
   Full-board title census (n = events carrying the title, of 177):

     169  Winner (incl. overtime)                    → moneyline FT
     169  Handicap (incl. overtime)*                 → spread    FT
     141  Total Points(incl. overtime)*              → total     FT   [1]
      46  Main result                                → 3-way regulation 1X2 [2]
      29  HomeTeam Total (incl. overtime)*           → team_total FT home
      29  AwayTeam Total (incl. overtime)*           → team_total FT away
      14  Odd/even        14  Odd/Even (incl. overtime)*          (skip)
      13  Will there be overtime*                                 (skip)
      10  Home Team odd/even   10  Away Team odd/even             (skip)
       4  Halftime/fulltime                          → htft FT [3]
       4  1st Half Result*                           → 3-way regulation H1 [2]
       4  1st Half - Draw No Bet                     → moneyline H1
       4  1st Half - Handicap                        → spread    H1
       4  1st Half - Total Points*                   → total     H1
       4  2nd Half - Draw No Bet (OT) / - Handicap (incl. overtime)* /
          - Total (incl. overtime)* / 2nd half - total / 2nd Half - Handicap
                                                     → H2 (no Period slot) [4]
       4  1st Quarter - Draw No Bet .. 4th. Quarter - Draw No Bet → moneyline Q1-Q4
       4  1st Quarter - Total Points / 2 quarter - total / 3 quarter - total /
          4 quarter - total                          → total  Q1-Q4  [5]
       4  1st. Quarter - Handicap / 2 quarter - handicap / 3 quarter -
          handicap / 4 quarter - handicap            → spread Q1-Q4  [5]
       4  Winning margin  4  Winner (including OT) & Total (including OT)  (skip)
       1  Handicap (including OT) & Total (including OT) -4.5/63.5 ×4     (skip)

   [1] NOTE the missing space: "Total Points(incl. overtime)". Basketball's
       pattern already allows `[\\s(]+` there, so it matches — but a pattern
       written as `total points \\(` would silently drop 141 events.

   [2] "Main result" / "1st Half Result*" are REGULATION-time 3-way markets
       — American football can be tied at the end of regulation, and CB
       prices that leg (13.2 on Seattle-New England). Pinnacle's AF moneyline
       is 2-way incl-OT (verified: sport 15 ships no "draw" designation on
       any of 217 moneyline entries), so the 3-way must NOT become the
       matched moneyline — it would pair a regulation price against an
       incl-OT one. It is emitted by the PERMISSIVE classifier only, where it
       feeds the CB-internal `ot_vs_regulation` consistency check (see
       src/consistency.py) and the htft bounds.

   [3] Halftime/fulltime is the standard 9-way combo; the model already has
       market_type="htft" and soccer's htft_combo consistency check is
       sport-agnostic, so it comes along for free on the permissive path.

   [4] H2 has no slot in the v1 Period literal, same as basketball. The rules
       still classify it so the anomaly scanner groups those ladders
       correctly rather than dumping them into FT; nothing downstream matches
       an H2 row because Pinnacle's PERIOD_MAP stops at H1.

   [5] THE ONE REAL TRAP. CB serves AF quarters under two spellings in the
       same detail page:
           "1st Quarter - Total Points"   ordinal + "Points"
           "2 quarter - total"            bare digit, no "Points"
       Basketball's rules only know the ordinal form, so 2/3/4-quarter
       ladders fall through — and basketball's permissive `_derive_period`
       matches `\\b(1st|first|1)\\b.*quarter` for Q1 but `\\b(2nd|second)\\b`
       for Q2, so a bare "2 quarter - total" derives period **FT** and its
       rungs interleave into the FT total ladder. That is a false-anomaly
       generator, so this module carries its own period deriver that accepts
       bare digits for every quarter.

CB sport_id: 27. Pinnacle sport_id: 15 ("Football").
"""
from __future__ import annotations

import re
from typing import Optional

from src.scrapers.cb_detail import MarketClassification
from src.scrapers.sports import basketball

SPORT_ID = 27
SPORT_NAME = "americanfootball"


def parse_loadinfo(raw, event_id, home, away, league, start_time, fetched_at):
    """Delegate to basketball.parse_loadinfo with sport_name="americanfootball"."""
    return basketball.parse_loadinfo(
        raw, event_id, home, away, league, start_time, fetched_at,
        sport_name=SPORT_NAME,
    )


def parse_div_odds(container, event_id, home, away, league, start_time, fetched_at):
    """Delegate to basketball.parse_div_odds with sport_name="americanfootball"."""
    return basketball.parse_div_odds(
        container, event_id, home, away, league, start_time, fetched_at,
        sport_name=SPORT_NAME,
    )


# ── Detail-page market-name classifier ────────────────────────────────────────
# Matched against a lowercased, whitespace-collapsed, asterisk-stripped title
# (see _normalize_title). Order matters — more specific first.
#
# Quarter/half tokens are written once as shared fragments so the ordinal and
# bare-digit spellings can never drift apart between market types.
#
# `_ORD[p]` is the bare alternation body (no group, no trailing dot) so it can
# be embedded in either an anchored rule or a \b-delimited period probe.
_ORD = {
    "Q1": r"1st|first|1",
    "Q2": r"2nd|second|2",
    "Q3": r"3rd|third|3",
    # CB's basketball pages carry a "4rt." typo; keep tolerating it here since
    # the same CMS renders both sports.
    "Q4": r"4th|4rt|fourth|4",
}
_Q = {p: rf"(?:{body})\.?" for p, body in _ORD.items()}
_HALF = {"H1": r"(?:1st|first|1)\.?", "H2": r"(?:2nd|second|2)\.?"}

_RULES: list[tuple[re.Pattern[str], str, str, int]] = [
    # ── FT (incl. OT — the variant Pinnacle prices) ──
    (re.compile(r"^winner[\s(]+incl(?:uding)?\.?\s*(?:ot|overtime)\b"),
     "moneyline", "FT", 0),
    # "Total Points(incl. overtime)" ships WITHOUT a space before the paren.
    (re.compile(r"^total points[\s(]+incl(?:uding)?\.?\s*(?:ot|overtime)\b"),
     "total", "FT", 0),
    (re.compile(r"^(?:asian\s+)?handicap[\s(]+incl(?:uding)?\.?\s*(?:ot|overtime)\b"),
     "spread", "FT", 0),

    # ── FT team totals — CB writes them closed-up ("HomeTeam Total") ──
    (re.compile(r"^home\s?team total[\s(]+incl(?:uding)?\.?\s*(?:ot|overtime)\b"),
     "team_total", "FT", 0),
    (re.compile(r"^away\s?team total[\s(]+incl(?:uding)?\.?\s*(?:ot|overtime)\b"),
     "team_total", "FT", 0),

    # ── H1 ── ("Draw No Bet" is CB's 2-way derivative, same as basketball)
    (re.compile(rf"^{_HALF['H1']}\s*half\s*-\s*draw no bet"), "moneyline", "H1", 0),
    (re.compile(rf"^{_HALF['H1']}\s*half\s*-\s*(?:asian\s+)?handicap\s*$"),
     "spread", "H1", 0),
    (re.compile(rf"^{_HALF['H1']}\s*half\s*-\s*total"), "total", "H1", 0),

    # ── H2 (incl OT preferred, regular time as fallback) ──
    # No H2 in the v1 Period model, so these never reach Pinnacle; they are
    # classified so the anomaly scanner keeps them out of the FT ladders.
    (re.compile(rf"^{_HALF['H2']}\s*half\s*-\s*draw no bet\s*\((?:ot|overtime)\)"),
     "moneyline", "H2", 0),
    (re.compile(rf"^{_HALF['H2']}\s*half\s*-\s*(?:asian\s+)?handicap[\s(]+"
                r"incl(?:uding)?\.?\s*(?:ot|overtime)\b"), "spread", "H2", 0),
    (re.compile(rf"^{_HALF['H2']}\s*half\s*-\s*total[\s(]+incl(?:uding)?\.?"
                r"\s*(?:ot|overtime)\b"), "total", "H2", 0),
    (re.compile(rf"^{_HALF['H2']}\s*half\s*-\s*draw no bet\s*$"), "moneyline", "H2", 1),
    (re.compile(rf"^{_HALF['H2']}\s*half\s*-\s*(?:asian\s+)?handicap\s*$"),
     "spread", "H2", 1),
    (re.compile(rf"^{_HALF['H2']}\s*half\s*-\s*total\s*$"), "total", "H2", 1),
]

# Quarters — generated so the ordinal ("1st Quarter - Total Points") and the
# bare-digit ("2 quarter - total") spellings are covered identically for all
# four quarters and all three market types. Both spellings occur on the SAME
# detail page; see note [5] in the module docstring.
for _p, _tok in _Q.items():
    _RULES += [
        (re.compile(rf"^{_tok}\s*quarter\s*-\s*draw no bet"), "moneyline", _p, 0),
        (re.compile(rf"^{_tok}\s*quarter\s*-\s*(?:asian\s+)?handicap\s*$"),
         "spread", _p, 0),
        (re.compile(rf"^{_tok}\s*quarter\s*-\s*total(?:\s*points)?\s*$"),
         "total", _p, 0),
    ]

# Titles that are known and deliberately out of scope. Checked BEFORE the
# rules so the dashboard doesn't log an unclassified warning every cycle.
_SKIP_PATTERNS: tuple[re.Pattern[str], ...] = (
    # combos: "Winner (including OT) & Total (including OT)",
    # "Handicap (including OT) & Total (including OT) -4.5/63.5"
    re.compile(r"&"),
    re.compile(r"\band\b"),
    re.compile(r"odd\s*/?\s*even"),
    re.compile(r"will there be overtime"),
    re.compile(r"winning margin"),
    re.compile(r"halftime\s*/\s*fulltime"),
    re.compile(r"correct score"),
    re.compile(r"\bplayer\b"),
    re.compile(r"touchdown"),
    re.compile(r"field goal"),
    re.compile(r"race to \d+"),
    re.compile(r"highest scoring"),
    re.compile(r"lowest scoring"),
    re.compile(r"\{?\$competitor"),          # CB template-rendering glitch
    # Bare regulation-time results. Both are real markets and both are
    # captured by the PERMISSIVE classifier for the OT-vs-regulation
    # consistency check — but neither may become the matched moneyline,
    # because Pinnacle's AF moneyline is 2-way incl-OT.
    re.compile(r"^main result$"),
    re.compile(r"^\d?\w*\s*half result$"),
    re.compile(r"^draw no bet$"),
)

_TEAM_SIDE_RE = re.compile(r"^(home|away)\s?team total")


def _normalize_title(title: str) -> str:
    """Lowercase, collapse whitespace, drop decorative '*' markers.

    Unlike soccer, AF asterisks carry no meaning — "Handicap (incl.
    overtime)*" and "Total Points(incl. overtime)*" are the ordinary
    markets — so they are stripped like basketball does.
    """
    t = (title or "").lower().strip()
    t = re.sub(r"\s+", " ", t)
    return t.replace("*", "").strip()


def classify_market_title(title: str) -> Optional[MarketClassification]:
    """Detail-page market title → MarketClassification, or None to skip.

    Strict (+EV path) classifier: only markets we can map onto a Pinnacle
    counterpart with confidence. See the module docstring for the full
    full-board census this was written against.
    """
    if not title:
        return None
    norm = _normalize_title(title)

    for pat in _SKIP_PATTERNS:
        if pat.search(norm):
            return None

    for pat, market_type, period, rank in _RULES:
        if pat.search(norm):
            team_side = None
            if market_type == "team_total":
                m = _TEAM_SIDE_RE.match(norm)
                if m is None:          # defensive: rule matched, name didn't
                    return None
                team_side = m.group(1)
            return MarketClassification(
                market_type=market_type, period=period, variant_rank=rank,
                team_side=team_side,
            )
    return None


# ── Permissive classifier (anomaly scanner only) ──────────────────────────────
# Same contract as basketball.classify_market_title_permissive: accept every
# 2-way ladder so the CB-internal consistency engine can see it, without
# needing a Pinnacle-grade mapping. Two AF-specific additions over the
# basketball version:
#   * regulation 3-way results ("Main result", "1st Half Result") are emitted
#     as n_way=3 moneylines — they are the legs the htft combo settles on AND
#     the input to the ot_vs_regulation check;
#   * the period deriver accepts bare-digit quarters (see docstring note [5]).
_PERIOD_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(rf"\b(?:{_ORD['Q4']})\b\s*quarter|\bq4\b"), "Q4"),
    (re.compile(rf"\b(?:{_ORD['Q3']})\b\s*quarter|\bq3\b"), "Q3"),
    (re.compile(rf"\b(?:{_ORD['Q2']})\b\s*quarter|\bq2\b"), "Q2"),
    (re.compile(rf"\b(?:{_ORD['Q1']})\b\s*quarter|\bq1\b"), "Q1"),
    (re.compile(r"\b(?:2nd|second|2)\b[\s.]*(?:half|period)"), "H2"),
    (re.compile(r"\b(?:1st|first|1)\b[\s.]*(?:half|period)"), "H1"),
    (re.compile(r"\bhalf ?time\b"), "H1"),
]

_RE_HTFT_TITLE = re.compile(r"^(?:half\s*time\s*/\s*full\s*time|ht\s*/\s*ft)\b")
_RE_HTFT_EXCLUDE = re.compile(r"\band\b|&|correct|exact|\btotal\b|score")
# Regulation-time N-way results. "Main result" is the FT one; "1st Half
# Result" the H1 one. Combos are excluded by the "&"/"and" guard.
_RE_REG_RESULT = re.compile(r"^(?:main result|(?:\d\w*\s*)?(?:half|quarter)\s*result)$")

# Skip patterns for the permissive path: the strict list minus the entries
# that exist only to keep regulation/exotic markets off the +EV path.
_PERMISSIVE_SKIP = tuple(
    p for p in _SKIP_PATTERNS
    if p.pattern not in (r"^main result$", r"^\d?\w*\s*half result$",
                         r"halftime\s*/\s*fulltime")
)


def _derive_period(norm: str) -> str:
    """Best-effort period from a title; FT when nothing matches. Quarters are
    tested before halves and in descending order so "2 quarter" cannot be
    swallowed by the Q1 rule's bare-digit alternative."""
    t = re.sub(r"[^a-z0-9]+", " ", norm)
    for pat, period in _PERIOD_PATTERNS:
        if pat.search(t):
            return period
    return "FT"


def classify_market_title_permissive(title: str) -> Optional[MarketClassification]:
    """Permissive ladder classifier for the CB-internal anomaly/consistency
    scan. Returns spread/total/moneyline/htft for anything recognisable;
    None for known noise."""
    if not title:
        return None
    norm = _normalize_title(title)

    if _RE_HTFT_TITLE.match(norm) and not _RE_HTFT_EXCLUDE.search(norm):
        return MarketClassification(market_type="htft", period="FT")
    if _RE_REG_RESULT.match(norm):
        return MarketClassification(
            market_type="moneyline", period=_derive_period(norm), n_way=3,
        )

    for pat in _PERMISSIVE_SKIP:
        if pat.search(norm):
            return None

    rank = 1 if "regular time" in norm else 0
    period = _derive_period(norm)
    # Team totals before plain totals — "hometeam total" contains "total".
    m = _TEAM_SIDE_RE.match(norm)
    if m:
        return MarketClassification(market_type="team_total", period=period,
                                    variant_rank=rank, team_side=m.group(1))
    if "handicap" in norm:
        return MarketClassification(market_type="spread", period=period,
                                    variant_rank=rank)
    if "total" in norm:
        return MarketClassification(market_type="total", period=period,
                                    variant_rank=rank)
    if "winner" in norm or "draw no bet" in norm or "moneyline" in norm:
        return MarketClassification(market_type="moneyline", period=period,
                                    variant_rank=rank, n_way=2)
    return None
