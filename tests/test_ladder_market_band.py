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
import time
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


def _run(monkeypatch, games, *, reset=True, expand_cost=0.0, **kw):
    """Drive the ladder branch against fakes.

    `reset` wipes the ladder caches AND the sweep cursor, so a test starts from
    a cold scan. Multi-pass tests pass reset=False for the later passes — they
    are testing exactly the state that carries between them.
    """
    from src.scrapers.sports import soccer as _soccer
    from src.scrapers import change_cache
    if reset:
        for ns in ("soccer", "soccer:ladder"):
            change_cache.reset_cache(ns)
            CB._sport_detail_odds_caches.pop(ns, None)
        CB._ladder_sweep_seen.clear()
    expanded: list[str] = []

    async def fake_refresh(*a, **k):
        return object(), "<html/>"

    async def fake_expand(g, *a, **k):
        if expand_cost:
            await asyncio.sleep(expand_cost)
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
    assert set(exp) == {"E0", "E1"}, "floor off, ceiling on"
    _, exp = _run(monkeypatch, games, min_markets=50, max_markets=0)
    assert set(exp) == {"E1", "E2"}, "floor on, ceiling off"
    _, exp = _run(monkeypatch, games, min_markets=0, max_markets=0)
    assert set(exp) == {"E0", "E1", "E2"}, "both off = the old behaviour"


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


def test_small_games_survive_the_defaults(monkeypatch):
    """The floor being off is what protects these. A third of the 50-300 band
    carries an HT/FT grid, and the owner had seen a +2 game raise a flag —
    nothing at the small end may be dropped."""
    from src import runtime_config as rc
    games = [_G(0, 2), _G(1, 60), _G(2, 250), _G(3, 480)]
    _, expanded = _run(monkeypatch, games, min_markets=0,
                       max_markets=int(rc.LIMITS["anomaly_max_markets"][0]()))
    assert set(expanded) == {"E0", "E1", "E2", "E3"}, "no floor means no floor"


def test_the_shipped_ceiling_drops_the_700_900_band(monkeypatch):
    """Stated plainly because it is the cost of 500 rather than an accident:
    44% of the soccer board sits at 700-900 markets and every one of those games
    carries an HT/FT grid. At 500 they are all skipped — HT/FT coverage goes
    from 65% of games to 7.2%. Raising `limits.anomaly_max_markets` to 900 is
    the documented way back, live and without a restart."""
    from src import runtime_config as rc
    soccer_ceiling = int(rc.LIMITS["anomaly_max_markets"][0]())   # 500
    games = [_G(0, 480), _G(1, 777), _G(2, 839), _G(3, 1200)]
    odds, expanded = _run(monkeypatch, games, min_markets=0,
                          max_markets=soccer_ceiling)
    assert expanded == ["E0"]
    assert CB._last_ladder_scan["soccer"]["skipped_big"] == 3
    # ...but they are still on the board via their list-view rows.
    assert {"list-1", "list-2", "list-3"} <= set(odds)

    _, expanded_900 = _run(monkeypatch, games, min_markets=0, max_markets=900)
    assert expanded_900 == ["E0", "E1", "E2"], (
        "900 is the setting that brings the HT/FT band back")


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


def test_the_module_default_is_no_filtering_at_all():
    """The regression this guards. The ceiling shipped as a global default of
    500 and immediately gutted basketball, whose board sits at 500-900 markets:
    23 of 45 games skipped, ladders 200 -> 28, games_without_ladder 2 -> 30, and
    the basketball consistency flags vanished off the tab.

    A filter that silently removes games must be opted into per sport, by a
    caller who has measured THAT sport. A caller that says nothing gets nothing
    filtered."""
    assert CB._LADDER_MIN_MARKETS == 0
    assert CB._LADDER_MAX_MARKETS == 0
    from src import runtime_config as rc
    assert rc.LIMITS["anomaly_max_markets"][0]() == 500.0, (
        "the CONFIG default is still 500 — that is soccer's number, applied by "
        "the caller to the sports in ANOMALY_MAX_MARKETS_SPORTS")


