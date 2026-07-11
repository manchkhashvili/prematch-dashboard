"""
FastAPI dashboard server for the prematch +EV/ARB scanner.

Multi-sport (Phase 2): runs FOUR background asyncio tasks — one per
(sport, source) pair:
  - _pinnacle_loop_for_sport(basketball)    every PINNACLE_POLL_SEC
  - _pinnacle_loop_for_sport(soccer)        every PINNACLE_POLL_SEC
  - _crystalbet_loop_for_sport(basketball)  every CRYSTALBET_POLL_SEC
  - _crystalbet_loop_for_sport(soccer)      every CRYSTALBET_POLL_SEC

Both sports share the basketball cadence (user 2026-05-26) — soccer odds
move on similar prematch timescales; not worth a separate env knob in v1.

State is held in a per-sport namespaced module-level dict, guarded by
`_state_lock` on the write paths. HTTP handlers read without locking —
Python's GIL makes the dict load atomic, and stale-by-one-update reads
are fine for a dashboard.

Endpoints
---------
  GET /api/matches        — one row per CB event ACROSS sports, Pin counterpart if matched.
                            Soccer rows include cb_draw/pin_draw/edge_draw_pct.
  GET /api/opportunities  — +EV + ARB rows across both sports, sorted by edge%.
  GET /api/status         — per-sport last-update timestamps + error indicators.
  GET /api/unmatched      — CB events with no Pinnacle match (basketball + soccer).
  /                       — static dashboard (matches.html as index).
"""
from __future__ import annotations

import asyncio
import csv
import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Query
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from src import bets, capital, ticks
from src.anomalies import find_ladder_anomalies
from src.consistency import find_consistency_flags
from src.edge import LINE_MATCH_TOLERANCE, compute_opportunities, match_confidence
from src.matcher import (
    MatchedEvent, UnmatchedEvent, log_unmatched,
    match_events, match_with_diagnostics,
)
from src.models import Odds
from src.scrapers import cache_persistence
from src.scrapers.crystalbet import (
    SAMPLE_OUT as CB_SAMPLE_PATH,
    SAMPLE_OUT_MMA as CB_SAMPLE_PATH_MMA,
    SAMPLE_OUT_SOCCER as CB_SAMPLE_PATH_SOCCER,
    SAMPLE_OUT_TENNIS as CB_SAMPLE_PATH_TENNIS,
    close_crystalbet,
    fetch_crystalbet_basketball_anomaly_ladders,
    fetch_crystalbet_basketball_games,
    fetch_crystalbet_basketball_prematch,
    fetch_crystalbet_mma_prematch,
    fetch_crystalbet_soccer_prematch,
    fetch_crystalbet_tennis_prematch,
    get_detail_status_map,
    get_last_expanded_map,
    parse_html as parse_cb_html,
    parse_html_mma as parse_cb_html_mma,
    parse_html_soccer as parse_cb_html_soccer,
    parse_html_tennis as parse_cb_html_tennis,
)
from src.scrapers.pinnacle import (
    fetch_pinnacle_basketball,
    fetch_pinnacle_mma,
    fetch_pinnacle_soccer,
    fetch_pinnacle_tennis,
)
from src.vig import devig_2way, devig_3way

log = logging.getLogger(__name__)

# ── Config (env-tunable) ──────────────────────────────────────────────────────
PINNACLE_POLL_SEC = int(os.environ.get("PINNACLE_POLL_SEC", "60"))
# 60s default since the browser-free CB transport (2026-06-12) — a full
# list-mode cycle is ~1-2s/sport over HTTP. Was 180s in the Playwright era;
# override via env if running CB_TRANSPORT=playwright with full detail.
CRYSTALBET_POLL_SEC = int(os.environ.get("CRYSTALBET_POLL_SEC", "60"))
CB_HEADLESS = os.environ.get("CB_HEADLESS", "1") != "0"

# When CB_USE_SAVED=1, skip live scraping for BOTH sports and parse the
# saved sample HTML every cycle. Useful for frontend dev — saves the
# ~25 s CB scrape per cycle and keeps the data deterministic.
CB_USE_SAVED = os.environ.get("CB_USE_SAVED", "0") == "1"


# ── Sport configuration ───────────────────────────────────────────────────────
@dataclass(frozen=True)
class SportConfig:
    """Per-sport plumbing: fetchers, saved-HTML path, parse fn."""
    sport_name: str
    cb_fetcher: Callable[..., Awaitable[list[Odds]]]
    pin_fetcher: Callable[..., Awaitable[list[Odds]]]
    cb_sample_path: Path
    cb_parse_html: Callable[[str, datetime], list[Odds]]


_ALL_SPORTS: list[SportConfig] = [
    SportConfig(
        sport_name="basketball",
        cb_fetcher=fetch_crystalbet_basketball_prematch,
        pin_fetcher=fetch_pinnacle_basketball,
        cb_sample_path=CB_SAMPLE_PATH,
        cb_parse_html=parse_cb_html,
    ),
    SportConfig(
        sport_name="soccer",
        cb_fetcher=fetch_crystalbet_soccer_prematch,
        pin_fetcher=fetch_pinnacle_soccer,
        cb_sample_path=CB_SAMPLE_PATH_SOCCER,
        cb_parse_html=parse_cb_html_soccer,
    ),
    SportConfig(
        sport_name="tennis",
        cb_fetcher=fetch_crystalbet_tennis_prematch,
        pin_fetcher=fetch_pinnacle_tennis,
        cb_sample_path=CB_SAMPLE_PATH_TENNIS,
        cb_parse_html=parse_cb_html_tennis,
    ),
    SportConfig(
        sport_name="mma",
        cb_fetcher=fetch_crystalbet_mma_prematch,
        pin_fetcher=fetch_pinnacle_mma,
        cb_sample_path=CB_SAMPLE_PATH_MMA,
        cb_parse_html=parse_cb_html_mma,
    ),
]

# Phase 2.5 #5: unified SPORTS env knob. Per-sport mode in one variable.
# Syntax (both forms accepted, colon and underscore are equivalent):
#   SPORTS=basketball:full,soccer:list
#   SPORTS=basketball_full,soccer_list
# Modes per sport:
#   full → CB scrape + Pin fetch + per-game detail expansion (default).
#   list → CB list-view scrape only; no per-game detail. ~30s/cycle instead
#          of minutes. Loses alt-lines, team_total, corners, H1 markets.
#          1X2/Total FT still populate.
#   off  → sport disabled entirely. No CB poll, no Pin fetch, no state entry.
#          Sports absent from SPORTS are equivalent to off.
# Examples:
#   SPORTS=basketball:full,soccer:list   recommended laptop-friendly
#   SPORTS=basketball                    basketball only, full mode
#   SPORTS=basketball:full,soccer:off    soccer disabled
#   (env unset)                          all known sports in full mode
#
# Back-compat: if SPORTS is unset but legacy ENABLED_SPORTS /
# CB_SKIP_DETAIL_SPORTS are set, those still work.
def _parse_sports_env(s: str) -> dict[str, str]:
    """Parse SPORTS env into {sport_name: mode}. Strict on mode values; logs and
    skips unknown modes. mode=off entries are dropped (treated as 'not listed')."""
    result: dict[str, str] = {}
    for token in s.split(","):
        token = token.strip()
        if not token:
            continue
        # Accept colon or underscore as separator. rsplit so multi-underscore
        # sport names (future-proof: 'ice_hockey_full') still work.
        if ":" in token:
            name, mode = token.split(":", 1)
        elif "_" in token:
            name, mode = token.rsplit("_", 1)
        else:
            name, mode = token, "full"
        name = name.strip().lower()
        mode = mode.strip().lower()
        if not name:
            continue
        if name == "anomalies":
            # Not a sport — it's the anomaly-scanner toggle, handled separately
            # (see ANOMALY_SCAN below). Skip so it doesn't warn as a bad mode.
            continue
        if mode == "off":
            continue
        if mode not in ("full", "list"):
            log.warning(
                "SPORTS=%r: unknown mode %r for sport %r; treating as 'full'",
                s, mode, name,
            )
            mode = "full"
        result[name] = mode
    return result


_SPORTS_ENV = os.environ.get("SPORTS", "").strip()
_LEGACY_ENABLED = os.environ.get("ENABLED_SPORTS", "").strip()
_LEGACY_SKIP = os.environ.get("CB_SKIP_DETAIL_SPORTS", "").strip()

if _SPORTS_ENV:
    _sport_modes = _parse_sports_env(_SPORTS_ENV)
    SPORTS = [s for s in _ALL_SPORTS if s.sport_name in _sport_modes]
    if not SPORTS:
        log.warning(
            "SPORTS=%r matched no known sports — falling back to all-full (%s)",
            _SPORTS_ENV, [s.sport_name for s in _ALL_SPORTS],
        )
        SPORTS = _ALL_SPORTS
        _sport_modes = {s.sport_name: "full" for s in _ALL_SPORTS}
    # Inject the list-only set into the CB scraper so its per-game expansion
    # loop short-circuits for the right sports. Overrides whatever
    # CB_SKIP_DETAIL_SPORTS contained at module-load time.
    from src.scrapers import crystalbet as _cb
    _cb.SKIP_DETAIL_SPORTS = frozenset(
        n for n, m in _sport_modes.items() if m == "list"
    )
    log.info(
        "SPORTS=%s → enabled=%s, list-only=%s",
        _SPORTS_ENV, [s.sport_name for s in SPORTS], sorted(_cb.SKIP_DETAIL_SPORTS),
    )
elif _LEGACY_ENABLED:
    # Legacy back-compat path. ENABLED_SPORTS still parses; list-only
    # comes from CB_SKIP_DETAIL_SPORTS (read at crystalbet import time).
    _enabled = {s.strip() for s in _LEGACY_ENABLED.split(",") if s.strip()}
    SPORTS = [s for s in _ALL_SPORTS if s.sport_name in _enabled]
    if not SPORTS:
        log.warning(
            "ENABLED_SPORTS=%r matched no known sports — falling back to all (%s)",
            _LEGACY_ENABLED, [s.sport_name for s in _ALL_SPORTS],
        )
        SPORTS = _ALL_SPORTS
    if _LEGACY_SKIP:
        log.info("legacy CB_SKIP_DETAIL_SPORTS=%s in effect", _LEGACY_SKIP)
else:
    SPORTS = _ALL_SPORTS
    if _LEGACY_SKIP:
        log.info("legacy CB_SKIP_DETAIL_SPORTS=%s in effect", _LEGACY_SKIP)

SPORT_NAMES = tuple(s.sport_name for s in SPORTS)


# ── Extra soft books (Phase 7 — Lider-Bet, Betlive; env-gated) ────────────────
# Two additional Georgian books, each matched against Pinnacle exactly like
# CrystalBet (and against each other for cross-book arbs). Entirely opt-in:
# default-off reproduces the CB-vs-Pin pipeline byte-for-byte (no state slots,
# no pollers, no opportunity rows). Enable per book:
#     LIDERBET=1 BETLIVE=1 python main.py
# (lowercase liderbet=1 / betlive=1 also accepted). EXTRA_BOOK_POLL_SEC sets the
# poll cadence for these JSON books (cheaper than CB; default 120s).
from src.scrapers import betlive as _betlive  # noqa: E402
from src.scrapers import liderbet as _liderbet  # noqa: E402
from src.scrapers import crocobet as _crocobet  # noqa: E402

_BOOK_FETCHERS: dict[str, dict[str, Any]] = {
    "liderbet": {
        "soccer":     _liderbet.fetch_liderbet_soccer,
        "basketball": _liderbet.fetch_liderbet_basketball,
        "tennis":     _liderbet.fetch_liderbet_tennis,
    },
    "betlive": {
        "soccer":     _betlive.fetch_betlive_soccer,
        "basketball": _betlive.fetch_betlive_basketball,
        "tennis":     _betlive.fetch_betlive_tennis,
    },
    "crocobet": {
        "soccer":     _crocobet.fetch_crocobet_soccer,
        "basketball": _crocobet.fetch_crocobet_basketball,
        "tennis":     _crocobet.fetch_crocobet_tennis,
    },
}


def _book_enabled(name: str) -> bool:
    val = os.environ.get(name.upper(), os.environ.get(name.lower(), "0")).strip().lower()
    return val in ("1", "on", "true", "yes")


EXTRA_BOOKS: tuple[str, ...] = tuple(b for b in ("liderbet", "betlive", "crocobet") if _book_enabled(b))
# All soft books in priority order — CB first (the original), then any enabled
# extras. Pinnacle is the sharp reference, never in this list.
SOFT_BOOKS: tuple[str, ...] = ("cb", *EXTRA_BOOKS)
EXTRA_BOOK_POLL_SEC = int(os.environ.get("EXTRA_BOOK_POLL_SEC", "120"))


# ── Anomaly scanner config (Phase 6) ──────────────────────────────────────────
# A SEPARATE hourly scan: scrapes CB basketball in FULL detail mode
# (force_detail=True) regardless of the SPORTS=...:list config, runs the
# CB-only ladder monotonicity detector, and serves the result on the
# Anomalies tab. Independent of the normal per-sport polls so you can run
# everything else in light list mode.
#
# Enable with ANOMALY_SCAN=1, or by adding an `anomalies` / `anomalies:on`
# token to SPORTS, e.g.:
#   ANOMALY_SCAN=1 SPORTS=basketball:list,soccer:list,tennis:list python main.py
#   SPORTS=basketball:list,soccer:list,anomalies:on python main.py
def _anomaly_enabled() -> bool:
    if os.environ.get("ANOMALY_SCAN", "").strip().lower() in ("1", "on", "true", "yes"):
        return True
    for token in _SPORTS_ENV.split(","):
        nm = token.split(":", 1)[0].split("_", 1)[0].strip().lower()
        if nm == "anomalies":
            return True
    return False


