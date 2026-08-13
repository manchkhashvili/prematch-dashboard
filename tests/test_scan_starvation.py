"""The ladder scan must not be able to wedge a whole sport (2026-08-13).

Owner's report: "anomalies are almost empty after 30min run, and also why do we
have 40 min age games if we refresh that games having 3%+ arbs". One cause for
both, measured on the running dashboard:

  * `anomaly_extra_horizon_h` had been raised 12 -> 48 in the Config tab. On the
    live CB soccer board that is 476 games in horizon instead of 135;
  * `_fetch_for_sport`'s bypass_cache branch expands every one of them while
    HOLDING the per-sport CB lock, and — unlike the normal branch — it accepted
    a `should_continue` callable and then never called it. The sweep was
    literally unstoppable;
  * so it held the soccer lock for 47 minutes. In that window: CB soccer ran ONE
    price cycle (07:29, then nothing — poll_cycles), every +EV row on the Arbs
    tab was scored off a 43-minute-old price against a 10-minute-old Pinnacle
    fair, the opportunity re-verify loop blocked on the same lock and reported
    `at: null` after an hour of uptime, and the tennis / americanfootball extra
    scans — which the loop only reaches after soccer RETURNS — never ran at all,
    which is why the Anomalies tab was empty.

The horizon says how much is worth scanning. It never said how long the rest of
the board may be made to wait for it, and that is what these tests pin down.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from src import app as A
from src.scrapers import crystalbet as CB


# ── the sweep is stoppable ───────────────────────────────────────────────────

def test_the_ladder_path_consults_should_continue():
    """The regression itself. The bypass_cache branch took the callable as a
    parameter and never invoked it, so both the Config-tab switch and any
    deadline built on top of it were dead letters."""
    import inspect
    src = inspect.getsource(CB._fetch_for_sport)
    head, _, tail = src.partition("if bypass_cache:")
    ladder, _, normal = tail.partition("# ── 3. Cache pruning")
    assert "should_continue" in ladder, (
        "the ladder/anomaly expansion loop must be abortable — it is the branch "
        "that holds the sport lock for minutes at a time")
    assert "should_continue is not None and not should_continue()" in ladder


def test_the_ladder_path_checks_every_game_not_every_progress_block():
    """The normal path checks on the _PROGRESS_LOG_EVERY boundary, which is
    fine for a 0.1 s cache hit and useless for a deadline: a CB soccer ladder
    expansion is seconds, so 25-game granularity overshoots by minutes."""
    import inspect
    src = inspect.getsource(CB._fetch_for_sport)
    ladder = src.partition("if bypass_cache:")[2].partition("# ── 3. Cache pruning")[0]
    stmt = [ln.strip() for ln in ladder.splitlines()
            if "not should_continue()" in ln]
    assert stmt, "no abort statement on the ladder path"
    assert not any("_PROGRESS_LOG_EVERY" in ln or "%" in ln for ln in stmt), (
        "the abort check must not be gated on the progress boundary: "
        f"{stmt}")
    # ...and the normal path is deliberately left on the cheaper boundary,
    # where an iteration is a cache hit rather than a postback.
    normal = src.partition("# ── 3. Cache pruning")[2]
    assert any("_PROGRESS_LOG_EVERY" in ln
               for ln in normal.splitlines() if "should_continue" in ln)


def test_games_past_the_cut_keep_their_list_view_odds():
    """A truncated pass REPLACES the snapshot, so dropping the tail outright
    would delete those games' flags every pass. Their list-view Odds cost
    nothing — they are already parsed — and keep the board-level checks alive."""
    import inspect
    ladder = (inspect.getsource(CB._fetch_for_sport)
              .partition("if bypass_cache:")[2]
              .partition("# ── 3. Cache pruning")[0])
    stop = ladder.index("not should_continue()")
    assert "rest.list_odds" in ladder[stop:stop + 900]


# ── the budget ───────────────────────────────────────────────────────────────

def test_the_extra_scan_has_a_wall_clock_budget():
    assert A.ANOMALY_EXTRA_MAX_SEC > 0
    assert A.ANOMALY_EXTRA_MAX_SEC <= 600, (
        "a budget longer than the poll cadence is not a budget — the sport's "
        "own price cycle queues behind this")


def test_the_budget_and_the_switch_are_both_honoured():
    """The callable the loop builds must go False for EITHER reason."""
    import inspect
    src = inspect.getsource(A._anomaly_extra_loop)
    assert 'runtime_config.active("scans", "anomaly_extra")' in src
    assert "time.monotonic() <" in src
    assert "anomaly_extra_max_sec" in src, "the budget must be runtime-tunable"


def test_a_deadline_callable_flips_when_it_is_spent():
    """Behavioural check on the exact lambda shape the loop uses."""
    deadline = time.monotonic() + 0.05
    should_continue = (lambda dl=deadline: time.monotonic() < dl)
    assert should_continue() is True
    time.sleep(0.06)
    assert should_continue() is False


def test_expansion_stops_at_the_deadline_and_keeps_the_tail():
    """The loop body's shape: 100 games, a budget that expires after a handful,
    and the result must still cover all 100 events — expanded rows for the
    prefix, list-view rows for the rest."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(tz=timezone.utc)

    class _Game:
        def __init__(self, i):
            self.event_id = str(i)
            self.home, self.away = f"H{i}", f"A{i}"
            self.start_time = now + timedelta(hours=1 + i)
            self.list_odds = [f"list-{i}"]

    games = [_Game(i) for i in range(100)]
    expanded: list[str] = []

    async def fake_expand(game, *a, **kw):
        await asyncio.sleep(0.01)
        expanded.append(game.event_id)
        return [f"detail-{game.event_id}"]

    async def _run():
        deadline = time.monotonic() + 0.05
        out: list[str] = []
        stopped = 0
        for i, game in enumerate(games, 1):
            if not (time.monotonic() < deadline):
                stopped = i - 1
                out.extend(g for rest in games[i - 1:] for g in rest.list_odds)
                break
            out.extend(await fake_expand(game))
        return out, stopped

    out, stopped = asyncio.run(_run())

    assert 0 < stopped < 100, "the budget must bite, and must not bite instantly"
    assert len(expanded) == stopped
    assert len(out) == 100, "every in-horizon game is still represented"
    assert out[0] == "detail-0", "the soonest kickoffs are the ones expanded"
    assert out[-1] == "list-99"


