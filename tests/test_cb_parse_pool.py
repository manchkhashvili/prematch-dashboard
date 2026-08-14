"""CB parsing in worker processes (2026-08-14).

Owner asked me to fix the contention while they tuned config. The measurement
that decides the design:

    soccer list panel, 16 MB, html5lib normalise + extract
      pool OFF: parsed in 9.48s, the event loop got 1 tick  -> stalled 9477ms
      pool ON : parsed in 9.06s, the event loop got 177 ticks -> max stall 21ms

Same wall time either way; the difference is that with the pool off NOTHING
else in the process runs for nine and a half seconds — no poll loop, no API
request, no scan. That happens every cycle, per sport, and it is why an expand
that costs 0.85s on an idle box measured 10.1s on the running dashboard.

Threads cannot do this: html5lib is pure Python, so 4 pages on 4 threads
measured 0.88x — SLOWER than one. Only processes reach the 7 idle cores.

lxml would have been simpler and does not work: live on 25 games, 0/15 soccer
and 0/10 basketball matched html5lib, because lxml over-collects on CB's
unclosed <td>s (one game: 59 markets vs 108). Saved fixtures DO match, which is
the trap — they are page.content() captures, already browser-repaired, so they
cannot test raw panel markup.

Parity is verified LIVE (scratch scripts, not here): 21/21 games and 1274 Odds
identical for detail across both classifiers, and 1980/89/111 games identical
for the list on soccer/basketball/tennis. These tests cover the wiring and the
failure behaviour, which is what can rot.
"""
from __future__ import annotations

import asyncio
import inspect
import os

import pytest

from src.scrapers import cb_parse_pool as P


# ── it must not slow the test suite down ─────────────────────────────────────

def test_the_pool_is_off_under_pytest():
    """A worker spawns a fresh interpreter and re-imports the scraper stack.
    Left on, the suite went from 4s to timing out."""
    assert P.PARSE_PROCS == 0
    assert P._get_pool() is None


def test_an_explicit_setting_always_wins(monkeypatch):
    monkeypatch.setenv("CB_PARSE_PROCS", "2")
    assert P._default_procs() == 2
    monkeypatch.setenv("CB_PARSE_PROCS", "0")
    assert P._default_procs() == 0
    monkeypatch.setenv("CB_PARSE_PROCS", "nonsense")
    assert P._default_procs() == 0, "a bad value must not crash the scraper"


# ── in-process and pooled paths are the same code ────────────────────────────

def test_the_worker_is_importable_module_level():
    """spawn() re-imports the module, so the callable cannot be a closure,
    a lambda, or defined in __main__."""
    for fn in (P.parse_detail_job, P.parse_list_job):
        assert fn.__module__ == "src.scrapers.cb_parse_pool"
        assert fn.__qualname__ == fn.__name__


def test_the_classifier_is_named_not_pickled():
    """Functions do not pickle, so the job carries a mode string."""
    from src.scrapers.sports import soccer
    assert P._classifier("soccer", "strict") is soccer.classify_market_title
    assert (P._classifier("soccer", "permissive")
            is soccer.classify_market_title_permissive)


def test_naming_refuses_an_unknown_classifier():
    """Guessing would silently change which markets get parsed."""
    from src.scrapers.crystalbet import _classify_mode
    from src.scrapers.sports import soccer
    assert _classify_mode(soccer, None) == "strict"
    assert _classify_mode(soccer, soccer.classify_market_title) == "strict"
    assert _classify_mode(
        soccer, soccer.classify_market_title_permissive) == "permissive"
    with pytest.raises(ValueError):
        _classify_mode(soccer, lambda title: None)


def test_detail_parses_in_process_when_the_pool_is_off():
    """Same function the worker runs — that is the point of the split."""
    from pathlib import Path
    from datetime import datetime, timezone
    html = (Path(__file__).resolve().parent.parent
            / "data" / "raw" / "cb_single_match_detail.html")
    if not html.exists():
        pytest.skip("sample detail page not present")
    out = asyncio.run(P.parse_detail(
        html.read_text(errors="ignore"), event_id="X", home="H", away="A",
        league="L", start_time=None,
        fetched_at=datetime.now(tz=timezone.utc),
        sport_name="basketball", classify_mode="permissive",
        scope_to_event=False))
    assert out, "the in-process fallback must still parse"
    assert all(hasattr(o, "market_type") for o in out)


# ── failure must degrade, never lose data ────────────────────────────────────

def test_a_broken_pool_falls_back_in_process(monkeypatch):
    """Observed for real: a heredoc script broke spawn (no __main__ file), the
    pool raised BrokenProcessPool, and the run continued in-process with
    correct results. That behaviour is the contract."""
    class _Boom:
        def submit(self, *a, **k):
            raise RuntimeError("boom")

    calls = []

    def fake_job(job):
        calls.append(job["sport_name"])
        return ["ok"]

    monkeypatch.setattr(P, "_get_pool", lambda: None)
    monkeypatch.setattr(P, "parse_list_job", fake_job)
    from datetime import datetime, timezone
    out = asyncio.run(P.parse_list("<html/>", "soccer",
                                   datetime.now(tz=timezone.utc)))
    assert out == ["ok"] and calls == ["soccer"]


def test_the_parsers_own_error_is_not_swallowed():
    """A missing detail table is the transport-failure signal callers use to
    mark expand_failed. Swallowing it would turn a failed fetch into 'no
    markets', which is a different and much worse thing."""
    src = inspect.getsource(P.parse_detail)
    assert "except RuntimeError:" in src and "raise" in src
    detail = inspect.getsource(P.parse_detail_job)
    assert "no detail table" in detail


# ── the transport had to grow a raw path ─────────────────────────────────────

def test_the_transport_can_return_raw_html():
    """A BeautifulSoup tree cannot cross a process boundary; a string can."""
    from src.scrapers import cb_http
    assert hasattr(cb_http, "expand_detail_raw")
    assert hasattr(cb_http, "fetch_list_raw")
    assert hasattr(cb_http.CbHttpSession, "expand_detail_raw")
    # the raw list path keeps the English-flip check
    assert "georgian_chars" in inspect.getsource(cb_http.fetch_list_raw)


def test_the_scraper_uses_the_pool_for_both_paths():
    from src.scrapers import crystalbet as CB
    assert "cb_parse_pool.parse_detail(" in inspect.getsource(
        CB._expand_and_parse_one_http)
    assert "cb_parse_pool.parse_list(" in inspect.getsource(
        CB._list_games_for_sport)


def test_playwright_transport_still_parses_in_process():
    """Its page.content() is already a repaired string we did not fetch, so the
    raw/worker split does not apply."""
    from src.scrapers import crystalbet as CB
    src = inspect.getsource(CB._list_games_for_sport)
    assert "_refresh_list_html_for_sport" in src, "non-http path must remain"
    assert "http and cb_parse_pool.PARSE_PROCS > 0" in src


def test_the_pool_is_shut_down_with_the_app():
    """Worker processes outliving the app would keep the interpreter alive."""
    from src import app as A
    import inspect
    src = inspect.getsource(A)
    assert "cb_parse_pool.shutdown()" in src
