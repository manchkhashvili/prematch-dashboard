"""
A generic LSport sport — for any CrystalBet sport the cycle discovers LSport
matches on and nobody has written a module for (2026-09-22).

WHY. "Cover all LSport matches" cannot be a fixed list: CB's sport nav moves
(Bandy, Floorball, CS2 and Sumo appeared between two days), and LSport's
coverage moves with the calendar. A full census on 2026-09-22 found LSport
matches in seven sports — soccer, tennis, volleyball, futsal, basketball,
handball, all with dedicated modules, and sumo, with none (8 bouts, one 2-way
price each, no detail markets). So the New inconsistencies cycle now sweeps
every sport id on the nav, counts LSport matches per sport, and scans any
sport that has some through this module. It is the floor, not the ceiling: a
dedicated module (volleyball, soccerlike) knows what a title means for its
sport; this one knows only what a title's WORDS mean on the LSport template.

WHAT IT READS. LSport's vocabulary is the same across sports, so a title-only
classifier is safe enough to be useful and conservative enough to be harmless:

    handicap                      -> spread      (not a "1X2"/"European" 3-way one)
    under/over, total             -> total       (team_total with "home/away team")
    winner, result, 1x2, 3 way,
    moneyline, match odds         -> moneyline, n_way=0 (AUTO: 3-way when an X
                                     label is present, 2-way otherwise — the
                                     parser decides, see cb_detail)
    draw no bet                   -> moneyline, 2-way
    ht/ft, halftime/fulltime      -> htft
    1st/2nd half, halftime        -> H1 / H2;  quarters -> Q1..Q4
    "period", set, game, frame,
    leg, map, inning, round       -> UNCLASSIFIED — "1st Period" is a set in
                                     volleyball, a third in hockey, a half in
                                     handball; filing it under H1 would feed
                                     total_additivity the wrong sum
    odd/even, race to, margin,
    exact, correct score, BTTS,
    double chance, to score, ...  -> unclassified (no Odds shape)

What that buys on an unknown sport: family-A ladders on every classified
ladder (per section, so distinct titles never interleave), and the
sport-agnostic consistency checks — ml_vs_spread, favourite_flip, period
totals on real halves/quarters, the pick'em trio where a 3-way, a DNB and a
0.0 rung coexist, htft_combo where an HT/FT grid and half results do.

List view: only the moneyline is kept (the AH/OU block layout differs per
sport and a misread total is worse than none); the cycle expands every game
anyway. Sport names are slugs of the nav label ("Table Tennis" ->
"tabletennis"); they are registered into crystalbet._SPORT_MODULES and
consistency.GENERIC_SPORTS at discovery time, and the parse-pool worker
resolves them by name with classify_mode="generic" so it needs no registry.
"""
from __future__ import annotations

import re
from typing import Optional

from src.scrapers.cb_detail import MarketClassification
from src.scrapers.sports import basketball, soccer

GENERIC = True

_SKIP = re.compile(
    r"odd/even|even/odd|race to|margin|exact|correct score|both teams|to score|"
    r"clean sheet|double chance|highest|last team|first team|to win a|to win exactly|"
    r"will there|yes/no|scorer|method|rounds|go the distance|combo|&|\bor\b|"
    r"\bset\b|\bsets\b|\bgame\b|\bgames\b|frame|\bleg\b|\blegs\b|\bmap\b|inning|"
    r"\bperiod\b|round"
)
_HALF1 = re.compile(r"1st\.? half|first half|\bhalftime\b|\bhalf time\b|1 half")
_HALF2 = re.compile(r"2nd\.? half|second half|2 half")
_QUARTER = re.compile(r"(1st|2nd|3rd|4th|1|2|3|4)\.?\s*quarter")


def slug(label: str, sport_id: Optional[int] = None) -> str:
    """'232 Table Tennis' -> 'tabletennis' (the count prefix is CB's). A label
    with no ASCII in it (a Georgian page) slugs to 'sport<id>' rather than to
    nothing, so two such sports can never collide on ''."""
    label = re.sub(r"^\d+\s+", "", label or "").strip()
    out = re.sub(r"[^a-z0-9]", "", label.lower())
    if not out and sport_id is not None:
        return f"sport{int(sport_id)}"
    return out


