"""Market-count band for the ladder scan (2026-08-14).

Owner: "can we somehow skip big games in anomalies that have like 300+ positions
on expanded versions? anyway its obscure games that have some anomalies and lags
and nothing is in big ones".

Both halves of that check out, and the filter is free because CB already tells
us the count before we pay for an expand — the "+N" badge rides on the very div
that triggers it:

    <div class="x_loop_game_active_add"
         onclick='DoGamesPostBack("ExpandDetail:2996090402")'>+4489</div>

COST, measured live on the soccer board (1831 games carrying a badge), by
expanding a sample per band and counting the ladder rungs a check can use:

    band        games   sec/game     MB   rungs   rungs/sec
    0-50           99       0.70   0.03       0         0.0
    50-300        292       0.70   0.07      10        14.3
    300-900       931       0.75   0.36      29        38.8
    900-2000      236       1.46   0.62      29        19.9
    2000+         271       3.12   2.07      43        13.8

YIELD, from 7421 historical ladder anomalies across 67 leagues: the top 10
leagues are 75% of all of them and every one is a minor competition (New Zealand
NBL, Brazil LDB U22, Lebanon, Rwanda, Vietnam VBA...). Genuine top-tier
fixtures account for 9 rows — 0.12%. Big games are the most expensive to expand
and the least likely to be wrong.

The floor was NOT part of the original idea and is worth as much: below ~50
markets a game has no alt-line ladder at all, so those 99 games were pure spend
returning zero usable rungs.

Effect on a full sweep, live per sport (estimated from the measured per-band
costs): soccer 1877 -> 1507 games, 2201s -> 1287s (-42%); tennis 187 -> 140,
131s -> 98s; americanfootball 170 -> 107, 120s -> 75s; basketball 65 -> 56.
Only soccer has any game above the ceiling — elsewhere the floor does the work.
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from src.scrapers import crystalbet as CB


# ── reading the badge ────────────────────────────────────────────────────────

def _container(html: str):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser")


def test_the_badge_is_read_off_the_expand_div():
    c = _container(
        '<div class="GContainerList" data-id="1">'
        '<div class="x_loop_game_active_add" '
        'onclick=\'DoGamesPostBack("ExpandDetail:2996090402")\'>+4489</div></div>')
    assert CB._market_count(c) == 4489


def test_a_game_with_no_badge_reports_unknown():
    """CB renders no badge when every extra market is locked."""
    assert CB._market_count(_container('<div class="GContainerList"></div>')) is None


@pytest.mark.parametrize("text,expected", [
    ("+2", 2), ("+ 62", 62), ("+6661", 6661), ("", None), ("more", None),
])
def test_badge_text_variants(text, expected):
    c = _container(f'<div class="x_loop_game_active_add">{text}</div>')
    assert CB._market_count(c) == expected


def test_the_list_parser_populates_market_count():
    src = inspect.getsource(CB._extract_games_from_list_html)
    assert "market_count=_market_count(container)" in src
    assert "market_count" in inspect.getsource(CB._GameOnList)


# ── the band itself ──────────────────────────────────────────────────────────

class _G:
    def __init__(self, i, n, hours=1):
        self.event_id = f"E{i}"
        self.home, self.away = f"H{i}", f"A{i}"
        self.league = "L"
        self.start_time = datetime.now(tz=timezone.utc) + timedelta(hours=hours)
        self.list_odds = [f"list-{i}"]
        self.loadinfo = "v1"
        self.market_count = n


def _run(monkeypatch, games, **kw):
    from src.scrapers.sports import soccer as _soccer
    from src.scrapers import change_cache
    for ns in ("soccer", "soccer:ladder"):
        change_cache.reset_cache(ns)
        CB._sport_detail_odds_caches.pop(ns, None)
    expanded: list[str] = []

    async def fake_refresh(*a, **k):
        return object(), "<html/>"

    async def fake_expand(g, *a, **k):
        expanded.append(g.event_id)
        return [f"ladder-{g.event_id}"]

    monkeypatch.setattr(CB, "_refresh_list_html_for_sport", fake_refresh)
    monkeypatch.setattr(CB, "_extract_games_from_list_html",
                        lambda html, fa, *, sport: list(games))
    monkeypatch.setattr(CB, "_expand_game", fake_expand)
    odds = asyncio.run(CB._fetch_for_sport(
        _soccer, headed=False, force_detail=True, bypass_cache=True,
        classify_override=None, **kw))
    return odds, expanded


def test_games_below_the_floor_are_skipped(monkeypatch):
    """They cannot produce a ladder anomaly — 99 such games on the live board
    returned zero usable rungs between them."""
    games = [_G(0, 2), _G(1, 4), _G(2, 800)]
    _, expanded = _run(monkeypatch, games, min_markets=50, max_markets=2000)
    assert expanded == ["E2"]
    st = CB._last_ladder_scan["soccer"]
    assert st["skipped_small"] == 2 and st["skipped_big"] == 0


def test_games_above_the_ceiling_are_skipped(monkeypatch):
    games = [_G(0, 800), _G(1, 6661), _G(2, 3407)]
    _, expanded = _run(monkeypatch, games, min_markets=50, max_markets=2000)
    assert expanded == ["E0"]
    assert CB._last_ladder_scan["soccer"]["skipped_big"] == 2


def test_a_game_with_an_unknown_count_is_kept(monkeypatch):
    """Failing OPEN. An unreadable badge must not silently drop a fixture —
    the band is an optimisation, not a correctness filter."""
    games = [_G(0, None), _G(1, 6661)]
    _, expanded = _run(monkeypatch, games, min_markets=50, max_markets=2000)
    assert expanded == ["E0"]


def test_zero_disables_each_side_independently(monkeypatch):
    games = [_G(0, 2), _G(1, 800), _G(2, 6661)]
    _, exp = _run(monkeypatch, games, min_markets=0, max_markets=2000)
    assert exp == ["E0", "E1"], "floor off, ceiling on"
    _, exp = _run(monkeypatch, games, min_markets=50, max_markets=0)
    assert exp == ["E1", "E2"], "floor on, ceiling off"
    _, exp = _run(monkeypatch, games, min_markets=0, max_markets=0)
    assert exp == ["E0", "E1", "E2"], "both off = the old behaviour"


def test_skipped_games_do_not_appear_at_all(monkeypatch):
    """Unlike a budget truncation, a banded-out game is not in scope for this
    scan — it must not contribute list-view rows either, or the snapshot grows
    with exactly the games we decided not to look at."""
    games = [_G(0, 2), _G(1, 800), _G(2, 6661)]
    odds, _ = _run(monkeypatch, games, min_markets=50, max_markets=2000)
    assert odds == ["ladder-E1"]


def test_the_band_is_applied_after_the_horizon_and_before_the_budget():
    """Order matters: the horizon decides what is worth scanning, the band
    removes what is not worth expanding, and only then does the budget decide
    how far down the list we get. Banding after the budget would save nothing."""
    ladder = (inspect.getsource(CB._fetch_for_sport)
              .partition("if bypass_cache:")[2]
              .partition("# ── 3. Cache pruning")[0])
    horizon = ladder.index("start_within_hours is not None")
    band = ladder.index("n_small = n_big = 0")
    budget = ladder.index("expand_until = ")
    assert horizon < band < budget


def test_the_defaults_match_what_was_measured():
    assert CB._LADDER_MIN_MARKETS == 50
    assert CB._LADDER_MAX_MARKETS == 2000


def test_the_band_is_runtime_tunable():
    from src import runtime_config as rc
    assert "anomaly_min_markets" in rc.LIMITS
    assert "anomaly_max_markets" in rc.LIMITS
    assert rc.LIMITS["anomaly_min_markets"][1] == 0.0, "0 must be allowed (= off)"
    from src import app as A
    src = inspect.getsource(A._anomaly_extra_loop)
    assert "min_markets=" in src and "max_markets=" in src


def test_the_price_path_is_untouched(monkeypatch):
    """The band is a ladder-scan filter. The dashboard's own expansion runs on
    cb_expand_within_hours and must keep every game — dropping a big fixture
    there would remove it from the Arbs tab."""
    normal = (inspect.getsource(CB._fetch_for_sport)
              .partition("# ── 3. Cache pruning")[2])
    assert "market_count" not in normal
    assert "n_small" not in normal
