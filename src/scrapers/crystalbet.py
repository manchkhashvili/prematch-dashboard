"""
CrystalBet prematch scraper — multi-sport (basketball + soccer in Phase 2).

Navigation (verified against live site, do not reorder):
  1. goto Sports.aspx (domcontentloaded, 60 s) + sleep 6 s
  2. __doPostBack('ctl00$ctl00$ImageButtonEn','') inside expect_navigation
     (domcontentloaded) + sleep 4 s — full page reload to English
  3. DoSportTypePostBack(<sport_id>) — UpdatePanel AJAX, plain sleep 4 s, NO navigation wrapper
  4. DoChampionatPostBack("SelectAllChampionats:<sport_id>") + sleep 8 s

This module owns NAVIGATION (singleton browser + per-sport pages, postback
driving) and the list-view HTML walking. Sport-specific bits (loadinfo JSON
parser, market-name classifier for detail pages) live in
src/scrapers/sports/<sport>.py — sport modules are passed through to the
generic refresh/extract/expand pipeline below.

Multi-sport architecture (Phase 2 / C2.2.b, revised in Phase 2.5 #6):
  * One Chromium browser process shared across all sports.
  * **One BrowserContext PER SPORT.** Each context has independent cookies
    (incl. ASP.NET session cookie). Sharing one context across sports caused
    server-side session-sport collision — DoSportTypePostBack(17) on one
    page would overwrite DoSportTypePostBack(16) on another, and the
    subsequent SelectAllChampionats(N) would return empty because session
    sport ≠ requested sport. Per-sport contexts make each sport's session
    independent. Memory cost: ~50MB per extra context.
  * One persistent Page per sport (in its own context), kept alive across
    cycles and selected to that sport (DoSportTypePostBack runs once per
    page on initial setup).
  * Per-sport asyncio.Lock so basketball and soccer cycles run in parallel
    on independent pages.
  * Per-sport ChangeCache (via src.scrapers.change_cache.get_cache(sport))
    and per-sport detail-odds cache so basketball's prune_missing doesn't
    wipe soccer entries.
  * Periodic forced re-init (every _REINIT_EVERY_N_CYCLES per sport) +
    health-check + retry-once unchanged from Phase 1, just applied per sport.

Back-compat (Phase 1 callers untouched):
  * fetch_crystalbet_basketball_prematch(headed) — same signature,
    internally calls _fetch_for_sport(basketball, headed=headed).
  * parse_html(html, fetched_at) — basketball list-view parse, unchanged.
  * get_detail_status_map / get_last_expanded_map / get_detail_odds_cache /
    restore_detail_odds_cache — all default to sport_name="basketball" so
    pre-Phase-2 calls work without modification.
  * _parse_loadinfo / _parse_div_odds / _identify_loadinfo_roles / _make_odds
    — re-exported basketball aliases for the test imports.
  * _select_basketball, _load_all_leagues — preserved as basketball-default
    wrappers for the standalone scripts that import them.
  * close_crystalbet — same signature, now closes all sport pages + browser.

The list view has two formats in the wild for both sports:
  A. div.game_loading[data-loadinfo] — JSON array  (sport.parse_loadinfo)
  B. div.x_loop_res.Snatch col{N}    — positional column divs  (sport.parse_div_odds)

Both formats emit list-view fallback Odds for sport's primary period(s).
Markets with any side <= 1.0 are skipped (CB encodes suspended markets as 1.00).

Start times: CB displays Georgian local (UTC+4). We parse naive, then subtract
4 h and tag as UTC-aware before constructing Odds. Pinnacle's start_time is
already UTC-aware, so the matcher can compare both with no special-casing.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

TBILISI_UTC_OFFSET = timedelta(hours=4)  # CB shows Tbilisi local; subtract for UTC

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.models import Odds
from src.scrapers import cb_detail, cb_http, change_cache
from src.scrapers.sports import basketball, mma, soccer, tennis

# Re-export basketball list-view parsers so existing imports still resolve.
# Tests import these names from crystalbet directly (test_crystalbet_parser.py).
_parse_loadinfo = basketball.parse_loadinfo
_parse_div_odds = basketball.parse_div_odds
_identify_loadinfo_roles = basketball._identify_loadinfo_roles  # for tests
_make_odds = basketball._make_odds  # for tests

log = logging.getLogger(__name__)

SPORTS_URL = "https://www.crystalbet.com/Pages/Sports.aspx"
BASKETBALL_SPORT_ID = basketball.SPORT_ID  # 17
SOCCER_SPORT_ID = soccer.SPORT_ID          # 16
TENNIS_SPORT_ID = tennis.SPORT_ID          # 22
MMA_SPORT_ID = mma.SPORT_ID                # 69

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# CB_TRANSPORT env knob — which transport moves the bytes. Parsing is
# identical either way (same list/detail HTML fed to the same parsers).
#   "playwright" (default) — the original browser-driven path.
#   "http"                 — browser-free ASP.NET postbacks via cb_http
#                            (~0.3-0.5s/game detail vs ~3s, no Chromium).
# Verified live + parity-checked 2026-06-12 (scripts/cb_parity_check.py).
CB_TRANSPORT = os.environ.get("CB_TRANSPORT", "playwright").strip().lower()
if CB_TRANSPORT not in ("playwright", "http"):
    raise ValueError(
        f"CB_TRANSPORT={CB_TRANSPORT!r} — must be 'playwright' or 'http'"
    )
_USE_HTTP_TRANSPORT = CB_TRANSPORT == "http"
if _USE_HTTP_TRANSPORT:
    log.info("CB_TRANSPORT=http — browser-free transport (no Playwright)")

# The hourly anomaly scan sweeps the WHOLE basketball board in full detail, so it
# is the most transport-sensitive job. It defaults to the browser-free HTTP path
# even when the main app runs Playwright — ExpandDetail is ~355 KB / ~0.3 s per
# game over HTTP vs seconds of browser nav, and needs no Chromium. The cb_http
# sessions are independent of the Playwright pages, so the two transports coexist
# fine. Override with CB_ANOMALY_TRANSPORT=playwright to force the browser path.
CB_ANOMALY_TRANSPORT = os.environ.get("CB_ANOMALY_TRANSPORT", "http").strip().lower()
if CB_ANOMALY_TRANSPORT not in ("playwright", "http"):
    raise ValueError(
        f"CB_ANOMALY_TRANSPORT={CB_ANOMALY_TRANSPORT!r} — must be 'playwright' or 'http'"
    )
_ANOMALY_USE_HTTP = CB_ANOMALY_TRANSPORT == "http"
if _ANOMALY_USE_HTTP and not _USE_HTTP_TRANSPORT:
    log.info("CB_ANOMALY_TRANSPORT=http — anomaly scan runs browser-free "
             "(no Playwright) even though CB_TRANSPORT=playwright")

LEAGUE_SKIP = frozenset({"outright", "outrights"})

# Saved-HTML sample paths (one per sport — used by dry_run_parse_saved
# and the CB_USE_SAVED path in app.py).
SAMPLE_OUT = (
    Path(__file__).resolve().parents[2] / "data" / "raw" / "cb_prematch_sample.html"
)
SAMPLE_OUT_SOCCER = (
    Path(__file__).resolve().parents[2] / "data" / "raw" / "cb_prematch_sample_soccer.html"
)
SAMPLE_OUT_TENNIS = (
    Path(__file__).resolve().parents[2] / "data" / "raw" / "cb_prematch_sample_tennis.html"
)
SAMPLE_OUT_MMA = (
    Path(__file__).resolve().parents[2] / "data" / "raw" / "cb_prematch_sample_mma.html"
)

# Map sport_name → module + saved-HTML path for ergonomic lookup.
_SPORT_MODULES: dict[str, Any] = {
    basketball.SPORT_NAME: basketball,
    soccer.SPORT_NAME: soccer,
    tennis.SPORT_NAME: tennis,
    mma.SPORT_NAME: mma,
}
_SPORT_SAMPLE_PATHS: dict[str, Path] = {
    basketball.SPORT_NAME: SAMPLE_OUT,
    soccer.SPORT_NAME: SAMPLE_OUT_SOCCER,
    tennis.SPORT_NAME: SAMPLE_OUT_TENNIS,
    mma.SPORT_NAME: SAMPLE_OUT_MMA,
}


# ── Navigation primitives ─────────────────────────────────────────────────────

async def _init_browser(playwright, *, headed: bool):
    """Spawn a Chromium + context + page, navigate to Sports.aspx.

    Used by the singleton lifecycle below AND by dry_run_capture /
    scripts/capture_single_match_detail.py for one-off captures.
    """
    browser = await playwright.chromium.launch(
        headless=not headed,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = await browser.new_context(
        user_agent=USER_AGENT,
        locale="ka-GE",
        timezone_id="Asia/Tbilisi",
    )
    page = await context.new_page()
    log.info("goto %s", SPORTS_URL)
    await page.goto(SPORTS_URL, wait_until="domcontentloaded", timeout=60_000)
    await asyncio.sleep(6)
    return browser, context, page


async def _flip_to_english(page) -> None:
    """Click the English flag — full ASP.NET page reload.

    Phase 2.5 fix: wait for CB's `__doPostBack` JS binding before calling
    it. The `wait_until="domcontentloaded"` in `page.goto` only waits for
    the DOM tree, not for the script that defines `__doPostBack`. Without
    this guard a too-fast cold-start path can race the JS load and we'd
    hit `ReferenceError: __doPostBack is not defined`.
    """
    try:
        await page.wait_for_function(
            "typeof __doPostBack === 'function'", timeout=30_000,
        )
    except Exception as e:
        raise RuntimeError(
            f"CB __doPostBack didn't bind within 30s of page load "
            f"({type(e).__name__}); treating as transient"
        ) from e
    log.info("__doPostBack ImageButtonEn — full page reload")
    async with page.expect_navigation(wait_until="domcontentloaded"):
        await page.evaluate("__doPostBack('ctl00$ctl00$ImageButtonEn','')")
    await asyncio.sleep(4)


async def _select_sport(page, sport_id: int) -> None:
    """Run DoSportTypePostBack for the given sport_id (UpdatePanel AJAX, plain sleep).

    Phase 2.5 fix: wait for `DoSportTypePostBack` JS binding before calling.
    Pre-fix this raced the page's JS bootstrap on cold-start cold cycles
    — the cycle's outer try/except did catch it and recover via the
    reset-page retry, but the first-attempt failure was noisy and slowed
    cold start by an extra goto/flip/select round-trip.
    """
    try:
        await page.wait_for_function(
            "typeof DoSportTypePostBack === 'function'", timeout=30_000,
        )
    except Exception as e:
        raise RuntimeError(
            f"CB DoSportTypePostBack didn't bind within 30s for sport_id={sport_id} "
            f"({type(e).__name__}); treating as transient"
        ) from e
    log.info("DoSportTypePostBack(%d) — UpdatePanel AJAX, plain sleep", sport_id)
    await page.evaluate(f"DoSportTypePostBack({sport_id})")
    await asyncio.sleep(4)


async def _select_basketball(page) -> None:
    """Back-compat wrapper for scripts/capture_single_match_detail.py."""
    await _select_sport(page, BASKETBALL_SPORT_ID)


async def _load_all_leagues_for_sport(page, sport_id: int) -> None:
    """Run DoChampionatPostBack('SelectAllChampionats:<sport_id>'), then wait
    for game containers to fully render.

    CB loads leagues progressively via UpdatePanel AJAX — firing on the first
    visible container (wait_for_selector) then immediately reading page.content()
    captures only the first batch and misses the rest. We instead poll the
    container count until it has been stable for several consecutive checks,
    meaning CB's AJAX has finished writing all leagues to the DOM.
    """
    js = f'DoChampionatPostBack("SelectAllChampionats:{sport_id}")'
    try:
        await page.wait_for_function(
            "typeof DoChampionatPostBack === 'function'", timeout=30_000,
        )
    except Exception as e:
        raise RuntimeError(
            f"CB DoChampionatPostBack didn't bind within 30s for sport_id={sport_id} "
            f"({type(e).__name__}); treating as transient"
        ) from e
    log.info(js)
    await page.evaluate(js)

    # Poll container count until stable: wait for the first container to
    # appear (guards the 0-games transient), then keep polling until the
    # count hasn't grown for _STABLE_POLLS consecutive ticks.
    _POLL_INTERVAL = 1.5   # seconds between polls
    _STABLE_POLLS  = 3     # how many consecutive equal-count polls = "done"
    _TIMEOUT       = 45.0  # hard upper bound before we give up and use what we have

    deadline = asyncio.get_event_loop().time() + _TIMEOUT
    prev_count = 0
    stable_streak = 0

    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(_POLL_INTERVAL)
        try:
            count = await asyncio.wait_for(
                page.evaluate(
                    "() => document.querySelectorAll('div.GContainerList[data-id]').length"
                ),
                timeout=5.0,
            )
        except Exception:
            count = prev_count  # page briefly unresponsive — don't reset streak

        if count == 0:
            stable_streak = 0   # not started yet
        elif count == prev_count:
            stable_streak += 1
            if stable_streak >= _STABLE_POLLS:
                log.info(
                    "CB sport_id=%d list view stable at %d containers "
                    "(after %d polls)",
                    sport_id, count,
                    stable_streak + (prev_count > 0),
                )
                return
        else:
            stable_streak = 0   # count grew — keep waiting

        prev_count = count

    # Timed out — use whatever rendered. Raise only if we got nothing at all.
    if prev_count == 0:
        raise RuntimeError(
            f"CB sport_id={sport_id} list view rendered no game containers "
            f"within {_TIMEOUT:.0f}s after SelectAllChampionats — "
            "treating as transient; next cycle will retry"
        )
    log.warning(
        "CB sport_id=%d list view timed out waiting for stable count "
        "— using %d containers (may be incomplete)",
        sport_id, prev_count,
    )


async def _load_all_leagues(page) -> None:
    """Back-compat wrapper (basketball-default) for scripts that imported this name."""
    await _load_all_leagues_for_sport(page, BASKETBALL_SPORT_ID)


# ── HTML entry points ─────────────────────────────────────────────────────────

def _parse_html_for_sport(
    html: str, fetched_at: datetime, sport: Any,
) -> list[Odds]:
    """Parse a full Sports.aspx HTML page using the given sport module's parsers.

    Walks div.GContainerList containers, extracts metadata, dispatches to
    sport.parse_loadinfo (Format A) or sport.parse_div_odds (Format B).
    Returns list-view Odds for that sport — used by the saved-HTML path
    (dry_run_parse_saved / CB_USE_SAVED) and as a fallback inside the
    expansion flow.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    results: list[Odds] = []
    skipped_outrights = 0

    # Carry date_str across iterations: CB's prematch page groups multiple
    # consecutive game-table blocks under a single date header, so blocks
    # without their own x_loop_title_block inherit the previous one's date.
    date_str: Optional[str] = None
    for game_table in soup.select("div.game-table"):
        title = game_table.select_one("div.x_loop_title_block")
        if title:
            m = re.search(r"(\d{2})[./](\d{2})[./](\d{4})", title.text)
            if m:
                date_str = f"{m.group(1)}/{m.group(2)}/{m.group(3)}"

        for container in game_table.select("div.GContainerList[data-id]"):
            event_id = container["data-id"]

            hint = container.select_one("div.game_hint label")
            league_text = hint.text.strip() if hint else ""
            if any(skip in league_text.lower() for skip in LEAGUE_SKIP):
                skipped_outrights += 1
                continue

            teams_div = container.select_one("div.teams_name")
            if not teams_div:
                continue
            parts = teams_div.text.strip().split(" - ", 1)
            if len(parts) != 2:
                log.debug("cannot split teams %r — skipping", teams_div.text.strip())
                continue
            home, away = parts[0].strip(), parts[1].strip()

            time_el = container.select_one("span.time")
            time_str = time_el.text.strip() if time_el else None
            start_time: Optional[datetime] = None
            if date_str and time_str:
                try:
                    naive_local = datetime.strptime(
                        f"{date_str} {time_str}", "%d/%m/%Y %H:%M"
                    )
                    start_time = (naive_local - TBILISI_UTC_OFFSET).replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    pass

            gl = container.select_one("div.game_loading[data-loadinfo]")
            if gl:
                rows = sport.parse_loadinfo(
                    gl["data-loadinfo"], event_id,
                    home, away, league_text, start_time, fetched_at,
                )
            else:
                rows = sport.parse_div_odds(
                    container, event_id,
                    home, away, league_text, start_time, fetched_at,
                )

            results.extend(rows)

    log.info(
        "parsed %d Odds objects (%d outrights skipped)",
        len(results), skipped_outrights,
    )
    return results


