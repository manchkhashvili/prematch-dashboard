"""Short TTL on the expensive read endpoints (2026-08-15).

The find that ended a very long day. The owner's soccer ladder scan was getting
23-37 s per game against 1.1 s standalone, and every fix aimed at the scanner
missed, because the scanner was not slow — it was starved by the dashboard
watching it.

`/api/opportunities` costs ~25 s of pure Python (match_events is 2.6 s per
book/sport pair, and _compute_opportunities_now does every enabled soft book x
every sport). `static/alerts.js` runs on EVERY dashboard page and polls it every
30 s, per tab, independently. Two or three tabs is a 10-second effective poll of
a 25-second computation, and the loop never gets out from under it.

Measured on the same process, nothing changed but closing the browser tabs:

                       UI open      UI closed
    /api/config         8-17 s       0.004 s
    /api/anomalies         66 s      0.035 s
    app CPU               97 %         0.6 %

So the cache is not a micro-optimisation, it is what makes the dashboard safe to
leave open — which is the actual requirement. The underlying odds only change
when a book polls (60-600 s), so a few seconds of TTL costs no freshness anyone
could perceive.
"""
from __future__ import annotations

import time

import pytest

from src import app as A
from src import runtime_config as rc



@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "CONFIG_PATH", tmp_path / "rc.json")
    rc.load(force=True)
    A._ttl_cache.clear()
    A._paused_result_cache.clear()
    yield
    A._ttl_cache.clear()
    A._paused_result_cache.clear()
    rc.load(force=True)


def test_repeat_calls_share_one_computation():
    rc.update({"limits": {"api_cache_sec": 30}})
    calls = []

    def compute():
        calls.append(1)
        return ["result", len(calls)]

    first = A._paused_memo(("k",), compute)
    for _ in range(20):
        assert A._paused_memo(("k",), compute) == first
    assert len(calls) == 1, (
        "20 pollers must cost ONE computation, not 20 — this is the whole point")


def test_the_cache_expires():
    calls = []
    rc.update({"limits": {"api_cache_sec": 0.05}})
    A._paused_memo(("k",), lambda: calls.append(1))
    time.sleep(0.08)
    A._paused_memo(("k",), lambda: calls.append(1))
    assert len(calls) == 2, "a stale entry must not be served forever"


def test_different_queries_do_not_collide():
    """The key carries the query params — a min_edge=3 poll must not be served
    a min_edge=10 result."""
    rc.update({"limits": {"api_cache_sec": 30}})
    out = {}
    for edge in (3, 10):
        out[edge] = A._paused_memo(("opportunities", edge), lambda e=edge: f"rows-{e}")
    assert out[3] == "rows-3" and out[10] == "rows-10"


def test_zero_disables_the_cache():
    calls = []
    rc.update({"limits": {"api_cache_sec": 0}})
    for _ in range(3):
        A._paused_memo(("k",), lambda: calls.append(1))
    assert len(calls) == 3, "0 must mean compute every time (the old behaviour)"


def test_pause_still_caches_for_the_whole_pause():
    """Pause caching predates this and must survive: while paused the board is
    frozen, so recomputing is pure waste."""
    calls = []
    rc.set_paused(True)
    for _ in range(5):
        A._paused_memo(("k",), lambda: calls.append(1))
    assert len(calls) == 1
    rc.set_paused(False)
    A._paused_memo(("k",), lambda: calls.append(1))
    assert len(calls) == 2, "resuming must recompute"


def test_the_cache_cannot_grow_without_bound():
    """Keys carry query params, so a curious user hitting the API with varied
    parameters must not be able to grow this dict forever."""
    rc.update({"limits": {"api_cache_sec": 30}})
    for i in range(200):
        A._paused_memo(("opportunities", i), lambda i=i: i)
    assert len(A._ttl_cache) <= 64


def test_the_expensive_endpoints_route_through_it():
    """/api/opportunities is the 25s one and the one alerts.js polls from every
    page; it must not bypass the cache."""
    import inspect
    src = inspect.getsource(A)
    assert '("opportunities", min_edge, kind, book, ref)' in src
    assert "_paused_memo(" in inspect.getsource(A.api_opportunities)


def test_it_is_runtime_tunable():
    assert "api_cache_sec" in rc.LIMITS
    assert rc.LIMITS["api_cache_sec"][0]() == 0.0, "off under pytest"
    assert A.API_CACHE_SEC == 0.0, "and the module constant agrees"
    assert rc.LIMITS["api_cache_sec"][1] == 0.0, "0 = off must be settable"