ANOMALY_SCAN = _anomaly_enabled()
# CB extended (full-ladder) scan cadence. Owner call 2026-07-11: CB every 5 min
# (it's the heaviest book — ~65s+CPU for a full board), other books every 2.5
# min (see BETLIVE_DISCOVER_SEC / EXTRA_BOOK_POLL_SEC). List-mode polls unchanged.
ANOMALY_SCAN_SEC = int(os.environ.get("ANOMALY_SCAN_SEC", "300"))  # fixed-interval fallback
ANOMALY_MARKETS = ("spread", "total")
# Fast "watch" loop: re-scrape ONLY games that already have >=1 anomaly, this
# often (default every 5 min), so flagged games refresh faster than the 30-min
# full board scan. 0 disables the watch loop.
ANOMALY_WATCH_SEC = int(os.environ.get("ANOMALY_WATCH_SEC", "300"))

# Clock-aligned scan: run at each listed minute of every hour. Default "15,45"
# = every 30 min, so a fresh scan lands a couple of minutes before :00 and :30.
# Comma-separated; the scan takes ~3 min. Set ANOMALY_SCAN_AT_MINUTE to ""/"off"
# /"-1" to fall back to the fixed ANOMALY_SCAN_SEC interval instead.
_scan_min_env = os.environ.get("ANOMALY_SCAN_AT_MINUTE", "15,45").strip().lower()
if _scan_min_env in ("", "off", "none", "-1"):
    ANOMALY_SCAN_AT_MINUTES: list[int] | None = None
else:
    try:
        ANOMALY_SCAN_AT_MINUTES = sorted({int(x) % 60 for x in _scan_min_env.split(",") if x.strip()})
        if not ANOMALY_SCAN_AT_MINUTES:
            ANOMALY_SCAN_AT_MINUTES = [15, 45]
    except ValueError:
        ANOMALY_SCAN_AT_MINUTES = [15, 45]


# ── Betlive favourite-flip watch (browser-free, two-speed) ────────────────────
# Opt-in (BETLIVE_ANOMALY=1), independent of the Playwright CB ANOMALY_SCAN — it
# is browser-free REST, so it runs anywhere. Surfaces incl-OT vs regulation
# moneyline inconsistencies (src/betlive_anomalies.py) into the Anomalies tab's
# consistency list. DISCOVER does the heavy full-ladder sweep infrequently;
# WATCH re-prices just the moneylines via refreshOdds every few seconds (cheap),
# so detection latency is seconds while the board cost stays tiny.
def _betlive_anomaly_enabled() -> bool:
    return os.environ.get("BETLIVE_ANOMALY", "").strip().lower() in ("1", "on", "true", "yes")


BETLIVE_ANOMALY = _betlive_anomaly_enabled()
# Betlive extended sweep — a non-CB book → 2.5 min (owner call 2026-07-11).
BETLIVE_DISCOVER_SEC = int(os.environ.get("BETLIVE_DISCOVER_SEC", "150"))  # heavy sweep cadence
BETLIVE_WATCH_SEC = int(os.environ.get("BETLIVE_WATCH_SEC", "8"))          # cheap refreshOdds cadence
BETLIVE_MIN_GAP_PP = float(os.environ.get("BETLIVE_MIN_GAP_PP", "2.0"))    # report >= this many pp
try:
    BETLIVE_ANOMALY_SPORT_IDS = tuple(
        int(x) for x in os.environ.get("BETLIVE_ANOMALY_SPORT_IDS", "6").split(",") if x.strip()
    ) or (6,)
except ValueError:
    BETLIVE_ANOMALY_SPORT_IDS = (6,)


# ── Soft-book HT/FT + basketball-favourite sweep (opt-in, slow) ───────────────
# Filtered soccer HT/FT (>=1.2× the first-half leg) + basketball favourite
# disagreement (2-way/3-way/HT ML) across CrystalBet, Betlive, Lider-Bet. Heavy
# per sweep (opens the filtered set's full ladders) but the anomalies persist for
# hours, so it runs on its own SLOW cadence — default 60 min — independent of the
# 30-min CB basketball ladder scan (which it does NOT touch). See src/soft_scan.py.
SOFT_SCAN = os.environ.get("SOFT_SCAN", "").strip().lower() in ("1", "on", "true", "yes")
SOFT_SCAN_SEC = int(os.environ.get("SOFT_SCAN_SEC", "3600"))


# ── Shared state (per-sport namespaced) ───────────────────────────────────────
def _empty_source_state() -> dict[str, Any]:
    return {"odds": [], "fetched_at": None, "error": None, "count": 0}


def _empty_sport_state() -> dict[str, Any]:
    # cb + pin always; one slot per enabled extra book (none by default).
    st = {"cb": _empty_source_state(), "pin": _empty_source_state()}
    for book in EXTRA_BOOKS:
        st[book] = _empty_source_state()
    return st


_state: dict[str, Any] = {sport: _empty_sport_state() for sport in SPORT_NAMES}
_state_lock = asyncio.Lock()

# ── Pinnacle steam / top-moves detector (Phase 5.1) ───────────────────────────
# For each sport we keep the PREVIOUS cycle's no-vig fair probabilities keyed by
# market, and a ROLLING WINDOW of moves from the last _MOVE_RETENTION_SEC. A
# "move" = a side whose fair probability rose by >= _MOVE_MIN_PP percentage
# points vs the previous snapshot — i.e., sharp money came in on that side.
# Surfacing only the rising side(s) is the steam signal (the opposing side
# falls by definition). Phase 5.3: _recent_moves accumulates across cycles and
# is pruned by age, so the Top Moves page shows the last few minutes of steam
# rather than just the most recent cycle.
_pin_prev_fair: dict[str, dict] = {sport: {} for sport in SPORT_NAMES}
_recent_moves: dict[str, list[dict]] = {sport: [] for sport in SPORT_NAMES}
_MOVE_MIN_PP = 2.0       # minimum fair-prob shift (percentage points) to surface a move
_MOVE_RETENTION_SEC = 600  # keep moves for this long (10 min) on the Top Moves feed

# Phase 5.6: per-market MONEYLINE series for the cumulative ("net over 1h")
# view. Scoped to moneyline only to keep memory small (~15 MB; full-depth
# would be 10x+). Structure: sport → market_key → {"meta": {...}, "points":
# [(ts_iso, fair_prob_dict, odds_dict), ...]}. Pruned to _HOUR_WINDOW_SEC.
_pin_ml_series: dict[str, dict] = {sport: {} for sport in SPORT_NAMES}
_HOUR_WINDOW_SEC = 3600  # cumulative-moves lookback window (1 hour)

# ── Anomaly scan state (Phase 6) ──────────────────────────────────────────────
# Filled by the hourly _anomaly_loop. `_anomaly_cb_odds` keeps the full CB odds
# from the last scan so the endpoint can re-match against the CURRENT Pinnacle
# state (Pin fair + recent moves stay live even though the CB scan is hourly).
_recent_anomalies: list[dict] = []          # base anomaly rows (CB-only fields)
_recent_consistency: list[dict] = []        # CB-internal consistency flags (this scan)
_anomaly_coverage: dict = {}                 # what the last scan actually processed
_anomaly_cb_odds: list[Odds] = []           # CB odds snapshot from the last scan
_anomaly_watchlist: set[str] = set()        # event_ids with >=1 anomaly (fast-refresh set)
_anomaly_watch_at: datetime | None = None    # last fast watch re-scan time
_anomalies_computed_at: datetime | None = None
_anomalies_cb_fetched_at: datetime | None = None
_anomalies_error: str | None = None

# ── Betlive favourite-flip watch state ────────────────────────────────────────
# Decoupled from the CB consistency list so neither subsystem can clobber the
# other. Merged into /api/anomalies `consistency` at request time.
_betlive_consistency: list[dict] = []        # favourite-flip flags (betlive book)
_betlive_watch: dict = {}                    # {event_id: {meta, reg, ot}} from discover
_betlive_computed_at: datetime | None = None  # last discover/watch update
_betlive_error: str | None = None
_betlive_energy: dict = {}                    # bytes/time of the last discover sweep

# ── Soft-book HT/FT + basketball-favourite sweep state ────────────────────────
_soft_scan_flags: list[dict] = []             # consistency rows from soft_scan.scan_all
_soft_scan_at: datetime | None = None
_soft_scan_error: str | None = None


# ── Background pollers (parameterized over sport) ─────────────────────────────
async def _pinnacle_loop_for_sport(cfg: SportConfig):
    """Poll Pinnacle for one sport. Writes into _state[sport]['pin'].

    After each successful fetch, ALSO records an odds snapshot for every open
    bet whose sport matches (or whose sport isn't in our pool — caught by
    _current_odds_for_bet's sport-filter). Bounded: only OPEN bets get
    snapshots, and once a bet is settled the recorder stops touching it.
    """
    sport = cfg.sport_name
    while True:
        try:
            t0 = time.monotonic()
            odds = await cfg.pin_fetcher()
            dt = time.monotonic() - t0
            async with _state_lock:
                _state[sport]["pin"]["odds"] = odds
                _state[sport]["pin"]["fetched_at"] = datetime.now(tz=timezone.utc)
                _state[sport]["pin"]["error"] = None
                _state[sport]["pin"]["count"] = len(odds)
            log.info("pinnacle %s: %d Odds rows in %.1fs", sport, len(odds), dt)

            # Change-only tick history (charts) — see src/ticks.py.
            try:
                await asyncio.to_thread(
                    ticks.get_store().record_cycle, "pin", sport,
                    ticks.rows_from_odds(odds), dur_ms=int(dt * 1000),
                )
            except Exception as e:
                log.warning("tick store write failed (pin %s): %s", sport, e)

            # Snapshot every open bet for THIS sport. Other sports' pollers
            # do the same for their bets — each Pin cycle gives all-sport
            # bets a row, just staggered by sport poll timing.
            try:
                _snapshot_open_bets_for_sport(sport)
            except Exception as e:
                log.warning("bet history snapshot failed (%s): %s", sport, e)

            # Phase 5.1: compute Pinnacle line moves vs the previous cycle.
            try:
                _compute_pin_moves(sport, odds)
            except Exception as e:
                log.warning("pin move detection failed (%s): %s", sport, e)
        except Exception as e:
            log.exception("pinnacle %s fetch failed", sport)
            async with _state_lock:
                _state[sport]["pin"]["error"] = str(e)[:200]
            try:
                await asyncio.to_thread(
                    ticks.get_store().record_cycle, "pin", sport, [],
                    ok=False, error=str(e)[:200],
                )
            except Exception:
                pass
        await asyncio.sleep(PINNACLE_POLL_SEC)


async def _extra_book_loop_for_sport(book: str, sport: str):
    """Poll one extra soft book (Lider-Bet / Betlive) for one sport.

    Writes into _state[sport][book]. Same shape as the Pinnacle/CB loops but
    for a plain JSON fetcher; also feeds the change-only tick store so these
    books get odds-history charts too. Only spawned when the book's env flag
    is set, so this code path is inert in the default CB-vs-Pin config.
    """
    fetcher = _BOOK_FETCHERS.get(book, {}).get(sport)
    if fetcher is None:
        log.info("%s: no fetcher for sport %s — loop not started", book, sport)
        return
    while True:
        try:
            t0 = time.monotonic()
            odds = await fetcher()
            dt = time.monotonic() - t0
            async with _state_lock:
                _state[sport][book]["odds"] = odds
                _state[sport][book]["fetched_at"] = datetime.now(tz=timezone.utc)
                _state[sport][book]["error"] = None
                _state[sport][book]["count"] = len(odds)
            log.info("%s %s: %d Odds rows in %.1fs", book, sport, len(odds), dt)
            try:
                await asyncio.to_thread(
                    ticks.get_store().record_cycle, book, sport,
                    ticks.rows_from_odds(odds), dur_ms=int(dt * 1000),
                )
            except Exception as e:
                log.warning("tick store write failed (%s %s): %s", book, sport, e)
        except Exception as e:
            log.exception("%s %s fetch failed", book, sport)
            async with _state_lock:
                _state[sport][book]["error"] = str(e)[:200]
        await asyncio.sleep(EXTRA_BOOK_POLL_SEC)


def _snapshot_open_bets_for_sport(sport: str) -> None:
    """Insert one bet_odds_history row per open bet in this sport.

    Also stamps `pin_fair_closing` once kickoff has passed — that becomes the
    permanent CLV anchor. After kickoff we keep the bet 'open' (the user
    settles manually) but stop touching the closing-fair field.
    """
    now = datetime.now(tz=timezone.utc)
    for bet in bets.list_bets(status="open"):
        if bet.get("sport") != sport:
            continue
        cb_now, pin_now = _current_odds_for_bet(bet)
        if cb_now is None and pin_now is None:
            continue
        bets.record_history_snapshot(bet["id"], cb_now, pin_now)

        # Set pin_fair_closing once kickoff has passed and we haven't yet.
        if bet.get("pin_fair_closing") is None and pin_now is not None:
            st_iso = bet.get("start_time")
            if st_iso:
                try:
                    st = datetime.fromisoformat(st_iso)
                except ValueError:
                    continue
                if st <= now:
                    bets.update_bet(bet["id"], pin_fair_closing=pin_now)
                    log.info(
                        "bet %d: closing Pin fair stamped at %.3f (kickoff passed)",
                        bet["id"], pin_now,
                    )


# ── Pinnacle move detection ────────────────────────────────────────────────────
def _pin_market_key(o: Odds) -> tuple:
    """Stable key identifying one Pinnacle market across cycles."""
    return (o.raw_event_id, o.market_type, o.period, o.line, o.submarket, o.team_side)


def _move_market_label(o: Odds) -> str:
    """Compact market label for a move row, e.g. 'spread FT -3.5' or 'moneyline FT'."""
    parts = [o.market_type, o.period]
    if o.line is not None:
        parts.append(f"{o.line:+g}")
    label = " ".join(parts)
    if o.submarket:
        label += f" ({o.submarket})"
    if o.team_side:
        label += f" ({o.team_side})"
    return label