def parse_html(html: str, fetched_at: datetime) -> list[Odds]:
    """Parse a basketball Sports.aspx HTML page. Back-compat signature for tests/app.py."""
    return _parse_html_for_sport(html, fetched_at, basketball)


def parse_html_soccer(html: str, fetched_at: datetime) -> list[Odds]:
    """Parse a soccer Sports.aspx HTML page."""
    return _parse_html_for_sport(html, fetched_at, soccer)


def parse_html_tennis(html: str, fetched_at: datetime) -> list[Odds]:
    """Parse a tennis Sports.aspx HTML page."""
    return _parse_html_for_sport(html, fetched_at, tennis)


def parse_html_mma(html: str, fetched_at: datetime) -> list[Odds]:
    """Parse an MMA Sports.aspx HTML page."""
    return _parse_html_for_sport(html, fetched_at, mma)


# ── Singletons: one browser, one page per sport ───────────────────────────────
#
# Pre-Phase-2 had a single _page singleton — tearing down Chromium every
# cycle was the original optimization win; Phase 2 extends to multi-sport by
# keeping ONE browser + ONE page per sport. Two sports' cycles can run in
# parallel since they hit different pages. Browser-level init still gates
# behind _browser_init_lock (only one Chromium spawning at a time).
#
# dry_run_capture() below is intentionally NOT routed through these singletons:
# it's a one-off and benefits from a clean isolated context.

