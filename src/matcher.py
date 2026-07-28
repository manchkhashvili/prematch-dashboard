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
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

from rapidfuzz import fuzz

from src.models import Odds
from src.normalize import (
    has_women_suffix_pair,
    has_women_tag,
    has_youth_tag,
    normalize_team,
    normalize_tennis_name,
)

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

# Tennis is not scheduled to a clock (2026-07-28 unmatched-log audit).
# Matches are "not before" / follow-on-court, so CB and Pinnacle legitimately
# post start times hours apart for the same match. 8 026 fixtures scored at the
# STRONG name tier and were still dropped by the ±1 h gate — 91 % of them
# tennis (Wimbledon alone: 3 201). Spot-checked: 'landaluce m' vs
# 'martin landaluce' = 90, 'humbert u' vs 'ugo humbert' = 90.
#
# Applied to the STRONG tier only. The medium tier keeps ±1 h because tennis is
# exactly where surname collisions live ("lots of similar surnames" — the
# Phase 3.10 note above), and a 65-79 name score plus a loose clock is how a
# phantom match gets made. Both players must clear SCORE_LOOSE to earn the
# wider window.
TIME_LOOSE_BY_SPORT = {"tennis": 6 * 3600}    # ±6 h


def _time_loose_for(sport: str | None) -> int:
    return TIME_LOOSE_BY_SPORT.get((sport or "").lower(), TIME_LOOSE_SECONDS)

# Backwards-compat alias (one legacy reference may still import this name).
SCORE_THRESHOLD = SCORE_LOOSE

# ── Wrong-fixture guards (2026-07-11 — the 1xbet arbs false-positive audit) ───
# Three failure modes found live, each with its own guard:
#   1. One-sided matches: croco "tps/oulu" ↔ 1xbet "VJS Vantaa/OLS Oulu" —
#      away scored 100, home 31, mean 65 passed the medium tier. Guard:
#      BOTH sides must clear MIN_SIDE_SCORE.
#   2. Same-city different clubs: "Gold Coast Knights/Brisbane City" ↔
#      "Gold Coast United/Brisbane Olympic" (mean 78, min 76!). Shared city
#      tokens inflate both sides while the DISTINCTIVE tokens contradict
#      (Knights≠United, City≠Olympic). Guard: when both names keep leftover
#      tokens after removing common ones and no leftover pair fuzzy-agrees,
#      demand a near-perfect mean (CONTRADICTION_MEAN).
#   3. Reversed fixtures: 1xbet lists "Tallinna Kalev U21 v Levadia U19" where
#      the softs list Levadia home (direct 65, SWAPPED 100). Cross-priced
#      home/away → phantom 88% edges. Guard/feature: score both orientations;
#      when swapped wins decisively, MATCH IT with the reference odds flipped
#      (selections home↔away, spread lines negated, team_side swapped).
MIN_SIDE_SCORE = 50.0
CONTRADICTION_MEAN = 88.0
FLIP_MIN_MEAN = 85.0
FLIP_MARGIN = 15.0
FLIP_MIN_SIDE = 70.0
_TOKEN_PAIR_OK = 70.0     # leftover-token partial_ratio to count as agreeing

UNMATCHED_LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "unmatched_log.csv"


def _tokens_contradict(a_norm: str, b_norm: str) -> bool:
    """True when both names have leftover distinctive tokens and none of them
    fuzzy-agree — the same-city-different-club tell. Abbreviations survive via
    partial_ratio ("man"→"manchester"=100, "utd"→"united"≥70)."""
    at, bt = set(a_norm.split()), set(b_norm.split())
    a_left, b_left = at - bt, bt - at
    if not a_left or not b_left:
        return False
    for x in a_left:
        for y in b_left:
            if fuzz.partial_ratio(x, y) >= _TOKEN_PAIR_OK:
                return False
    return True


