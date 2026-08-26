"""
The pass, and the loop around it.

CADENCE
-------
The brief was "every 1 hour ... its not necessary to be exactly 1 hour and it
can't be, but just for avoiding endless cycles". So the loop schedules from the
START of the previous pass, not its end: sleep until `started + interval`, and
if the pass already overran that, start the next one immediately — but never
closer together than `min_gap`. That floor is the anti-runaway: if CrystalBet
starts taking 90 minutes a pass, the collector settles into back-to-back passes
at the book's own speed instead of spinning up an ever-growing backlog, and the
`dur_ms` on each snapshot row makes the overrun visible rather than silent.

Both books run concurrently. They are different hosts with different transports
and neither contends with the other; serialising them would roughly double the
wall clock for no benefit. Within CrystalBet, sports run `--cb-concurrency` at a
time, because CrystalBet serialises concurrent postbacks inside one ASP.NET
session and the only real parallelism is across sessions.

WHAT A PASS COSTS (measured 2026-08-26)
---------------------------------------
    Lider-Bet     25 sports,  2,857 matches   ~90 s      ~0.8 GB transport
    CrystalBet    33 sports,  4,242 games     ~8-15 min  ~4 GB   transport

The transport figure is the honest one and it is large: a CrystalBet expand
re-renders the whole games panel, so each of 4,242 expands drags back ~1.5 MB
of markup for one game's markets. docs/performance.md describes the fix
(keep each session's view to 1-2 championships, ~350 KB/expand) and it is the
first optimisation to reach for if the bandwidth bites.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sportsdatamovement import crystalbet, liderbet
from sportsdatamovement.store import Store, get_store

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 3600.0
DEFAULT_MIN_GAP_SEC = 300.0


def next_sleep(elapsed: float, interval: float, min_gap: float) -> float:
    """How long to wait before the next pass, given how long this one took.

    Scheduling from the pass START keeps the cadence on an hourly grid when
    passes are quick, and `min_gap` stops it degenerating into a hot loop when
    they are not. See the module docstring.
    """
    return max(min_gap, interval - elapsed)


async def run_pass(store: Store, *, books: tuple[str, ...] = ("liderbet", "crystalbet"),
                   cb_concurrency: int = 3, max_start_days: float = 0.0,
                   skip_simulated: bool = True, max_events: int = 0,
                   cb_sports: list[int] | None = None,
                   lider_sections: list[str] | None = None) -> dict:
    """One snapshot of everything. Returns per-book results plus totals."""
    t0 = time.monotonic()
    started = datetime.now(timezone.utc)
    jobs = {}
    if "liderbet" in books:
        jobs["liderbet"] = liderbet.collect(
            store, skip_simulated=skip_simulated, max_start_days=max_start_days,
            max_matches=max_events, sections=lider_sections)
    if "crystalbet" in books:
        jobs["crystalbet"] = crystalbet.collect(
            store, sports=cb_sports, concurrency=cb_concurrency,
            max_start_days=max_start_days, max_games=max_events)

    done = await asyncio.gather(*jobs.values(), return_exceptions=True)
    out: dict = {"started_at": started.isoformat(timespec="seconds"),
                 "sec": round(time.monotonic() - t0, 1), "books": {}}
    totals = {"events": 0, "positions": 0, "new": 0, "moved": 0, "gone": 0,
              "dupes": 0, "bytes": 0, "errors": 0}
    for book, res in zip(jobs, done):
        if isinstance(res, BaseException):
            log.error("%s pass failed: %s", book, res)
            out["books"][book] = {"error": str(res)}
            totals["errors"] += 1
            continue
        out["books"][book] = res
        for r in res:
            if r.get("error"):
                totals["errors"] += 1
                continue
            totals["events"] += r.get("n_events", 0)
            totals["positions"] += r.get("n_positions", 0)
            totals["new"] += r.get("n_new", 0)
            totals["moved"] += r.get("n_moved", 0)
            totals["gone"] += r.get("n_gone", 0)
            totals["dupes"] += r.get("n_dupes", 0)
            totals["bytes"] += r.get("bytes", 0)
    out["totals"] = totals
    return out


async def run_loop(store: Store, *, interval_sec: float = DEFAULT_INTERVAL_SEC,
                   min_gap_sec: float = DEFAULT_MIN_GAP_SEC,
                   passes: int = 0, prune_days: float = 0.0, **kw) -> None:
    """Repeat `run_pass` on the cadence described in the module docstring.

    `passes=0` runs until interrupted.
    """
    n = 0
    while True:
        n += 1
        start = time.monotonic()
        log.info("=== pass %d starting ===", n)
        try:
            res = await run_pass(store, **kw)
            t = res["totals"]
            log.info("=== pass %d done in %.0fs: %d events, %d positions, "
                     "%d new / %d moved / %d gone, %.2f GB, %d errors ===",
                     n, res["sec"], t["events"], t["positions"], t["new"],
                     t["moved"], t["gone"], t["bytes"] / 1e9, t["errors"])
            if t["dupes"]:
                log.warning("%d colliding position keys this pass — a "
                            "collector key bug, not movement", t["dupes"])
            log.info("db: %s", store.stats())
        except Exception:
            log.exception("pass %d blew up; the loop continues", n)

        if prune_days > 0:
            try:
                dropped = await asyncio.to_thread(store.prune, prune_days)
                if dropped["events"]:
                    log.info("pruned %s", dropped)
            except Exception:
                log.exception("prune failed")

        if passes and n >= passes:
            return

        elapsed = time.monotonic() - start
        sleep_for = next_sleep(elapsed, interval_sec, min_gap_sec)
        if elapsed > interval_sec:
            log.warning("pass %d took %.0fs, longer than the %.0fs interval — "
                        "next pass in %.0fs (min gap)", n, elapsed,
                        interval_sec, sleep_for)
        log.info("sleeping %.0fs until the next pass", sleep_for)
        await asyncio.sleep(sleep_for)


def open_store(path: str | None = None) -> Store:
    return get_store(path)


@contextmanager
def exclusive(store: Store):
    """Refuse to collect if another collector already is.

    Two loops against one database do not crash — they interleave, and the
    damage is silent and total: each pass reads a `latest` the other has just
    updated, so both attribute the other's movement to themselves and neither
    change count means anything. It happened here on 2026-08-26 because a
    `pkill -f "python -m sportsdatamovement"` missed a process whose argv reads
    `/…/Python -m sportsdatamovement` — capital P — and three hours of change
    data had to be thrown away.

    An flock on a file beside the database is the cheap fix: the lock dies with
    the process, so a crashed collector leaves nothing to clean up. Read-only
    commands do not take it.
    """
    import fcntl

    path = Path(str(store.path) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("w")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(
                f"another collector is already running against {store.path}\n"
                f"(lock held on {path}) — stop it first, or pass --db to use a "
                f"separate database.")
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
        except Exception:
            pass
