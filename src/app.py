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
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Query
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from src import bets
from src.edge import LINE_MATCH_TOLERANCE, compute_opportunities
from src.matcher import (
    MatchedEvent, UnmatchedEvent, log_unmatched,
    match_events, match_with_diagnostics,
)
from src.models import Odds
from src.scrapers import cache_persistence
from src.scrapers.crystalbet import (
    SAMPLE_OUT as CB_SAMPLE_PATH,
    SAMPLE_OUT_SOCCER as CB_SAMPLE_PATH_SOCCER,
    SAMPLE_OUT_TENNIS as CB_SAMPLE_PATH_TENNIS,
    close_crystalbet,
    fetch_crystalbet_basketball_prematch,
    fetch_crystalbet_soccer_prematch,
    fetch_crystalbet_tennis_prematch,
    get_detail_status_map,
    get_last_expanded_map,
    parse_html as parse_cb_html,
    parse_html_soccer as parse_cb_html_soccer,
    parse_html_tennis as parse_cb_html_tennis,
)
from src.scrapers.pinnacle import (
    fetch_pinnacle_basketball,
    fetch_pinnacle_soccer,
    fetch_pinnacle_tennis,
)
from src.vig import devig_2way, devig_3way

log = logging.getLogger(__name__)

# ── Config (env-tunable) ──────────────────────────────────────────────────────
PINNACLE_POLL_SEC = int(os.environ.get("PINNACLE_POLL_SEC", "60"))
CRYSTALBET_POLL_SEC = int(os.environ.get("CRYSTALBET_POLL_SEC", "180"))
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


# ── Shared state (per-sport namespaced) ───────────────────────────────────────
def _empty_source_state() -> dict[str, Any]:
    return {"odds": [], "fetched_at": None, "error": None, "count": 0}


def _empty_sport_state() -> dict[str, Any]:
    return {"cb": _empty_source_state(), "pin": _empty_source_state()}


_state: dict[str, Any] = {sport: _empty_sport_state() for sport in SPORT_NAMES}
_state_lock = asyncio.Lock()


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

            # Snapshot every open bet for THIS sport. Other sports' pollers
            # do the same for their bets — each Pin cycle gives all-sport
            # bets a row, just staggered by sport poll timing.
            try:
                _snapshot_open_bets_for_sport(sport)
            except Exception as e:
                log.warning("bet history snapshot failed (%s): %s", sport, e)
        except Exception as e:
            log.exception("pinnacle %s fetch failed", sport)
            async with _state_lock:
                _state[sport]["pin"]["error"] = str(e)[:200]
        await asyncio.sleep(PINNACLE_POLL_SEC)


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
        await asyncio.sleep(CRYSTALBET_POLL_SEC)


# ── FastAPI app ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Init the bets SQLite DB once at boot. Idempotent — safe across restarts.
    bets.init_db()

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
) -> list[dict]:
    """+EV + ARB opportunities across all sports, sorted by edge% desc."""
    all_opps: list[dict] = []
    for sport in SPORT_NAMES:
        cb_odds = _state[sport]["cb"]["odds"]
        pin_odds = _state[sport]["pin"]["odds"]
        if not cb_odds or not pin_odds:
            continue
        matched = match_events(cb_odds, pin_odds)
        opps = compute_opportunities(matched, min_edge_pct=min_edge)
        for o in opps:
            if kind is not None and o.kind != kind:
                continue
            d = _opp_to_dict(o)
            d["sport"] = sport
            all_opps.append(d)
    # Re-sort across sports by edge% desc (compute_opportunities sorts within
    # each sport's matched set).
    all_opps.sort(key=lambda d: -d["edge_pct"])
    return all_opps


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
    """
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