# ── Initialism bridge (2026-07-28 unmatched-log audit) ───────────────────────
# token_set_ratio cannot see that an initialism is the same club:
#   'seinajoen jk' vs 'sjk'  = 26.7      (real match, dropped)
#   'cr brasil al' vs 'crb'  = 26.7      (real match, dropped)
# Both are below SCORE_TIGHT, so they were never matchable at any clock.
#
# This is the highest-risk fix in the audit — a wrong initialism invents a
# fixture and therefore a phantom edge — so it is fenced in hard:
#   * one side must be a single 2-4 char token, the other multi-token;
#   * the expansion must be EXACT (no fuzz) against one of two constructions;
#   * it only lifts the score to SCORE_TIGHT, never to the strong tier, so the
#     pair still has to satisfy the ±1 h medium-tier clock; and
#   * _initialism_bonus is only consulted when the OTHER side of the fixture
#     already scores >= SCORE_LOOSE (see the caller) — it can never on its own
#     turn two weak sides into a match.
_INITIALISM_MAX = 4
_ACRONYM_TOKEN_MAX = 3      # a token this short is itself likely an acronym


def _acronym_variants(tokens: list[str]) -> set[str]:
    """Ways a multi-word club name is shortened.

    A: first letter of every token          seinajoen jk -> 'sj'
    B: short tokens kept whole               seinajoen jk -> 's' + 'jk' = 'sjk'
                                             cr brasil    -> 'cr' + 'b' = 'crb'
    """
    if len(tokens) < 2:
        return set()
    first = "".join(t[0] for t in tokens if t)
    whole = "".join(t if len(t) <= _ACRONYM_TOKEN_MAX else t[0] for t in tokens if t)
    return {first, whole}


def _initialism_bonus(a_norm: str, b_norm: str) -> float:
    """SCORE_TIGHT when one name is exactly the other's initialism, else 0."""
    at, bt = a_norm.split(), b_norm.split()
    for short, long_ in ((at, bt), (bt, at)):
        if len(short) != 1 or len(long_) < 2:
            continue
        s = short[0]
        if not (2 <= len(s) <= _INITIALISM_MAX) or not s.isalpha():
            continue
        if s in _acronym_variants(long_):
            return SCORE_TIGHT
    return 0.0


def _flip_odds(o: Odds) -> Odds:
    """Home/away-swapped view of a reference Odds row (reversed fixture)."""
    import dataclasses
    sel = dict(o.selections)
    if "home" in sel or "away" in sel:
        sel["home"], sel["away"] = sel.get("away"), sel.get("home")
        sel = {k: v for k, v in sel.items() if v is not None}
    line = -o.line if (o.market_type == "spread" and o.line is not None) else o.line
    team_side = {"home": "away", "away": "home"}.get(o.team_side, o.team_side)
    return dataclasses.replace(o, home=o.away, away=o.home, selections=sel,
                               line=line, team_side=team_side)


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
    # Why the best candidate was not accepted. Without these the log could not
    # explain its own rejections: the 2026-07-28 audit needed a separate
    # scoring experiment to discover that 8 026 perfect-NAME fixtures (91 %
    # tennis) were being dropped purely by the ±1 h kickoff gate.
    best_pin_start_time: datetime | None = None
    reject_reason: str = ""


class MatchResults(NamedTuple):
    matched: list[MatchedEvent]
    unmatched: list[UnmatchedEvent]


def match_events(cb_odds: list[Odds], pin_odds: list[Odds]) -> list[MatchedEvent]:
    """Just the `matched` list. This is the hot path — every book, every cycle.

    It skips the unmatched-candidate diagnostics, which lets it also skip
    fuzzy-scoring pairs whose kickoffs are too far apart to ever be accepted
    (see `_time_compatible`). Measured on Lider vs Pinnacle soccer: 895 x 532 =
    476 140 pairs scored, of which only 15 205 were within the +/-1 h window —
    **96.8 % of the scoring was provably wasted**. Matching was 64 % of a full
    cycle's CPU.
    """
    return match_with_diagnostics(cb_odds, pin_odds, diagnostics=False).matched


def _time_compatible(cb_time, pin_time, sport: str | None = None) -> bool:
    """Could this pair EVER pass `_accept`, on time alone?

    The prefilter must never be tighter than the widest bound `_accept` could
    use for this sport, or it would prune pairs that would have matched — so
    it takes the max of the tight bound and this sport's loose bound (±6 h for
    tennis, ±1 h elsewhere).

    An unknown kickoff on either side must stay compatible: `_accept` falls
    back to name confidence alone there, so those pairs are still matchable.
    """
    if cb_time is None or pin_time is None:
        return True
    return abs((cb_time - pin_time).total_seconds()) <= max(
        _time_loose_for(sport), TIME_TIGHT_SECONDS)


