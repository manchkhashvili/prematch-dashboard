"""
Core data models for the prematch dashboard.

Three dataclasses:
  - Odds:        one market entry, both sides, decimal odds.
  - Match:       a unique game (CB ↔ Pinnacle pairing after matching).
  - Opportunity: a betting edge (ARB or +EV) ready to render in the arbs table.

Notes
-----
Odds carries BOTH sides in `selections`. The SQLite schema is normalized per-
side (one row per side); db.py is responsible for the flattening. Keeping both
sides together at the scraper layer lets us compute fair prices and edges
without rejoining sides downstream.

`period` follows Pinnacle's vocabulary: "FT" for full time, "H1" for first
half, "Q1".."Q4" for quarters (CB only; Pinnacle has no prematch Qs).

`market_type` uses Pinnacle's terms: "moneyline" | "spread" | "total".
The reference doc (§11) used "match_winner" — we drop that in favor of
Pinnacle's vocabulary since Pinnacle is our reference book.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Source     = Literal["crystalbet", "pinnacle"]
MarketType = Literal["moneyline", "spread", "total", "team_total"]
Period     = Literal["FT", "H1", "Q1", "Q2", "Q3", "Q4"]
Submarket  = Literal["corners", "bookings"]
TeamSide   = Literal["home", "away"]


@dataclass
class Odds:
    """One market line, both sides, from one source at one point in time."""
    source: Source
    sport: str                       # "basketball" / "soccer"
    home: str                        # team name as the source presents it
    away: str
    market_type: MarketType
    period: Period
    selections: dict[str, float]     # moneyline 2-way:  {"home", "away"}
                                     # moneyline 3-way:  {"home", "draw", "away"}  (soccer 1X2)
                                     # spread:           {"home", "away"}
                                     # total/team_total: {"over", "under"}
    fetched_at: datetime             # UTC
    line: float | None = None        # spread/total/team_total line; None for moneyline
    start_time: datetime | None = None  # UTC; None if unknown
    league: str | None = None
    raw_event_id: str | None = None  # CB GContainerG id or Pinnacle matchupId

    # Phase 2 additions (soccer) — both default None; basketball is untouched.
    # submarket distinguishes corners/bookings child markets from the parent
    # match's markets. Pinnacle ships these as separate matchupIds under
    # dedicated leagues ("X - Y Corners"); we fold them into the parent
    # matchupId tagged with submarket so the matcher's CB↔Pin join stays 1:1.
    submarket: Submarket | None = None
    # team_side is required (logically) when market_type=="team_total" — it
    # picks which team's total this is. The market identity for matcher
    # dedup becomes (event, period, market_type, line, submarket, team_side).
    # Not enforced at model level yet — that lands in C2.2 once we have a
    # constructor path for team_total Odds; adding the check now would
    # surprise existing test fixtures.
    team_side: TeamSide | None = None

    def __post_init__(self) -> None:
        # Sanity: all decimal odds must be > 1.0. CB renders 1.0 for suspended
        # markets and the reference doc is emphatic that ≤ 1.0 is never real.
        for side, val in self.selections.items():
            if val is not None and val <= 1.0:
                raise ValueError(
                    f"decimal odds {val!r} for {side} <= 1.0 — "
                    f"caller should filter suspended markets before constructing Odds"
                )


@dataclass
class Match:
    """A single game, possibly paired between CB and Pinnacle."""
    sport: str
    league: str
    home: str
    away: str
    start_time: datetime
    cb_event_id: str | None = None
    pin_matchup_id: str | None = None
    id: int | None = None  # SQLite rowid, populated after insert


@dataclass
class Opportunity:
    """One row of the arbs / +EV table."""
    start_time: datetime
    match_label: str        # "Home — Away"
    market: str             # e.g. "Spread H1 -2.5" or "Moneyline FT"
    side: str               # "home" / "away" / "over" / "under"
    cb_odds: float          # decimal
    pin_no_vig: float       # decimal, devigged same-side fair price (for BOTH
                            # +EV and ARB rows — unified meaning across kinds).
    edge_pct: float         # >0 for +EV; for ARB this is the combined edge
    kind: Literal["ARB", "+EV"]
    kelly_stake: float      # in currency units (bankroll constant in edge.py)
    cb_event_id: str | None = None  # CB raw_event_id — for arbs→matches deep link
    # ARB rows only: the OPPOSITE-side Pinnacle leg you'd bet alongside the CB
    # side to lock the arb in. arb_partner_odds is the vig'd price.
    arb_partner_side: str | None = None
    arb_partner_odds: float | None = None
    # Structured market spec — same data as `market` but parseable. Added
    # 2026-05-27 so the bet tracker's "Log bet" button can prefill the form
    # without parsing the human-readable label. None on legacy callers.
    market_type: str | None = None    # moneyline | spread | total | team_total | corners
    period: str | None = None         # FT | H1 | Q1 ...
    line: float | None = None         # spread/total line; None for ML
    submarket: str | None = None      # corners | None
    team_side: str | None = None      # home | away | None (team_total)
