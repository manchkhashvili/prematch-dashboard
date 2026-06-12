"""
Match CB Odds to Pinnacle Odds by team name similarity + start-time proximity.

Algorithm
---------
  1. Group each source's Odds by (home, away) team pair → "events".
  2. Enumerate ALL (cb, pin) pairs above SCORE_THRESHOLD whose start times
     are within TIME_MATCH_SECONDS of each other.
  3. Sort by score descending; greedy-assign so each event matches at most
     one counterpart. Robust against team-name collisions across leagues.
  4. For every CB event that didn't make threshold, compute its single best
     Pinnacle candidate (regardless of threshold) and bundle that into an
     UnmatchedEvent — for the unmatched_log.csv curation loop.

Start times: both sides are UTC-aware after the scraper-level fixes
(crystalbet.py converts naive Tbilisi → UTC at parse). The matcher does
no timezone arithmetic.

Scoring uses rapidfuzz.fuzz.token_set_ratio over normalize_team() output.
normalize_team applies team_aliases.yaml first, so a CB "Connecticut"
becomes "Connecticut Sun" before fuzzy scoring against Pinnacle.

Public API
----------
  match_events(cb_odds, pin_odds) -> list[MatchedEvent]
      Backwards-compat — returns just the matched list.

  match_with_diagnostics(cb_odds, pin_odds) -> MatchResults
      Returns MatchResults(matched, unmatched). Use this when you also
      want unmatched-CB info for logging / dashboard.

  log_unmatched(result, path=None) -> None
      Append result.unmatched rows to data/unmatched_log.csv.
"""
from __future__ import annotations

import csv
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from rapidfuzz import fuzz

from src.models import Odds
from src.normalize import has_youth_tag, normalize_team, normalize_tennis_name

log = logging.getLogger(__name__)

# Two-tier matching (Phase 3.10, 2026-05-27 — both tiers widened to ±1h):
#   - Strong name match (≥ SCORE_LOOSE) + start times within ±TIME_LOOSE: accept.
#   - Medium name match (≥ SCORE_TIGHT, < SCORE_LOOSE) within ±TIME_TIGHT: accept.
#   - Below SCORE_TIGHT, never accept.
#   - If either start_time is unknown, only the strong tier applies — we lose
#     the time signal so the name confidence has to carry the whole match.
#
# Rationale for the wide window: CB and Pinnacle often disagree on kickoff
# time by 15-45 minutes for the same fixture, especially in tournament
# brackets where TBD slots get filled close to start. ±1h catches those
# without much false-positive risk because rare are two unrelated games
# with similar team names within an hour. Risk concentrated in tennis (lots
# of similar surnames) — watch unmatched_log if false matches appear.
#
# Historical context: previously LOOSE was ±10 min and TIGHT was ±5 min. Many
# pairs that fuzzy-scored 75-100 were getting dropped because of small
# kickoff-time disagreements between books (e.g., CB lists 20:00, Pin lists
# 20:15 for the same Brazilian league fixture).
SCORE_LOOSE = 80.0
SCORE_TIGHT = 65.0
TIME_LOOSE_SECONDS = 3600   # ±1 h (was 600 = ±10 min — Phase 3.10)
TIME_TIGHT_SECONDS = 3600   # ±1 h (was 300 = ±5  min — Phase 3.10; both tiers per user 2026-05-27)

# Backwards-compat alias (one legacy reference may still import this name).
SCORE_THRESHOLD = SCORE_LOOSE

UNMATCHED_LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "unmatched_log.csv"


class MatchedEvent(NamedTuple):
    cb: list[Odds]
    pin: list[Odds]
    home: str   # CB home name (display)
    away: str
    score: float


@dataclass
class UnmatchedEvent:
    """CB event with no Pinnacle match above threshold, plus best below-threshold candidate."""
    cb_home: str
    cb_away: str
    cb_league: str | None
    cb_start_time: datetime | None
    best_pin_home: str | None
    best_pin_away: str | None
    best_pin_league: str | None
    best_score: float