def match_with_diagnostics(
    cb_odds: list[Odds], pin_odds: list[Odds], *, diagnostics: bool = True
) -> MatchResults:
    """Match events + collect best-candidate info for every unmatched CB event.

    `diagnostics=False` skips the "best candidate regardless of threshold" pass
    (the curation-log input) and, because nothing then needs scores for
    time-incompatible pairs, prunes those before scoring. Matching results are
    identical either way — the pruned pairs could never have been accepted.
    """
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

    # Enumerate (cb, pin) pairs with their scores. When diagnostics are wanted
    # this is EVERY pair — the curation loop reports the best candidate
    # regardless of threshold, so it needs them all. When they are not
    # (match_events, the hot path), pairs that cannot pass `_accept` on time are
    # skipped before the two fuzzy scores, which is where the CPU goes.
    # Each entry: (score, cb_key, pin_key, flipped, eligible) — `flipped` marks
    # a reversed-fixture match whose reference odds must be home/away-swapped.
    scored: list[tuple[float, tuple, tuple, bool]] = []
    _cb_start = {k: v[0].start_time for k, v in cb_events.items()}
    _pin_start = {k: v[0].start_time for k, v in pin_events.items()}
    _cb_sport = {k: v[0].sport for k, v in cb_events.items()}
    for cb_key, (ch_n, ca_n) in cb_norm.items():
        cb_t = _cb_start[cb_key]
        cb_sp = _cb_sport[cb_key]
        for pin_key, (ph_n, pa_n) in pin_norm.items():
            if not diagnostics and not _time_compatible(
                    cb_t, _pin_start[pin_key], cb_sp):
                continue
            score_h = fuzz.token_set_ratio(ch_n, ph_n)
            score_a = fuzz.token_set_ratio(ca_n, pa_n)
            # Initialism bridge, anchored: only the side whose PARTNER already
            # matches strongly may be lifted, so a bad expansion can never
            # manufacture a fixture out of two weak sides.
            if score_h >= SCORE_LOOSE and score_a < SCORE_TIGHT:
                score_a = max(score_a, _initialism_bonus(ca_n, pa_n))
            elif score_a >= SCORE_LOOSE and score_h < SCORE_TIGHT:
                score_h = max(score_h, _initialism_bonus(ch_n, ph_n))
            score = (score_h + score_a) / 2.0
            # Orientation check: the reference book sometimes lists the same
            # fixture with home/away reversed. When the swapped orientation
            # wins decisively, match it flipped instead of cross-priced.
            sw_h = fuzz.token_set_ratio(ch_n, pa_n)
            sw_a = fuzz.token_set_ratio(ca_n, ph_n)
            sw = (sw_h + sw_a) / 2.0
            flipped = (sw >= FLIP_MIN_MEAN and sw >= score + FLIP_MARGIN
                       and min(sw_h, sw_a) >= FLIP_MIN_SIDE)
            if flipped:
                score, score_h, score_a = sw, sw_h, sw_a
                ph_eff, pa_eff = pa_n, ph_n
            else:
                ph_eff, pa_eff = ph_n, pa_n
            # Guard 1: both sides must independently look like the same team.
            # Guard 2: same-city-different-club — distinctive tokens disagree
            # on either side → only a near-perfect mean may pass.
            # Guard failures stay in `scored` (the unmatched-diagnostics loop
            # wants the best candidate regardless) but are match-ineligible.
            eligible = min(score_h, score_a) >= MIN_SIDE_SCORE and not (
                score < CONTRADICTION_MEAN and (
                    _tokens_contradict(ch_n, ph_eff)
                    or _tokens_contradict(ca_n, pa_eff)))
            scored.append((score, cb_key, pin_key, flipped, eligible))

    # Hard youth guard (v2 finding #3): a U16–U23 side must never pair with a
    # senior side — fuzzy scoring can't be trusted here because
    # token_set_ratio scores "nws spirit u20" vs "nws spirit" at 100 (subset).
    # Compared per side, on the RAW names. Unmatched diagnostics below stay
    # unguarded on purpose so the curation log still shows the near-miss.
    cb_youth = {k: (has_youth_tag(k[0]), has_youth_tag(k[1])) for k in cb_events}
    pin_youth = {k: (has_youth_tag(k[0]), has_youth_tag(k[1])) for k in pin_events}

    # Apply tiered accept + global sorted-by-score greedy assignment. Two hard
    # guards beat the fuzzy score: youth (U16–U23 ≠ senior, on names) and gender
    # (women ≠ men — c[*][2] is the is_women flag from the event key). Both must
    # agree or the pair is dropped no matter how high the name score.
    candidates = sorted(
        (
            c for c in scored
            if c[4]                                # wrong-fixture guards passed
            # youth guard: compare per-side in MATCHED orientation (flip-aware)
            and cb_youth[c[1]] == (
                (pin_youth[c[2]][1], pin_youth[c[2]][0]) if c[3] else pin_youth[c[2]])
            and c[1][2] == c[2][2]                 # gender guard: women↔women only
            and _accept(
                c[0],
                cb_events[c[1]][0].start_time,
                pin_events[c[2]][0].start_time,
                cb_events[c[1]][0].sport,
            )
        ),
        key=lambda c: -c[0],
    )
    used_cb: set[tuple] = set()
    used_pin: set[tuple] = set()
    matched: list[MatchedEvent] = []

    for score, cb_key, pin_key, flipped, _eligible in candidates:
        if cb_key in used_cb or pin_key in used_pin:
            continue
        used_cb.add(cb_key)
        used_pin.add(pin_key)
        pin_rows = pin_events[pin_key]
        if flipped:
            pin_rows = [_flip_odds(o) for o in pin_rows]
            log.info("matched FLIPPED (%.0f) %s vs %s ↔ reference had %s vs %s",
                     score, cb_key[0], cb_key[1], pin_key[0], pin_key[1])
        matched.append(MatchedEvent(
            cb=cb_events[cb_key],
            pin=pin_rows,
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
    best_by_cb: dict[tuple, tuple[float, tuple, bool, bool]] = {}
    for score, cb_key, pin_key, flipped, eligible in scored:
        if pin_key in used_pin:
            continue  # already taken by another CB event
        prev = best_by_cb.get(cb_key)
        if prev is None or score > prev[0]:
            best_by_cb[cb_key] = (score, pin_key, flipped, eligible)

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
                best_pin_start_time=None, reject_reason="no_candidate",
            ))
            continue
        bscore, bkey, bflip, belig = best
        bpin_first = pin_events[bkey][0]
        unmatched.append(UnmatchedEvent(
            cb_home=cb_key[0], cb_away=cb_key[1],
            cb_league=cb_first.league,
            cb_start_time=cb_first.start_time,
            best_pin_home=bkey[0],
            best_pin_away=bkey[1],
            best_pin_league=bpin_first.league,
            best_score=bscore,
            best_pin_start_time=bpin_first.start_time,
            reject_reason=_reject_reason(
                bscore, belig,
                cb_first.start_time, bpin_first.start_time,
                cb_youth[cb_key] == ((pin_youth[bkey][1], pin_youth[bkey][0])
                                     if bflip else pin_youth[bkey]),
                cb_key[2] == bkey[2],
                cb_first.sport,
            ),
        ))

    log.info(
        "matched %d / %d CB events (%d Pinnacle events; %d unmatched)",
        len(matched), len(cb_events), len(pin_events), len(unmatched),
    )
    return MatchResults(matched=matched, unmatched=unmatched)