def _compute_pin_moves(sport: str, odds: list[Odds]) -> None:
    """Detect Pinnacle moves against CHANGE-ANCHORED baselines.

    A "move" is a side whose fair probability ROSE by >= _MOVE_MIN_PP points
    since its BASELINE — the snapshot where this market last moved (or was
    first seen) — not just since the previous poll cycle. Pinnacle's guest
    API is poll-only (no push), so this is how "record moves when they
    happen" translates: a creeping line (+0.8pp per cycle for three cycles)
    surfaces as one move the moment its cumulative shift crosses the
    threshold, with the actual time window it took (`window_sec`). Markets
    that don't change never re-anchor and never emit. The opposing side
    falls by construction; we don't double-report it.

    Stores the move list in _recent_moves[sport] and the baselines in
    _pin_prev_fair[sport].
    """
    # Build current fair-prob + posted-odds map keyed by market.
    current: dict[tuple, dict] = {}
    for o in odds:
        fair_dec = _maybe_devig(o)
        if not fair_dec:
            continue
        fair_prob = {side: (1.0 / dec) for side, dec in fair_dec.items() if dec and dec > 0}
        if not fair_prob:
            continue
        current[_pin_market_key(o)] = {
            "fair_prob": fair_prob,
            "odds": dict(o.selections),
            "home": o.home, "away": o.away,
            "label": _move_market_label(o),
            "league": o.league,
            "start_time": o.start_time.isoformat() if o.start_time else None,
            "max_stake": o.max_stake,
            "pin_event_id": o.raw_event_id,  # for the odds-history chart
            # Structured market spec — drives the Log-bet prefill on moves.html.
            "market_type": o.market_type,
            "period": o.period,
            "line": o.line,
            "submarket": o.submarket,
            "team_side": o.team_side,
        }

    baselines = _pin_prev_fair.get(sport, {})
    now = datetime.now(tz=timezone.utc)
    now_iso = now.isoformat()
    moves: list[dict] = []
    new_baselines: dict = {}

    for key, cur in current.items():
        base = baselines.get(key)
        if not base:
            # First sighting — anchor here, nothing to compare yet.
            new_baselines[key] = {
                "fair_prob": cur["fair_prob"], "odds": cur["odds"], "ts": now_iso,
            }
            continue
        fired = False
        for side, new_prob in cur["fair_prob"].items():
            old_prob = base["fair_prob"].get(side)
            if old_prob is None:
                continue
            delta_pp = (new_prob - old_prob) * 100.0
            if delta_pp < _MOVE_MIN_PP:
                continue  # only surface the side that strengthened
            fired = True
            try:
                window_sec = int((now - datetime.fromisoformat(base["ts"])).total_seconds())
            except (ValueError, KeyError):
                window_sec = None
            moves.append({
                "sport": sport,
                "match_label": f"{cur['home']} — {cur['away']}",
                "market": cur["label"],
                "league": cur.get("league"),
                "side": side,
                "old_odds": base["odds"].get(side),
                "new_odds": cur["odds"].get(side),
                # No-vig fair DECIMAL odds at the current snapshot (1/fair_prob).
                # Computed from the unrounded prob for precision. The gap
                # between new_odds (posted) and fair_now_odds is the vig left
                # on this side right now.
                "fair_now_odds": round(1.0 / new_prob, 3) if new_prob > 0 else None,
                "old_prob_pct": round(old_prob * 100.0, 2),
                "new_prob_pct": round(new_prob * 100.0, 2),
                "delta_pp": round(delta_pp, 2),
                "window_sec": window_sec,
                "max_stake": cur.get("max_stake"),
                "pin_event_id": cur.get("pin_event_id"),
                "start_time": cur["start_time"],
                "recorded_at": now_iso,
                # Structured spec for the Log-bet prefill (Phase 5.4).
                "market_type": cur["market_type"],
                "period": cur["period"],
                "line": cur["line"],
                "submarket": cur["submarket"],
                "team_side": cur["team_side"],
            })
        if fired:
            # Move emitted — re-anchor at the current snapshot so the next
            # move measures from here.
            new_baselines[key] = {
                "fair_prob": cur["fair_prob"], "odds": cur["odds"], "ts": now_iso,
            }
        else:
            # Below threshold — keep the anchor so slow drift accumulates.
            new_baselines[key] = base

    # Markets that vanished from this cycle (kickoff, suspension): keep their
    # baseline for up to an hour so a transient gap doesn't reset the anchor.
    cutoff_baseline = now - timedelta(seconds=_HOUR_WINDOW_SEC)
    for key, base in baselines.items():
        if key in new_baselines:
            continue
        try:
            if datetime.fromisoformat(base["ts"]) >= cutoff_baseline:
                new_baselines[key] = base
        except (ValueError, KeyError):
            continue

    # Phase 5.3: accumulate into a rolling window instead of overwriting.
    # Append this cycle's moves to whatever's still within the retention
    # window, then drop anything older than _MOVE_RETENTION_SEC.
    cutoff = now - timedelta(seconds=_MOVE_RETENTION_SEC)
    kept: list[dict] = []
    for m in _recent_moves.get(sport, []) + moves:
        try:
            if datetime.fromisoformat(m["recorded_at"]) >= cutoff:
                kept.append(m)
        except (ValueError, KeyError):
            continue  # malformed timestamp — drop it
    _recent_moves[sport] = kept
    _pin_prev_fair[sport] = new_baselines

    # Phase 5.6: append this cycle's MONEYLINE fair/odds to the 1h series,
    # then prune. Used by the cumulative ("net over 1h") view.
    hr_cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=_HOUR_WINDOW_SEC)
    series_map = _pin_ml_series.setdefault(sport, {})
    seen_keys: set = set()
    for key, cur in current.items():
        if key[1] != "moneyline":   # key = (eid, market_type, period, line, sub, team)
            continue
        seen_keys.add(key)
        entry = series_map.get(key)
        if entry is None:
            entry = {
                "meta": {
                    "home": cur["home"], "away": cur["away"], "label": cur["label"],
                    "league": cur.get("league"),
                    "start_time": cur["start_time"], "market_type": cur["market_type"],
                    "period": cur["period"], "line": cur["line"],
                    "submarket": cur["submarket"], "team_side": cur["team_side"],
                    "pin_event_id": cur.get("pin_event_id"),
                },
                "points": [],
            }
            series_map[key] = entry
        entry["points"].append((now_iso, cur["fair_prob"], cur["odds"]))
        # Prune points outside the window.
        entry["points"] = [
            p for p in entry["points"]
            if datetime.fromisoformat(p[0]) >= hr_cutoff
        ]
    # Drop markets not seen this cycle whose last point has aged out.
    for k in list(series_map.keys()):
        pts = series_map[k]["points"]
        if not pts or datetime.fromisoformat(pts[-1][0]) < hr_cutoff:
            del series_map[k]


async def _crystalbet_loop_for_sport(cfg: SportConfig):
    """Poll CB for one sport. Writes into _state[sport]['cb']. Logs unmatched
    diagnostics once per cycle when Pin data is available."""
    sport = cfg.sport_name
    while True:
        try:
            t0 = time.monotonic()
            if CB_USE_SAVED:
                if not cfg.cb_sample_path.exists():
                    raise FileNotFoundError(
                        f"CB_USE_SAVED=1 but {cfg.cb_sample_path} missing"
                    )
                html = cfg.cb_sample_path.read_text(encoding="utf-8")
                odds = cfg.cb_parse_html(html, datetime.now(tz=timezone.utc))
                src_label = "saved HTML"
            else:
                odds = await cfg.cb_fetcher(headed=not CB_HEADLESS)
                src_label = "live"
            dt = time.monotonic() - t0
            async with _state_lock:
                _state[sport]["cb"]["odds"] = odds
                _state[sport]["cb"]["fetched_at"] = datetime.now(tz=timezone.utc)
                _state[sport]["cb"]["error"] = None
                _state[sport]["cb"]["count"] = len(odds)
            log.info("crystalbet %s (%s): %d Odds rows in %.1fs",
                     sport, src_label, len(odds), dt)

            # Change-only tick history (charts) — see src/ticks.py.
            try:
                await asyncio.to_thread(
                    ticks.get_store().record_cycle, "cb", sport,
                    ticks.rows_from_odds(odds), dur_ms=int(dt * 1000),
                )
            except Exception as e:
                log.warning("tick store write failed (cb %s): %s", sport, e)

            # Per-cycle unmatched diagnostic, only when Pin has data.
            pin_odds = _state[sport]["pin"]["odds"]
            if pin_odds:
                try:
                    result = match_with_diagnostics(odds, pin_odds)
                    log_unmatched(result)
                except Exception as e:
                    log.warning("unmatched_log write failed (%s): %s", sport, e)
        except Exception as e:
            log.exception("crystalbet %s fetch failed", sport)
            async with _state_lock:
                _state[sport]["cb"]["error"] = str(e)[:200]
            try:
                await asyncio.to_thread(
                    ticks.get_store().record_cycle, "cb", sport, [],
                    ok=False, error=str(e)[:200],
                )
            except Exception:
                pass
        await asyncio.sleep(CRYSTALBET_POLL_SEC)


# ── Anomaly scanner (Phase 6) ─────────────────────────────────────────────────
def _anomaly_base_row(a) -> dict:
    """CB-only fields for one ladder anomaly. Pin attachment added at request time.

    The 'inflated' rung — the side+line carrying the suspiciously high price you
    would actually bet — depends on the violation direction:
      expected 'down' (odds should fall as the line rises) → high price at line_hi
      expected 'up'   (odds should rise)                    → high price at line_lo
    """
    if a.expected == "down":
        bet_line, bet_odds = a.line_hi, a.odds_hi
    else:
        bet_line, bet_odds = a.line_lo, a.odds_lo
    return {
        "sport": "basketball",
        "league": a.league,
        "match_label": f"{a.home} — {a.away}",
        "home": a.home, "away": a.away,
        "cb_event_id": a.event_id,
        "start_time": a.start_time.isoformat() if a.start_time else None,
        "period": a.period,
        "market_type": a.market_type,
        "submarket": a.submarket,
        "section": a.section,
        # Prefer the real CB section title ("Asian Handicap 1st Period") for the
        # human label; fall back to the canonical "spread H1" form.
        "market": a.section or (f"{a.market_type} {a.period}"
                                + (f" ({a.submarket})" if a.submarket else "")),
        "side": a.side,
        "line_lo": a.line_lo, "line_hi": a.line_hi,
        "odds_lo": a.odds_lo, "odds_hi": a.odds_hi,
        "expected": a.expected,
        "pct": round(a.pct, 2),
        "delta": round(a.delta, 3),
        "bet_line": bet_line,
        "bet_odds": bet_odds,
    }


async def _compute_anomalies() -> bool:
    """Hourly: scrape CB basketball in FULL detail mode, run the ladder
    detector, store the result. Pin attachment happens later (request time) so
    fair prices + moves stay live between hourly scans. Returns True if the CB
    scrape produced odds (lets the loop retry sooner on a cold start)."""
    global _recent_anomalies, _anomaly_cb_odds
    global _recent_consistency, _anomaly_coverage, _anomaly_watchlist
    global _anomalies_computed_at, _anomalies_cb_fetched_at, _anomalies_error
    try:
        if CB_USE_SAVED:
            from src.scrapers.crystalbet import dry_run_parse_saved
            cb_odds = dry_run_parse_saved()
        else:
            # Permissive full-detail scrape: captures every 2-way handicap/total
            # ladder (incl. "Asian Handicap 1st Period" the strict classifier drops).
            cb_odds = await fetch_crystalbet_basketball_anomaly_ladders(
                headed=not CB_HEADLESS,
            )
    except Exception as e:
        log.exception("anomaly scan: CB full-detail scrape failed")
        async with _state_lock:
            _anomalies_error = str(e)[:200]
        return False

    anoms = find_ladder_anomalies(cb_odds, markets=ANOMALY_MARKETS, min_pct=0.0)
    rows = [_anomaly_base_row(a) for a in anoms]
    coverage = _coverage_stats(cb_odds)
    watchlist = {r["cb_event_id"] for r in rows if r["cb_event_id"]}
    ts = datetime.now(tz=timezone.utc)
    flags = _merge_flag_first_seen(
        [_consistency_to_dict(f) for f in find_consistency_flags(cb_odds)],
        _recent_consistency, ts.isoformat(),
    )
    async with _state_lock:
        _anomaly_cb_odds = cb_odds
        _recent_anomalies = rows
        _recent_consistency = flags
        _anomaly_coverage = coverage
        _anomaly_watchlist = watchlist
        _anomalies_computed_at = ts
        _anomalies_cb_fetched_at = ts
        _anomalies_error = None
    # Append a point-in-time snapshot to the history logs. Pin is attached at
    # scan time so the history can later answer "did any flagged rung beat
    # Pinnacle". Best-effort — never let logging break the scan.
    try:
        bball = _state.get("basketball")
        pin_odds = bball["pin"]["odds"] if bball else []
        enriched = _attach_pin_to_anomalies(
            rows, cb_odds, pin_odds, _recent_moves.get("basketball", []))
        _append_scan_history(ts.isoformat(), enriched, flags)
        _append_csv(
            _SCANS_HISTORY,
            ["scan_ts", "odds", "games", "ladders", "rungs", "moneylines",
             "games_without_ladder", "anomalies", "consistency_flags"],
            [[ts.isoformat(), coverage["odds"], coverage["games"], coverage["ladders"],
              coverage["rungs"], coverage["moneylines"], coverage["games_without_ladder"],
              len(rows), len(flags)]],
        )
    except Exception:
        log.exception("anomaly scan history append failed")
    log.info("anomaly scan complete: %d CB odds across %d games, %d ladders, "
             "%d rungs, %d ML; %d games without ladder; %d anomalies, %d consistency flags",
             coverage["odds"], coverage["games"], coverage["ladders"], coverage["rungs"],
             coverage["moneylines"], coverage["games_without_ladder"], len(rows), len(flags))
    return bool(cb_odds)


def _consistency_to_dict(f) -> dict:
    return {
        "sport": f.sport, "league": f.league,
        "match_label": f.match_label, "home": f.home, "away": f.away,
        "cb_event_id": f.event_id,
        "start_time": f.start_time.isoformat() if f.start_time else None,
        "kind": f.kind, "periods": f.periods, "detail": f.detail,
        "severity": f.severity, "outcome": f.outcome,
    }