# ── the cost is reportable ───────────────────────────────────────────────────

def test_the_sweep_records_what_the_horizon_cost():
    """Raising the horizon used to have no visible price until the board went
    quiet. games-in-horizon / expanded / truncated-at / seconds makes the knob
    answerable."""
    CB._last_ladder_scan.clear()
    CB._last_ladder_scan["soccer"] = {
        "at": "2026-08-13T08:00:00+00:00", "in_horizon": 476, "expanded": 41,
        "fallback": 0, "truncated_at": 41, "sec": 240.0,
    }
    stats = CB.last_ladder_scan_stats()
    assert set(stats["soccer"]) >= {"in_horizon", "expanded", "truncated_at", "sec"}
    stats["soccer"]["expanded"] = 999
    assert CB._last_ladder_scan["soccer"]["expanded"] == 41, "must hand out a copy"
    CB._last_ladder_scan.clear()


def test_the_anomalies_api_surfaces_the_cost():
    import inspect
    src = inspect.getsource(A.api_anomalies) if hasattr(A, "api_anomalies") else ""
    if not src:
        src = inspect.getsource(A)
    assert '"cost": _cb_last_ladder_scan_stats()' in src
    assert '"max_sec"' in src


# ── nothing may block on a busy sport ────────────────────────────────────────

def test_sport_busy_reports_the_lock_and_never_raises():
    lock = CB._get_sport_lock(CB.soccer.SPORT_ID)
    assert CB.sport_busy("soccer") is False
    assert CB.sport_busy("quidditch") is False, "an unknown sport is not busy"

    async def _hold():
        async with lock:
            return CB.sport_busy("soccer")

    assert asyncio.run(_hold()) is True
    assert CB.sport_busy("soccer") is False


def test_the_reverify_loop_skips_a_busy_sport_instead_of_queueing():
    """It iterates sports in dict order and awaits each fetch. Awaiting the CB
    lock is an unbounded wait, so one wedged sport starved every other sport AND
    every later tick — the loop had not completed a pass in an hour."""
    import inspect
    src = inspect.getsource(A._opportunity_reverify_loop)
    assert "cb_sport_busy(sport)" in src
    busy = src.index("cb_sport_busy(sport)")
    fetch = src.index("await fetch_crystalbet_games")
    assert busy < fetch, "the probe is worthless after the blocking call"
    assert "skipped_busy" in src


def test_the_reverify_loop_always_stamps_its_last_run():
    """`at: null` was the only symptom of the starvation and was
    indistinguishable from 'this loop was never started'. A tick that found
    nothing to do must still say it ran."""
    import inspect
    src = inspect.getsource(A._opportunity_reverify_loop)
    assert "if not by_sport:\n                continue" not in src, (
        "an empty shortlist must not skip publishing the stats")
    assert "candidates" in src
    assert A.opportunity_reverify_stats() is not A._opp_reverify_stats


# ── an aborted sweep must not overwrite a good snapshot ──────────────────────

def test_an_aborted_scan_keeps_the_previous_snapshot():
    """Now that the sweep is abortable, hitting Pause mid-sweep would otherwise
    publish the stump it got as far as — emptying the tab as a side effect of
    pausing."""
    import inspect
    for fn in (A._compute_anomalies, A._anomaly_extra_loop):
        src = inspect.getsource(fn)
        assert "switched off mid-sweep" in src, f"{fn.__name__} publishes a stump"