UNMATCHED_HEADER = [
    "ts", "cb_home", "cb_away", "cb_league", "cb_start_time",
    "best_pin_home", "best_pin_away", "best_pin_league", "best_score",
    "best_pin_start_time", "reject_reason",
]

# Re-log a fixture only when something about it CHANGES, or once a day.
#
# The log is a curation input — it wants the SET of unmatched fixtures, not one
# row per fixture per poll. Before this, every unmatched event was appended
# every cycle: the 2026-07-28 audit found 13 803 338 rows describing 49 520
# distinct fixtures (279x duplication, 2.1 GB over 47 days, ~45 MB/day).
UNMATCHED_REFRESH_SEC = 86_400.0
UNMATCHED_RETENTION_DAYS = 10.0
UNMATCHED_PRUNE_EVERY_SEC = 3600.0
_SEEN_MAX = 100_000        # dedupe-map eviction trigger (~2x a full live board)

# fixture key -> (last_logged_monotonic-ish ts, state tuple)
_unmatched_seen: dict[tuple, tuple[float, tuple]] = {}
_last_prune = 0.0


def _unmatched_key(u: UnmatchedEvent) -> tuple:
    return (u.cb_home, u.cb_away,
            u.cb_start_time.isoformat() if u.cb_start_time else "")