def _merge_flag_first_seen(
    new_flags: list[dict], prev_flags: list[dict], scan_ts_iso: str,
) -> list[dict]:
    """Carry first_seen across scans so a flag shows how long the finding has
    been live, not when the latest scan ran. Identity = (kind, event, periods,
    outcome) — `detail` carries live odds and would reset on every reprice."""
    prev = {
        f.get("flag_key"): f.get("first_seen")
        for f in prev_flags if f.get("flag_key")
    }
    for f in new_flags:
        f["flag_key"] = "|".join([
            str(f.get("kind")), str(f.get("cb_event_id")),
            str(f.get("periods")), str(f.get("outcome") or ""),
        ])
        f["first_seen"] = prev.get(f["flag_key"]) or scan_ts_iso
    return new_flags


# Append-only scan history — one row per anomaly / flag PER scan (scan_ts column),
# so watching the hourly scans builds a real dataset rather than a screen you
# happen to glance at. Lives outside the in-memory state so it survives restarts.
_HISTORY_DIR = Path(__file__).resolve().parent.parent / "output" / "history"
_ANOM_HISTORY = _HISTORY_DIR / "anomalies_history.csv"
_CONS_HISTORY = _HISTORY_DIR / "consistency_history.csv"
_SCANS_HISTORY = _HISTORY_DIR / "scans_history.csv"


def _coverage_stats(cb_odds: list[Odds]) -> dict:
    """What the scan actually saw — so a '0 anomalies' result is auditable
    ('out of N games / M ladders / K rungs') rather than an ambiguous blank.
    games_without_ladder ≈ games that fell back to list-view (failed detail
    expansion) and therefore can't be checked for ladder anomalies."""
    from collections import defaultdict
    events: set = set()
    ladders: dict = defaultdict(int)
    rungs = 0
    moneylines = 0
    for o in cb_odds:
        events.add(o.raw_event_id)
        if o.market_type in ("spread", "total"):
            ladders[(o.raw_event_id, o.period, o.market_type, o.section)] += 1
            rungs += 1
        elif o.market_type == "moneyline":
            moneylines += 1
    n_ladders = sum(1 for c in ladders.values() if c >= 2)
    games_with_ladder = {k[0] for k, c in ladders.items() if c >= 2}
    return {
        "odds": len(cb_odds),
        "games": len(events),
        "ladders": n_ladders,
        "rungs": rungs,
        "moneylines": moneylines,
        "games_without_ladder": len(events - games_with_ladder),
    }


