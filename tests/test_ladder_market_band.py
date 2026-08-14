"""Market-count band for the ladder scan (2026-08-14).

Owner: "can we somehow skip big games in anomalies that have like 300+ positions
on expanded versions? anyway its obscure games that have some anomalies and lags
and nothing is in big ones" — then, on the first cut of this filter: "what is
too small? do you consider consistency flags too? cause there was +2 event that
fired it before, also htfts are mostly usefull make sure you dont go backwards".

The second message corrected the first cut, and the correction is the point of
this file. The filter is free either way because CB already tells us the count
before we pay for an expand — the "+N" badge rides on the very div that triggers
it:

    <div class="x_loop_game_active_add"
         onclick='DoGamesPostBack("ExpandDetail:2996090402")'>+4489</div>

THE CEILING is where the value is. Cost per band, measured live by expanding a
sample and counting the ladder rungs a check can use:

    band        games   sec/game     MB   rungs   rungs/sec
    0-50           99       0.70   0.03       0         0.0
    50-300        292       0.70   0.07      10        14.3
    300-900       931       0.75   0.36      29        38.8
    900-2000      236       1.46   0.62      29        19.9
    2000+         271       3.12   2.07      43        13.8

Exact soccer savings (1827 games with a badge, full sweep ~2232 s):

    ceiling   skipped   saved         sweep
    >2000        282     880 s (39%)   1353 s
    >1000        509    1211 s (54%)   1021 s   <- shipped
     >800        941    1553 s (70%)    679 s

1000 halves the soccer sweep and touches nothing else: basketball's biggest game
is under 1000 markets, and tennis and american football have nothing above 500.
A global ceiling is a soccer-only filter in practice.

And the yield argument holds — of 7421 historical ladder anomalies across 67
leagues the top 10 are 75% and every one is a minor competition (New Zealand
NBL, Brazil LDB U22, Lebanon, Rwanda, Vietnam VBA...); genuine top-tier fixtures
are 9 rows, 0.12%.

THE FLOOR IS OFF. The first cut set it to 50 on the strength of ladder rungs
alone, which was the wrong measurement — consistency checks need no ladder.
Re-measured on what they actually consume:

    band        games   rungs   htft   periods w/1X2   markets
    0-20           83       0    0/6               1         1
    20-50          14       6    0/6               1         8
    50-300        284       9    2/6               2        11
    300-900       911      29    6/6               2        44

The 50-300 band carries an HT/FT grid in a third of games, so a floor at 50 was
cutting into htft_combo / htft_fair — for 64 s of a 2232 s sweep, 2.9%. Tennis
and AF save more of their own (25% / 37%) but already finish inside the budget,
so it buys nothing there either.

A banded-out game also keeps its list-view Odds now. Not expanded is not the
same as erased: those rows are already parsed, and they carry the FT 1X2 and
main total that several consistency checks read.
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


def test_a_skipped_game_keeps_its_list_view_odds(monkeypatch):
    """Not expanded is not the same as erased. Owner pushed back on exactly
    this: consistency checks need no ladder, and dropping the game outright
    would take its FT 1X2 and main total off the board too. Those rows are
    already parsed, so keeping them costs nothing."""
    games = [_G(0, 2), _G(1, 800), _G(2, 6661)]
    odds, expanded = _run(monkeypatch, games, min_markets=50, max_markets=2000)
    assert expanded == ["E1"], "only the in-band game costs a postback"
    assert set(odds) == {"list-0", "ladder-E1", "list-2"}


def test_the_floor_is_off_by_default():
    """It was justified on ladder rungs alone. Re-measured on what the
    CONSISTENCY checks consume, the 50-300 band carries an HT/FT grid in a
    third of games — so a floor at 50 cut into htft_combo / htft_fair for 2.9%
    of the soccer sweep. Wrong trade; the knob stays, the default does not."""
    assert CB._LADDER_MIN_MARKETS == 0
    from src import runtime_config as rc
    assert rc.LIMITS["anomaly_min_markets"][0]() == 0.0


def test_htft_bearing_games_survive_the_defaults(monkeypatch):
    """A game in the 50-300 band — where a third carry an HT/FT grid — must be
    expanded under the shipped defaults, not banded out."""
    games = [_G(0, 60), _G(1, 250), _G(2, 900), _G(3, 1200)]
    _, expanded = _run(monkeypatch, games,
                       min_markets=CB._LADDER_MIN_MARKETS,
                       max_markets=CB._LADDER_MAX_MARKETS)
    assert expanded == ["E0", "E1", "E2"], (
        "small and mid games must all still be expanded; only >1000 is dropped")


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
    """Ceiling 1000 is the knee: soccer 2232s -> 1021s (-54%), and it touches
    no other sport — basketball's biggest game is under 1000 markets and
    tennis/AF have nothing above 500."""
    assert CB._LADDER_MIN_MARKETS == 0
    assert CB._LADDER_MAX_MARKETS == 1000
    from src import runtime_config as rc
    assert rc.LIMITS["anomaly_max_markets"][0]() == 1000.0


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