_pw: Any = None
_browser: Any = None
_contexts: dict[int, Any] = {}          # sport_id → BrowserContext (Phase 2.5 #6)
_pages: dict[int, Any] = {}             # sport_id → page (in its sport's context)
_sport_cycle_counts: dict[int, int] = {}  # sport_id → cycle count (per-sport re-init)
_sport_locks: dict[int, asyncio.Lock] = {}  # sport_id → per-sport refresh lock
_browser_init_lock = asyncio.Lock()      # protects browser-level setup/teardown
_init_count: int = 0                     # browser inits across the process lifetime

_REINIT_EVERY_N_CYCLES = 50  # ~4 hours at the default 5-min cadence


def _get_sport_lock(sport_id: int) -> asyncio.Lock:
    """Return (creating if needed) the per-sport refresh lock."""
    if sport_id not in _sport_locks:
        _sport_locks[sport_id] = asyncio.Lock()
    return _sport_locks[sport_id]


async def _ensure_browser_singleton(*, headed: bool) -> None:
    """Ensure the shared Chromium browser exists. Caller must hold _browser_init_lock.

    Phase 2.5 #6: no shared BrowserContext here. Each sport gets its OWN
    context (created lazily by _ensure_context_for_sport) so server-side
    ASP.NET session state doesn't leak between sports.
    """
    global _pw, _browser, _init_count

    if _browser is not None:
        return  # already alive

    from playwright.async_api import async_playwright
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(
        headless=not headed,
        args=["--disable-blink-features=AutomationControlled"],
    )
    _init_count += 1
    log.info("CB browser: init #%d (headed=%s)", _init_count, headed)


async def _ensure_context_for_sport(sport_id: int) -> Any:
    """Ensure a BrowserContext exists for this sport. Caller must hold _browser_init_lock.

    Phase 2.5 #6: per-sport contexts isolate session cookies. Without this,
    DoSportTypePostBack from one sport's page overwrites another sport's
    server-side session sport, and SelectAllChampionats returns empty
    because session sport != requested sport.

    Memory: each context is ~50 MB; cheap relative to Chromium itself.
    """
    global _contexts
    ctx = _contexts.get(sport_id)
    if ctx is not None:
        return ctx
    ctx = await _browser.new_context(
        user_agent=USER_AGENT,
        locale="ka-GE",
        timezone_id="Asia/Tbilisi",
    )
    _contexts[sport_id] = ctx
    log.info("CB browser: new context for sport_id=%d", sport_id)
    return ctx


async def _ensure_page_for_sport(sport_id: int, *, headed: bool) -> Any:
    """Ensure a page exists for this sport_id, navigated + sport-selected.

    Returns the ready-to-postback page. Three defensive layers (same as
    Phase 1's _ensure_browser, just per-sport now):
      1. Periodic forced re-init — every _REINIT_EVERY_N_CYCLES cycles for
         THIS sport, drop the page so we re-init (memory growth + ASP.NET
         session expiry insurance).
      2. Health check — `typeof DoChampionatPostBack === "function"` on the
         page before each refresh. If page state drifted, drop + re-init.
      3. (Retry-once on actual fetch failure is handled by _fetch_for_sport.)
    """
    global _pages, _sport_cycle_counts

    # Browser-level: hold the init lock briefly to ensure Chromium is alive
    # AND this sport's context exists. Both inside the same lock so two
    # sports don't race the browser/context creation.
    async with _browser_init_lock:
        await _ensure_browser_singleton(headed=headed)
        ctx = await _ensure_context_for_sport(sport_id)

    page = _pages.get(sport_id)
    cycle_count = _sport_cycle_counts.get(sport_id, 0)

    # 1. Periodic forced re-init for this sport's page.
    if page is not None and cycle_count >= _REINIT_EVERY_N_CYCLES:
        log.info(
            "CB browser: forced re-init for sport_id=%d after %d cycles",
            sport_id, cycle_count,
        )
        await _close_page_internal(sport_id)
        page = None

    # 2. Health check. Tests BOTH the league-listing function and the
    # per-game expand function. Pre-Phase-2.5 we only checked
    # DoChampionatPostBack; cycle 1 sometimes hit 'DoGamesPostBack is not
    # defined' mid-expand because that JS binds later in the page bootstrap.
    if page is not None and not page.is_closed():
        try:
            fn_types = await asyncio.wait_for(
                page.evaluate(
                    "[typeof DoChampionatPostBack, typeof DoGamesPostBack]"
                ),
                timeout=5.0,
            )
            if fn_types == ["function", "function"]:
                return page
            log.info(
                "CB browser: page for sport_id=%d in unexpected state (typeof=%s); re-initing",
                sport_id, fn_types,
            )
        except Exception as e:
            log.warning(
                "CB browser: health check failed for sport_id=%d (%s); re-initing",
                sport_id, e,
            )
        await _close_page_internal(sport_id)
        page = None

    # Spawn new page in THIS sport's context (cookies/session isolated per sport).
    page = await ctx.new_page()
    log.info("CB browser: goto %s for sport_id=%d", SPORTS_URL, sport_id)
    await page.goto(SPORTS_URL, wait_until="domcontentloaded", timeout=60_000)
    await asyncio.sleep(6)
    await _flip_to_english(page)
    await _select_sport(page, sport_id)
    _pages[sport_id] = page
    _sport_cycle_counts[sport_id] = 0
    log.info("CB browser: ready for sport_id=%d (English + sport selected)", sport_id)
    return page