def _unmatched_state(u: UnmatchedEvent) -> tuple:
    """What we consider a meaningful change worth a new row."""
    return (u.best_pin_home or "", u.best_pin_away or "",
            round(u.best_score, 0), u.reject_reason)


def prune_unmatched_log(path: Path = UNMATCHED_LOG_PATH, *,
                        days: float = UNMATCHED_RETENTION_DAYS) -> int:
    """Drop rows older than `days`. Returns the number removed.

    Rewrites via a temp file + atomic replace so a crash mid-prune cannot
    truncate the log. A row whose ts cannot be parsed is KEPT — pruning is
    housekeeping and must never be the thing that eats data.

    Also migrates the legacy 9-column layout (pre best_pin_start_time /
    reject_reason) by padding surviving rows out to the current header, so an
    existing log keeps working instead of interleaving two row widths.
    """
    if not path.exists():
        return 0
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    width = len(UNMATCHED_HEADER)
    tmp = path.with_suffix(path.suffix + ".tmp")
    removed = kept = 0
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as src, \
             tmp.open("w", newline="", encoding="utf-8") as dst:
            r = csv.reader(src)
            w = csv.writer(dst)
            try:
                next(r)                       # old header discarded on purpose
            except StopIteration:
                tmp.unlink(missing_ok=True)
                return 0
            w.writerow(UNMATCHED_HEADER)
            for row in r:
                if not row:
                    continue
                try:
                    if datetime.fromisoformat(row[0]) < cutoff:
                        removed += 1
                        continue
                except (ValueError, IndexError):
                    pass                      # unparseable ts → keep
                w.writerow((row + [""] * width)[:width])
                kept += 1
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    if removed:
        log.info("unmatched_log: pruned %d row(s) older than %.0fd (%d kept) → %s",
                 removed, days, kept, path)
    return removed