def _append_csv(path: Path, header: list[str], rows: list[list]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(header)
        w.writerows(rows)


def _append_scan_history(scan_ts: str, anomalies: list[dict], flags: list[dict]) -> None:
    """Append this scan's anomalies (Pin-attached) + consistency flags to the
    history CSVs. Best-effort: callers wrap in try/except so a disk hiccup never
    breaks the scan."""
    _append_csv(
        _ANOM_HISTORY,
        ["scan_ts", "league", "match", "start_time", "cb_event_id", "period",
         "market", "section", "side", "line_lo", "line_hi", "odds_lo", "odds_hi",
         "pct", "bet_line", "bet_odds", "pin_fair_odds", "pin_edge_pct"],
        [[scan_ts, a.get("league") or "", a["match_label"], a.get("start_time") or "",
          a.get("cb_event_id") or "", a["period"], a["market"], a.get("section") or "",
          a["side"], a["line_lo"], a["line_hi"], a["odds_lo"], a["odds_hi"], a["pct"],
          a["bet_line"], a["bet_odds"], a.get("pin_fair_odds"), a.get("pin_edge_pct")]
         for a in anomalies],
    )
    _append_csv(
        _CONS_HISTORY,
        ["scan_ts", "league", "match", "cb_event_id", "kind", "periods", "detail", "severity"],
        [[scan_ts, f.get("league") or "", f["match_label"], f.get("cb_event_id") or "",
          f["kind"], f["periods"], f["detail"], f["severity"]] for f in flags],
    )


def _seconds_until_minute(target_min: int, now: datetime | None = None) -> float:
    """Seconds from `now` (default: local wall clock) until the next time the
    clock hits minute target_min at :00 seconds. Always positive. `now` is
    injectable for testing; minute-of-hour is timezone-offset-agnostic for the
    whole-hour offsets that matter here."""
    now = now or datetime.now()
    target = now.replace(minute=target_min % 60, second=0, microsecond=0)
    if target <= now:
        target += timedelta(hours=1)
    return (target - now).total_seconds()


def _seconds_until_minutes(minutes: list[int], now: datetime | None = None) -> float:
    """Seconds until the SOONEST upcoming minute in `minutes` (e.g. [15, 45] →
    every 30 min). Always positive."""
    now = now or datetime.now()
    return min(_seconds_until_minute(m, now) for m in minutes)


async def _anomaly_loop():
    """Run the anomaly scan, clock-aligned to ANOMALY_SCAN_AT_MINUTES (e.g.
    [15, 45] = every 30 min). Falls back to a fixed ANOMALY_SCAN_SEC interval
    when alignment is disabled. Does one immediate scan on boot so the tab isn't
    blank until the first aligned slot."""
    # Boot scan ASAP (after a short delay so the per-sport pollers init first),
    # retrying until the first success — otherwise a cold start could leave the
    # tab blank for up to an hour.
    await asyncio.sleep(15)
    ok = await _compute_anomalies()
    while not ok:
        await asyncio.sleep(120)
        ok = await _compute_anomalies()

    while True:
        if ANOMALY_SCAN_AT_MINUTES is not None:
            wait = _seconds_until_minutes(ANOMALY_SCAN_AT_MINUTES)
            log.info("anomaly scan: next run in %.0fs (aligned to minutes %s)",
                     wait, ANOMALY_SCAN_AT_MINUTES)
            await asyncio.sleep(wait)
            ok = await _compute_anomalies()
            if not ok:
                # transient fail at the aligned slot — one quick retry instead
                # of waiting a full hour.
                await asyncio.sleep(120)
                await _compute_anomalies()
        else:
            await asyncio.sleep(ANOMALY_SCAN_SEC)
            await _compute_anomalies()


def _merge_watch_rescan(
    event_ids: set[str], fresh_odds: list[Odds],
    cur_cb: list[Odds], cur_anoms: list[dict], cur_flags: list[dict],
    cur_watch: set[str],
) -> tuple[list[Odds], list[dict], list[dict], set[str]]:
    """Replace the watched events' slice of state with freshly-scanned data.
    Pure (no globals) so it's unit-testable. A watched game that re-scans clean
    drops off the watchlist; one still anomalous stays; all OTHER events are
    left exactly as they were."""
    kept_cb = [o for o in cur_cb if o.raw_event_id not in event_ids]
    new_cb = kept_cb + fresh_odds

    fresh_anoms = [_anomaly_base_row(a) for a in
                   find_ladder_anomalies(fresh_odds, markets=ANOMALY_MARKETS, min_pct=0.0)]
    new_anoms = [r for r in cur_anoms if r["cb_event_id"] not in event_ids] + fresh_anoms

    fresh_flags = [_consistency_to_dict(f) for f in find_consistency_flags(fresh_odds)]
    new_flags = [r for r in cur_flags if r["cb_event_id"] not in event_ids] + fresh_flags

    still_anomalous = {r["cb_event_id"] for r in fresh_anoms if r["cb_event_id"]}
    new_watch = (cur_watch - event_ids) | still_anomalous
    return new_cb, new_anoms, new_flags, new_watch


async def _anomaly_watch_loop():
    """Every ANOMALY_WATCH_SEC, re-scrape ONLY the games that already have an
    anomaly and merge fresh results in — so flagged games refresh fast without
    re-expanding the whole board. Silent (no chime); the 30-min full scan owns
    the chime + history. Does nothing while the watchlist is empty."""
    global _anomaly_cb_odds, _recent_anomalies, _recent_consistency
    global _anomaly_watchlist, _anomaly_watch_at
    while True:
        await asyncio.sleep(ANOMALY_WATCH_SEC)
        watch = set(_anomaly_watchlist)
        if not watch:
            continue
        try:
            if CB_USE_SAVED:
                continue  # no per-game re-scrape in saved-HTML dev mode
            fresh = await fetch_crystalbet_basketball_games(watch, headed=not CB_HEADLESS)
            new_cb, new_anoms, new_flags, new_watch = _merge_watch_rescan(
                watch, fresh, _anomaly_cb_odds, _recent_anomalies,
                _recent_consistency, _anomaly_watchlist)
            ts = datetime.now(tz=timezone.utc)
            async with _state_lock:
                _anomaly_cb_odds = new_cb
                _recent_anomalies = new_anoms
                _recent_consistency = new_flags
                _anomaly_watchlist = new_watch
                _anomaly_watch_at = ts
            log.info("anomaly watch re-scan: %d games → %d still anomalous; "
                     "%d total anomalies now", len(watch), len(new_watch), len(new_anoms))
        except Exception:
            log.exception("anomaly watch re-scan failed")


# ── Betlive favourite-flip loops ──────────────────────────────────────────────
async def _betlive_set_flags(anomalies) -> None:
    """Recompute flag dicts (carrying first_seen) and publish them."""
    from src import betlive_watch as bw
    global _betlive_consistency, _betlive_computed_at
    ts = datetime.now(tz=timezone.utc)
    flags = bw.flags_with_first_seen(
        [a for a in anomalies if a.gap_pp >= BETLIVE_MIN_GAP_PP],
        _betlive_consistency, ts.isoformat())
    async with _state_lock:
        _betlive_consistency = flags
        _betlive_computed_at = ts


async def _betlive_discover_loop():
    """Slow: full getPrematchEvent sweep — refreshes the watch map (new events /
    outcomeIds) and re-detects from full ladders. Heavy but infrequent; runs in
    a worker thread so it never blocks the event loop."""
    from src import betlive_watch as bw
    global _betlive_watch, _betlive_error, _betlive_energy
    await asyncio.sleep(20)   # let the cheap pollers init first
    while True:
        try:
            watch, anomalies, energy = await asyncio.to_thread(
                bw.discover, BETLIVE_ANOMALY_SPORT_IDS)
            async with _state_lock:
                _betlive_watch = watch
                _betlive_energy = energy
                _betlive_error = None
            await _betlive_set_flags(anomalies)
            log.info("betlive discover: watching %d events, %d flip(s) "
                     "(%.1f MB extended)", len(watch),
                     len(_betlive_consistency), energy.get("ext_bytes", 0) / 1e6)
        except Exception as e:
            log.exception("betlive discover failed")
            async with _state_lock:
                _betlive_error = str(e)[:200]
        await asyncio.sleep(BETLIVE_DISCOVER_SEC)


async def _betlive_watch_loop():
    """Fast: one refreshOdds POST re-prices every watched event's moneylines
    (~50 KB) and the OT-fold check is recomputed. This is what catches a flip
    within seconds, between the slow discover sweeps."""
    from src import betlive_watch as bw
    global _betlive_error
    sess = None
    while True:
        await asyncio.sleep(BETLIVE_WATCH_SEC)
        watch = dict(_betlive_watch)
        if not watch:
            continue
        try:
            if sess is None:
                sess = await asyncio.to_thread(bw.session)
            anomalies, _nbytes = await asyncio.to_thread(bw.refresh, sess, watch)
            await _betlive_set_flags(anomalies)
        except Exception as e:
            sess = None    # drop a possibly-dead session; rebuilt next tick
            async with _state_lock:
                _betlive_error = str(e)[:200]
            log.debug("betlive watch refresh failed: %s", e)


async def _soft_scan_loop():
    """Slow (SOFT_SCAN_SEC, default 60 min): filtered soccer HT/FT + basketball
    favourite disagreement across CB/Betlive/Lider-Bet → Anomalies-tab flags.
    Runs in a worker thread; carries first_seen across sweeps."""
    from src import soft_scan
    global _soft_scan_flags, _soft_scan_at, _soft_scan_error
    # A scanner that errors keeps its last-good flags for up to this long (so a
    # transient network/DNS blip doesn't blank the tab), but no longer — a book
    # that's genuinely down for hours shouldn't show stale lines forever.
    stale_max = max(3 * SOFT_SCAN_SEC, 3 * 3600)
    await asyncio.sleep(30)
    while True:
        try:
            flags, failed = await asyncio.to_thread(soft_scan.scan_all)
            ts = datetime.now(tz=timezone.utc)
            # Carry over previous flags for scanners that FAILED this sweep (a book
            # we couldn't reach) so a transient blip doesn't wipe them.
            flags = _carry_stale_flags(_soft_scan_flags, flags, failed, ts, stale_max)
            prev = {(f.get("book"), f.get("book_event_id"), f.get("kind"), f.get("outcome")):
                    f.get("first_seen") for f in _soft_scan_flags}
            for f in flags:
                key = (f.get("book"), f.get("book_event_id"), f.get("kind"), f.get("outcome"))
                f["first_seen"] = prev.get(key) or ts.isoformat()
            flags.sort(key=lambda f: f["severity"], reverse=True)
            async with _state_lock:
                _soft_scan_flags = flags
                _soft_scan_at = ts
                _soft_scan_error = (f"{len(failed)} scanner(s) unreachable; kept last-good"
                                    if failed else None)
            log.info("soft_scan: %d flags (%d fresh, %d failed scanner(s) carried)",
                     len(flags), len(flags) - sum(1 for f in flags if f.get("stale")),
                     len(failed))
        except Exception as e:
            log.exception("soft_scan sweep failed")
            async with _state_lock:
                _soft_scan_error = str(e)[:200]
        await asyncio.sleep(SOFT_SCAN_SEC)


def _carry_stale_flags(prev: list[dict], fresh: list[dict], failed: set,
                       now: datetime, stale_max: float) -> list[dict]:
    """Keep last-good flags for (book, sport) scanners that failed this sweep, so
    an unreachable book doesn't blank the tab. Carried flags are marked stale and
    dropped once the game kicks off or they've been stale too long. Scanners that
    SUCCEEDED (even with 0 flags) are not carried — that anomaly genuinely cleared."""
    if not failed:
        return fresh
    out = list(fresh)
    for pf in prev:
        if (pf.get("book"), pf.get("sport")) not in failed:
            continue
        sf = dict(pf)
        sf["stale"] = True
        sf["stale_since"] = pf.get("stale_since") or now.isoformat()
        if not _flag_expired(sf, now, stale_max):
            out.append(sf)
    return out


def _flag_expired(f: dict, now: datetime, stale_max: float) -> bool:
    """True if a carried-over stale flag should be dropped: its game has kicked
    off, or it's been stale longer than stale_max seconds."""
    st = f.get("start_time")
    if st:
        try:
            if datetime.fromisoformat(st) <= now:
                return True
        except (ValueError, TypeError):
            pass
    ss = f.get("stale_since")
    if ss:
        try:
            if (now - datetime.fromisoformat(ss)).total_seconds() > stale_max:
                return True
        except (ValueError, TypeError):
            pass
    return False


def _attach_pin_to_anomalies(
    rows: list[dict], cb_odds: list[Odds], pin_odds: list[Odds],
    recent_moves: list[dict],
) -> list[dict]:
    """Add live Pinnacle fair-at-line, +EV edge, and recent-move context to each
    anomaly row. Pin columns stay None where there's no Pinnacle match/line."""
    if not rows:
        return []
    pin_by_event: dict[str, list[Odds]] = {}
    pin_label_by_event: dict[str, str] = {}
    if cb_odds and pin_odds:
        for me in match_events(cb_odds, pin_odds):
            if not me.cb:
                continue
            eid = me.cb[0].raw_event_id
            pin_by_event[eid] = me.pin
            if me.pin:
                pin_label_by_event[eid] = f"{me.pin[0].home} — {me.pin[0].away}"
    moves_by_label: dict[str, list[dict]] = {}
    for m in recent_moves:
        moves_by_label.setdefault(m["match_label"], []).append(m)

    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["pin_fair_odds"] = None
        d["pin_edge_pct"] = None
        d["pin_move_pp"] = None
        d["pin_move_label"] = None
        pin_rows = pin_by_event.get(r["cb_event_id"])
        if pin_rows:
            sel = ({"home": 2.0, "away": 2.0} if r["market_type"] == "spread"
                   else {"over": 2.0, "under": 2.0})
            pin = None
            try:
                virtual = Odds(
                    source="crystalbet", sport="basketball",
                    home=r["home"], away=r["away"],
                    market_type=r["market_type"], period=r["period"],
                    selections=sel, fetched_at=datetime.now(tz=timezone.utc),
                    line=r["bet_line"], submarket=r["submarket"],
                )
                pin = _closest_pin(virtual, pin_rows)
            except Exception:
                pin = None
            if pin:
                fair = _maybe_devig(pin)
                if fair and r["side"] in fair and fair[r["side"]] > 0:
                    pin_fair_dec = fair[r["side"]]
                    d["pin_fair_odds"] = round(pin_fair_dec, 3)
                    prob = 1.0 / pin_fair_dec
                    d["pin_edge_pct"] = round((r["bet_odds"] * prob - 1.0) * 100.0, 2)
            mv = moves_by_label.get(pin_label_by_event.get(r["cb_event_id"], ""), [])
            if mv:
                top = max(mv, key=lambda m: m["delta_pp"])
                d["pin_move_pp"] = top["delta_pp"]
                d["pin_move_label"] = f'{top["market"]} {top["side"]}'
        out.append(d)
    return out


# ── FastAPI app ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Init the bets SQLite DB once at boot. Idempotent — safe across restarts.
    bets.init_db()
    capital.ensure_seed_accounts()
    capital.ensure_dividend_account()

    # Restore caches from disk (per sport). First cycle warm-up if loaded.
    loaded = cache_persistence.load_all(SPORT_NAMES)
    for sport, ok in loaded.items():
        log.info(
            "CB cache %s for %s — first cycle will be %s",
            "restored from disk" if ok else "empty",
            sport,
            "warm" if ok else "a full cold expansion",
        )

    log.info(
        "starting background pollers — Pinnacle/%ds, CB/%ds, sports=%s, CB_USE_SAVED=%s",
        PINNACLE_POLL_SEC, CRYSTALBET_POLL_SEC, SPORT_NAMES, CB_USE_SAVED,
    )
    tasks: list[asyncio.Task] = []
    for cfg in SPORTS:
        tasks.append(asyncio.create_task(
            _pinnacle_loop_for_sport(cfg), name=f"pinnacle_loop_{cfg.sport_name}",
        ))
        tasks.append(asyncio.create_task(
            _crystalbet_loop_for_sport(cfg), name=f"crystalbet_loop_{cfg.sport_name}",
        ))
        for book in EXTRA_BOOKS:
            tasks.append(asyncio.create_task(
                _extra_book_loop_for_sport(book, cfg.sport_name),
                name=f"{book}_loop_{cfg.sport_name}",
            ))
    if EXTRA_BOOKS:
        log.info("extra books ENABLED: %s (poll %ds)", ", ".join(EXTRA_BOOKS),
                 EXTRA_BOOK_POLL_SEC)
    if ANOMALY_SCAN:
        tasks.append(asyncio.create_task(_anomaly_loop(), name="anomaly_loop"))
        log.info(
            "anomaly scanner ENABLED — full-detail basketball scan every %ds",
            ANOMALY_SCAN_SEC,
        )
        if ANOMALY_WATCH_SEC > 0:
            tasks.append(asyncio.create_task(_anomaly_watch_loop(), name="anomaly_watch_loop"))
            log.info("anomaly watch loop ENABLED — flagged games re-scanned every %ds",
                     ANOMALY_WATCH_SEC)
    if BETLIVE_ANOMALY:
        tasks.append(asyncio.create_task(_betlive_discover_loop(), name="betlive_discover_loop"))
        tasks.append(asyncio.create_task(_betlive_watch_loop(), name="betlive_watch_loop"))
        log.info("betlive favourite-flip watch ENABLED — discover/%ds, refreshOdds/%ds, sports=%s",
                 BETLIVE_DISCOVER_SEC, BETLIVE_WATCH_SEC, BETLIVE_ANOMALY_SPORT_IDS)
    if SOFT_SCAN:
        tasks.append(asyncio.create_task(_soft_scan_loop(), name="soft_scan_loop"))
        log.info("soft-book HT/FT + basketball-favourite sweep ENABLED — every %ds "
                 "(CB + Betlive + Lider-Bet)", SOFT_SCAN_SEC)
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Tear down the CB singleton browser cleanly (all sport pages + browser).
        try:
            await close_crystalbet()
        except Exception as e:
            log.warning("CB browser close on shutdown raised: %s", e)
        # Persist each sport's cache. Best-effort — save_all never raises.
        cache_persistence.save_all(SPORT_NAMES)
        log.info("background pollers stopped")


app = FastAPI(title="Prematch +EV/ARB scanner", lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/matches.html", status_code=302)


# ── API endpoints ─────────────────────────────────────────────────────────────
@app.get("/api/status")
async def api_status() -> dict:
    """Diagnostic snapshot — per-sport counts, last-update timestamps, errors."""
    sports_status: dict[str, dict] = {}
    for sport in SPORT_NAMES:
        cb = _state[sport]["cb"]
        pin = _state[sport]["pin"]
        sports_status[sport] = {
            "cb": {
                "count":      cb["count"],
                "fetched_at": cb["fetched_at"].isoformat() if cb["fetched_at"] else None,
                "age_sec":    _age_sec(cb["fetched_at"]),
                "error":      cb["error"],
            },
            "pin": {
                "count":      pin["count"],
                "fetched_at": pin["fetched_at"].isoformat() if pin["fetched_at"] else None,
                "age_sec":    _age_sec(pin["fetched_at"]),
                "error":      pin["error"],
            },
        }
    return {
        "sports": sports_status,
        "config": {
            "pin_poll_sec":  PINNACLE_POLL_SEC,
            "cb_poll_sec":   CRYSTALBET_POLL_SEC,
            "cb_use_saved":  CB_USE_SAVED,
            "sport_names":   list(SPORT_NAMES),
        },
    }


@app.get("/api/matches")
async def api_matches() -> list[dict]:
    """One row per CB event across all sports. Pin columns null where unmatched.

    Soccer rows include `cb_draw`, `pin_draw`, `pin_draw_fair`, `edge_draw_pct`
    when the matched Pin moneyline is 3-way (presence of "draw" key).
    Basketball rows have those fields null.
    """
    all_rows: list[dict] = []
    # Per-sport detail-status / last-expanded maps. Each maintains its own
    # change_cache namespaced by sport (cb_detail caching).
    detail_status_per_sport = {s: get_detail_status_map(s) for s in SPORT_NAMES}
    last_expanded_per_sport = {s: get_last_expanded_map(s) for s in SPORT_NAMES}

    for sport in SPORT_NAMES:
        cb_odds = _state[sport]["cb"]["odds"]
        if not cb_odds:
            continue
        pin_odds = _state[sport]["pin"]["odds"]
        matched = match_events(cb_odds, pin_odds) if pin_odds else []
        rows = _build_matches_view(
            cb_odds, matched,
            detail_status=detail_status_per_sport[sport],
            last_expanded=last_expanded_per_sport[sport],
        )
        all_rows.extend(rows)

    # Sort across sports by start_time asc; missing start_time → end.
    all_rows.sort(key=lambda r: (r["start_time"] is None, r["start_time"] or ""))
    return all_rows


@app.get("/api/moves")
async def api_moves(
    min_move: float = Query(2.0, ge=0.0, le=100.0,
                            description="Minimum fair-prob shift (percentage points)"),
) -> list[dict]:
    """Pinnacle line moves from the last ~5 minutes (Phase 5.3 rolling window),
    across all sports. Each row is a side whose no-vig fair probability rose —
    the side sharp money came in on. Sorted newest-first, then by magnitude
    within the same cycle, so the feed reads as 'what just moved'."""
    all_moves: list[dict] = []
    for sport in SPORT_NAMES:
        for m in _recent_moves.get(sport, []):
            if m["delta_pp"] >= min_move:
                all_moves.append(m)
    # Newest first; within the same recorded_at (one cycle) biggest mover first.
    all_moves.sort(key=lambda m: (m["recorded_at"], m["delta_pp"]), reverse=True)
    return all_moves


@app.get("/api/moves/cumulative")
async def api_moves_cumulative(
    min_move: float = Query(2.0, ge=0.0, le=100.0,
                            description="Minimum |net fair-prob shift| (pp) over the 1h window"),
) -> list[dict]:
    """Net moneyline drift over the last hour, per market, across all sports.
    Sorted by absolute net shift desc. Moneyline only (Phase 5.6)."""
    all_moves: list[dict] = []
    for sport in SPORT_NAMES:
        all_moves.extend(_compute_cumulative_moves(sport, min_move))
    all_moves.sort(key=lambda m: -abs(m["net_pp"]))
    return all_moves


@app.get("/api/anomalies")
async def api_anomalies(
    min_pct: float = Query(0.5, ge=0.0, le=1000.0,
                           description="Minimum wrong-direction move (% of smaller price)"),
    spread_only: bool = Query(False, description="Handicap ladders only; skip totals"),
    min_severity: float = Query(0.0, ge=0.0, le=1000.0,
                                description="Minimum severity for consistency flags"),
) -> dict:
    """CB-only ladder monotonicity anomalies from the latest hourly scan, with
    live Pinnacle fair-at-line + recent-move context attached per row. Sorted
    by % off, biggest first."""
    base = [
        r for r in _recent_anomalies
        if r["pct"] >= min_pct and (not spread_only or r["market_type"] == "spread")
    ]
    bball = _state.get("basketball")
    pin_odds = bball["pin"]["odds"] if bball else []
    recent_moves = _recent_moves.get("basketball", [])
    rows = _attach_pin_to_anomalies(base, _anomaly_cb_odds, pin_odds, recent_moves)
    rows.sort(key=lambda r: r["pct"], reverse=True)
    # CB-internal flags + betlive favourite-flip flags share the consistency
    # list; both carry kind/periods/detail/severity so the tab renders them the
    # same way (betlive rows tagged book="betlive").
    cons = [f for f in (_recent_consistency + _betlive_consistency + _soft_scan_flags)
            if f["severity"] >= min_severity]
    cons.sort(key=lambda f: f["severity"], reverse=True)
    return {
        "enabled": ANOMALY_SCAN or BETLIVE_ANOMALY or SOFT_SCAN,
        "computed_at": _anomalies_computed_at.isoformat() if _anomalies_computed_at else None,
        "cb_fetched_at": _anomalies_cb_fetched_at.isoformat() if _anomalies_cb_fetched_at else None,
        "scan_sec": ANOMALY_SCAN_SEC,
        "at_minutes": ANOMALY_SCAN_AT_MINUTES,
        "next_scan_in_sec": (
            round(_seconds_until_minutes(ANOMALY_SCAN_AT_MINUTES))
            if ANOMALY_SCAN_AT_MINUTES is not None else None
        ),
        "error": _anomalies_error,
        "coverage": _anomaly_coverage,
        "watch_count": len(_anomaly_watchlist),
        "watch_sec": ANOMALY_WATCH_SEC,
        "watch_at": _anomaly_watch_at.isoformat() if _anomaly_watch_at else None,
        "betlive": {
            "enabled": BETLIVE_ANOMALY,
            "watching": len(_betlive_watch),
            "flips": len(_betlive_consistency),
            "computed_at": _betlive_computed_at.isoformat() if _betlive_computed_at else None,
            "watch_sec": BETLIVE_WATCH_SEC,
            "discover_sec": BETLIVE_DISCOVER_SEC,
            "error": _betlive_error,
        },
        "count": len(rows),
        "anomalies": rows,
        "consistency": cons,
        "consistency_count": len(cons),
    }


@app.get("/api/anomalies/status")
async def api_anomalies_status() -> dict:
    """Cheap heartbeat for the cross-page scan-refresh alert — just the last
    scan's timestamp + counts, with NONE of the request-time Pinnacle matching
    that /api/anomalies does. Lets alerts.js poll every 30s on every page
    without paying the full re-match cost."""
    return {
        "enabled": ANOMALY_SCAN or BETLIVE_ANOMALY,
        "computed_at": _anomalies_computed_at.isoformat() if _anomalies_computed_at else None,
        "next_scan_in_sec": (
            round(_seconds_until_minutes(ANOMALY_SCAN_AT_MINUTES))
            if ANOMALY_SCAN_AT_MINUTES is not None else None
        ),
        "anomalies": len(_recent_anomalies),
        "consistency": len(_recent_consistency) + len(_betlive_consistency),
        "coverage": _anomaly_coverage,
    }


@app.get("/api/unmatched")
async def api_unmatched() -> list[dict]:
    """CB events across sports with no Pin match + their best below-threshold candidate."""
    rows: list[dict] = []
    for sport in SPORT_NAMES:
        cb_odds = _state[sport]["cb"]["odds"]
        pin_odds = _state[sport]["pin"]["odds"]
        if not cb_odds or not pin_odds:
            continue
        result = match_with_diagnostics(cb_odds, pin_odds)
        for u in result.unmatched:
            d = _unmatched_to_dict(u)
            d["sport"] = sport
            rows.append(d)
    rows.sort(key=lambda r: -r["best_score"])
    return rows


@app.get("/api/opportunities")
async def api_opportunities(
    min_edge: float = Query(1.0, ge=-100.0, le=100.0,
                            description="Minimum edge% to include"),
    kind: str | None = Query(None, pattern="^(\\+EV|ARB)$",
                             description="Filter to one kind; default both"),
    book: str | None = Query(None,
                             description="Filter to one soft book (cb/liderbet/"
                                         "betlive); default all enabled books"),
) -> list[dict]:
    """+EV + ARB opportunities (each soft book vs Pinnacle) across all sports.

    Every row carries `book` (cb / liderbet / betlive). With only CB enabled
    this is byte-identical to the original CB-vs-Pin feed plus a constant
    book="cb". `?book=` filters to one book.
    """
    # isinstance guard: when this coroutine is called directly (tests, not via
    # HTTP) an unpassed `book` is the FastAPI Query() sentinel, not a str.
    books = list(SOFT_BOOKS)
    if isinstance(book, str) and book and book != "all":
        books = [book] if book in SOFT_BOOKS else []
    all_opps: list[dict] = []
    for sport in SPORT_NAMES:
        pin_odds = _state[sport]["pin"]["odds"]
        if not pin_odds:
            continue
        for bk in books:
            bk_odds = _state[sport].get(bk, {}).get("odds")
            if not bk_odds:
                continue
            matched = match_events(bk_odds, pin_odds)
            opps = compute_opportunities(matched, min_edge_pct=min_edge)
            for o in opps:
                if kind is not None and o.kind != kind:
                    continue
                d = _opp_to_dict(o)
                d["sport"] = sport
                d["book"] = bk
                all_opps.append(d)
    # Re-sort across sports/books by edge% desc (compute_opportunities sorts
    # within each matched set).
    all_opps.sort(key=lambda d: -d["edge_pct"])
    return all_opps


# ── Cross-book arbitrage (local books, SportRadar-id join) ────────────────────
# Unlike the vs-Pinnacle edges above, these are arbs you can LOCK by placing
# both legs at Georgian books. Books are joined EXACTLY on the SportRadar match
# id (Lider-Bet's sr:match:N == Betlive's providerEventId — verified identical),
# so no name fuzzing and no Cyrillic problem. For each market (same type/period/
# line) we take the best price per side across books; if the inverse-odds sum
# < 1 the gap is a guaranteed profit. At least two distinct books must supply
# the legs (otherwise it's a single-book mispricing, not a placeable arb).
_SIDE_SETS = {2: 2, 3: 3}  # n distinct sides required by market width


def _cross_book_arbs_for_sport(sport: str, min_edge_pct: float) -> list[dict]:
    from collections import defaultdict

    # side -> best {book, odds, ...} per market group
    groups: dict[tuple, dict[str, dict]] = defaultdict(dict)
    for book in SOFT_BOOKS:
        for o in _state[sport].get(book, {}).get("odds") or []:
            if not o.sr_match_id:
                continue
            line_key = round(o.line, 2) if o.line is not None else None
            key = (o.sr_match_id, o.market_type, o.period, line_key,
                   o.submarket, o.team_side)
            best = groups[key]
            for side, dec in o.selections.items():
                if dec is None or dec <= 1.0:
                    continue
                cur = best.get(side)
                if cur is None or dec > cur["odds"]:
                    best[side] = {
                        "book": book, "odds": dec,
                        "home": o.home, "away": o.away, "start_time": o.start_time,
                        "league": o.league, "event_id": o.raw_event_id,
                    }

    out: list[dict] = []
    for (sr_id, market_type, period, line_key, submarket, team_side), sides in groups.items():
        need = 3 if (market_type == "moneyline" and "draw" in sides) else 2
        if len(sides) < need:
            continue
        if len({s["book"] for s in sides.values()}) < 2:   # must be placeable across books
            continue
        inv = sum(1.0 / s["odds"] for s in sides.values())
        edge_pct = (1.0 - inv) * 100.0
        if edge_pct < min_edge_pct:
            continue
        ref = next(iter(sides.values()))
        out.append({
            "sport": sport,
            "sr_match_id": sr_id,
            "match_label": f"{ref['home']} — {ref['away']}",
            "market_type": market_type,
            "period": period,
            "line": line_key,
            "submarket": submarket,
            "team_side": team_side,
            "edge_pct": round(edge_pct, 3),
            "inv_sum": round(inv, 5),
            "legs": [
                {"side": side, "book": s["book"], "odds": s["odds"],
                 "stake_pct": round((1.0 / s["odds"]) / inv * 100.0, 2)}
                for side, s in sides.items()
            ],
            "league": ref["league"],
            "start_time": ref["start_time"].isoformat() if ref["start_time"] else None,
        })
    return out


@app.get("/api/cross_arbs")
async def api_cross_arbs(
    min_edge: float = Query(0.0, ge=-100.0, le=100.0,
                            description="Minimum combined arb edge% to include"),
) -> list[dict]:
    """Cross-book arbs between the local books (Lider-Bet / Betlive / CB),
    joined on the SportRadar match id. Each row's `legs` give the side, book and
    stake split to lock the arb. Empty unless >=2 books are enabled and supply
    the same market. `stake_pct` per leg splits a unit stake for equal payout."""
    rows: list[dict] = []
    for sport in SPORT_NAMES:
        rows.extend(_cross_book_arbs_for_sport(sport, min_edge))
    rows.sort(key=lambda d: -d["edge_pct"])
    return rows


# ── Cross-book "best line" grid (SportRadar-id joined) ────────────────────────
# The Cross-book tab. Local books are joined EXACTLY on the SportRadar match id
# (Lider's sr:match:N == Betlive's providerEventId), so the comparison works for
# every fixture both books carry — independent of name spelling (Lider's
# Cyrillic/Georgian) and independent of whether Pinnacle even has the match.
# Only markets where >=2 books price a side are kept (a single-book "comparison"
# is noise). Pinnacle is attached as the fair reference where it matched the
# fixture (per-book name-match), giving the +EV column; markets Pinnacle lacks
# still show the cross-book price spread + arb. CrystalBet (no SR id) is folded
# in via the pin_event_id bridge: a CB opp's Pinnacle event maps to an SR id
# through the SR↔Pinnacle pairs the soft books already established.
_GRID_FLOOR = -1.0e9


def _lk(line: float | None) -> float | None:
    return round(line, 2) if line is not None else None


def _cross_book_for_sport(sport: str, min_edge: float, kind: str | None) -> list[dict]:
    pin_odds = _state[sport]["pin"]["odds"]

    # 1. Per-book opps vs Pinnacle (floor → every side). Build the Pinnacle fair
    #    lookup keyed by SR id, the SR↔pin bridge, and stash CB opps to fold in.
    fair: dict[tuple, float] = {}           # (sr, mtype, period, line, submkt, team, side) -> fair price
    sr_of_pin: dict[str, str] = {}          # pin_event_id -> sr_match_id
    cb_opps: list = []
    if pin_odds:
        for bk in SOFT_BOOKS:
            bk_odds = _state[sport].get(bk, {}).get("odds")
            if not bk_odds:
                continue
            for o in compute_opportunities(match_events(bk_odds, pin_odds), min_edge_pct=_GRID_FLOOR):
                if o.kind != "+EV":
                    continue
                if o.sr_match_id:
                    sr_of_pin[o.pin_event_id] = o.sr_match_id
                    fair[(o.sr_match_id, o.market_type, o.period, _lk(o.line),
                          o.submarket, o.team_side, o.side)] = o.pin_no_vig
                elif bk == "cb":
                    cb_opps.append(o)

    # 2. Cross-book market table from the SR-capable books' RAW odds (all shared
    #    fixtures, Pinnacle-matched or not). base key = the market identity.
    table: dict[tuple, dict[str, dict[str, float]]] = {}   # base -> side -> {book: odds}
    meta: dict[str, dict] = {}                              # sr -> display meta
    for bk in SOFT_BOOKS:
        for od in _state[sport].get(bk, {}).get("odds") or []:
            if not od.sr_match_id:
                continue
            base = (od.sr_match_id, od.market_type, od.period, _lk(od.line),
                    od.submarket, od.team_side)
            sides = table.setdefault(base, {})
            for side, dec in od.selections.items():
                if dec is None or dec <= 1.0:
                    continue
                bag = sides.setdefault(side, {})
                if dec > bag.get(bk, 0.0):
                    bag[bk] = dec
            # Prefer Betlive's (clean English) names for display; else Lider's.
            mslot = meta.get(od.sr_match_id)
            if mslot is None or bk == "betlive":
                meta[od.sr_match_id] = {"home": od.home, "away": od.away,
                                        "league": od.league, "sport": sport,
                                        "start_time": od.start_time.isoformat() if od.start_time else None}

    # 3. Fold CrystalBet in via the SR↔pin bridge (only onto existing markets).
    for o in cb_opps:
        sr = sr_of_pin.get(o.pin_event_id)
        if not sr:
            continue
        base = (sr, o.market_type, o.period, _lk(o.line), o.submarket, o.team_side)
        sides = table.get(base)
        if sides is None:
            continue
        bag = sides.setdefault(o.side, {})
        if o.cb_odds > bag.get("cb", 0.0):
            bag["cb"] = o.cb_odds

    # 4. Emit — only markets with a genuine cross-book comparison (>=2 books on
    #    some side). One row per side; best book + its +EV vs Pin fair + arb.
    out: list[dict] = []
    for base, sides in table.items():
        sr, mtype, period, line_k, submkt, team_side = base
        if not any(len(bag) >= 2 for bag in sides.values()):
            continue
        best = {}   # side -> (book, odds, edge|None)
        for side, bag in sides.items():
            bk, odds = max(bag.items(), key=lambda kv: kv[1])
            f = fair.get((sr, mtype, period, line_k, submkt, team_side, side))
            best[side] = (bk, odds, ((odds / f - 1.0) * 100.0) if f else None)
        need = 3 if (mtype == "moneyline" and "draw" in sides) else 2
        arb_pct = None
        if len(best) >= need:
            inv = sum(1.0 / v[1] for v in best.values())
            if inv < 1.0:
                arb_pct = round((1.0 - inv) * 100.0, 3)
        evs = [v[2] for v in best.values() if v[2] is not None]
        top_ev = max(evs) if evs else None
        if kind == "ev" and (top_ev is None or top_ev < min_edge):
            continue
        if kind == "arb" and (arb_pct is None or arb_pct < min_edge):
            continue
        if kind is None and not ((top_ev is not None and top_ev >= min_edge)
                                 or (arb_pct is not None and arb_pct >= min_edge)):
            continue
        m = meta.get(sr, {})
        label = _market_label_for(mtype, period, line_k)
        for side, (bk, odds, edge) in best.items():
            f = fair.get((sr, mtype, period, line_k, submkt, team_side, side))
            out.append({
                "sport": sport, "sr_match_id": sr,
                "match_label": f"{m.get('home','?')} — {m.get('away','?')}",
                "league": m.get("league"), "start_time": m.get("start_time"),
                "market": label, "market_type": mtype, "period": period,
                "line": line_k, "side": side,
                "pin_fair": round(f, 4) if f else None,
                "prices": {b: round(v, 3) for b, v in sides[side].items()},
                "edges": ({b: round((v / f - 1.0) * 100.0, 2) for b, v in sides[side].items()}
                          if f else {}),
                "best_book": bk, "best_odds": round(odds, 3),
                "best_edge_pct": round(edge, 2) if edge is not None else None,
                "arb_pct": arb_pct,
            })
    return out


def _market_label_for(market_type: str, period: str, line: float | None) -> str:
    base = {"moneyline": "Moneyline", "spread": "Spread",
            "total": "Total", "team_total": "Team total"}.get(market_type, market_type)
    parts = [base, period] if period and period != "FT" else [base]
    if line is not None:
        parts.append(f"{line:+g}" if market_type in ("spread",) else f"{line:g}")
    return " ".join(parts)


@app.get("/api/cross_book")
async def api_cross_book(
    min_edge: float = Query(0.0, ge=-100.0, le=100.0,
                            description="Keep markets whose best +EV OR arb edge% >= this"),
    kind: str | None = Query(None, pattern="^(ev|arb)$",
                             description="ev = +EV markets only; arb = lockable arbs only"),
) -> list[dict]:
    """Cross-book best-line grid, joined on the SportRadar match id.

    One row per (market, side): every book's price, the best book, its +EV vs
    Pinnacle's devigged fair (where Pinnacle has the match), and a market-level
    `arb_pct` when the best opposing prices lock a profit. Only markets where
    >=2 books price a side appear. Needs >=2 soft books enabled (LIDERBET=1 /
    BETLIVE=1, and/or CB).
    """
    rows: list[dict] = []
    for sport in SPORT_NAMES:
        rows.extend(_cross_book_for_sport(sport, min_edge, kind))
    rows.sort(key=lambda d: -max(
        d["best_edge_pct"] if d["best_edge_pct"] is not None else -1e9,
        d["arb_pct"] if d["arb_pct"] is not None else -1e9))
    return rows


def _compute_cumulative_moves(sport: str, min_move: float) -> list[dict]:
    """Net moneyline fair-prob drift over the 1h window, per market.

    For each market series with >= 2 points, find the side with the largest
    ABSOLUTE net shift (first point → last point). Signed: positive = the
    side's fair prob rose (odds shortened / money in); negative = it fell
    (odds drifted out). The user's example — a side going 1.5 → 1.7 → 1.9
    over the hour — surfaces here as that side with a negative net_pp and
    first_odds 1.5 → last_odds 1.9.
    """
    out: list[dict] = []
    for entry in _pin_ml_series.get(sport, {}).values():
        pts = entry["points"]
        if len(pts) < 2:
            continue
        first_ts, first_prob, first_odds = pts[0]
        last_ts, last_prob, last_odds = pts[-1]

        best_side = None
        best_abs = 0.0
        best_net = 0.0
        for side, lp in last_prob.items():
            fp = first_prob.get(side)
            if fp is None:
                continue
            net_pp = (lp - fp) * 100.0
            if abs(net_pp) > best_abs:
                best_abs = abs(net_pp)
                best_side = side
                best_net = net_pp
        if best_side is None or best_abs < min_move:
            continue

        lp = last_prob[best_side]
        fp = first_prob[best_side]
        meta = entry["meta"]
        out.append({
            "sport": sport,
            "match_label": f"{meta['home']} — {meta['away']}",
            "market": meta["label"],
            "league": meta.get("league"),
            "side": best_side,
            "first_odds": first_odds.get(best_side),
            "last_odds": last_odds.get(best_side),
            "fair_now_odds": round(1.0 / lp, 3) if lp > 0 else None,
            "first_prob_pct": round(fp * 100.0, 2),
            "last_prob_pct": round(lp * 100.0, 2),
            "net_pp": round(best_net, 2),
            "points": len(pts),
            "first_seen": first_ts,
            "last_seen": last_ts,
            "start_time": meta["start_time"],
            "pin_event_id": meta.get("pin_event_id"),
            # structured spec for the Log-bet prefill
            "market_type": meta["market_type"], "period": meta["period"],
            "line": meta["line"], "submarket": meta["submarket"],
            "team_side": meta["team_side"],
        })
    return out


# ── Bets API ──────────────────────────────────────────────────────────────────
# The bet tracker stores manual wagers in SQLite. For each "open" bet the
# dashboard surfaces the *current* CB + Pin fair so the user can see CLV /
# line movement since placement. We compute this by scanning _state for an
# Odds row matching the bet's (sport, cb_event_id|match_label, period,
# market_type, line, side, submarket, team_side) tuple and devigging the
# matching Pin counterpart.
def _find_cb_odds_for_bet(bet: dict) -> Odds | None:
    """Find the live CB Odds row that matches a bet's market spec.

    Match key: cb_event_id (if present) OR (home, away) inferred from
    match_label "Home vs Away", then narrow by period/market_type/line/
    submarket/team_side.
    """
    sport = bet.get("sport")
    if sport not in _state:
        return None
    cb_odds_list = _state[sport]["cb"]["odds"]
    if not cb_odds_list:
        return None

    eid = bet.get("cb_event_id")
    home, away = _parse_match_label(bet.get("match_label") or "")

    candidates = []
    for o in cb_odds_list:
        if eid and o.raw_event_id == eid:
            candidates.append(o)
            continue
        # Fallback for off-platform bets logged without an event id.
        if not eid and home and away and o.home == home and o.away == away:
            candidates.append(o)

    # Narrow by market spec.
    for o in candidates:
        if o.market_type != bet["market_type"]:
            continue
        if o.period != bet["period"]:
            continue
        if (o.submarket or None) != (bet.get("submarket") or None):
            continue
        if (o.team_side or None) != (bet.get("team_side") or None):
            continue
        # Lines must match exactly (we use the same tolerance as elsewhere).
        bet_line = bet.get("line")
        if bet_line is not None and o.line is not None:
            if abs(o.line - bet_line) > LINE_MATCH_TOLERANCE:
                continue
        elif (bet_line is None) != (o.line is None):
            continue
        return o
    return None


def _parse_match_label(label: str) -> tuple[str | None, str | None]:
    """Split 'Home vs Away' into (home, away). Forgiving on whitespace."""
    if " vs " in label:
        h, a = label.split(" vs ", 1)
        return h.strip(), a.strip()
    return None, None


def _current_odds_for_bet(bet: dict) -> tuple[float | None, float | None]:
    """Return (current_cb_decimal, current_pin_fair_decimal) for the bet's side.

    Used by /api/bets responses and by the history-recorder hook.
    """
    sport = bet.get("sport")
    if sport not in _state:
        return None, None

    cb = _find_cb_odds_for_bet(bet)
    cb_dec = cb.selections.get(bet["side"]) if cb else None

    # Find matching Pin row via _closest_pin against the bet's "virtual" CB row.
    pin_dec_fair = None
    pin_odds_list = _state[sport]["pin"]["odds"]
    if cb and pin_odds_list:
        pin = _closest_pin(cb, pin_odds_list)
        if pin is not None:
            fair = _maybe_devig(pin)
            if fair:
                pin_dec_fair = fair.get(bet["side"])
    return cb_dec, pin_dec_fair


def _bet_to_dict(bet: dict) -> dict:
    """Augment a stored bet row with computed-on-demand fields for the UI."""
    if bet.get("is_parlay"):
        # A parlay has no single market, so live odds / edge / CLV don't apply;
        # the UI renders the legs list + combined odds (header odds_taken) instead.
        out = dict(bet)
        out["legs"] = bets.list_legs(bet["id"])
        out["cb_odds_now"] = out["pin_fair_now"] = None
        out["edge_now_pct"] = out["clv_pct"] = None
        return out
    cb_now, pin_now = _current_odds_for_bet(bet)

    edge_now_pct = None
    if cb_now is not None and pin_now is not None and pin_now > 0:
        # edge = my_odds × fair_prob − 1 ; fair_prob = 1 / pin_fair_decimal
        edge_now_pct = (bet["odds_taken"] * (1 / pin_now) - 1) * 100

    # CLV proxy: did Pin's fair *for our side* shorten since placement?
    # Lower fair odds == higher implied prob == sharper says we're more likely
    # to win == positive CLV. Sign convention matches the "pin_fair_at_placement
    # → pin_fair_now" arrow.
    clv_pct = None
    pfp = bet.get("pin_fair_at_placement")
    if pfp and pin_now and pfp > 0:
        clv_pct = (pfp / pin_now - 1) * 100  # positive = our side shortened

    out = dict(bet)
    out["cb_odds_now"] = cb_now
    out["pin_fair_now"] = pin_now
    out["edge_now_pct"] = edge_now_pct
    out["clv_pct"] = clv_pct
    out["legs"] = []
    return out


@app.get("/api/bets")
async def api_list_bets(
    status: str | None = Query(None, description="open | settled | won | lost | pushed | void | all"),
) -> list[dict]:
    rows = bets.list_bets(status=status)
    return [_bet_to_dict(r) for r in rows]


@app.get("/api/bets/{bet_id}")
async def api_get_bet(bet_id: int) -> dict:
    b = bets.get_bet(bet_id)
    if not b:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"bet {bet_id} not found")
    return _bet_to_dict(b)


@app.get("/api/bets/{bet_id}/history")
async def api_bet_history(bet_id: int) -> list[dict]:
    """Odds snapshots for the sparkline. Oldest first."""
    if not bets.get_bet(bet_id):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"bet {bet_id} not found")
    return bets.get_history(bet_id)