def test_only_soccer_opts_into_the_ceiling():
    from src import app as A
    assert A.ANOMALY_MAX_MARKETS_SPORTS == ("soccer",)
    for sport in ("basketball", "tennis", "americanfootball"):
        assert sport not in A.ANOMALY_MAX_MARKETS_SPORTS, (
            f"{sport} finishes its sweep inside the budget — a ceiling there "
            "can only remove coverage it already had")


def test_the_basketball_scan_states_its_band_explicitly():
    """Inheriting a default is exactly how this scan lost half its board."""
    import inspect
    from src import app as A
    src = inspect.getsource(A._compute_anomalies)
    assert "max_markets=" in src and "ANOMALY_MAX_MARKETS_SPORTS" in src
    sig = inspect.signature(CB.fetch_crystalbet_basketball_anomaly_ladders)
    assert "max_markets" in sig.parameters and "min_markets" in sig.parameters


def test_the_extra_loop_only_bands_opted_in_sports():
    import inspect
    from src import app as A
    src = inspect.getsource(A._anomaly_extra_loop)
    assert "if sport in ANOMALY_MAX_MARKETS_SPORTS else 0" in src


def test_the_ceiling_is_not_a_smooth_knob():
    """The one thing anyone tuning this needs to know, kept next to the number
    rather than in a doc. 811 soccer games — 44% of the board — sit in a single
    band at 700-900 markets, all carrying an HT/FT grid, all expanding in 0.75s.
    Every ceiling below 700 drops all of them at once:

        ceiling   kept   sweep   games w/ HT/FT
            500    480    376s    104  ( 7.2%)
            700    482    378s    106  ( 7.3%)
            900   1293    951s    917  (63.3%)
           1000   1318   1021s    942  (65.0%)

    So 500 and 700 cost the same, and 900 costs 3x for 6x the HT/FT coverage.
    """
    from pathlib import Path
    from src import app as A
    doc = Path(CB.__file__).read_text()
    assert "811" in doc and "700-900" in doc, (
        "the cliff must be documented AT the constant — a bare 500 reads as a "
        "smooth knob and it is not")
    html = (Path(A.__file__).resolve().parent.parent / "static" / "config.html").read_text()
    assert "anomaly_max_markets" in html
    assert "700" in html and "900" in html, (
        "the tuning table belongs on the page where tuning happens")


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


# ── gradual: cheapest first, published as it goes ────────────────────────────

def test_ordering_is_cheapest_first_not_kickoff(monkeypatch):
    """Owner: "go from lowest markets to highest".

    Plain ascending market count. A yield-rank band used to come first, and it
    was measured and right for the checks that existed then — it counted HT/FT
    grids and ladder rungs, which thin boards do not carry. The pick'em family
    changed that: it needs a 1X2, a Draw No Bet and one 0.0 rung, three markets
    a 39-market board has, so cheap is no longer a proxy for empty.

    This also replaces soonest-kickoff-first: with the ceiling on, every game
    in scope expands in roughly the same time, so the cost spread the kickoff
    ordering protected against no longer exists."""
    CB._ladder_sweep_seen.clear()
    games = [_G(0, 480, hours=1), _G(1, 60, hours=9), _G(2, 300, hours=5)]
    _, expanded = _run(monkeypatch, games, min_markets=0, max_markets=500,
                       start_within_hours=48)
    assert expanded == ["E1", "E2", "E0"], (
        "ascending market count: E1 (60), E2 (300), E0 (480). Kickoff order "
        "would have been E0,E2,E1 (hours 1,5,9), so this also shows kickoff "
        "is not the key.")
    CB._ladder_sweep_seen.clear()


def test_a_game_with_no_badge_is_expanded_last(monkeypatch):
    """Unknown cost, and CB omits the badge when every extra market is locked —
    so there is usually nothing there. Last, but never dropped."""
    games = [_G(0, None, hours=1), _G(1, 400, hours=9)]
    _, expanded = _run(monkeypatch, games, min_markets=0, max_markets=500,
                       start_within_hours=48)
    assert expanded == ["E1", "E0"]


