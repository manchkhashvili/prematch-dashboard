"""New inconsistencies — the LSport-only CrystalBet cycle.

WHY A SEPARATE CYCLE. The owner knows from bet tickets that CrystalBet prices
its board from two feeds and that the mistakes live in one of them, LSport
(see scrapers/cb_provider.py for how a game's feed is read off the list view,
and the measurement behind it). LSport is a thin slice of the board — 9 % of
soccer, 8 % of basketball, 46 % of tennis, none of American football or ice
hockey — but it carried 13 of 19 soccer flags, 2 of 3 basketball and 2 of 2
tennis on the night it was measured. The existing anomaly scans spend their
budget on the other 91 %: a soccer ladder pass is ~900 games at a median of
815 markets each and never completes inside its 240 s allowance, while the
whole LSport soccer board is ~90 games at ~120 markets and expands in ~45 s.

So this module runs the SAME detectors (`find_ladder_anomalies` +
`find_consistency_flags`, permissive classifier, per-section ladders — exactly
what the Anomalies tab runs) over ONLY the LSport games, on its own clock:

  * its own `CbHttpSession` per sport. The shared per-sport sessions in
    cb_http are guarded by crystalbet's sport lock, and ASP.NET keeps the
    expand/collapse view state per session — reusing one without the lock
    would interleave with the price poll's postbacks. A second session is a
    second ASP.NET session on CB's side, and the two never touch;
  * no sport lock, so it never queues behind a 47-minute soccer sweep and
    never makes one queue behind it;
  * no change cache: the board is small enough to expand in full every pass,
    and a pass that re-reads every LSport game is the point — these are the
    prices that go wrong.

Parsing goes through cb_parse_pool like every other CB path, so html5lib
stays off the event loop when the pool is on and the in-process fallback is
byte-identical when it is off (pytest).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from src import horizon
from src.anomalies import find_ladder_anomalies
from src.consistency import find_consistency_flags
from src.models import Odds
from src.scrapers import cb_http, cb_parse_pool, cb_provider

log = logging.getLogger(__name__)

# Same market set the Anomalies tab's ladder detector runs on.
LADDER_MARKETS = ("spread", "total")
# Publish a partial result this often during a pass, so the tab fills as the
# pass runs rather than in one lump at the end.
PROGRESS_EVERY = 10


@dataclass
class ScanStats:
    """What one sport's pass actually did — surfaced on the tab so the cost of
    a horizon or a cadence can be read rather than guessed."""
    sport: str
    at: str
    board: int = 0          # games on the list after the outright/SRL skip
    lsport: int = 0         # of which LSport
    in_horizon: int = 0     # of which inside lsport_horizon_h
    expanded: int = 0
    failed: int = 0         # expansion failed → list-view Odds kept
    list_only: int = 0      # no "+N" badge: nothing to expand, list rows kept
    truncated_at: Optional[int] = None   # index the budget/switch stopped at
    list_sec: float = 0.0
    expand_sec: float = 0.0
    sec: float = 0.0
    by_provider: dict = field(default_factory=dict)   # board census per feed

    def to_dict(self) -> dict:
        return asdict(self)


def _sport_module(sport_name: str):
    from src.scrapers.crystalbet import _SPORT_MODULES
    return _SPORT_MODULES[sport_name]


# ── the session ──────────────────────────────────────────────────────────────
# One per sport, created on first use. Separate from cb_http's module-level
# `_sessions` on purpose (see module docstring).
_sessions: dict[int, cb_http.CbHttpSession] = {}
_locks: dict[int, asyncio.Lock] = {}


def _session(sport_id: int) -> cb_http.CbHttpSession:
    if sport_id not in _sessions:
        _sessions[sport_id] = cb_http.CbHttpSession(sport_id)
    return _sessions[sport_id]


def _lock(sport_id: int) -> asyncio.Lock:
    if sport_id not in _locks:
        _locks[sport_id] = asyncio.Lock()
    return _locks[sport_id]


def reset_session(sport_id: int) -> None:
    sess = _sessions.pop(sport_id, None)
    if sess is not None:
        sess.close()


def close_all() -> None:
    for sid in list(_sessions):
        reset_session(sid)


async def _list_raw(sport_id: int) -> str:
    """Raw list panel on this cycle's own session; one re-warm-and-retry."""
    sess = _session(sport_id)
    try:
        return await asyncio.to_thread(sess.fetch_list_raw)
    except Exception as e:
        log.warning("lsport scan: list fetch failed for sport_id=%d (%s) — "
                    "re-warming and retrying once", sport_id, e)
        reset_session(sport_id)
        return await asyncio.to_thread(_session(sport_id).fetch_list_raw)