@app.post("/api/bets")
async def api_create_bet(payload: dict) -> dict:
    """Create a bet. Required fields enforced by src.bets.create_bet.

    Auto-snapshots cb_fair_at_placement, pin_fair_at_placement, and
    edge_at_placement_pct from current _state at the time of the call.
    Caller can override by including those fields explicitly in payload.

    The UI sends only account_id (the merged Book/Account picker, 2026-06-12):
    `book` is derived from the account's book_tag, and `bankroll_at_time` is
    stamped from current capital equity instead of being typed by hand.
    """
    if payload.get("account_id") is not None and not payload.get("book"):
        acct = next(
            (a for a in capital.list_accounts()
             if a["id"] == payload["account_id"]), None,
        )
        payload["book"] = (acct or {}).get("book_tag") or "other"
    if payload.get("bankroll_at_time") is None:
        try:
            payload["bankroll_at_time"] = capital.capital_summary()["totals"]["equity"]
        except Exception as e:
            log.warning("equity lookup failed at bet creation: %s", e)
            payload["bankroll_at_time"] = 0.0
    # Compute current cb/pin if caller didn't provide them, using a synthetic
    # bet dict so we can reuse _current_odds_for_bet.
    if "pin_fair_at_placement" not in payload or "cb_fair_at_placement" not in payload:
        try:
            cb_now, pin_now = _current_odds_for_bet(payload)
        except Exception as e:
            log.warning("snapshot lookup failed at bet creation: %s", e)
            cb_now, pin_now = None, None
        if "cb_fair_at_placement" not in payload:
            payload["cb_fair_at_placement"] = cb_now
        if "pin_fair_at_placement" not in payload:
            payload["pin_fair_at_placement"] = pin_now
        if (payload.get("pin_fair_at_placement")
                and payload.get("odds_taken")
                and "edge_at_placement_pct" not in payload):
            payload["edge_at_placement_pct"] = (
                payload["odds_taken"] * (1 / payload["pin_fair_at_placement"]) - 1
            ) * 100
    try:
        bid = bets.create_bet(**payload)
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(e))
    b = bets.get_bet(bid)
    return _bet_to_dict(b)