def _period(t: str) -> Optional[str]:
    """FT / H1 / H2 / Q1..Q4, or None for a period this module refuses."""
    if _HALF1.search(t):
        return "H1"
    if _HALF2.search(t):
        return "H2"
    m = _QUARTER.search(t)
    if m:
        n = m.group(1)[0]
        return f"Q{n}"
    if re.search(r"\b(1st|2nd|3rd|4th|5th)\b", t) and not _HALF1.search(t):
        return None                     # "1st Period" / "1st Set" style
    return "FT"


def classify(title: str) -> Optional[MarketClassification]:
    t = re.sub(r"\s+", " ", (title or "").strip().lower())
    if not t:
        return None
    if "ht/ft" in t or "halftime/fulltime" in t or "half time/full time" in t:
        return MarketClassification(market_type="htft", period="FT")
    if _SKIP.search(t):
        return None
    per = _period(t)
    if per is None:
        return None
    if "handicap" in t or "spread" in t:
        if "1x2" in t or "european" in t or "3-way" in t or "3 way" in t:
            return None
        return MarketClassification(market_type="spread", period=per)
    if "under/over" in t or "over/under" in t or "total" in t:
        side = ("home" if re.search(r"home ?team|hometeam|team 1\b", t)
                else "away" if re.search(r"away ?team|awayteam|team 2\b", t) else None)
        if side:
            return MarketClassification(market_type="team_total", period=per, team_side=side)
        return MarketClassification(market_type="total", period=per)
    if "draw no bet" in t or "no draw" in t:
        return MarketClassification(market_type="moneyline", period=per, n_way=2)
    if re.search(r"\bwinner\b|\bresult\b|\b1x2\b|3 way|money ?line|match odds|\bwin\b", t):
        return MarketClassification(market_type="moneyline", period=per, n_way=0)
    return None


def _moneyline_only(rows, sport_name):
    out = [o for o in rows if o.market_type == "moneyline"]
    for o in out:
        o.sport = sport_name
    return out


class GenericSport:
    """Duck-types a sports/<name>.py module for the cycle and the parse pool."""
    GENERIC = True

    def __init__(self, sport_id: int, sport_name: str, label: str = ""):
        self.SPORT_ID = int(sport_id)
        self.SPORT_NAME = sport_name
        self.LABEL = label
        self.classify_market_title = classify
        self.classify_market_title_permissive = classify

    def parse_loadinfo(self, raw, event_id, home, away, league, start_time, fetched_at):
        parser = soccer.parse_loadinfo if '"X"' in (raw or "") else basketball.parse_loadinfo
        try:
            rows = parser(raw, event_id, home, away, league, start_time, fetched_at)
        except Exception:
            return []
        return _moneyline_only(rows, self.SPORT_NAME)

    def parse_div_odds(self, container, event_id, home, away, league, start_time, fetched_at):
        try:
            rows = basketball.parse_div_odds(container, event_id, home, away, league,
                                             start_time, fetched_at)
        except Exception:
            return []
        return _moneyline_only(rows, self.SPORT_NAME)

    def __repr__(self) -> str:
        return f"GenericSport({self.SPORT_ID}, {self.SPORT_NAME!r})"


def register(sport_id: int, label: str) -> GenericSport:
    """Make (or fetch) the generic module for a nav sport and register it where
    the cycle and the consistency engine look. Idempotent. Never replaces a
    dedicated module."""
    from src.scrapers import crystalbet
    from src import consistency
    name = slug(label, sport_id)
    existing = crystalbet._SPORT_MODULES.get(name)
    if existing is None:
        # A dedicated module under another name ("Football" -> soccer) wins.
        existing = next((m for m in crystalbet._SPORT_MODULES.values()
                         if getattr(m, "SPORT_ID", None) == int(sport_id)), None)
    if existing is not None:
        return existing
    mod = GenericSport(sport_id, name, label)
    crystalbet._SPORT_MODULES[name] = mod
    consistency.GENERIC_SPORTS.add(name)
    return mod