async def _expand_raw(sport_id: int, event_id: str) -> str:
    sess = _session(sport_id)
    if sess.s is None:
        raise RuntimeError("lsport scan: session not warmed — list first")
    return await asyncio.to_thread(sess.expand_detail_raw, event_id)


# ── the pass ─────────────────────────────────────────────────────────────────

def select_games(games: list, fetched_at: datetime, horizon_h: float) -> tuple[list, dict]:
    """The LSport games starting within `horizon_h`, soonest kickoff first,
    plus a per-provider census of the whole (kept) board.

    Pure, so the filter is unit-testable without a session. Games with no
    provider (no `data-game-code`) are NOT LSport and are left out — an
    unknown feed is not the one we are looking for.
    """
    census: dict[str, int] = {}
    for g in games:
        census[g.provider or "unknown"] = census.get(g.provider or "unknown", 0) + 1
    cutoff = fetched_at + timedelta(hours=horizon_h)
    picked = [g for g in games
              if g.provider == cb_provider.LSPORT
              and g.start_time is not None and g.start_time <= cutoff]
    picked.sort(key=lambda g: (g.start_time, g.event_id))
    return picked, census


async def scan_sport(
    sport_name: str, *,
    horizon_h: float,
    max_sec: float = 0.0,
    should_continue: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[list[Odds], int, int], Awaitable[None]]] = None,
) -> tuple[list[Odds], ScanStats]:
    """One pass over one sport's LSport games. Returns (odds, stats).

    Every LSport game in horizon is expanded with the permissive classifier in
    per-section ladder mode — the Anomalies tab's parse, unchanged. A game
    whose expansion fails keeps its list-view Odds so it stays on the board
    with its headline markets. `max_sec` bounds the EXPANSION loop only; the
    list fetch before it is one postback and is not worth a clock.
    """
    sport = _sport_module(sport_name)
    sport_id = sport.SPORT_ID
    # A discovered sport has no module of its own; the worker resolves the
    # generic classifier by mode, not by name.
    classify_mode = "generic" if getattr(sport, "GENERIC", False) else "permissive"
    fetched_at = datetime.now(tz=timezone.utc)
    stats = ScanStats(sport=sport_name, at=fetched_at.isoformat())
    t0 = time.monotonic()
    async with _lock(sport_id):
        raw = await _list_raw(sport_id)
        games = await cb_parse_pool.parse_list(raw, sport_name, fetched_at)
        games = horizon.filter_items(games, lambda g: g.start_time,
                                     label=f"lsport {sport_name}")
        stats.board = len(games)
        picked, census = select_games(games, fetched_at, horizon_h)
        stats.by_provider = census
        stats.lsport = census.get(cb_provider.LSPORT, 0)
        stats.in_horizon = len(picked)
        stats.list_sec = round(time.monotonic() - t0, 1)
        log.info("lsport scan %s: %d games on the board, %d LSport, %d within %.0fh",
                 sport_name, stats.board, stats.lsport, stats.in_horizon, horizon_h)

        odds: list[Odds] = []
        t_exp = time.monotonic()
        deadline = t_exp + max_sec if max_sec and max_sec > 0 else None
        for i, g in enumerate(picked, 1):
            out_of_time = deadline is not None and time.monotonic() >= deadline
            if out_of_time or (should_continue is not None and not should_continue()):
                stats.truncated_at = i - 1
                for rest in picked[i - 1:]:
                    odds.extend(rest.list_odds)
                log.info("lsport scan %s: expansion STOPPED at %d/%d (%s) — the "
                         "rest keep their list-view odds", sport_name, i - 1,
                         len(picked), "budget spent" if out_of_time else "switched off")
                break
            if g.market_count is None:
                # CB omits the badge when there are no extra markets (a sumo
                # bout is one 2-way price); an ExpandDetail there can only
                # come back without a table. Keep the list rows, skip the POST.
                odds.extend(g.list_odds)
                stats.list_only += 1
                continue
            try:
                blob = await _expand_raw(sport_id, g.event_id)
                rows = await cb_parse_pool.parse_detail(
                    blob, event_id=g.event_id, home=g.home, away=g.away,
                    league=g.league, start_time=g.start_time, fetched_at=fetched_at,
                    sport_name=sport_name, classify_mode=classify_mode,
                    scope_to_event=True, per_section=True)
                for o in rows:
                    o.provider = g.provider
                if rows:
                    odds.extend(rows)
                    stats.expanded += 1
                else:
                    odds.extend(g.list_odds)
                    stats.failed += 1
            except Exception as e:
                log.warning("lsport scan %s: expand %s (%s v %s) failed: %s",
                            sport_name, g.event_id, g.home, g.away, e)
                odds.extend(g.list_odds)
                stats.failed += 1
            if on_progress is not None and i % PROGRESS_EVERY == 0 and i < len(picked):
                try:
                    await on_progress(list(odds), i, len(picked))
                except Exception:
                    log.exception("lsport scan %s: progress callback failed", sport_name)
        stats.expand_sec = round(time.monotonic() - t_exp, 1)
        stats.sec = round(time.monotonic() - t0, 1)
        log.info("lsport scan %s: %d expanded, %d fallback, %d list-only → %d odds "
                 "(list %.1fs, expand %.1fs)", sport_name, stats.expanded,
                 stats.failed, stats.list_only, len(odds), stats.list_sec, stats.expand_sec)
        return odds, stats