@app.patch("/api/bets/{bet_id}")
async def api_update_bet(bet_id: int, payload: dict) -> dict:
    """Update a bet. To settle, send {'outcome': 'won'|'lost'|'pushed'|'void'}
    (and optionally 'payout'); other patches use the standard partial-update
    field set (note, cb_event_id, start_time)."""
    from fastapi import HTTPException
    if "outcome" in payload:
        outcome = payload["outcome"]
        payout = payload.get("payout")
        try:
            ok = bets.settle_bet(bet_id, outcome, payout=payout)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not ok:
            raise HTTPException(status_code=404, detail=f"bet {bet_id} not found")
    else:
        # Account moved → keep the derived book tag in sync (same rule as create).
        if payload.get("account_id") is not None and "book" not in payload:
            acct = next(
                (a for a in capital.list_accounts()
                 if a["id"] == payload["account_id"]), None,
            )
            if acct is not None:
                payload["book"] = acct.get("book_tag") or "other"
        try:
            ok = bets.update_bet(bet_id, **payload)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not ok:
            # Could be 404 OR "no fields actually changed". Disambiguate by reading.
            if not bets.get_bet(bet_id):
                raise HTTPException(status_code=404, detail=f"bet {bet_id} not found")
    b = bets.get_bet(bet_id)
    return _bet_to_dict(b)


@app.delete("/api/bets/{bet_id}")
async def api_delete_bet(bet_id: int) -> dict:
    if not bets.delete_bet(bet_id):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"bet {bet_id} not found")
    return {"deleted": bet_id}


