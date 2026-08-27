"""Move CrystalBet's html5lib parse off the event loop, into worker processes.

WHY, measured rather than assumed (2026-08-14):

  * CB's detail parse is `BeautifulSoup(html, "html5lib")` + `cb_detail`, and
    html5lib is pure Python. Threads therefore do nothing — 4 pages on 4
    threads measured SLOWER than on 1 (0.88x) because the GIL serialises them
    anyway. `asyncio.to_thread`, which the transport already used, moves the
    work off the loop's *stack* but not off its *thread of execution*.
  * On an unloaded box a CB soccer expand+parse is 0.85 s. On the owner's
    running dashboard — six books, four sports, three ladder scans, Pinnacle
    matching, all on one interpreter — the same work measured **10.1 s**. A 12x
    contention factor, and it is what stops the soccer ladder scan covering
    more than 24 of 919 games in a 300 s budget.
  * Processes do help: 8 pages serial 8.35 s, on 6 processes 3.38 s (2.47x),
    with byte-identical results. The raw speedup is the smaller prize; the
    real one is that the parse stops blocking every other loop in the process.

WHY NOT lxml, which would have been simpler: it does not produce the same Odds
on CB's markup. Tested live on 25 games, 0/15 soccer and 0/10 basketball
matched — lxml over-collects on the unclosed `<td>`s (one game: 59 markets
against 108). Saved fixtures DO match, which is a trap: those are
`page.content()` captures, i.e. already browser-repaired, so they cannot test
raw panel HTML. The docstring in cb_http was right.

DESIGN. A BeautifulSoup tree cannot cross a process boundary, so the split has
to be bytes-in / Odds-out: the transport returns the raw delta string, the
worker owns normalise + parse, and `Odds` (a dataclass) pickles back. The
worker takes a classifier NAME rather than a function, since functions do not
pickle.

SAFETY. Any failure — pool broken, worker crash, pickling problem — falls back
to parsing in-process, which is exactly the old behaviour. A parse pool is an
optimisation and must never be a new way to lose data.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime
from typing import Any, Optional

log = logging.getLogger(__name__)

# 0 disables the pool entirely (parse in-process, the pre-2026-08-14 path).
# Default 3: the win is mostly in unblocking the loop rather than in raw
# parallelism, and every worker re-imports the scraper stack, so a big pool
# costs memory for little extra throughput.
def _default_procs() -> int:
    """3 in production, 0 under pytest.

    A worker spawns a fresh interpreter and re-imports the scraper stack, which
    is seconds per pool and turns a fast unit suite into a slow flaky one. The
    in-process path is the same code — parity is verified live against the pool
    (21/21 games, 1274 Odds, both classifiers) rather than by making every test
    pay for a fork. An explicit CB_PARSE_PROCS always wins, so a test that
    wants the pool can ask for it.
    """
    raw = os.environ.get("CB_PARSE_PROCS")
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            return 0
    if "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules:
        return 0
    return 3


PARSE_PROCS = _default_procs()

_pool: Any = None
_pool_broken = False


def _get_pool():
    """Lazily create the pool. Returns None when disabled or unavailable."""
    global _pool, _pool_broken
    if PARSE_PROCS <= 0 or _pool_broken:
        return None
    if _pool is None:
        try:
            from concurrent.futures import ProcessPoolExecutor
            _pool = ProcessPoolExecutor(max_workers=PARSE_PROCS)
            log.info("CB parse pool: %d worker processes", PARSE_PROCS)
        except Exception:
            log.exception("CB parse pool unavailable — parsing in-process")
            _pool_broken = True
            return None
    return _pool


def shutdown() -> None:
    global _pool
    if _pool is not None:
        try:
            _pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        _pool = None


# ── the worker ───────────────────────────────────────────────────────────────
# Module-level and importable: a spawned worker re-imports this module, so the
# callable cannot be a closure or a lambda.

def _classifier(sport_name: str, mode: str):
    from src.scrapers.sports import (americanfootball, basketball, icehockey,
                                     soccer, tennis)
    mod = {"soccer": soccer, "basketball": basketball, "tennis": tennis,
           "americanfootball": americanfootball,
           "icehockey": icehockey}[sport_name]
    if mode == "permissive":
        return getattr(mod, "classify_market_title_permissive",
                       mod.classify_market_title)
    return mod.classify_market_title


def parse_detail_job(job: dict) -> list:
    """Normalise + parse one detail blob. Runs in a worker process."""
    from src.scrapers import cb_detail, cb_http
    soup = cb_http.normalize_soup(job["html"])
    if soup.select_one("table.game-details") is None:
        # Same transport-failure signal the in-process path raises on.
        raise RuntimeError(
            f"ExpandDetail returned no detail table for {job['event_id']}")
    return cb_detail.parse_detail_page(
        soup,
        event_id=job["event_id"],
        home=job["home"], away=job["away"],
        league=job["league"],
        start_time=job["start_time"],
        fetched_at=job["fetched_at"],
        sport_name=job["sport_name"],
        classify=_classifier(job["sport_name"], job["classify_mode"]),
        scope_to_event=job["scope_to_event"],
        per_section=job["per_section"],
    )


# ── the async entry point ────────────────────────────────────────────────────

async def parse_detail(
    html: str, *, event_id: str, home: str, away: str, league: Optional[str],
    start_time: Optional[datetime], fetched_at: datetime, sport_name: str,
    classify_mode: str, scope_to_event: bool = True, per_section: bool = False,
) -> list:
    """Parse a detail blob in a worker process, falling back in-process.

    Raises whatever the parse raises (notably the no-detail-table
    RuntimeError), because callers distinguish transport failure from an empty
    result. Only POOL problems are swallowed.
    """
    job = {
        "html": html, "event_id": event_id, "home": home, "away": away,
        "league": league, "start_time": start_time, "fetched_at": fetched_at,
        "sport_name": sport_name, "classify_mode": classify_mode,
        "scope_to_event": scope_to_event, "per_section": per_section,
    }
    pool = _get_pool()
    if pool is not None:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(pool, parse_detail_job, job)
        except RuntimeError:
            raise                       # the parser's own signal — propagate
        except Exception as e:
            # BrokenProcessPool, pickling trouble, worker died: degrade to
            # in-process rather than lose the game.
            global _pool_broken, _pool
            log.warning("CB parse pool failed (%s: %s) — falling back "
                        "in-process for the rest of this run",
                        type(e).__name__, e)
            _pool_broken = True
            _pool = None
    return parse_detail_job(job)


# ── list view ────────────────────────────────────────────────────────────────
# The heavier of the two: the soccer list panel is ~13.6 MB and its normalise
# is seconds of solid GIL, once per cycle per sport. It returns _GameOnList
# dataclasses (each holding its list-view Odds), which pickle back fine.

def parse_list_job(job: dict) -> list:
    """Normalise + extract the list view. Runs in a worker process."""
    from src.scrapers import crystalbet, cb_http
    from src.scrapers.sports import (americanfootball, basketball, icehockey,
                                     soccer, tennis)
    mod = {"soccer": soccer, "basketball": basketball, "tennis": tennis,
           "americanfootball": americanfootball,
           "icehockey": icehockey}[job["sport_name"]]
    soup = cb_http.normalize_soup(job["html"])
    return crystalbet._extract_games_from_list_html(
        soup, job["fetched_at"], sport=mod)


async def parse_list(html: str, sport_name: str, fetched_at: datetime) -> list:
    """Parse a list panel in a worker process, falling back in-process."""
    job = {"html": html, "sport_name": sport_name, "fetched_at": fetched_at}
    pool = _get_pool()
    if pool is not None:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(pool, parse_list_job, job)
        except Exception as e:
            global _pool_broken, _pool
            log.warning("CB parse pool failed on list (%s: %s) — falling back "
                        "in-process for the rest of this run",
                        type(e).__name__, e)
            _pool_broken = True
            _pool = None
    return parse_list_job(job)