async def _close_page_internal(sport_id: int) -> None:
    """Close and remove the page AND context for one sport. Browser stays alive.

    Phase 2.5 #6: closing the context too — each sport's context is dedicated
    and has no other pages. Keeping it after the page closes would leak
    memory + stale session cookies that could re-bite us on the next init.
    """
    global _contexts
    page = _pages.pop(sport_id, None)
    _sport_cycle_counts.pop(sport_id, None)
    if page is not None:
        try:
            await asyncio.wait_for(page.close(), timeout=5.0)
        except Exception as e:
            log.debug("CB browser: page close failed for sport_id=%d: %s", sport_id, e)
    ctx = _contexts.pop(sport_id, None)
    if ctx is not None:
        try:
            await asyncio.wait_for(ctx.close(), timeout=5.0)
        except Exception as e:
            log.debug("CB browser: context close failed for sport_id=%d: %s", sport_id, e)


async def _close_browser_internal() -> None:
    """Tear down all singleton state. Must be called with _browser_init_lock held."""
    global _pw, _browser, _contexts

    # Close every per-sport page+context first so we don't get spurious
    # "context closing while page in use" warnings from Playwright on shutdown.
    for sport_id in list(_pages.keys()):
        await _close_page_internal(sport_id)

    # Safety net: any orphaned contexts (shouldn't happen, but defensive).
    for sport_id in list(_contexts.keys()):
        ctx = _contexts.pop(sport_id, None)
        if ctx is not None:
            try:
                await asyncio.wait_for(ctx.close(), timeout=5.0)
            except Exception as e:
                log.debug("CB browser: orphan context close failed (sport_id=%d): %s", sport_id, e)

    if _browser is not None:
        try:
            await asyncio.wait_for(_browser.close(), timeout=5.0)
        except Exception as e:
            log.debug("CB browser: close browser failed: %s", e)
    if _pw is not None:
        try:
            await asyncio.wait_for(_pw.stop(), timeout=5.0)
        except Exception as e:
            log.debug("CB browser: stop playwright failed: %s", e)
    _pw = _browser = None


async def close_crystalbet() -> None:
    """Public teardown — call from app shutdown to free the singleton browser + all sport pages."""
    async with _browser_init_lock:
        await _close_browser_internal()
    await cb_http.close_all()
    log.info("CB browser: closed cleanly")


# ── Detail-page expansion (per-game) ──────────────────────────────────────────
# Module-level per-sport caches of the most recent detail-page Odds per event_id.
# Survive across cycles. When a game's list-view hash is stable, we serve its
# cached detail rows instead of re-expanding. Pruned alongside change_cache
# on each cycle.
_sport_detail_odds_caches: dict[str, dict[str, list[Odds]]] = {}


def _get_sport_detail_cache(sport_name: str) -> dict[str, list[Odds]]:
    """Return (creating if needed) the per-sport detail-odds cache."""
    if sport_name not in _sport_detail_odds_caches:
        _sport_detail_odds_caches[sport_name] = {}
    return _sport_detail_odds_caches[sport_name]


# Per-game timeouts. All in seconds. Each is a hard upper bound; we want to
# move on quickly if a single game stalls rather than block the whole cycle.
_EXPAND_POSTBACK_TIMEOUT_SEC = 10.0   # JS evaluate for DoGamesPostBack
_EXPAND_SELECTOR_WAIT_SEC = 12.0      # wait_for_selector for the detail block — bumped from 6.0 (2026-05-26) after soccer cold cycle showed 43% expand timeouts at 6s. Soccer detail pages render slower than basketball.
_COLLAPSE_DETACH_WAIT_SEC = 3.0       # wait for old block to detach after collapse
_EXTRACT_BLOCK_TIMEOUT_SEC = 5.0      # evaluate to read the block's outerHTML

# Log a progress line every N games so long cold-start cycles don't run silently.
_PROGRESS_LOG_EVERY = 25

# When expand keeps failing, don't keep serving stale cached detail forever.
# Past this age, fall back to fresh list-view Odds instead of last-known-good
# cached detail. List view is always fresh per cycle so it's a safer fallback
# than hours-old detail.
_STALE_CACHE_MAX_AGE_SEC = 6 * 60 * 60   # 6 hours — bumped from 30 min (2026-05-26). The 30-min cap was dropping ALL disk-loaded cache entries on warm restart since the cache file is usually >30 min old. 6h still protects against truly stale data while letting the cache actually serve detail Odds across normal restart gaps.

# CB_SKIP_DETAIL_SPORTS env knob — comma-separated sport names whose cycles
# should SKIP the per-game detail expansion entirely. The cycle still does:
#   1. List-view scrape (fast — one HTTP/Playwright round-trip)
#   2. Per-game extraction (Format A loadinfo + Format B col-divs)
#   3. Return list-view Odds for every game (no DoGamesPostBack:ExpandDetail)
#
# Use for laptop-load relief or when one sport's detail-page expansion is
# unreliable. Soccer is the typical target (~795 games × 12s wait = up to
# ~150 min cold cycle; list-only is ~30s).
#
# What you lose with skip-detail enabled, per game:
#   - All alt-line spread/total ladders (only main line from list view)
#   - team_total markets entirely
#   - corners markets entirely
#   - H1 markets (most prematch books ship FT in list view, H1 in detail)
#
# What you keep:
#   - 1X2 / moneyline FT
#   - Main-line Total FT
#   - (Spread depends on list-view format — soccer list view DOES NOT ship
#     AH, so soccer list-only loses spread too. Basketball list view ships
#     AH/OU main lines so basketball list-only retains those.)
_SKIP_DETAIL_SPORTS_ENV = os.environ.get("CB_SKIP_DETAIL_SPORTS", "").strip()
SKIP_DETAIL_SPORTS: frozenset[str] = frozenset(
    s.strip() for s in _SKIP_DETAIL_SPORTS_ENV.split(",") if s.strip()
)
if SKIP_DETAIL_SPORTS:
    log.info(
        "CB_SKIP_DETAIL_SPORTS=%s — list-view-only mode (no per-game expansion) for: %s",
        _SKIP_DETAIL_SPORTS_ENV, sorted(SKIP_DETAIL_SPORTS),
    )


@dataclass
class _GameOnList:
    """One game's metadata extracted from the list view, plus the loadinfo
    string used for change-cache hashing."""
    event_id: str
    home: str
    away: str
    league: str
    start_time: Optional[datetime]
    loadinfo: str   # raw JSON string — empty if Format B
    list_odds: list[Odds]   # rows parsed from the list view (fallback)