class MatchResults(NamedTuple):
    matched: list[MatchedEvent]
    unmatched: list[UnmatchedEvent]


def match_events(cb_odds: list[Odds], pin_odds: list[Odds]) -> list[MatchedEvent]:
    """Backwards-compat alias: returns just the `matched` list from MatchResults."""
    return match_with_diagnostics(cb_odds, pin_odds).matched


def match_with_diagnostics(
    cb_odds: list[Odds], pin_odds: list[Odds]
) -> MatchResults:
    """Match events + collect best-candidate info for every unmatched CB event."""
    cb_events = _group_by_event(cb_odds)
    pin_events = _group_by_event(pin_odds)

    # Tennis player names need a different normalizer than team-sport names —
    # CB writes "Cobolli F" / "Teixido Garcia M.A." while Pinnacle writes the
    # firstname-first form. See src/normalize.normalize_tennis_name. Detect
    # per-event so a mixed-sport input is handled correctly.
    cb_norm = {
        k: _normalize_event(k[0], k[1], v[0].sport)
        for k, v in cb_events.items()
    }
    pin_norm = {
        k: _normalize_event(k[0], k[1], v[0].sport)
        for k, v in pin_events.items()
    }

    # Enumerate EVERY (cb, pin) pair with its score — no time filter at this
    # stage. The tiered _accept() applies time as part of the threshold rule
    # so we can't pre-filter on time without knowing the score first.
    scored: list[tuple[float, tuple[str, str], tuple[str, str]]] = []
    for cb_key, (ch_n, ca_n) in cb_norm.items():
        cb_time = cb_events[cb_key][0].start_time
        for pin_key, (ph_n, pa_n) in pin_norm.items():
            pin_time = pin_events[pin_key][0].start_time
            score_h = fuzz.token_set_ratio(ch_n, ph_n)
            score_a = fuzz.token_set_ratio(ca_n, pa_n)
            score = (score_h + score_a) / 2.0
            scored.append((score, cb_key, pin_key))

    # Hard youth guard (v2 finding #3): a U16–U23 side must never pair with a
    # senior side — fuzzy scoring can't be trusted here because
    # token_set_ratio scores "nws spirit u20" vs "nws spirit" at 100 (subset).
    # Compared per side, on the RAW names. Unmatched diagnostics below stay
    # unguarded on purpose so the curation log still shows the near-miss.
    cb_youth = {k: (has_youth_tag(k[0]), has_youth_tag(k[1])) for k in cb_events}
    pin_youth = {k: (has_youth_tag(k[0]), has_youth_tag(k[1])) for k in pin_events}

    # Apply tiered accept + global sorted-by-score greedy assignment.
    candidates = sorted(
        (
            c for c in scored
            if cb_youth[c[1]] == pin_youth[c[2]]
            and _accept(
                c[0],
                cb_events[c[1]][0].start_time,
                pin_events[c[2]][0].start_time,
            )
        ),
        key=lambda c: -c[0],
    )
    used_cb: set[tuple[str, str]] = set()
    used_pin: set[tuple[str, str]] = set()
    matched: list[MatchedEvent] = []

    for score, cb_key, pin_key in candidates:
        if cb_key in used_cb or pin_key in used_pin:
            continue
        used_cb.add(cb_key)
        used_pin.add(pin_key)
        matched.append(MatchedEvent(
            cb=cb_events[cb_key],
            pin=pin_events[pin_key],
            home=cb_key[0],
            away=cb_key[1],
            score=score,
        ))
        log.debug(
            "matched (%.0f) %s vs %s → %s vs %s",
            score, cb_key[0], cb_key[1], pin_key[0], pin_key[1],
        )

    # For every unmatched CB event, find its best (still-unused) Pin candidate
    # regardless of threshold — for the curation loop.
    unmatched: list[UnmatchedEvent] = []
    best_by_cb: dict[tuple[str, str], tuple[float, tuple[str, str]]] = {}
    for score, cb_key, pin_key in scored:
        if pin_key in used_pin:
            continue  # already taken by another CB event
        prev = best_by_cb.get(cb_key)
        if prev is None or score > prev[0]:
            best_by_cb[cb_key] = (score, pin_key)

    for cb_key in cb_events:
        if cb_key in used_cb:
            continue
        cb_first = cb_events[cb_key][0]
        best = best_by_cb.get(cb_key)
        if best is None:
            unmatched.append(UnmatchedEvent(
                cb_home=cb_key[0], cb_away=cb_key[1],
                cb_league=cb_first.league,
                cb_start_time=cb_first.start_time,
                best_pin_home=None, best_pin_away=None,
                best_pin_league=None, best_score=0.0,
            ))
            continue
        bscore, bkey = best
        bpin_first = pin_events[bkey][0]
        unmatched.append(UnmatchedEvent(
            cb_home=cb_key[0], cb_away=cb_key[1],
            cb_league=cb_first.league,
            cb_start_time=cb_first.start_time,
            best_pin_home=bkey[0],
            best_pin_away=bkey[1],
            best_pin_league=bpin_first.league,
            best_score=bscore,
        ))

    log.info(
        "matched %d / %d CB events (%d Pinnacle events; %d unmatched)",
        len(matched), len(cb_events), len(pin_events), len(unmatched),
    )
    return MatchResults(matched=matched, unmatched=unmatched)