@app.post("/api/bets/{bet_id}/legs")
async def api_add_leg(bet_id: int, payload: dict) -> dict:
    """Add a game (leg) to a bet, making it a parlay. Body = the leg's game
    fields (match_label, side, odds, + sport/period/market_type/line/...). Stake
    and account are inherited from the bet — no new money. Backs both the
    'add a game from edit' and 'roll a previous bet' UI flows."""
    from fastapi import HTTPException
    try:
        bets.add_leg(bet_id, **payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _bet_to_dict(bets.get_bet(bet_id))


@app.patch("/api/bets/{bet_id}/legs/{leg_index}")
async def api_settle_leg(bet_id: int, leg_index: int, payload: dict) -> dict:
    """Set one leg's result: {'outcome': won|lost|pushed|void|open}. The parlay
    status/odds/payout roll up automatically."""
    from fastapi import HTTPException
    try:
        ok = bets.settle_leg(bet_id, leg_index, payload.get("outcome"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail=f"leg {leg_index} of bet {bet_id} not found")
    return _bet_to_dict(bets.get_bet(bet_id))


@app.delete("/api/bets/{bet_id}/legs/{leg_index}")
async def api_remove_leg(bet_id: int, leg_index: int) -> dict:
    from fastapi import HTTPException
    if not bets.remove_leg(bet_id, leg_index):
        raise HTTPException(status_code=404, detail=f"leg {leg_index} of bet {bet_id} not found")
    return _bet_to_dict(bets.get_bet(bet_id))


# ── Odds history charts (src/ticks.py) ────────────────────────────────────────
@app.get("/api/chart")
async def api_chart(
    sport: str = Query(...),
    market_type: str = Query(...),
    period: str = Query("FT"),
    cb_src: str | None = Query(None, description="CB raw event id"),
    pin_src: str | None = Query(None, description="Pinnacle matchup id"),
    line: float | None = Query(None),
    team_side: str | None = Query(None),
    submarket: str | None = Query(None),
    hours: float = Query(24.0, gt=0, le=24 * 14),
) -> dict:
    """Tick series for one market on BOTH books — feeds the matches-page chart.
    Selections map to step lines; a NULL tick means the market vanished."""
    store = ticks.get_store()
    since = (datetime.now(tz=timezone.utc) - timedelta(hours=hours)) \
        .isoformat(timespec="seconds")
    mtype = f"{submarket}_{market_type}" if submarket else market_type
    out: dict = {"since": since, "cb": {}, "pin": {}}
    for book, src in (("cb", cb_src), ("pin", pin_src)):
        if not src:
            continue
        eid = store.event_id(book, sport, src)
        if eid is None:
            continue
        series = await asyncio.to_thread(
            store.series, eid, mtype, period, line, team_side, since,
        )
        if series:
            out[book] = series
    return out


# ── Capital / PnL tracker (src/capital.py) ────────────────────────────────────
@app.get("/api/capital")
async def api_capital(
    days: int | None = Query(None, ge=1, le=3650,
                             description="Performance lookback window (days); omit for all-time"),
) -> dict:
    """Per-account balances + totals + cumulative settled-PnL curve.
    `days` windows the performance stats (settled PnL / yield / ROI / curve);
    balances stay all-time."""
    since = None
    if days is not None:
        since = (datetime.now(tz=timezone.utc) - timedelta(days=days)) \
            .isoformat(timespec="seconds")
    return capital.capital_summary(since=since)


@app.post("/api/capital/accounts")
async def api_add_account(payload: dict) -> dict:
    from fastapi import HTTPException
    try:
        aid = capital.add_account(
            payload.get("name", ""), payload.get("book_tag") or None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="account name already exists")
    return {"id": aid}


@app.patch("/api/capital/accounts/{account_id}")
async def api_patch_account(account_id: int, payload: dict) -> dict:
    from fastapi import HTTPException
    try:
        if payload.get("unarchive"):
            ok = capital.unarchive_account(account_id)
        elif "balance" in payload:
            exp = (float(payload["open_exposure"])
                   if payload.get("open_exposure") not in (None, "") else None)
            capital.reconcile_account(account_id, float(payload["balance"]),
                                      exp, payload.get("note"))
            ok = True
        elif "open_exposure" in payload:
            ok = capital.set_open_exposure(account_id, float(payload["open_exposure"]))
        elif "book_tag" in payload:
            ok = capital.set_book_tag(account_id, payload.get("book_tag"))
        else:
            ok = capital.rename_account(account_id, payload.get("name", ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="account name already exists")
    if not ok:
        raise HTTPException(status_code=404, detail=f"account {account_id} not found")
    return {"ok": True}


@app.delete("/api/capital/accounts/{account_id}")
async def api_delete_account(account_id: int) -> dict:
    from fastapi import HTTPException
    try:
        result = capital.delete_account(account_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"result": result}


@app.post("/api/capital/ledger")
async def api_add_ledger(payload: dict) -> dict:
    """One money movement. Body: {account_id, kind, amount, note?, ts?} or a
    transfer: {transfer: true, from_id, to_id, amount, note?}."""
    from fastapi import HTTPException
    try:
        if payload.get("transfer"):
            out_id, in_id = capital.transfer(
                int(payload["from_id"]), int(payload["to_id"]),
                float(payload["amount"]), payload.get("note"),
            )
            return {"ids": [out_id, in_id]}
        eid = capital.add_entry(
            int(payload["account_id"]), payload.get("kind", ""),
            float(payload["amount"]), payload.get("note"), payload.get("ts"),
        )
        return {"id": eid}
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/capital/ledger")
async def api_list_ledger(account_id: int | None = Query(None)) -> list[dict]:
    return capital.list_entries(account_id)


@app.delete("/api/capital/ledger/{entry_id}")
async def api_delete_ledger(entry_id: int) -> dict:
    from fastapi import HTTPException
    if not capital.delete_entry(entry_id):
        raise HTTPException(status_code=404, detail=f"ledger entry {entry_id} not found")
    return {"deleted": entry_id}


@app.patch("/api/capital/ledger/{entry_id}")
async def api_update_ledger(entry_id: int, payload: dict) -> dict:
    """Edit a ledger entry's amount/note/ts in place."""
    from fastapi import HTTPException
    try:
        ok = capital.update_entry(
            entry_id,
            amount=payload.get("amount"),
            note=payload.get("note"),
            ts=payload.get("ts"),
        )
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail=f"ledger entry {entry_id} not found")
    return {"ok": True}


@app.get("/api/capital/export/{what}.csv")
async def api_capital_export(what: str):
    from fastapi import HTTPException
    from fastapi.responses import PlainTextResponse
    try:
        text = capital.export_csv(what)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return PlainTextResponse(
        text, media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{what}.csv"'},
    )


# ── View builders ─────────────────────────────────────────────────────────────
def _build_matches_view(
    cb_odds: list[Odds],
    matched: list[MatchedEvent],
    *,
    detail_status: dict[str, str] | None = None,
    last_expanded: dict[str, datetime] | None = None,
) -> list[dict]:
    """One dict per CB event, with Pin counterpart when matched.

    The detail_status / last_expanded maps are passed in (per-sport) rather
    than read from globals — keeps this fn pure-ish and lets api_matches
    iterate over multiple sports cleanly.
    """
    cb_by_event: dict[tuple[str, str], list[Odds]] = {}
    for o in cb_odds:
        cb_by_event.setdefault((o.home, o.away), []).append(o)

    matched_lookup: dict[tuple[str, str], MatchedEvent] = {
        (m.home, m.away): m for m in matched
    }

    rows: list[dict] = []
    for (home, away), cb_rows in cb_by_event.items():
        m = matched_lookup.get((home, away))
        row = _build_match_row(home, away, cb_rows, m,
                                detail_status, last_expanded)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda r: (r["start_time"] is None, r["start_time"] or ""))
    return rows


def _build_match_row(home: str, away: str, cb_rows: list[Odds],
                     match: MatchedEvent | None,
                     detail_status: dict[str, str] | None = None,
                     last_expanded: dict[str, datetime] | None = None,
                     ) -> dict | None:
    """Build one matches-table row.

    Schema (Phase 2):
      - cb_home / cb_away          — always present (None if no CB ML)
      - cb_draw                    — NEW: soccer 3-way only; None for basketball
      - pin_home / pin_away        — Pinnacle vigged prices
      - pin_draw                   — NEW: soccer 3-way only; None otherwise
      - pin_*_fair                 — devigged fair decimals (incl. pin_draw_fair)
      - edge_*_pct                 — per-side edge%

    For soccer matches with 3-way ML, the draw columns populate. Basketball
    rows always have draw fields None — front-end renders them as empty.

    test_app.py exercises this fn with basketball Odds (no draw). Signature
    unchanged from Phase 1 for back-compat.
    """
    cb_ml = next((o for o in cb_rows if o.market_type == "moneyline" and o.period == "FT"), None)
    if cb_ml is None:
        cb_ml = next((o for o in cb_rows if o.market_type == "moneyline"), None)
    anchor = cb_ml if cb_ml is not None else cb_rows[0]

    eid = anchor.raw_event_id or ""
    status = (detail_status or {}).get(eid, "list_only")
    last_exp = (last_expanded or {}).get(eid)
    last_exp_iso = last_exp.isoformat() if last_exp else None

    row: dict = {
        "cb_event_id": anchor.raw_event_id,
        # Pinnacle matchup id — the chart drawer needs both books' source ids.
        "pin_event_id": match.pin[0].raw_event_id if (match and match.pin) else None,
        "start_time": anchor.start_time.isoformat() if anchor.start_time else None,
        "sport": anchor.sport,
        "league": anchor.league,
        "home": home,
        "away": away,
        "has_pin": match is not None,
        "markets_status": status,
        "last_expanded_at": last_exp_iso,
        "cb_home": cb_ml.selections.get("home") if cb_ml else None,
        "cb_away": cb_ml.selections.get("away") if cb_ml else None,
        "cb_draw": cb_ml.selections.get("draw") if cb_ml else None,
        "pin_home": None,
        "pin_away": None,
        "pin_draw": None,
        "pin_home_fair": None,
        "pin_away_fair": None,
        "pin_draw_fair": None,
        "edge_home_pct": None,
        "edge_away_pct": None,
        "edge_draw_pct": None,
    }

    if match is not None and cb_ml is not None:
        # Match Pin ML by period AND submarket=None AND team_side=None
        # (parent-match ML, not corners/team_total). For soccer the matcher
        # join is sport-segregated upstream so submarket is naturally None
        # here, but include the filter for defensive consistency with
        # _closest_pin / _find_pin_match.
        pin_ml = next(
            (o for o in match.pin
             if o.market_type == "moneyline" and o.period == cb_ml.period
             and o.submarket is None and o.team_side is None),
            None,
        )
        if pin_ml is not None:
            ph = pin_ml.selections.get("home")
            pa = pin_ml.selections.get("away")
            pd = pin_ml.selections.get("draw")  # None for 2-way (basketball)
            row["pin_home"] = ph
            row["pin_away"] = pa
            row["pin_draw"] = pd

            # 3-way (soccer 1X2) → devig_3way; 2-way (basketball) → devig_2way.
            if (ph and pa and pd and ph > 1.0 and pa > 1.0 and pd > 1.0):
                try:
                    fair_h, fair_d, fair_a = devig_3way(ph, pd, pa)
                    fh_dec, fd_dec, fa_dec = 1 / fair_h, 1 / fair_d, 1 / fair_a
                    row["pin_home_fair"] = fh_dec
                    row["pin_draw_fair"] = fd_dec
                    row["pin_away_fair"] = fa_dec
                    if cb_ml.selections.get("home"):
                        row["edge_home_pct"] = (cb_ml.selections["home"] / fh_dec - 1) * 100
                    if cb_ml.selections.get("draw"):
                        row["edge_draw_pct"] = (cb_ml.selections["draw"] / fd_dec - 1) * 100
                    if cb_ml.selections.get("away"):
                        row["edge_away_pct"] = (cb_ml.selections["away"] / fa_dec - 1) * 100
                except (KeyError, ValueError):
                    pass
            elif ph and pa and ph > 1.0 and pa > 1.0:
                fair_h, fair_a = devig_2way(ph, pa)
                fh_dec, fa_dec = 1 / fair_h, 1 / fair_a
                row["pin_home_fair"] = fh_dec
                row["pin_away_fair"] = fa_dec
                row["edge_home_pct"] = (cb_ml.selections["home"] / fh_dec - 1) * 100
                row["edge_away_pct"] = (cb_ml.selections["away"] / fa_dec - 1) * 100

    row["markets"] = _build_markets_view(cb_rows, match.pin if match else [])
    return row


def _build_markets_view(cb_rows: list[Odds], pin_rows: list[Odds]) -> list[dict]:
    """For the [+] expander: every CB market alongside the closest Pin row.

    Includes Phase 2 fields on each entry: `submarket` and `team_side` so the
    frontend can label corners/team_total markets distinctly from parent ones.
    """
    out: list[dict] = []
    for cb in cb_rows:
        pin = _closest_pin(cb, pin_rows) if pin_rows else None
        entry: dict = {
            "market_type": cb.market_type,
            "period": cb.period,
            "line": cb.line,
            "submarket": cb.submarket,
            "team_side": cb.team_side,
            "cb": dict(cb.selections),
        }
        if pin is None:
            entry["pin"] = None
            entry["pin_fair"] = None
        else:
            entry["pin"] = dict(pin.selections)
            entry["pin_line"] = pin.line
            entry["pin_fair"] = _maybe_devig(pin)
        out.append(entry)
    return out


def _closest_pin(cb: Odds, pin_rows: list[Odds]) -> Odds | None:
    """Match by (market_type, period, submarket, team_side); for spread/total
    /team_total pick closest line within ±0.5.

    Phase 2 (soccer): submarket and team_side are part of the join key so
    corners markets and team_total halves don't accidentally match each
    other or basketball-style parent markets. Basketball Odds have both
    fields None → equality checks no-op.
    """
    cands = [
        p for p in pin_rows
        if p.market_type == cb.market_type
        and p.period == cb.period
        and p.submarket == cb.submarket
        and p.team_side == cb.team_side
    ]
    if cb.market_type == "moneyline":
        return cands[0] if cands else None
    if cb.line is None:
        return None
    # Phase 3.1.2: tightened tolerance from 0.5 to LINE_MATCH_TOLERANCE
    # (=0.01). 0.5 falsely paired distinct lines like +1.0 vs +1.5.
    cands = [p for p in cands
             if p.line is not None
             and abs(p.line - cb.line) <= LINE_MATCH_TOLERANCE]
    if not cands:
        return None
    cands.sort(key=lambda p: abs((p.line or 0.0) - cb.line))
    return cands[0]


def _maybe_devig(pin: Odds) -> dict | None:
    """Return fair-decimal dict mirroring pin.selections, or None on failure.

    Handles:
      - 2-way moneyline (basketball + soccer 2-way derivatives) → {home, away}
      - 3-way moneyline (soccer 1X2)                           → {home, draw, away}
      - spread                                                 → {home, away}
      - total / team_total                                     → {over, under}
    """
    s = pin.selections
    try:
        if pin.market_type == "moneyline":
            if "draw" in s:
                fh, fd, fa = devig_3way(s["home"], s["draw"], s["away"])
                return {"home": 1 / fh, "draw": 1 / fd, "away": 1 / fa}
            fh, fa = devig_2way(s["home"], s["away"])
            return {"home": 1 / fh, "away": 1 / fa}
        if pin.market_type == "spread":
            fh, fa = devig_2way(s["home"], s["away"])
            return {"home": 1 / fh, "away": 1 / fa}
        if pin.market_type in ("total", "team_total"):
            fo, fu = devig_2way(s["over"], s["under"])
            return {"over": 1 / fo, "under": 1 / fu}
    except (KeyError, ValueError):
        return None
    return None


def _unmatched_to_dict(u: UnmatchedEvent) -> dict:
    return {
        "cb_home": u.cb_home,
        "cb_away": u.cb_away,
        "cb_league": u.cb_league,
        "cb_start_time": u.cb_start_time.isoformat() if u.cb_start_time else None,
        "best_pin_home": u.best_pin_home,
        "best_pin_away": u.best_pin_away,
        "best_pin_league": u.best_pin_league,
        "best_score": u.best_score,
    }


def _opp_to_dict(o) -> dict:
    return {
        "start_time": o.start_time.isoformat() if o.start_time else None,
        "match_label": o.match_label,
        "market": o.market,
        "side": o.side,
        "cb_odds": o.cb_odds,
        "pin_no_vig": o.pin_no_vig,
        "edge_pct": o.edge_pct,
        "kind": o.kind,
        "kelly_stake": o.kelly_stake,
        "cb_event_id": o.cb_event_id,
        "arb_partner_side": o.arb_partner_side,
        "arb_partner_odds": o.arb_partner_odds,
        # Structured market spec — drives the "Log bet" prefill on arbs.html.
        "market_type": o.market_type,
        "period": o.period,
        "line": o.line,
        "submarket": o.submarket,
        "team_side": o.team_side,
        "league": o.league,
        "pin_max_stake": o.pin_max_stake,
        "pin_event_id": o.pin_event_id,
        # Match-quality chip (how much to trust the Pinnacle pairing).
        "confidence": match_confidence(o),
        "match_score": round(o.match_score, 1) if o.match_score is not None else None,
        "match_time_delta_min": (round(o.match_time_delta_sec / 60.0, 1)
                                 if o.match_time_delta_sec is not None else None),
    }


def _age_sec(ts: datetime | None) -> int | None:
    if ts is None:
        return None
    return int((datetime.now(tz=timezone.utc) - ts).total_seconds())


# ── Static mount (last; catches everything else) ──────────────────────────────
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
else:
    log.warning("static/ not present at %s — frontend won't serve", STATIC_DIR)
