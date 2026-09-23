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
half, "Q1".."Q4" for quarters (CB only; Pinnacle has no prematch Qs),
"P1".."P3" for ice-hockey periods, and "REG" for regulation time.

"REG" vs "FT" is an ice-hockey distinction and it is not cosmetic. "FT" always
means *the game as it settles*, overtime and shootout included; "REG" means the
60 minutes of regulation, where a tie is a third outcome. Hockey is the first
sport where a book prices both and they are DIFFERENT markets — CrystalBet
ships "Total Goals*" alongside "Total Goals(incl. overtime and penalties)", and
Pinnacle ships regulation on period 6 and incl-OT on period 0. Measured across
the live board 2026-08-27: the incl-OT ladder's devigged P(over) runs +0.024
above the regulation one at the same line (+0.05 mid-ladder), and team totals
+0.017 on 98 % of 888 rungs. Folding them into one period would score a
60-minute price against a full-game one on every event.

The puck line is the exception, and by arithmetic rather than luck: overtime is
sudden death and only ever reached from a tie, so the OT goal always makes a
one-goal win. Winning by 2+ including overtime IS winning by 2+ in regulation.
CB's two handicap ladders were byte-identical on 300 of 300 rungs at |line|
>= 1.5 (the only lines it offers), so either may be matched against Pinnacle's
period-6 spread.

`market_type` uses Pinnacle's terms: "moneyline" | "spread" | "total".
The reference doc (§11) used "match_winner" — we drop that in favor of
Pinnacle's vocabulary since Pinnacle is our reference book.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Source     = Literal["crystalbet", "pinnacle", "xbet", "liderbet", "betlive", "crocobet",
                     "setanta"]
# "htft" = the 9-way Halftime/Fulltime combo market (selections keyed "1/1",
# "1/X", ..., "2/2"). Captured only by the permissive (anomaly-scan) classifier
# for CB-internal consistency checks — never matched against Pinnacle.
# "fts" = First Team To Score, a 3-way over {home, none, away}. It is NOT a
# moneyline: its middle leg is "nobody scores" (P(0-0)), not a draw, so filing
# it as one would feed a 0-0 price into every check that reads a 1X2.
# "correct_score" = the exact set score, selections keyed "2-0"/"2-1"/"1-2"/"0-2"
# on a best-of-3 (and "3-0".."0-3" on a best-of-5), always HOME-AWAY order.
# Tennis only for now. Like "htft" it is captured by the permissive
# (anomaly-scan) classifier for CB-internal checks and never matched against
# Pinnacle. The legs form an exhaustive partition of the match, which is what
# makes them checkable: they must sum to 1 after devig, and their SHAPE is
# fixed by the per-set win probability the moneyline already implies.
#
# 2026-09-08: src/scrapers/sports/tennis.py used to say wiring this "needs a
# schema change". It did not — `selections` is a free-form dict and htft has
# carried 9 keys through it since soccer. Only the Literal needed widening.
MarketType = Literal["moneyline", "spread", "total", "team_total", "htft", "fts",
                     "correct_score"]
Period     = Literal["FT", "H1", "H2", "Q1", "Q2", "Q3", "Q4", "REG", "P1", "P2", "P3"]
Submarket  = Literal["corners", "bookings", "sets"]   # "sets": volleyball set markets
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
    # The CB detail-page market section this row came from (the human-readable
    # title, e.g. "Asian Handicap 1st Period"). Set by cb_detail on detail-page
    # Odds; None for list-view Odds. Used by the anomaly scanner to analyse each
    # ladder section independently so distinct sections that happen to share
    # (period, market_type) — e.g. incl-OT vs regular-time, or a mis-derived
    # period — never interleave into a single ladder. Ignored by edge/matcher
    # and not persisted (cache_persistence lists fields explicitly).
    section: str | None = None
    # Pinnacle only: the book's maxRiskStake limit for this market (from the
    # market entry's `limits` array). High limits = market Pinnacle trusts its
    # price on; also caps how much an arb partner-leg can take. None on CB rows
    # and on Pinnacle entries that ship no limits.
    max_stake: float | None = None
    # SportRadar match id, bare numeric string e.g. "71792526" (2026-06-15).
    # Set by the Lider-Bet scraper (from meta.matchProvider.matchId "sr:match:N")
    # and the Betlive scraper (from providerEventId when the feed is SportRadar).
    # Lets the matcher join Lider↔Betlive EXACTLY for cross-book arbs, bypassing
    # name fuzzing (and Lider's occasional Cyrillic names). None on CrystalBet /
    # Pinnacle, which expose no SportRadar id — those legs match on name+time.
    sr_match_id: str | None = None
    # CrystalBet only: which odds feed priced this row — "lsport" or "other",
    # read off the list view's `data-game-code` (see scrapers/cb_provider.py).
    # LSport is the feed whose prices carry the mistakes, so the New
    # inconsistencies cycle scans only rows tagged with it. None on every other
    # book, on saved-fixture rows and on CB rows whose game had no code. Not
    # persisted (cache_persistence lists fields explicitly).
    provider: str | None = None

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
    league: str | None = None         # CB league name — for the arbs League column
    pin_max_stake: float | None = None  # Pinnacle maxRiskStake for the matched market
    pin_event_id: str | None = None   # Pinnacle matchup id — for the odds-history chart
    # SportRadar match id of the soft-book event (None on CB). Lets the
    # cross-book grid join Lider↔Betlive exactly, independent of the Pinnacle
    # name-match — see /api/cross_book.
    sr_match_id: str | None = None
    # Match-quality signals carried from the matcher so the UI can flag how much
    # to trust this row's soft-book↔Pinnacle pairing (see edge.match_confidence).
    # A high fuzzy name score with an implausibly large edge usually means the
    # Pinnacle leg is the WRONG game (common in same-named lower divisions).
    match_score: float | None = None         # fuzzy team-name score 0–100
    match_time_delta_sec: float | None = None  # |kickoff_cb − kickoff_pin|; None if unknown
    # When the SOFT-BOOK leg's price was actually fetched. Not cosmetic: edge.py
    # compares a soft price to a reference price without ever checking how old
    # either is, so a stale soft leg reports the drift since it was pulled as
    # edge. CB is the case that matters — a full soccer sweep is ~191 s (3127 s
    # at worst) against Pinnacle's 25 s, and cached detail rows keep the
    # `fetched_at` of the cycle they were EXPANDED in. Carried through so the
    # UI can show an age and the re-verify loop can be judged.
    cb_fetched_at: datetime | None = None
