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


def test_games_past_the_cut_are_not_dropped():
    """A truncated pass REPLACES the snapshot, so dropping the tail outright
    deletes those games' flags every pass — which is what the owner saw as
    "there was plenty of games there and they disappeared". The tail keeps its
    cached ladder if it has a fresh one, and its already-parsed list-view Odds
    otherwise."""
    import inspect
    ladder = (inspect.getsource(CB._fetch_for_sport)
              .partition("if bypass_cache:")[2]
              .partition("# ── 3. Cache pruning")[0])
    tail = ladder[ladder.index("stopped_at = i - 1"):]
    assert "_cached_ladder(rest)" in tail
    assert "rest.list_odds" in tail


# ── the ladder cache ─────────────────────────────────────────────────────────

def test_the_ladder_scan_caches_in_its_own_namespace():
    """It cannot share the dashboard's: the two run different classifiers
    (permissive vs strict) over the same games, so one cache would serve each
    path the other's rows."""
    assert CB._LADDER_CACHE_NS.format("soccer") == "soccer:ladder"
    assert CB._LADDER_CACHE_NS.format("soccer") != "soccer"
    ladder = (__import__("inspect").getsource(CB._fetch_for_sport)
              .partition("if bypass_cache:")[2]
              .partition("# ── 3. Cache pruning")[0])
    assert "_LADDER_CACHE_NS.format(sport_name)" in ladder
    assert "change_cache.get_cache(lname)" in ladder
    assert "_get_sport_detail_cache(lname)" in ladder


def test_an_unmoved_game_does_not_spend_the_budget():
    """The whole point. At ~16 s/game a 500-game horizon is a 2.2-hour pass; no
    budget makes that fit, it only decides how small a prefix you see. Expanding
    only what MOVED is the same deal the dashboard path has always had, and it
    is why coverage can accumulate across passes."""
    ladder = (__import__("inspect").getsource(CB._fetch_for_sport)
              .partition("if bypass_cache:")[2]
              .partition("# ── 3. Cache pruning")[0])
    assert "lcache.needs_expansion(game.event_id, game.loadinfo)" in ladder
    gate = ladder.index("lcache.needs_expansion")
    expand = ladder.index("await _expand_game")
    assert gate < expand, "the cache check must precede the postback"
    assert "lcache.mark_loaded(game.event_id, game.loadinfo)" in ladder
    assert "ldetail[game.event_id] = detail_odds" in ladder


def test_the_ladder_cache_expires_faster_than_the_dashboard_cache():
    """A ladder anomaly is an ALT-LINE claim and an unmoved main does not prove
    an unmoved rung — it only makes it likely. So this cache may not inherit the
    6-hour bound that exists for a different purpose (surviving restarts)."""
    assert CB._LADDER_CACHE_MAX_AGE_SEC < CB._STALE_CACHE_MAX_AGE_SEC
    assert CB._LADDER_CACHE_MAX_AGE_SEC >= 30 * 60, (
        "shorter than a few scan passes and coverage can never accumulate")
    ladder = (__import__("inspect").getsource(CB._fetch_for_sport)
              .partition("if bypass_cache:")[2]
              .partition("# ── 3. Cache pruning")[0])
    assert "max_age_sec=_LADDER_CACHE_MAX_AGE_SEC" in ladder


def test_the_ladder_cache_is_pruned_against_the_whole_board():
    """Not against the in-horizon subset — a game must not lose its cached
    ladder every time the horizon happens not to reach it."""
    ladder = (__import__("inspect").getsource(CB._fetch_for_sport)
              .partition("if bypass_cache:")[2]
              .partition("# ── 3. Cache pruning")[0])
    prune = ladder.index("lcache.prune_missing(board_ids)")
    horizon_filter = ladder.index("start_within_hours is not None")
    assert prune < horizon_filter, (
        "pruning must happen before `games` is narrowed to the horizon")


# ── the budget ───────────────────────────────────────────────────────────────

def test_the_extra_scan_has_a_wall_clock_budget():
    assert A.ANOMALY_EXTRA_MAX_SEC > 0
    assert A.ANOMALY_EXTRA_MAX_SEC <= 600, (
        "a budget longer than the poll cadence is not a budget — the sport's "
        "own price cycle queues behind this")


def test_the_budget_and_the_switch_are_both_honoured():
    """Two separate stop conditions, deliberately not merged into one callable:
    the switch is a predicate the caller owns, the budget is a duration only
    the scraper can time."""
    import inspect
    src = inspect.getsource(A._anomaly_extra_loop)
    assert 'runtime_config.active("scans", "anomaly_extra")' in src
    assert "anomaly_extra_max_sec" in src, "the budget must be runtime-tunable"
    assert "max_expand_sec=" in src


