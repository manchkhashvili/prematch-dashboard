"""
Betlive single-book cross-market anomaly detection.

The class of bug this hunts is the one you flagged on the VBA basketball game
(Ho Chi Minh City Wings vs Nha Trang Dolphins): the **incl-overtime 2-way
winner** and the **regulation 3-way result** disagree about who the favourite
is. Concretely betlive priced

    12 (OT)          1 @ 1.60   2 @ 2.00        (incl. overtime, no draw)
    Fulltime Result  1 @ 2.30   X @ 12.25  2 @ 1.60   (regulation, draw possible)

and those two markets cannot both be right. The argument is purely structural —
no model, no reference book:

  Including overtime only RESOLVES the regulation draw into a win for one side
  or the other. It moves probability mass OUT of the draw and INTO the two win
  outcomes; it can never take probability AWAY from a side. So after de-vigging
  each market on its own:

      fair P(home, incl-OT)  >=  fair P(home, regulation)
      fair P(away, incl-OT)  >=  fair P(away, regulation)

  (and the two incl-OT probs sum to 1, the draw having been folded in).

For this game that bound is violated hard on the away side:

      regulation (devig):  home 0.381   draw 0.072   away 0.548
      incl-OT   (devig):   home 0.556              away 0.444   <-- away DROPPED

Away "lost" 10.3 points of win probability by adding overtime — impossible. The
favourite flips (away was the 0.55 favourite in regulation, home is the 0.56
favourite incl-OT). Almost always the root cause is a swapped home/away on the
incl-OT 2-way, but the detector doesn't need to know the cause to flag it.

Scope. The check only fires when an event carries BOTH a 3-way result and a
separate 2-way (no-draw, no-line) winner — i.e. sports that play overtime off a
drawable regulation result (basketball, ice hockey, their e- feeds). Plain
soccer events expose no incl-OT 2-way, so they never trigger this (a soccer
draw is a real terminal outcome, not OT-resolved).

Input is the raw getPrematchEvent JSON (the "extended mode" detail call — the
list feed only prices one of the two markets, so the whole class is invisible
without the per-event detail fetch). See find_betlive_anomalies for the parse.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

try:                                                       # de-vig (project)
    from src.vig import devig_2way, devig_3way
except Exception:                                          # pragma: no cover
    def devig_2way(d1, d2):
        i1, i2 = 1 / d1, 1 / d2
        t = i1 + i2
        return i1 / t, i2 / t

    def devig_3way(d1, d2, d3):
        i = [1 / d1, 1 / d2, 1 / d3]
        t = sum(i)
        return tuple(x / t for x in i)

_DOTNET_EPOCH = datetime(1, 1, 1)

# Name fragments that mark a market as the incl-overtime 2-way winner, used to
# pick it out when an event happens to expose more than one no-draw 2-way
# moneyline. Matched case-insensitively against marketName.
_OT_HINTS = ("ot", "overtime", "incl", "winner", "money", "12")

# Name fragments for the regulation 3-way result.
_RESULT_HINTS = ("result", "1x2", "main", "regular")

# A market is full-game only if its name carries NONE of these. Sub-period and
# prop markets ("2nd Half - Winner (1X2)", "Which Team Scores Point") would
# otherwise masquerade as the full-game result / winner and pair into a false
# violation — the OT-fold bound is only valid between two FULL-GAME markets on
# the same regulation result. Mirrors the betlive scraper's own _SUBPERIOD/_PROP.
_DISQUALIFY = (
    "half", "quarter", " set", "period", " map", "1st", "2nd", "3rd", "4th", "5th",
    "inning", "frame", " leg", "which", "scores", "race", "odd", "even", "exact",
    "correct", "range", "player", "both teams", "margin", "highest", "method",
    "first", "last", "to score", "double chance",
)


def _full_game(name: str) -> bool:
    n = " " + (name or "").lower() + " "
    return not any(tok in n for tok in _DISQUALIFY)


def _ticks_to_utc(ticks) -> Optional[datetime]:
    try:
        return (_DOTNET_EPOCH + timedelta(microseconds=int(ticks) // 10)).replace(
            tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _odd(o: dict) -> Optional[float]:
    """Bettable decimal odds for one outcome, else None (suspended / junk)."""
    if (o.get("statusId") not in (None, 1)) or (o.get("marketStatusId") not in (None, 1)):
        return None
    try:
        v = float(o.get("odd"))
    except (TypeError, ValueError):
        return None
    return v if v > 1.0 else None


def _has_line(o: dict) -> bool:
    """True if the outcome carries a totals/handicap line (so it's not a clean
    moneyline — excludes Points Spread, whose labels are also '1'/'2')."""
    ax = o.get("argumentX")
    if ax not in (None, 0, 0.0):
        return True
    return bool((o.get("argumentString") or "").strip())


@dataclass(frozen=True)
class BetliveAnomaly:
    """One incl-OT vs regulation moneyline inconsistency on a single event."""
    sport: Optional[str]
    league: Optional[str]
    home: str
    away: str
    start_time: Optional[datetime]
    event_id: Optional[str]
    reg_market: str            # the regulation 3-way market name
    ot_market: str             # the incl-OT 2-way market name
    reg_odds: tuple            # (home, draw, away) decimal odds, regulation
    ot_odds: tuple             # (home, away) decimal odds, incl-OT
    fair_reg: tuple            # (home, draw, away) devigged probs
    fair_ot: tuple             # (home, away) devigged probs
    side: str                  # "home" | "away" — the side that lost probability
    gap_pp: float              # how far the bound is violated, percentage points
    favourite_flip: bool       # did the 2-way favourite differ from the 3-way?

    @property
    def pct(self) -> float:
        """Violation as a % of the side's regulation probability (severity)."""
        base = self.fair_reg[0] if self.side == "home" else self.fair_reg[2]
        return (self.gap_pp / (base * 100.0)) * 100.0 if base else 0.0

    @property
    def match_label(self) -> str:
        return f"{self.home} vs {self.away}"

    def describe(self) -> str:
        h_r, d_r, a_r = (p * 100 for p in self.fair_reg)
        h_o, a_o = (p * 100 for p in self.fair_ot)
        flip = "  [FAVOURITE FLIP]" if self.favourite_flip else ""
        return (
            f"{self.side} prob fell when folding in OT: "
            f"regulation {self.fair_reg[0 if self.side=='home' else 2]*100:.1f}% "
            f"-> incl-OT {self.fair_ot[0 if self.side=='home' else 1]*100:.1f}% "
            f"(down {self.gap_pp:.1f}pp){flip}\n"
            f"        {self.ot_market!r}: 1@{self.ot_odds[0]:g} 2@{self.ot_odds[1]:g}  "
            f"|  {self.reg_market!r}: 1@{self.reg_odds[0]:g} X@{self.reg_odds[1]:g} 2@{self.reg_odds[2]:g}\n"
            f"        devig  reg(h/d/a)={h_r:.1f}/{d_r:.1f}/{a_r:.1f}%  "
            f"ot(h/a)={h_o:.1f}/{a_o:.1f}%"
        )