def test_partial_results_are_published_during_the_pass(monkeypatch):
    """A soccer pass is minutes long. Without this the tab shows nothing at all
    until it finishes, then everything at once."""
    seen = []

    async def on_progress(partial, done, total):
        seen.append((done, total, len(partial)))

    games = [_G(i, 100 + i) for i in range(60)]
    _run(monkeypatch, games, min_markets=0, max_markets=500,
         on_progress=on_progress)
    assert seen, "no progress callback fired across 60 games"
    assert [d for d, _, _ in seen] == [25, 50], "fires on the progress boundary"
    assert all(t == 60 for _, t, _ in seen)
    assert seen[0][2] < seen[1][2], "each publish carries strictly more rows"


def test_a_failing_progress_callback_cannot_break_the_scan(monkeypatch):
    """Publishing is a convenience. A scan that dies because a UI-facing
    callback threw would be a bad trade."""
    async def boom(partial, done, total):
        raise RuntimeError("nope")

    games = [_G(i, 100 + i) for i in range(30)]
    odds, expanded = _run(monkeypatch, games, min_markets=0, max_markets=500,
                          on_progress=boom)
    assert len(expanded) == 30, "the pass must complete regardless"


def test_the_scan_reports_live_progress():
    from src import app as A
    import inspect
    assert "_extra_anom_progress" in inspect.getsource(A._anomaly_extra_loop)
    assert '"progress": dict(_extra_anom_progress)' in inspect.getsource(A)


def test_a_sport_that_did_not_opt_in_is_never_filtered(monkeypatch):
    """Basketball's board sits at 500-900 markets. Under the global default it
    lost 23 of 45 games; with the ceiling off it must keep every one, whatever
    the soccer number happens to be."""
    games = [_G(0, 520), _G(1, 780), _G(2, 6000)]
    _, expanded = _run(monkeypatch, games, min_markets=0, max_markets=0)
    assert expanded == ["E0", "E1", "E2"]
    st = CB._last_ladder_scan["soccer"]
    assert st["skipped_big"] == 0 and st["skipped_small"] == 0


# ── the sweep advances instead of re-doing the same prefix ───────────────────

def test_successive_passes_cover_different_games(monkeypatch):
    """The flaw the owner hit as "it doesn't work at all" for soccer HT/FT.

    Live numbers: 951 soccer games in horizon, a 300s budget buying 23
    expansions (11.1s/game under full load), and `cached` sitting at 0 because
    a soccer game's list-view hash moves within one scan interval far more
    often than not. Sorted the same way every pass, that is the same 23 games
    forever and the other 928 are never looked at.
    """
    from src.scrapers import change_cache
    CB._ladder_sweep_seen.clear()
    games = [_G(i, 100 + i) for i in range(30)]

    first_pass = [True]

    def one_pass():
        # every game's mains moved, so the ladder cache cannot help
        for g in games:
            g.loadinfo = f"v{time.time_ns()}"
        _, expanded = _run(monkeypatch, games, reset=first_pass[0],
                           min_markets=0, max_markets=500,
                           max_expand_sec=0.05, expand_cost=0.02)
        first_pass[0] = False
        return expanded

    first = one_pass()
    second = one_pass()
    third = one_pass()
    assert first and second and third
    assert set(first).isdisjoint(second), (
        f"pass 2 repeated pass 1: {first} vs {second}")
    assert set(second).isdisjoint(third), (
        f"pass 3 repeated pass 2: {second} vs {third}")
    CB._ladder_sweep_seen.clear()


def test_the_sweep_restarts_once_the_board_is_covered(monkeypatch):
    """Otherwise the scan goes quiet forever after one lap."""
    CB._ladder_sweep_seen.clear()
    games = [_G(i, 100 + i) for i in range(4)]
    seen_all = set()
    for n in range(6):
        for g in games:
            g.loadinfo = f"v{time.time_ns()}"
        _, exp = _run(monkeypatch, games, reset=(n == 0),
                      min_markets=0, max_markets=500,
                      max_expand_sec=0.03, expand_cost=0.02)
        seen_all |= set(exp)
    assert seen_all == {g.event_id for g in games}, (
        "every game must be reached within a few passes")
    CB._ladder_sweep_seen.clear()