def _extract_games_from_list_html(
    html: str, fetched_at: datetime, sport: Any = None,
) -> list[_GameOnList]:
    """
    Walk the list-view HTML producing per-game metadata + list-view Odds.

    Mirrors _parse_html_for_sport() structurally but emits a richer per-game
    object that the expansion flow needs (event_id, loadinfo string for
    hashing, list-view Odds as fallback).

    `sport` defaults to basketball for back-compat with the tests that import
    this name and call it without a sport argument.
    """
    from bs4 import BeautifulSoup

    if sport is None:
        sport = basketball

    soup = BeautifulSoup(html, "html.parser")
    games: list[_GameOnList] = []
    date_str: Optional[str] = None

    for game_table in soup.select("div.game-table"):
        title = game_table.select_one("div.x_loop_title_block")
        if title:
            m = re.search(r"(\d{2})[./](\d{2})[./](\d{4})", title.text)
            if m:
                date_str = f"{m.group(1)}/{m.group(2)}/{m.group(3)}"

        for container in game_table.select("div.GContainerList[data-id]"):
            event_id = container["data-id"]
            hint = container.select_one("div.game_hint label")
            league_text = hint.text.strip() if hint else ""
            if any(s in league_text.lower() for s in LEAGUE_SKIP):
                continue

            teams_div = container.select_one("div.teams_name")
            if not teams_div:
                continue
            parts = teams_div.text.strip().split(" - ", 1)
            if len(parts) != 2:
                continue
            home, away = parts[0].strip(), parts[1].strip()

            time_el = container.select_one("span.time")
            time_str = time_el.text.strip() if time_el else None
            start_time: Optional[datetime] = None
            if date_str and time_str:
                try:
                    naive_local = datetime.strptime(
                        f"{date_str} {time_str}", "%d/%m/%Y %H:%M"
                    )
                    start_time = (naive_local - TBILISI_UTC_OFFSET).replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    pass

            # Extract loadinfo (Format A) or fall back to Format B
            gl = container.select_one("div.game_loading[data-loadinfo]")
            if gl:
                loadinfo = gl.get("data-loadinfo") or ""
                list_odds = sport.parse_loadinfo(
                    loadinfo, event_id,
                    home, away, league_text, start_time, fetched_at,
                )
            else:
                loadinfo = ""  # Format B has no loadinfo — hash empty string
                list_odds = sport.parse_div_odds(
                    container, event_id,
                    home, away, league_text, start_time, fetched_at,
                )

            games.append(_GameOnList(
                event_id=event_id, home=home, away=away,
                league=league_text, start_time=start_time,
                loadinfo=loadinfo, list_odds=list_odds,
            ))
    return games


async def _expand_and_parse_one(
    game: _GameOnList, fetched_at: datetime, sport: Any, page: Any,
    classify: Any = None, ladder_mode: bool = False,
) -> list[Odds]:
    """
    Trigger ExpandDetail for one game on the given sport's page, wait for the
    detail block to render, extract JUST that block's outerHTML, parse via
    cb_detail with the sport's classifier, return.

    See pre-Phase-2 notes inline below for the O(N²) page.content() fix and
    the CollapseDetail-before-ExpandDetail pattern — both unchanged, just
    parameterized over `sport` and `page`.

    Raises on any failure; caller marks expand_failed and falls back.
    """
    selector = (
        f"div.GContainerList[data-id=\"{game.event_id}\"] table.game-details"
    )

    # 0. If this game has a prior detail block from a previous cycle,
    #    collapse it first via CB's own JS so the upcoming ExpandDetail
    #    produces fresh content (not a stale no-op).
    block_exists = await asyncio.wait_for(
        page.evaluate(
            "(sel) => !!document.querySelector(sel)",
            selector,
        ),
        timeout=_EXTRACT_BLOCK_TIMEOUT_SEC,
    )
    if block_exists:
        try:
            await asyncio.wait_for(
                page.evaluate(
                    f"DoGamesPostBack('CollapseDetail:{game.event_id}')"
                ),
                timeout=_EXPAND_POSTBACK_TIMEOUT_SEC,
            )
            try:
                await page.wait_for_selector(
                    selector, state="detached",
                    timeout=int(_COLLAPSE_DETACH_WAIT_SEC * 1000),
                )
            except Exception:
                log.debug(
                    "collapse didn't detach block for %s — proceeding to expand",
                    game.event_id,
                )
        except Exception as e:
            log.debug(
                "CollapseDetail failed for %s (%s); proceeding to expand directly",
                game.event_id, e,
            )

    # 1. Trigger ExpandDetail.
    await asyncio.wait_for(
        page.evaluate(
            f"DoGamesPostBack('ExpandDetail:{game.event_id}')"
        ),
        timeout=_EXPAND_POSTBACK_TIMEOUT_SEC,
    )

    # 2. Wait for the detail block to appear.
    await page.wait_for_selector(
        selector,
        timeout=int(_EXPAND_SELECTOR_WAIT_SEC * 1000),
    )

    # 3. Extract JUST this game's detail block.
    block_html = await asyncio.wait_for(
        page.evaluate(
            "(sel) => {"
            "  const el = document.querySelector(sel);"
            "  return el ? el.outerHTML : '';"
            "}",
            selector,
        ),
        timeout=_EXTRACT_BLOCK_TIMEOUT_SEC,
    )
    if not block_html:
        raise RuntimeError(
            f"detail block evaluate returned empty for {game.event_id}"
        )

    odds = cb_detail.parse_detail_page(
        block_html,
        event_id=game.event_id,
        home=game.home, away=game.away,
        league=game.league,
        start_time=game.start_time,
        fetched_at=fetched_at,
        sport_name=sport.SPORT_NAME,
        classify=classify or sport.classify_market_title,
        scope_to_event=False,  # block_html is already the scoped table only
        per_section=ladder_mode,
    )
    return odds


async def _expand_and_parse_one_http(
    game: _GameOnList, fetched_at: datetime, sport: Any,
    classify: Any = None, ladder_mode: bool = False,
) -> list[Odds]:
    """HTTP-transport twin of _expand_and_parse_one — same parser, same
    classification, same Odds. The ExpandDetail delta's updatePanel segments
    contain the game's championship re-rendered with its table.game-details;
    parse_detail_page(scope_to_event=True) scopes by data-id exactly like the
    Playwright path's querySelector did.

    Raises when no detail table came back at all (the transport-failure
    signal, mirroring the Playwright wait_for_selector timeout) so the caller
    marks expand_failed rather than list-only.
    """
    blob = await cb_http.expand_detail_html(sport.SPORT_ID, game.event_id)
    if "game-details" not in blob:
        raise RuntimeError(
            f"ExpandDetail returned no detail table for {game.event_id} "
            f"({len(blob):,}B of panels)"
        )
    return cb_detail.parse_detail_page(
        blob,
        event_id=game.event_id,
        home=game.home, away=game.away,
        league=game.league,
        start_time=game.start_time,
        fetched_at=fetched_at,
        sport_name=sport.SPORT_NAME,
        classify=classify or sport.classify_market_title,
        scope_to_event=True,  # blob holds whole panels — scope by data-id
        per_section=ladder_mode,
    )


async def _expand_game(
    game: _GameOnList, fetched_at: datetime, sport: Any, page: Any,
    classify: Any = None, ladder_mode: bool = False, use_http: bool | None = None,
) -> list[Odds]:
    """Transport dispatch for one game's detail expansion. `use_http` overrides
    the global CB_TRANSPORT for this call (None = follow the global), letting the
    anomaly scan run browser-free even on a Playwright main app."""
    http = _USE_HTTP_TRANSPORT if use_http is None else use_http
    if http:
        return await _expand_and_parse_one_http(
            game, fetched_at, sport, classify=classify, ladder_mode=ladder_mode,
        )
    return await _expand_and_parse_one(
        game, fetched_at, sport, page, classify=classify, ladder_mode=ladder_mode,
    )