def _priced_markets(event: dict):
    """Yield (name, marketId, {label: odd}, has_line, raw_market) per priced
    market. raw_market is the original dict (carries outcome ids) so callers can
    grab the moneyline outcomeIds for a cheap refreshOdds watch."""
    for mk in event.get("markets") or []:
        ocs = mk.get("outcomes") or []
        if not ocs:
            continue
        labels: dict[str, float] = {}
        line = False
        for o in ocs:
            od = _odd(o)
            if od is None:
                continue
            labels[(o.get("name") or "").strip()] = od
            line = line or _has_line(o)
        if labels:
            yield ((ocs[0].get("marketName") or "").strip(),
                   ocs[0].get("marketId"), labels, line, mk)


def pick_ml_markets(event: dict) -> tuple[dict | None, dict | None]:
    """The (regulation 3-way result, incl-OT 2-way winner) raw market dicts for
    one event, or (None, None) if either is absent. Public so the live watcher
    can capture their outcomeIds without re-deriving the selection logic."""
    markets = list(_priced_markets(event))
    res = _pick_result_3way(markets)
    win = _pick_winner_2way(markets)
    return (res[4] if res else None, win[4] if win else None)


def _pick_result_3way(markets):
    """The regulation full-game 1/X/2 market: labels superset {1,X,2}, no line,
    full-game (not a half/quarter), and named like a result."""
    cands = [m for m in markets
             if {"1", "X", "2"} <= set(m[2]) and not m[3] and _full_game(m[0])
             and any(h in m[0].lower() for h in _RESULT_HINTS)]
    if not cands:
        return None
    cands.sort(key=lambda m: m[1] not in (1, 409))          # stable ids first
    return cands[0]