def test_the_budget_is_timed_by_the_scraper_not_the_caller():
    """The bug in the first cut of this fix. A caller-computed deadline is
    spent by the two unbounded phases that precede expansion — lock wait, then
    the league tree. Measured live: soccer reached the expansion loop 889 s
    after the call under a 240 s caller-side budget and expanded 0 of 500
    games, while reporting `truncated_at: null` (a clean finish)."""
    import inspect
    caller = inspect.getsource(A._anomaly_extra_loop)
    assert "time.monotonic() + budget" not in caller, (
        "the caller cannot time a phase it does not start")
    scraper = inspect.getsource(CB._fetch_for_sport)
    ladder = scraper.partition("if bypass_cache:")[2].partition("# ── 3. Cache pruning")[0]
    assert "expand_until = (t_scan0 + max_expand_sec" in ladder, (
        "the deadline must be anchored to the start of the EXPANSION loop")
    # ...and t_scan0 must be set after the list refresh, not before the lock.
    pre = scraper.partition("if bypass_cache:")[0]
    assert "t_scan0" not in pre


def test_a_stop_on_the_very_first_game_is_reported_as_truncation():
    """`stopped_at or None` mapped a stop at game 1 (stopped_at == 0) to None,
    i.e. 'ran to completion' — which is exactly how 'expanded 0 of 500' passed
    for healthy."""
    ladder = (__import__("inspect").getsource(CB._fetch_for_sport)
              .partition("if bypass_cache:")[2]
              .partition("# ── 3. Cache pruning")[0])
    code = "\n".join(ln for ln in ladder.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert '"truncated_at": stopped_at,' in code
    assert "stopped_at or None" not in code
    assert "stopped_at: int | None = None" in code, (
        "the not-truncated case must be distinguishable from a stop at zero")


def test_the_phases_are_timed_separately():
    """Lock wait, list refresh and expansion have completely different fixes,
    so one aggregate number cannot tell you which one is hurting."""
    ladder = (__import__("inspect").getsource(CB._fetch_for_sport)
              .partition("if bypass_cache:")[2]
              .partition("# ── 3. Cache pruning")[0])
    for field in ("wait_sec", "list_sec", "expand_sec"):
        assert f'"{field}"' in ladder


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


# ── the ladder branch, driven for real ───────────────────────────────────────

def _drive_ladder_scan(monkeypatch, games, *, max_expand_sec=None,
                       expand_cost=0.0, sport_name="soccer"):
    """Run _fetch_for_sport's ladder branch against fake games, and report
    which events actually cost a postback."""
    from src.scrapers.sports import soccer as _soccer
    expanded: list[str] = []

    async def fake_refresh(*a, **kw):
        return object(), "<html/>"

    def fake_extract(html, fetched_at, *, sport):
        return list(games)

    async def fake_expand(game, fetched_at, sport, page, **kw):
        if expand_cost:
            await asyncio.sleep(expand_cost)
        expanded.append(game.event_id)
        return [f"ladder-{game.event_id}-{len(expanded)}"]

    monkeypatch.setattr(CB, "_refresh_list_html_for_sport", fake_refresh)
    monkeypatch.setattr(CB, "_extract_games_from_list_html", fake_extract)
    monkeypatch.setattr(CB, "_expand_game", fake_expand)

    odds = asyncio.run(CB._fetch_for_sport(
        _soccer, headed=False, force_detail=True, bypass_cache=True,
        classify_override=None, max_expand_sec=max_expand_sec))
    return odds, expanded


class _FakeGame:
    def __init__(self, i, *, loadinfo="v1", market_count=800):
        from datetime import datetime, timedelta, timezone
        self.event_id = f"E{i}"
        self.home, self.away = f"H{i}", f"A{i}"
        self.start_time = datetime.now(tz=timezone.utc) + timedelta(hours=1 + i)
        self.list_odds = [f"list-{self.event_id}"]
        self.loadinfo = loadinfo
        # Mid-band by default (the live soccer median is 777), so these tests
        # exercise the budget/cache behaviour rather than the market-count band
        # — that has its own file, tests/test_ladder_market_band.py.
        self.market_count = market_count


@pytest.fixture(autouse=True)
def _clean_ladder_cache():
    from src.scrapers import change_cache
    ns = CB._LADDER_CACHE_NS.format("soccer")
    change_cache.reset_cache(ns)
    CB._sport_detail_odds_caches.pop(ns, None)
    CB._last_ladder_scan.clear()
    yield
    change_cache.reset_cache(ns)
    CB._sport_detail_odds_caches.pop(ns, None)
    CB._last_ladder_scan.clear()


def test_an_unmoved_board_costs_nothing_on_the_second_pass(monkeypatch):
    """The measured fix for coverage. Pass 1 pays for every game; pass 2 pays
    for none of them, and still returns a full ladder for all of them."""
    games = [_FakeGame(i) for i in range(6)]
    odds1, exp1 = _drive_ladder_scan(monkeypatch, games)
    assert exp1 == [g.event_id for g in games], "cold pass expands everything"
    assert len(odds1) == 6

    odds2, exp2 = _drive_ladder_scan(monkeypatch, games)
    assert exp2 == [], "an unmoved board must not cost a single postback"
    assert len(odds2) == 6, "and must still yield a ladder for every game"
    assert odds2 == odds1, "served from cache, so byte-identical"
    assert CB._last_ladder_scan["soccer"]["cached"] == 6


def test_only_the_game_whose_mains_moved_is_re_expanded(monkeypatch):
    games = [_FakeGame(i) for i in range(6)]
    before, _ = _drive_ladder_scan(monkeypatch, games)
    games[3].loadinfo = "v2"          # this one's list-view odds changed
    after, expanded = _drive_ladder_scan(monkeypatch, games)
    assert expanded == ["E3"]
    assert len(after) == 6
    changed = {b for b, a in zip(before, after) if b != a}
    assert changed == {"ladder-E3-4"}, (
        "exactly the moved game gets a fresh ladder; the other five are served "
        f"unchanged from cache (got {changed})")


def test_a_truncated_pass_still_returns_the_whole_horizon(monkeypatch):
    """"There was plenty of games there and they disappeared." A pass that runs
    out of budget must narrow what got REFRESHED, not what is on screen."""
    games = [_FakeGame(i) for i in range(8)]
    _drive_ladder_scan(monkeypatch, games)            # warm the cache
    for g in games:
        g.loadinfo = "v2"                             # force re-expansion
    odds, expanded = _drive_ladder_scan(
        monkeypatch, games, max_expand_sec=0.05, expand_cost=0.02)

    assert 0 < len(expanded) < 8, "the budget must bite, and not instantly"
    st = CB._last_ladder_scan["soccer"]
    assert st["truncated_at"] == len(expanded)
    assert len(odds) == 8, "every in-horizon game is still represented"
    assert sum(1 for o in odds if o.startswith("ladder-")) == 8, (
        "the tail keeps its cached ladder rather than falling back to mains")


def test_a_cold_truncated_pass_falls_back_to_list_odds(monkeypatch):
    """With no cache to fall back on there is nothing better than the mains —
    but the games must still appear, not vanish."""
    games = [_FakeGame(i) for i in range(8)]
    odds, expanded = _drive_ladder_scan(
        monkeypatch, games, max_expand_sec=0.05, expand_cost=0.02)
    assert 0 < len(expanded) < 8
    assert len(odds) == 8
    assert any(o.startswith("list-") for o in odds)


def test_the_ladder_cache_does_not_leak_into_the_dashboard_cache(monkeypatch):
    """Different classifiers over the same games. One cache would serve each
    path the other's rows."""
    from src.scrapers import change_cache
    games = [_FakeGame(i) for i in range(3)]
    _drive_ladder_scan(monkeypatch, games)
    assert CB._get_sport_detail_cache("soccer:ladder"), "ladder cache populated"
    assert not CB._get_sport_detail_cache("soccer"), "dashboard cache untouched"
    assert not change_cache.get_cache("soccer").entries


# ── a re-verified game must STAY re-verified ─────────────────────────────────

def test_a_re_pull_is_written_back_to_the_cache(monkeypatch):
    """Owner: "why we have like 4h old arbs if we recheck them constantly".

    Because rechecking did not stick. `fetch_crystalbet_games` re-expanded the
    game and merged fresh rows into _state, but never touched the change/detail
    cache — so the next full cycle found the list-view hash unchanged, served
    the SAME cached detail rows the re-pull had just disproved, and replaced the
    slot wholesale. Measured live: a tennis game whose 4 edges all evaporated on
    a fresh price (rows_before 4 -> rows_after 0) would have had all four back,
    still stamped with their original 4h30m-old fetched_at, within one cycle.
    """
    from src.scrapers import change_cache
    from src.scrapers.sports import tennis as _tennis

    game = _FakeGame(1)
    change_cache.reset_cache("tennis")
    CB._sport_detail_odds_caches.pop("tennis", None)
    # The board is in the state the bug needs: a cached ladder from hours ago,
    # and a list-view hash that has not moved since.
    CB._get_sport_detail_cache("tennis")[game.event_id] = ["STALE"]

    async def fake_refresh(*a, **kw):
        return object(), "<html/>"

    monkeypatch.setattr(CB, "_refresh_list_html_for_sport", fake_refresh)
    monkeypatch.setattr(CB, "_extract_games_from_list_html",
                        lambda html, fa, *, sport: [game])

    async def fake_expand(g, fetched_at, sport, page, **kw):
        return ["FRESH"]

    monkeypatch.setattr(CB, "_expand_game", fake_expand)

    out = asyncio.run(CB.fetch_crystalbet_games(
        "tennis", [game.event_id], permissive=False, label="opportunity re-verify"))
    assert out == ["FRESH"]

    # The next full cycle must not be able to resurrect the stale rows.
    assert CB._get_sport_detail_cache("tennis")[game.event_id] == ["FRESH"]
    entry = change_cache.get_cache("tennis").entries.get(game.event_id)
    assert entry is not None and entry.last_expanded_at is not None
    assert not change_cache.get_cache("tennis").needs_expansion(
        game.event_id, game.loadinfo), (
        "hash unchanged, so the next cycle serves cache — which must now be the "
        "fresh rows")

    change_cache.reset_cache("tennis")
    CB._sport_detail_odds_caches.pop("tennis", None)


def test_the_write_back_respects_the_classifier_namespaces(monkeypatch):
    """Strict rows belong to the dashboard's cache, ladder_mode rows to the
    scan's. Crossing them is the contamination the namespaces exist for."""
    from src.scrapers import change_cache
    game = _FakeGame(2)

    async def fake_refresh(*a, **kw):
        return object(), "<html/>"

    async def fake_expand(g, fetched_at, sport, page, **kw):
        return ["LADDER"]

    monkeypatch.setattr(CB, "_refresh_list_html_for_sport", fake_refresh)
    monkeypatch.setattr(CB, "_extract_games_from_list_html",
                        lambda html, fa, *, sport: [game])
    monkeypatch.setattr(CB, "_expand_game", fake_expand)

    # basketball, because it is the sport that HAS a permissive classifier —
    # tennis has none (_ANOMALY_SPORT_TABLE maps it to None), so a permissive
    # call for tennis raises rather than silently downgrading to strict.
    for ns in ("basketball", "basketball:ladder"):
        change_cache.reset_cache(ns)
        CB._sport_detail_odds_caches.pop(ns, None)

    asyncio.run(CB.fetch_crystalbet_games(
        "basketball", [game.event_id], permissive=True, label="watch"))
    assert CB._get_sport_detail_cache("basketball:ladder") == {game.event_id: ["LADDER"]}
    assert not CB._get_sport_detail_cache("basketball"), (
        "permissive rows must never land in the strict dashboard cache")

    for ns in ("basketball", "basketball:ladder"):
        change_cache.reset_cache(ns)
        CB._sport_detail_odds_caches.pop(ns, None)


def test_a_failed_re_expansion_does_not_poison_the_cache(monkeypatch):
    """An expansion that returns nothing is not evidence the markets changed —
    it must leave the previous cached rows alone rather than marking the game
    loaded with no data."""
    from src.scrapers import change_cache
    game = _FakeGame(3)
    change_cache.reset_cache("tennis")
    CB._sport_detail_odds_caches.pop("tennis", None)
    CB._get_sport_detail_cache("tennis")[game.event_id] = ["PREVIOUS"]

    async def fake_refresh(*a, **kw):
        return object(), "<html/>"

    async def fake_expand(g, fetched_at, sport, page, **kw):
        return []

    monkeypatch.setattr(CB, "_refresh_list_html_for_sport", fake_refresh)
    monkeypatch.setattr(CB, "_extract_games_from_list_html",
                        lambda html, fa, *, sport: [game])
    monkeypatch.setattr(CB, "_expand_game", fake_expand)

    out = asyncio.run(CB.fetch_crystalbet_games(
        "tennis", [game.event_id], permissive=False, label="opportunity re-verify"))
    assert out == game.list_odds
    assert CB._get_sport_detail_cache("tennis")[game.event_id] == ["PREVIOUS"]

    change_cache.reset_cache("tennis")
    CB._sport_detail_odds_caches.pop("tennis", None)


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