async def _refresh_list_html_for_sport(
    sport_id: int, sport_name: str, *, headed: bool, use_http: bool | None = None,
) -> tuple[Any, str]:
    """Fetch the list-view HTML via the configured transport, with the
    standard reset-and-retry-once on failure. Returns (page, list_html);
    page is None on the HTTP transport (expansion doesn't need it there).
    `use_http` overrides CB_TRANSPORT for this call (None = follow the global).
    """
    if _USE_HTTP_TRANSPORT if use_http is None else use_http:
        try:
            return None, await cb_http.fetch_list_html(sport_id)
        except Exception as e:
            log.warning(
                "CB http list fetch failed for %s (%s); re-warming session "
                "and retrying once", sport_name, e,
            )
            cb_http.reset_session(sport_id)
            return None, await cb_http.fetch_list_html(sport_id)

    try:
        page = await _ensure_page_for_sport(sport_id, headed=headed)
        await _load_all_leagues_for_sport(page, sport_id)
        list_html = await page.content()
        _sport_cycle_counts[sport_id] = _sport_cycle_counts.get(sport_id, 0) + 1
    except Exception as e:
        log.warning(
            "CB list-view fetch failed for %s (%s); resetting page and retrying once",
            sport_name, e,
        )
        await _close_page_internal(sport_id)
        page = await _ensure_page_for_sport(sport_id, headed=headed)
        await _load_all_leagues_for_sport(page, sport_id)
        list_html = await page.content()
        _sport_cycle_counts[sport_id] = 1
    return page, list_html


# ── Public entry points ───────────────────────────────────────────────────────

async def _fetch_for_sport(
    sport: Any, *, headed: bool, force_detail: bool = False,
    classify_override: Any = None, bypass_cache: bool = False,
    use_http: bool | None = None, start_within_hours: float | None = None,
) -> list[Odds]:
    """Generic per-sport fetch — drives one sport's full cycle.

    Per cycle:
      1. Acquire per-sport lock (parallel cycles for different sports OK)
      2. Refresh list view on this sport's page
      3. Extract per-game metadata + list-view Odds (fallback)
      4. Consult per-sport change cache; expand new/changed games; reuse
         cached detail Odds for stable games
      5. Return all Odds (detail-page where available, list-view otherwise)
    """
    sport_id = sport.SPORT_ID
    sport_name = sport.SPORT_NAME
    fetched_at = datetime.now(tz=timezone.utc)

    sport_lock = _get_sport_lock(sport_id)
    async with sport_lock:
        # ── 1. List view refresh (transport-dispatched, retry-once inside) ──
        page, list_html = await _refresh_list_html_for_sport(
            sport_id, sport_name, headed=headed, use_http=use_http,
        )

        # ── 2. Per-game extraction from list HTML ──
        games = _extract_games_from_list_html(list_html, fetched_at, sport=sport)
        log.info("CB %s list view: %d games", sport_name, len(games))

        # Phase 2.5 fix: if the list view rendered something but our parser
        # extracted 0 game containers, treat as transient (avoids the
        # "first-cycle success with 0 Odds → status bar stuck at sc:0 for
        # one full poll interval" pathology). Belt-and-suspenders alongside
        # the wait_for_selector inside _load_all_leagues_for_sport: that
        # catches the page-not-rendered case; this catches "page rendered
        # but parsing yielded nothing" (e.g. CB shipped only outright games
        # → all skipped at extract time → 0 in-scope games).
        if not games:
            raise RuntimeError(
                f"CB {sport_name} list view extracted 0 games from "
                f"{len(list_html):,}-byte HTML — treating as transient; "
                "next cycle will retry"
            )

        # ── 3a. CB_SKIP_DETAIL_SPORTS short-circuit ──
        # When this sport is in the skip set, return list-view Odds for
        # every game and bypass the entire expansion loop. Massive cost
        # saving: soccer cold cycle drops from ~60 min to ~30s. Trade-off
        # documented at the SKIP_DETAIL_SPORTS constant up top.
        if sport_name in SKIP_DETAIL_SPORTS and not force_detail:
            all_odds: list[Odds] = []
            for g in games:
                all_odds.extend(g.list_odds)
            log.info(
                "CB %s cycle complete (LIST-VIEW-ONLY mode): %d games, %d Odds total",
                sport_name, len(games), len(all_odds),
            )
            return all_odds

        # ── 3-bis. Cache-bypass full expansion (anomaly scan path) ──
        # When bypass_cache is set we ALWAYS expand every game with the given
        # classifier and never touch the shared change_cache / detail cache.
        # This keeps the hourly anomaly scan (permissive classifier) from
        # contaminating the dashboard's strict-classified basketball cache, and
        # guarantees the ladders are current each scan. Falls back to list-view
        # Odds for any game that fails to expand.
        if bypass_cache:
            # Horizon filter (2026-07-11): a FULL soccer expansion is ~956
            # games x ~0.85s = ~14 min while HOLDING the sport lock — the
            # extra-sport anomaly scan never completed a pass. Ladder
            # anomalies are most actionable near kickoff anyway, so expand
            # only games starting within the horizon, soonest first (partial
            # progress covers the nearest games). None = whole board
            # (basketball keeps that: ~100 games is fine).
            if start_within_hours is not None:
                cutoff = fetched_at + timedelta(hours=start_within_hours)
                in_h = [g for g in games
                        if g.start_time is not None and g.start_time <= cutoff]
                in_h.sort(key=lambda g: g.start_time)
                log.info("CB %s anomaly scan horizon %.0fh: %d/%d games",
                         sport_name, start_within_hours, len(in_h), len(games))
                games = in_h
            all_odds: list[Odds] = []
            n_ok = n_fail = 0
            for i, game in enumerate(games, 1):
                if i % _PROGRESS_LOG_EVERY == 0:
                    log.info(
                        "CB %s anomaly-scan expansion: %d/%d (expanded=%d, fallback=%d)",
                        sport_name, i, len(games), n_ok, n_fail,
                    )
                try:
                    detail_odds = await _expand_game(
                        game, fetched_at, sport, page,
                        classify=classify_override, ladder_mode=True, use_http=use_http,
                    )
                    if detail_odds:
                        all_odds.extend(detail_odds)
                        n_ok += 1
                    else:
                        all_odds.extend(game.list_odds)
                        n_fail += 1
                except Exception as e:
                    log.warning(
                        "anomaly-scan expand failed for %s (%s vs %s): %s",
                        game.event_id, game.home, game.away, e,
                    )
                    all_odds.extend(game.list_odds)
                    n_fail += 1
            log.info(
                "CB %s anomaly-scan cycle complete: %d expanded, %d fallback → %d Odds",
                sport_name, n_ok, n_fail, len(all_odds),
            )
            return all_odds

        # ── 3. Cache pruning + per-game expansion decisions ──
        cache = change_cache.get_cache(sport_name)
        detail_odds_cache = _get_sport_detail_cache(sport_name)
        present_ids = {g.event_id for g in games}
        dropped = cache.prune_missing(present_ids)
        for eid in list(detail_odds_cache.keys()):
            if eid not in present_ids:
                detail_odds_cache.pop(eid, None)
        if dropped:
            log.info("change cache (%s): pruned %d stale entries", sport_name, dropped)

        # ── 4. Iterate, expanding only games that need it ──
        all_odds: list[Odds] = []
        n_expanded = 0
        n_cached_detail = 0
        n_list_fallback = 0
        n_expand_failed = 0

        for i, game in enumerate(games, 1):
            if i % _PROGRESS_LOG_EVERY == 0:
                log.info(
                    "CB %s expansion progress: %d/%d games processed "
                    "(expanded=%d, cached=%d, list-fallback=%d, failed=%d)",
                    sport_name, i, len(games), n_expanded, n_cached_detail,
                    n_list_fallback, n_expand_failed,
                )
            if cache.needs_expansion(game.event_id, game.loadinfo):
                try:
                    detail_odds = await _expand_game(
                        game, fetched_at, sport, page, use_http=use_http,
                    )
                    if detail_odds:
                        cache.mark_loaded(game.event_id, game.loadinfo)
                        detail_odds_cache[game.event_id] = detail_odds
                        all_odds.extend(detail_odds)
                        n_expanded += 1
                    else:
                        cache.mark_list_only(game.event_id, game.loadinfo)
                        all_odds.extend(game.list_odds)
                        n_list_fallback += 1
                except Exception as e:
                    log.warning(
                        "expand failed for %s (%s vs %s): %s",
                        game.event_id, game.home, game.away, e,
                    )
                    cache.mark_expand_failed(game.event_id, game.loadinfo)
                    cached = detail_odds_cache.get(game.event_id)
                    entry = cache.entries.get(game.event_id)
                    if _is_cached_detail_fresh_enough(cached, entry):
                        all_odds.extend(cached)
                    else:
                        if cached:
                            log.info(
                                "dropping stale cached detail for %s (last expanded too long ago) — using list-view",
                                game.event_id,
                            )
                            detail_odds_cache.pop(game.event_id, None)
                        all_odds.extend(game.list_odds)
                    n_expand_failed += 1
            else:
                cached = detail_odds_cache.get(game.event_id)
                entry = cache.entries.get(game.event_id)
                if _is_cached_detail_fresh_enough(cached, entry):
                    all_odds.extend(cached)
                    n_cached_detail += 1
                else:
                    if cached:
                        log.info(
                            "dropping stale cached detail for %s (hash stable but cache too old) — using list-view",
                            game.event_id,
                        )
                        detail_odds_cache.pop(game.event_id, None)
                    all_odds.extend(game.list_odds)
                    n_list_fallback += 1

        log.info(
            "CB %s cycle complete: %d expanded, %d cached-detail, "
            "%d list-fallback, %d expand-failed → %d Odds total",
            sport_name, n_expanded, n_cached_detail, n_list_fallback,
            n_expand_failed, len(all_odds),
        )

    return all_odds