def _pick_winner_2way(markets):
    """The incl-OT 2-way full-game winner: labels exactly {1,2}, no line, no
    draw, full-game (not a prop / sub-period)."""
    cands = [m for m in markets
             if set(m[2]) == {"1", "2"} and not m[3] and _full_game(m[0])]
    if not cands:
        return None
    cands.sort(key=lambda m: (
        not any(h in m[0].lower() for h in _OT_HINTS),      # OT-flagged first
        m[1] not in (396,),                                 # then stable id
    ))
    return cands[0]


def anomalies_for_event(event: dict, *, min_gap_pp: float = 1.5) -> list[BetliveAnomaly]:
    """Detect incl-OT vs regulation moneyline inconsistency on one event."""
    markets = list(_priced_markets(event))
    res = _pick_result_3way(markets)
    win = _pick_winner_2way(markets)
    if not res or not win:
        return []

    rh, rd, ra = res[2]["1"], res[2]["X"], res[2]["2"]
    oh, oa = win[2]["1"], win[2]["2"]
    fr_h, fr_d, fr_a = devig_3way(rh, rd, ra)
    fo_h, fo_a = devig_2way(oh, oa)

    out: list[BetliveAnomaly] = []
    flip = (fr_h > fr_a) != (fo_h > fo_a)               # favourite swapped sides
    for side, fair_reg, fair_ot in (("home", fr_h, fo_h), ("away", fr_a, fo_a)):
        gap_pp = (fair_reg - fair_ot) * 100.0           # >0 means bound violated
        if gap_pp < min_gap_pp:
            continue
        out.append(BetliveAnomaly(
            sport=event.get("sportName"), league=event.get("leagueName"),
            home=(event.get("homeTeamName") or "").strip(),
            away=(event.get("awayTeamName") or "").strip(),
            start_time=_ticks_to_utc(event.get("startDate")),
            event_id=str(event.get("id")) if event.get("id") else None,
            reg_market=res[0], ot_market=win[0],
            reg_odds=(rh, rd, ra), ot_odds=(oh, oa),
            fair_reg=(fr_h, fr_d, fr_a), fair_ot=(fo_h, fo_a),
            side=side, gap_pp=gap_pp, favourite_flip=flip,
        ))
    return out


def find_betlive_anomalies(
    events: Iterable[dict], *, min_gap_pp: float = 1.5,
) -> list[BetliveAnomaly]:
    """Run the incl-OT/regulation check over many getPrematchEvent payloads.

    `events` is an iterable of betlive event dicts (each the object returned by
    getPrematchEvent, or already unwrapped from its single-element list). Returns
    anomalies sorted by violation size (biggest gap first).

    min_gap_pp filters out sub-threshold disagreements: a couple tenths of a
    point is model/rounding noise between two independently-vigged markets; the
    real bugs (like the VBA game) violate the bound by 5-10+ points.
    """
    out: list[BetliveAnomaly] = []
    for ev in events:
        if isinstance(ev, list):                        # getPrematchEvent wraps in a list
            ev = ev[0] if ev else {}
        if isinstance(ev, dict):
            out.extend(anomalies_for_event(ev, min_gap_pp=min_gap_pp))
    out.sort(key=lambda a: a.gap_pp, reverse=True)
    return out