def log_unmatched(result: MatchResults, *, path: Path = UNMATCHED_LOG_PATH) -> None:
    """Append every unmatched CB event + its best Pinnacle candidate to CSV."""
    if not result.unmatched:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow([
                "ts", "cb_home", "cb_away", "cb_league", "cb_start_time",
                "best_pin_home", "best_pin_away", "best_pin_league", "best_score",
            ])
        for u in result.unmatched:
            w.writerow([
                now, u.cb_home, u.cb_away, u.cb_league or "",
                u.cb_start_time.isoformat() if u.cb_start_time else "",
                u.best_pin_home or "", u.best_pin_away or "",
                u.best_pin_league or "", f"{u.best_score:.1f}",
            ])
    log.info("unmatched_log: appended %d row(s) → %s", len(result.unmatched), path)


def _group_by_event(
    odds_list: list[Odds],
) -> dict[tuple[str, str], list[Odds]]:
    groups: dict[tuple[str, str], list[Odds]] = defaultdict(list)
    for o in odds_list:
        groups[(o.home, o.away)].append(o)
    return dict(groups)


def _normalize_event(home: str, away: str, sport: str) -> tuple[str, str]:
    """Sport-aware name normalization. Tennis uses player-name rules; others use the team rules."""
    if sport == "tennis":
        return normalize_tennis_name(home), normalize_tennis_name(away)
    return normalize_team(home), normalize_team(away)


def _accept(score: float, cb_time, pin_time) -> bool:
    """
    Two-tier acceptance for a candidate match.

    Below SCORE_TIGHT: never accept.
    At/above SCORE_LOOSE: accept within ±TIME_LOOSE_SECONDS.
    Between [SCORE_TIGHT, SCORE_LOOSE): require the tighter ±TIME_TIGHT_SECONDS.
    Either time unknown: fall back to the strong tier only — the time signal
    is unavailable, so name confidence has to carry the entire decision.
    """
    if score < SCORE_TIGHT:
        return False
    if cb_time is None or pin_time is None:
        return score >= SCORE_LOOSE
    delta = abs((cb_time - pin_time).total_seconds())
    if score >= SCORE_LOOSE:
        return delta <= TIME_LOOSE_SECONDS
    return delta <= TIME_TIGHT_SECONDS
