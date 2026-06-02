"""
CrystalBet alt-line ladder anomaly detection (CB-only, no Pinnacle).

The first anomaly we hunt for is a **monotonicity violation** in a handicap
(spread) or total ladder. The intuition is purely structural — it needs no
model and no reference book:

  As the home handicap line increases (home is given more points), the home
  side becomes more likely to cover, so its decimal odds must get SHORTER,
  never longer. Symmetrically the away side's odds must get LONGER.

  Example violation (the kind the user flagged):
      home +10.0 @ 1.50   then   home +11.0 @ 1.60
  The line improved for home (+10 -> +11) but the price went UP (1.50 -> 1.60).
  A coherent ladder can never do that.

Totals follow the mirror rule:
  As the total line rises, OVER becomes less likely (over odds must be
  non-decreasing) and UNDER more likely (under odds must be non-increasing).

We compare each rung to the *next present rung* on the same ladder, so gaps
in the line sequence (CB routinely skips 0.0 and some half-points) don't
create false positives — we only ever compare two prices that genuinely sit
next to each other in the ladder.

A "ladder" is one (event, period, market_type, submarket) group. Each Odds
row already carries both sides, so a single row is one rung with home+away
(or over+under) prices.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from src.models import Odds


# Which (market_type -> side) pairs we audit, and the direction each side's
# odds must move as the line increases. "down" = odds must be non-increasing
# as the line rises; "up" = odds must be non-decreasing.
_DIRECTION = {
    ("spread", "home"): "down",   # more points to home -> shorter home odds
    ("spread", "away"): "up",     # more points to home -> longer away odds
    ("total", "over"): "up",      # higher total -> over less likely -> longer
    ("total", "under"): "down",   # higher total -> under more likely -> shorter
}


@dataclass(frozen=True)
class LadderAnomaly:
    """One monotonicity violation between two adjacent rungs of one ladder."""
    sport: str
    league: Optional[str]
    home: str
    away: str
    start_time: Optional[datetime]
    event_id: Optional[str]
    period: str
    market_type: str
    submarket: Optional[str]
    section: Optional[str]    # CB detail-page section title, e.g. "Asian Handicap 1st Period"
    side: str                 # "home" | "away" | "over" | "under"
    line_lo: float            # the lower line of the adjacent pair
    line_hi: float            # the higher line of the adjacent pair
    odds_lo: float            # price at line_lo
    odds_hi: float            # price at line_hi
    expected: str             # "down" | "up" — direction the price should move

    @property
    def delta(self) -> float:
        """Signed odds move in the WRONG direction, always positive.

        For an 'expected down' side a violation means odds went up, so the
        magnitude is odds_hi - odds_lo. For 'expected up' it's odds_lo - odds_hi.
        """
        return abs(self.odds_hi - self.odds_lo)

    @property
    def pct(self) -> float:
        """Wrong-direction move as a percentage of the smaller price."""
        base = min(self.odds_lo, self.odds_hi)
        return (self.delta / base) * 100.0 if base else 0.0

    @property
    def match_label(self) -> str:
        return f"{self.home} vs {self.away}"

    def describe(self) -> str:
        mkt = self.section or (
            self.market_type if not self.submarket
            else f"{self.market_type} ({self.submarket})"
        )
        return (
            f"{mkt} {self.period} {self.side}: "
            f"line {self.line_lo:+g}@{self.odds_lo:g} -> {self.line_hi:+g}@{self.odds_hi:g} "
            f"(should move {self.expected}; off by {self.pct:.1f}%)"
        )


def _ladder_key(o: Odds) -> tuple:
    # `section` (the CB detail-page market title) is part of the key so two
    # distinct sections that share (period, market_type) — e.g. incl-OT vs
    # regular-time, or a quarter ladder a permissive classifier mapped to the
    # same period — are analysed as SEPARATE ladders and never interleave into
    # spurious violations. None for non-detail Odds (then key reduces to before).
    return (o.raw_event_id, o.period, o.market_type, o.submarket, o.section)


def find_ladder_anomalies(
    odds: Iterable[Odds],
    *,
    markets: tuple[str, ...] = ("spread", "total"),
    min_pct: float = 0.0,
    min_abs: float = 0.0,
) -> list[LadderAnomaly]:
    """Detect monotonicity violations across alt-line ladders.

    Parameters
    ----------
    odds        : flat iterable of Odds (any sports/markets; we filter).
    markets     : which market_types to audit. Default both spread + total.
    min_pct     : only report violations whose wrong-direction move is at
                  least this percentage of the smaller price. 0.0 = report
                  every strict violation.
    min_abs     : same idea, absolute decimal-odds threshold.

    Returns a list of LadderAnomaly sorted by pct descending (biggest first).
    """
    # Bucket rows into ladders.
    ladders: dict[tuple, list[Odds]] = {}
    for o in odds:
        if o.market_type not in markets:
            continue
        if o.line is None:
            continue
        ladders.setdefault(_ladder_key(o), []).append(o)

    out: list[LadderAnomaly] = []
    for rows in ladders.values():
        # Sort rungs by line; de-dupe exact-line repeats (keep first).
        seen_lines: set[float] = set()
        rungs: list[Odds] = []
        for o in sorted(rows, key=lambda r: r.line):  # type: ignore[arg-type]
            if o.line in seen_lines:
                continue
            seen_lines.add(o.line)  # type: ignore[arg-type]
            rungs.append(o)
        if len(rungs) < 2:
            continue

        mt = rungs[0].market_type
        sides = [s for (m, s) in _DIRECTION if m == mt]

        for lo, hi in zip(rungs, rungs[1:]):
            for side in sides:
                if side not in lo.selections or side not in hi.selections:
                    continue
                o_lo = lo.selections[side]
                o_hi = hi.selections[side]
                expected = _DIRECTION[(mt, side)]
                violated = (
                    (expected == "down" and o_hi > o_lo)
                    or (expected == "up" and o_hi < o_lo)
                )
                if not violated:
                    continue
                anom = LadderAnomaly(
                    sport=lo.sport,
                    league=lo.league,
                    home=lo.home,
                    away=lo.away,
                    start_time=lo.start_time,
                    event_id=lo.raw_event_id,
                    period=lo.period,
                    market_type=mt,
                    submarket=lo.submarket,
                    section=lo.section,
                    side=side,
                    line_lo=float(lo.line),   # type: ignore[arg-type]
                    line_hi=float(hi.line),   # type: ignore[arg-type]
                    odds_lo=o_lo,
                    odds_hi=o_hi,
                    expected=expected,
                )
                if anom.pct >= min_pct and anom.delta >= min_abs:
                    out.append(anom)

    out.sort(key=lambda a: a.pct, reverse=True)
    return out