def test_cache_hits_also_count_as_covered(monkeypatch):
    """A game served from the ladder cache has been accounted for this sweep —
    not marking it would make the cursor stick on cheap cached games."""
    CB._ladder_sweep_seen.clear()
    games = [_G(i, 100 + i) for i in range(6)]
    _run(monkeypatch, games, min_markets=0, max_markets=500)   # warm
    _, expanded = _run(monkeypatch, games, reset=False,
                       min_markets=0, max_markets=500)
    assert expanded == [], "unmoved board: all cache hits"
    assert len(CB._ladder_sweep_seen["soccer"]) == 6
    CB._ladder_sweep_seen.clear()


def test_the_sweep_position_is_reported(monkeypatch):
    CB._ladder_sweep_seen.clear()
    games = [_G(i, 100 + i) for i in range(10)]
    _run(monkeypatch, games, min_markets=0, max_markets=500)
    st = CB._last_ladder_scan["soccer"]
    assert st["sweep_seen"] == 10 and st["sweep_total"] == 10
    CB._ladder_sweep_seen.clear()


# ── order by what a pass can find, not just how many it can touch ────────────

def test_the_cheap_bands_are_expanded_first(monkeypatch):
    """The reversal, and the measurement behind it.

    Live soccer census, 18 games expanded per band — cost, whether the pick'em
    markets are present, and whether any pick'em flag came out:

        band       games  s/game  has 1X2+0.0  pickem flags  band cost
        0-50         155    0.16       6/18         0/18         24s
        50-300       186    0.19      18/18         2/18         36s
        300-900     1009    0.42      15/18         0/18        426s
        900-2000     215    0.51      17/18         0/18        110s
        2000+        303    2.14      17/18         0/18        650s

    The 50-300 band carries the markets on every game, produced the only flags,
    and costs 36 seconds for all 186 — yet the old ranking put it third, behind
    1 224 games. The 2000+ band cost eleven times as much per game and found
    nothing. The owner's +7.4 % lock sat in the band ranked LAST.
    """
    CB._ladder_sweep_seen.clear()
    games = [_G(0, 30), _G(1, 120), _G(2, 400), _G(3, 1200), _G(4, 5000)]
    _, expanded = _run(monkeypatch, games, min_markets=0, max_markets=0)
    assert expanded == ["E0", "E1", "E2", "E3", "E4"], (
        "ascending market count, cheapest first — the two cheap bands land in "
        "the first pass instead of an hour into the sweep")
    CB._ladder_sweep_seen.clear()


def test_the_dearest_board_is_expanded_last(monkeypatch):
    """2000+ markets cost 2.14 s/game against 0.19 for the 50-300 band, so a
    pass that leads with them covers far fewer games of every kind."""
    CB._ladder_sweep_seen.clear()
    games = [_G(0, 850), _G(1, 320), _G(2, 5000), _G(3, 500)]
    _, expanded = _run(monkeypatch, games, min_markets=0, max_markets=0)
    assert expanded == ["E1", "E3", "E0", "E2"]
    CB._ladder_sweep_seen.clear()


def test_nothing_is_skipped_by_the_ordering(monkeypatch):
    """Ordering is not filtering — the band does the filtering and the sweep
    cursor still guarantees every game is reached. This is what makes
    cheapest-first safe for the HT/FT and ladder checks the old ranking was
    protecting: rich boards arrive LATER in a sweep, never not at all."""
    CB._ladder_sweep_seen.clear()
    games = [_G(i, n) for i, n in enumerate((30, 120, 400, 1200, 5000))]
    odds, expanded = _run(monkeypatch, games, min_markets=0, max_markets=0)
    assert len(expanded) == 5
    CB._ladder_sweep_seen.clear()
