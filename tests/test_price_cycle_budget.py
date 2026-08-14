"""Wall-clock budget on the CB PRICE cycle (2026-08-14).

Owner asked me to watch the running app "for a while to detect whats going on".
What it was doing:

    cb/soccer  first completed cycle after restart: 3365 s — 56 minutes
    every other book+sport in the same window: 3-7 completed cycles

Sixteen minutes of polling showed soccer flat at 0 rows, then 16 431 in one
step. It is not new and not a regression from this session's work — from the
owner's own ticks.db, the last 400 cb/soccer cycles:

    p50 90 s | p90 643 s | max 3895 s
    40 % over 2 min, 11 % over 10 min, 1.8 % over 30 min
    56 % of ALL cb/soccer wall time sits inside cycles longer than 10 min

The cycle holds the per-sport CB lock for its whole duration, so during one of
those the Arbs tab has no CB soccer, the soccer ladder scan cannot get the lock
(no soccer flags at all — `extra.cost` had no soccer entry after 45 minutes),
and the opportunity re-verify skips the sport. The variance is cache-driven: a
warm change cache makes most games a hit and the cycle takes 90 s; after a
restart they are misses at seconds each and it runs for an hour.

The ladder scan got a budget earlier the same day. This is the branch that
actually stops the board, and it had none.

Truncating is safe HERE specifically because the fallback already exists: a game
we do not reach serves its cached detail (if still fresh) or its list-view Odds,
so the board stays complete with fewer alt-lines for a cycle. And every
successful expansion is marked in the change cache, so the next cycle finds
those games are hits and spends its budget further down — progress accumulates
instead of restarting from the top.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from datetime import datetime, timedelta, timezone

import pytest

from src import app as A
from src.scrapers import crystalbet as CB


class _G:
    def __init__(self, i, *, loadinfo="v1"):
        self.event_id = f"E{i}"
        self.home, self.away = f"H{i}", f"A{i}"
        self.league = "L"
        self.start_time = datetime.now(tz=timezone.utc) + timedelta(hours=1)
        self.list_odds = [f"list-{self.event_id}"]
        self.loadinfo = loadinfo
        self.market_count = 300


@pytest.fixture(autouse=True)
def _clean():
    from src.scrapers import change_cache
    change_cache.reset_cache("soccer")
    CB._sport_detail_odds_caches.pop("soccer", None)
    CB._last_price_cycle.clear()
    yield
    change_cache.reset_cache("soccer")
    CB._sport_detail_odds_caches.pop("soccer", None)
    CB._last_price_cycle.clear()


def _run(monkeypatch, games, *, max_expand_sec=None, expand_cost=0.0):
    from src.scrapers.sports import soccer as _soccer
    expanded: list[str] = []

    async def fake_refresh(*a, **k):
        return object(), "<html/>"

    async def fake_expand(g, *a, **k):
        if expand_cost:
            await asyncio.sleep(expand_cost)
        expanded.append(g.event_id)
        return [f"detail-{g.event_id}"]

    monkeypatch.setattr(CB, "_refresh_list_html_for_sport", fake_refresh)
    monkeypatch.setattr(CB, "_extract_games_from_list_html",
                        lambda html, fa, *, sport: list(games))
    monkeypatch.setattr(CB, "_expand_game", fake_expand)
    odds = asyncio.run(CB._fetch_for_sport(
        _soccer, headed=False, max_expand_sec=max_expand_sec))
    return odds, expanded


# ── the budget binds ─────────────────────────────────────────────────────────

def test_the_cycle_stops_at_the_budget(monkeypatch):
    games = [_G(i) for i in range(40)]
    odds, expanded = _run(monkeypatch, games, max_expand_sec=0.05,
                          expand_cost=0.02)
    assert 0 < len(expanded) < 40, "must bite, and not instantly"
    st = CB._last_price_cycle["soccer"]
    assert st["past_budget"] == 40 - len(expanded)
    assert st["budget_sec"] == 0.05


def test_no_game_disappears_when_the_budget_is_spent(monkeypatch):
    """The whole safety argument. A truncated cycle must still describe the
    entire board — this feeds the Arbs tab."""
    games = [_G(i) for i in range(40)]
    odds, expanded = _run(monkeypatch, games, max_expand_sec=0.05,
                          expand_cost=0.02)
    covered = {o.split("-", 1)[1] for o in odds}
    assert covered == {g.event_id for g in games}, "every game still represented"
    assert len(odds) == 40


def test_unreached_games_prefer_cached_detail_over_list_view(monkeypatch):
    """A game we ran out of time for should not lose its alt-lines if we still
    have fresh ones — list view is the fallback of last resort."""
    games = [_G(i) for i in range(30)]
    _run(monkeypatch, games)                      # warm: everything expanded
    for g in games:
        g.loadinfo = "v2"                         # force re-expansion
    odds, expanded = _run(monkeypatch, games, max_expand_sec=0.05,
                          expand_cost=0.02)
    assert len(expanded) < 30
    assert sum(1 for o in odds if o.startswith("detail-")) == 30, (
        "the tail served cached DETAIL, not list-view mains")


def test_progress_accumulates_across_cycles(monkeypatch):
    """The property that makes truncation converge rather than starve: an
    expanded game is marked in the change cache, so the next cycle finds it a
    hit and spends its budget on games further down."""
    games = [_G(i) for i in range(30)]
    _, first = _run(monkeypatch, games, max_expand_sec=0.04, expand_cost=0.02)
    _, second = _run(monkeypatch, games, max_expand_sec=0.04, expand_cost=0.02)
    assert first and second
    assert set(first).isdisjoint(second), (
        "the second cycle must move PAST what the first already expanded, "
        f"got first={first} second={second}")


@pytest.mark.parametrize("budget", [0, None])
def test_zero_and_none_mean_unlimited(monkeypatch, budget):
    """The pre-2026-08-14 behaviour has to remain reachable. Parametrised
    rather than run twice in one test: the first run warms the change cache, so
    a second run in the same test would legitimately expand nothing and the
    assertion would be measuring the cache, not the budget."""
    games = [_G(i) for i in range(12)]
    _, expanded = _run(monkeypatch, games, max_expand_sec=budget, expand_cost=0.005)
    assert len(expanded) == 12
    assert CB._last_price_cycle["soccer"]["past_budget"] == 0


# ── wiring ───────────────────────────────────────────────────────────────────

def test_every_price_fetcher_accepts_and_forwards_the_budget():
    for sport in ("soccer", "tennis", "basketball", "americanfootball"):
        fn = getattr(CB, f"fetch_crystalbet_{sport}_prematch")
        assert "max_expand_sec" in inspect.signature(fn).parameters, sport
        assert "max_expand_sec=max_expand_sec" in inspect.getsource(fn), sport


def test_the_poll_loop_passes_the_configured_budget():
    src = inspect.getsource(A._crystalbet_loop_for_sport)
    assert 'runtime_config.num(\n                        "limits", "cb_expand_max_sec", 300.0)' in src \
        or '"cb_expand_max_sec"' in src


def test_the_budget_is_runtime_tunable_and_can_be_switched_off():
    from src import runtime_config as rc
    assert "cb_expand_max_sec" in rc.LIMITS
    assert rc.LIMITS["cb_expand_max_sec"][0]() == 300.0
    assert rc.LIMITS["cb_expand_max_sec"][1] == 0.0, "0 = unlimited must be settable"


def test_the_cycle_shape_is_reported():
    """A 56-minute cycle was only diagnosable from ticks.db after the fact."""
    CB._last_price_cycle["soccer"] = {"games": 1, "expanded": 1, "sec": 1.0}
    out = CB.last_price_cycle_stats()
    out["soccer"]["games"] = 999
    assert CB._last_price_cycle["soccer"]["games"] == 1, "must hand out a copy"
    assert '"cb_cycles": _cb_last_price_cycle_stats()' in inspect.getsource(A)


def test_it_applies_to_every_sport_unlike_the_market_band():
    """A time bound can only bind on the tail — basketball's median cycle is
    27s and tennis's 109s, so neither reaches a 300s budget. That is why this
    one is global where the market-count ceiling had to be opted into."""
    src = inspect.getsource(CB._fetch_for_sport)
    normal = src.partition("# ── 3. Cache pruning")[2]
    assert "expand_until" in normal
    assert "_LADDER" not in normal.split("expand_until")[1][:400], (
        "the price budget must not be entangled with the ladder-scan band")