async def discover(known_ids: set[int], horizon_h: float) -> tuple[dict, list[str]]:
    """Sweep every sport on CB's nav that the cycle is not already scanning,
    count its LSport matches in horizon, and register a module for any sport
    that has some (sports/lsport_generic.py; a dedicated module for that sport
    id wins if one exists). Returns (census, names_to_scan).

    "Cover all LSport matches" cannot be a list: the nav moves (four sports
    appeared between two days) and LSport's coverage moves with the calendar.
    One list postback per sport, sessions dropped again where nothing was
    found, so the sweep costs ~1 s per sport and keeps no idle state.
    """
    from src.scrapers.sports import lsport_generic
    fetched_at = datetime.now(tz=timezone.utc)
    nav = await asyncio.to_thread(cb_http.fetch_nav_sports)
    census: dict[str, dict] = {}
    active: list[str] = []
    for sid, label in sorted(nav.items()):
        if sid in known_ids:
            continue
        name = lsport_generic.slug(label, sid)
        row = {"sport_id": sid, "label": label, "kept": 0, "lsport": 0,
               "in_horizon": 0, "other": 0, "at": fetched_at.isoformat(), "error": None}
        try:
            async with _lock(sid):
                raw = await _list_raw(sid)
                games = await cb_parse_pool.parse_list(raw, name, fetched_at)
                games = horizon.filter_items(games, lambda g: g.start_time,
                                             label=f"lsport discover {name}")
                picked, c = select_games(games, fetched_at, horizon_h)
                row.update(kept=len(games), lsport=c.get(cb_provider.LSPORT, 0),
                           in_horizon=len(picked), other=c.get(cb_provider.OTHER, 0))
                if picked:
                    mod = lsport_generic.register(sid, label)
                    row["module"] = mod.SPORT_NAME
                    row["generic"] = bool(getattr(mod, "GENERIC", False))
                    active.append(mod.SPORT_NAME)
                else:
                    reset_session(sid)
        except Exception as e:
            log.warning("lsport discover %s (id %d): %s", name, sid, e)
            row["error"] = str(e)[:120]
            reset_session(sid)
        census[name] = row
    found = {n: r["in_horizon"] for n, r in census.items() if r["in_horizon"]}
    log.info("lsport discover: %d sports on the nav, %d already covered, LSport matches "
             "elsewhere: %s", len(nav), len(known_ids), found or "none")
    return census, active


def detect(odds: list[Odds]) -> tuple[list[Any], list[Any]]:
    """The two detectors the Anomalies tab runs, on this cycle's odds."""
    anoms = find_ladder_anomalies(odds, markets=LADDER_MARKETS, min_pct=0.0)
    flags = find_consistency_flags(odds)
    return anoms, flags