async def fetch_crystalbet_basketball_prematch(
    *, headed: bool = False, force_detail: bool = False,
) -> list[Odds]:
    """Scrape CB prematch basketball with full detail-page expansion.

    `force_detail=True` overrides CB_SKIP_DETAIL_SPORTS / the SPORTS=...:list
    short-circuit so the full alt-line ladders are scraped even when the
    dashboard otherwise runs basketball in list-only mode. Used by the hourly
    anomaly scanner, which needs the ladders the list view doesn't expose.
    """
    return await _fetch_for_sport(basketball, headed=headed, force_detail=force_detail)


# sport → (module, permissive classifier or None→strict) for the anomaly scan.
# Tennis has no permissive classifier yet — its strict classifier still yields
# the games-total/handicap ladders the monotonicity detector needs.
_ANOMALY_SPORT_TABLE = {
    "basketball": (basketball, basketball.classify_market_title_permissive),
    "soccer": (soccer, soccer.classify_market_title_permissive),
    "tennis": (tennis, None),
}


async def fetch_crystalbet_anomaly_ladders(
    sport_name: str, *, headed: bool = False,
    start_within_hours: float | None = None,
) -> list[Odds]:
    """Full-detail anomaly scrape for ANY supported sport (2026-07-11 —
    the scan was basketball-only before). Same semantics as the basketball
    variant below: force_detail + bypass_cache (+ permissive classifier where
    one exists)."""
    mod, clf = _ANOMALY_SPORT_TABLE[sport_name]
    return await _fetch_for_sport(
        mod, headed=headed, force_detail=True,
        classify_override=clf, bypass_cache=True, use_http=_ANOMALY_USE_HTTP,
        start_within_hours=start_within_hours,
    )


async def fetch_crystalbet_basketball_anomaly_ladders(
    *, headed: bool = False,
) -> list[Odds]:
    """Full-detail basketball scrape for the hourly anomaly scanner.

    Differs from the normal basketball fetch in three ways:
      - force_detail: expands ladders even when basketball runs list-only;
      - PERMISSIVE detail classifier: captures every 2-way handicap/total
        ladder regardless of title phrasing (catches "Asian Handicap 1st
        Period" and friends the strict classifier drops), while still excluding
        prop/3-way/exotic markets via the shared skip-list;
      - bypass_cache: always re-expands and never touches the dashboard's
        strict-classified basketball change-cache.
    """
    return await _fetch_for_sport(
        basketball, headed=headed, force_detail=True,
        classify_override=basketball.classify_market_title_permissive,
        bypass_cache=True, use_http=_ANOMALY_USE_HTTP,
    )


async def fetch_crystalbet_basketball_games(
    event_ids, *, headed: bool = False,
) -> list[Odds]:
    """Re-scrape ONLY the given basketball games' detail ladders (permissive,
    ladder_mode) — for the fast 'watch' loop that re-checks already-flagged
    games every few minutes without re-expanding the whole board.

    Loads the list view to locate the games (needed to drive ExpandDetail), then
    expands only the targets. Falls back to a game's list-view Odds if its
    expansion fails. Shares the basketball sport-lock with the other CB tasks."""
    wanted = {str(e) for e in event_ids}
    if not wanted:
        return []
    sport = basketball
    sport_id = sport.SPORT_ID
    fetched_at = datetime.now(tz=timezone.utc)
    sport_lock = _get_sport_lock(sport_id)
    async with sport_lock:
        page, list_html = await _refresh_list_html_for_sport(
            sport_id, sport.SPORT_NAME, headed=headed, use_http=_ANOMALY_USE_HTTP,
        )
        games = _extract_games_from_list_html(list_html, fetched_at, sport=sport)
        targets = [g for g in games if g.event_id in wanted]
        log.info("CB basketball watch re-scan: %d/%d target games present",
                 len(targets), len(wanted))
        out: list[Odds] = []
        for g in targets:
            try:
                detail = await _expand_game(
                    g, fetched_at, sport, page,
                    classify=sport.classify_market_title_permissive,
                    ladder_mode=True, use_http=_ANOMALY_USE_HTTP,
                )
                out.extend(detail if detail else g.list_odds)
            except Exception as e:
                log.warning("watch re-scan expand failed for %s (%s vs %s): %s",
                            g.event_id, g.home, g.away, e)
                out.extend(g.list_odds)
        return out


async def fetch_crystalbet_soccer_prematch(
    *, headed: bool = False,
) -> list[Odds]:
    """Scrape CB prematch soccer with full detail-page expansion."""
    return await _fetch_for_sport(soccer, headed=headed)


async def fetch_crystalbet_tennis_prematch(
    *, headed: bool = False,
) -> list[Odds]:
    """Scrape CB prematch tennis. Designed for list-only mode (SPORTS=tennis:list);
    full mode would also work (basketball-style detail expansion) but tennis
    has 500+ matches per cycle which is too heavy without server resources."""
    return await _fetch_for_sport(tennis, headed=headed)