def log_unmatched(result: MatchResults, *, path: Path = UNMATCHED_LOG_PATH) -> None:
    """Append unmatched CB events whose state changed since we last saw them."""
    global _last_prune
    if not result.unmatched:
        return
    # Housekeeping lives here so it cannot be forgotten by a caller. It must
    # never be able to stop us writing the log, hence the bare guard.
    if time.time() - _last_prune > UNMATCHED_PRUNE_EVERY_SEC:
        _last_prune = time.time()
        try:
            prune_unmatched_log(path)
        except Exception as e:
            log.warning("unmatched_log prune failed: %s", e)
    now_dt = datetime.now(tz=timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    ts = now_dt.timestamp()

    # Evict fixtures we have not seen for two refresh windows. Without this the
    # dedupe map grows for the life of the process — fixtures churn as events
    # kick off, and nothing ever removed them.
    if len(_unmatched_seen) > _SEEN_MAX:
        stale = ts - 2 * UNMATCHED_REFRESH_SEC
        for k in [k for k, (seen, _) in _unmatched_seen.items() if seen < stale]:
            del _unmatched_seen[k]

    fresh: list[UnmatchedEvent] = []
    for u in result.unmatched:
        key = _unmatched_key(u)
        state = _unmatched_state(u)
        prev = _unmatched_seen.get(key)
        if prev is not None and prev[1] == state and ts - prev[0] < UNMATCHED_REFRESH_SEC:
            continue
        _unmatched_seen[key] = (ts, state)
        fresh.append(u)
    if not fresh:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(UNMATCHED_HEADER)
        for u in fresh:
            w.writerow([
                now, u.cb_home, u.cb_away, u.cb_league or "",
                u.cb_start_time.isoformat() if u.cb_start_time else "",
                u.best_pin_home or "", u.best_pin_away or "",
                u.best_pin_league or "", f"{u.best_score:.1f}",
                u.best_pin_start_time.isoformat() if u.best_pin_start_time else "",
                u.reject_reason,
            ])
    log.info("unmatched_log: appended %d of %d unmatched (rest unchanged) → %s",
             len(fresh), len(result.unmatched), path)


# Kickoff bucket for the event key, in seconds. Two same-named fixtures this far
# apart are DIFFERENT events and must not merge (see _group_by_event). Chosen
# well below _accept()'s +/-1 h tolerance — so it never separates two things the
# matcher would consider the same fixture — and well above any observed
# intra-source drift (measured 2026-07-27 across Pinnacle/Lider/Setanta/Crocobet:
# 0 of 2 820 name-pairs had more than one kickoff, and none mixed a missing
# kickoff with a real one, so nothing legitimate splits).
_EVENT_TIME_BUCKET_SEC = 900


def _time_bucket(start_time) -> int | None:
    if start_time is None:
        return None
    return int(start_time.timestamp()) // _EVENT_TIME_BUCKET_SEC


def _group_by_event(
    odds_list: list[Odds],
) -> dict[tuple[str, str, bool, int | None], list[Odds]]:
    """Group a source's Odds into events. Key is (home, away, is_women, kickoff).

    Gender is part of the key so a men's and women's game that share IDENTICAL
    team names (Australian NBL1 double-headers — same clubs, ~2h apart, marked
    only by "Women" in the league) don't collapse into one mixed event. That
    collapse previously priced a men's line against the women's fair price and
    produced phantom +EV. The marker lives in the league; team names are checked
    too for books that suffix "(W)" or a bare " W".

    KICKOFF is part of the key for the same reason, and it catches the case the
    name-based guards cannot: **Pinnacle lists reserve and U20 fixtures under the
    identical senior team names**. Observed live 2026-07-26 —

        PINNACLE  Aguila v Luis Angel Firpo        -> {21:00, 18:00}
        PINNACLE  Fuerte San Francisco v Platense  -> {21:00, 18:00}

    where Lider showed the 18:00 games as "Cd Aguila U20" / "agila (rez)".
    Without time in the key both Pinnacle events merged into one bucket, and the
    senior soft-book fixture was priced against the RESERVE ladder — 14 phantom
    rows including 6 of 9 ARBs. The youth guard cannot help because Pinnacle's
    names carry no marker; time is the only signal present.
    """
    groups: dict[tuple[str, str, bool, int | None], list[Odds]] = defaultdict(list)
    for o in odds_list:
        women = (has_women_tag(o.league or "", o.home, o.away)
                 or has_women_suffix_pair(o.home, o.away))
        groups[(o.home, o.away, women, _time_bucket(o.start_time))].append(o)
    return dict(groups)


def _normalize_event(home: str, away: str, sport: str) -> tuple[str, str]:
    """Sport-aware name normalization. Tennis uses player-name rules; others use the team rules."""
    if sport == "tennis":
        return normalize_tennis_name(home), normalize_tennis_name(away)
    return normalize_team(home), normalize_team(away)


def _accept(score: float, cb_time, pin_time, sport: str | None = None) -> bool:
    """
    Two-tier acceptance for a candidate match.

    Below SCORE_TIGHT: never accept.
    At/above SCORE_LOOSE: accept within ±_time_loose_for(sport) — ±1 h for
    everything except tennis, which gets ±6 h (see TIME_LOOSE_BY_SPORT).
    Between [SCORE_TIGHT, SCORE_LOOSE): require ±TIME_TIGHT_SECONDS, which
    stays at ±1 h for every sport.
    Either time unknown: fall back to the strong tier only — the time signal
    is unavailable, so name confidence has to carry the entire decision.
    """
    if score < SCORE_TIGHT:
        return False
    if cb_time is None or pin_time is None:
        return score >= SCORE_LOOSE
    delta = abs((cb_time - pin_time).total_seconds())
    if score >= SCORE_LOOSE:
        return delta <= _time_loose_for(sport)
    return delta <= TIME_TIGHT_SECONDS


def _reject_reason(score: float, eligible: bool, cb_time, pin_time,
                   youth_ok: bool, gender_ok: bool, sport: str | None) -> str:
    """Why the best candidate lost. Ordered most-specific first.

    Purely diagnostic — it never influences matching. Written into
    unmatched_log.csv so the log can answer "why" without a separate
    experiment (see UnmatchedEvent).
    """
    # Name first: a pair that never scored is simply not the same fixture.
    # Reserving "wrong_fixture_guard" for pairs that DID score well enough to
    # be candidates is what makes that reason worth reading.
    if score < SCORE_TIGHT:
        return "name"
    if not gender_ok:
        return "gender"
    if not youth_ok:
        return "youth"
    if not eligible:
        return "wrong_fixture_guard"
    if _accept(score, cb_time, pin_time, sport):
        return "taken"        # would have matched, but its Pin event was used
    if cb_time is None or pin_time is None:
        return "no_kickoff"   # medium tier needs a clock and we have none
    return "time"