async def fetch_crystalbet_mma_prematch(
    *, headed: bool = False,
) -> list[Odds]:
    """Scrape CB prematch MMA. List-only mode (SPORTS=mma:list) recommended —
    MMA's CB list view exposes ML + total rounds only; per-fight method-of-
    victory / round-betting markets live behind detail expansion which v1
    skips. ~58 fights per cycle in typical samples (UFC + regional promotions)."""
    return await _fetch_for_sport(mma, headed=headed)


# ── Per-sport accessors (with sport_name params for app.py / cache_persistence) ──

def get_detail_status_map(sport_name: str = "basketball") -> dict[str, str]:
    """Return {event_id: detail_status} for the most recent cycle of this sport.

    Default sport_name="basketball" preserves the pre-Phase-2 signature for
    existing call sites (app.py, the per-game freshness diagnostic).
    """
    cache = change_cache.get_cache(sport_name)
    return {eid: entry.detail_status for eid, entry in cache.entries.items()}


def get_last_expanded_map(sport_name: str = "basketball") -> dict[str, datetime]:
    """Return {event_id: last_expanded_at} for events successfully expanded at least once.

    Default sport_name="basketball" preserves pre-Phase-2 callers.
    """
    cache = change_cache.get_cache(sport_name)
    return {
        eid: entry.last_expanded_at
        for eid, entry in cache.entries.items()
        if entry.last_expanded_at is not None
    }


def get_detail_odds_cache(sport_name: str = "basketball") -> dict[str, list[Odds]]:
    """Expose the per-sport detail-odds cache for serialization on shutdown.

    Default sport_name="basketball" preserves pre-Phase-2 callers
    (cache_persistence.save reads this).
    """
    return _get_sport_detail_cache(sport_name)


def restore_detail_odds_cache(
    cache: dict[str, list[Odds]], sport_name: str = "basketball",
) -> None:
    """Restore a sport's detail-odds cache from disk on startup. Replaces current state.

    Default sport_name="basketball" preserves pre-Phase-2 callers
    (cache_persistence.load writes through this).
    """
    _sport_detail_odds_caches[sport_name] = cache


def _is_cached_detail_fresh_enough(
    cached: Optional[list[Odds]],
    entry: Optional[change_cache.CacheEntry],
    *,
    now: Optional[datetime] = None,
    max_age_sec: float = _STALE_CACHE_MAX_AGE_SEC,
) -> bool:
    """
    Decide whether cached detail Odds are recent enough to serve.

    Returns False (→ caller should fall back to list-view) when:
      - There's no cached detail data
      - We have no recorded last_expanded_at (never successfully expanded)
      - last_expanded_at is older than max_age_sec
    """
    if not cached:
        return False
    if entry is None or entry.last_expanded_at is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    age_sec = (now - entry.last_expanded_at).total_seconds()
    return age_sec <= max_age_sec


# ── dry_run helpers (basketball-only; one-off, not routed through singletons) ──

async def dry_run_capture(
    *, headed: bool = True, out_path: Path = SAMPLE_OUT,
) -> None:
    """Navigate, save HTML, parse, print summary — for smoke testing (basketball)."""
    from playwright.async_api import async_playwright

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(tz=timezone.utc)

    async with async_playwright() as pw:
        browser, context, page = await _init_browser(pw, headed=headed)
        try:
            await _flip_to_english(page)
            await _select_basketball(page)
            await _load_all_leagues(page)
            html = await page.content()
        finally:
            await context.close()
            await browser.close()

    out_path.write_text(html, encoding="utf-8")
    odds_list = parse_html(html, fetched_at)
    _print_summary(odds_list, out_path, len(html))


def dry_run_parse_saved(out_path: Path = SAMPLE_OUT) -> list[Odds]:
    """Parse an already-saved basketball HTML sample without launching a browser."""
    if not out_path.exists():
        print(f"No saved HTML at {out_path} — run dry_run_capture() first")
        return []
    html = out_path.read_text(encoding="utf-8")
    fetched_at = datetime.now(tz=timezone.utc)
    odds_list = parse_html(html, fetched_at)
    _print_summary(odds_list, out_path, len(html))
    return odds_list


def dry_run_parse_saved_soccer(out_path: Path = SAMPLE_OUT_SOCCER) -> list[Odds]:
    """Parse an already-saved soccer HTML sample without launching a browser."""
    if not out_path.exists():
        print(f"No saved HTML at {out_path}")
        return []
    html = out_path.read_text(encoding="utf-8")
    fetched_at = datetime.now(tz=timezone.utc)
    odds_list = parse_html_soccer(html, fetched_at)
    _print_summary(odds_list, out_path, len(html))
    return odds_list


def dry_run_parse_saved_tennis(out_path: Path = SAMPLE_OUT_TENNIS) -> list[Odds]:
    """Parse an already-saved tennis HTML sample without launching a browser."""
    if not out_path.exists():
        print(f"No saved HTML at {out_path}")
        return []
    html = out_path.read_text(encoding="utf-8")
    fetched_at = datetime.now(tz=timezone.utc)
    odds_list = parse_html_tennis(html, fetched_at)
    _print_summary(odds_list, out_path, len(html))
    return odds_list


def dry_run_parse_saved_mma(out_path: Path = SAMPLE_OUT_MMA) -> list[Odds]:
    """Parse an already-saved MMA HTML sample without launching a browser."""
    if not out_path.exists():
        print(f"No saved HTML at {out_path}")
        return []
    html = out_path.read_text(encoding="utf-8")
    fetched_at = datetime.now(tz=timezone.utc)
    odds_list = parse_html_mma(html, fetched_at)
    _print_summary(odds_list, out_path, len(html))
    return odds_list


def _print_summary(odds_list: list[Odds], path: Path, html_len: int) -> None:
    by_type: dict[str, int] = {}
    for o in odds_list:
        by_type[o.market_type] = by_type.get(o.market_type, 0) + 1

    print()
    print("=" * 64)
    print("  CrystalBet prematch — parse summary")
    print("=" * 64)
    print(f"  HTML source:   {path}")
    print(f"  HTML size:     {html_len:,} bytes")
    print(f"  Total Odds:    {len(odds_list)}")
    for mtype, n in sorted(by_type.items()):
        print(f"    {mtype}: {n}")
    if odds_list:
        print("\n  Sample (first 5):")
        for o in odds_list[:5]:
            print(
                f"    {o.home} vs {o.away}"
                f" | {o.market_type} {o.period}"
                f" | line={o.line}"
                f" | {o.selections}"
            )
    print()


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    ap = argparse.ArgumentParser(description="CrystalBet prematch scraper")
    ap.add_argument(
        "--parse-saved",
        action="store_true",
        help="parse data/raw/cb_prematch_sample.html without launching browser",
    )
    ap.add_argument(
        "--parse-saved-soccer",
        action="store_true",
        help="parse data/raw/cb_prematch_sample_soccer.html without launching browser",
    )
    ap.add_argument(
        "--parse-saved-tennis",
        action="store_true",
        help="parse data/raw/cb_prematch_sample_tennis.html without launching browser",
    )
    ap.add_argument(
        "--parse-saved-mma",
        action="store_true",
        help="parse data/raw/cb_prematch_sample_mma.html without launching browser",
    )
    ap.add_argument("--headless", action="store_true", help="run browser headless")
    args = ap.parse_args()

    if args.parse_saved:
        dry_run_parse_saved()
    elif args.parse_saved_soccer:
        dry_run_parse_saved_soccer()
    elif args.parse_saved_tennis:
        dry_run_parse_saved_tennis()
    elif args.parse_saved_mma:
        dry_run_parse_saved_mma()
    else:
        asyncio.run(dry_run_capture(headed=not args.headless))
