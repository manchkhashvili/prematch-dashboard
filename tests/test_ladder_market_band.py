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


def test_small_games_survive_the_defaults(monkeypatch):
    """The floor being off is what protects these. A third of the 50-300 band
    carries an HT/FT grid, and the owner had seen a +2 game raise a flag —
    nothing at the small end may be dropped."""
    from src import runtime_config as rc
    games = [_G(0, 2), _G(1, 60), _G(2, 250), _G(3, 480)]
    _, expanded = _run(monkeypatch, games, min_markets=0,
                       max_markets=int(rc.LIMITS["anomaly_max_markets"][0]()))
    assert expanded == ["E0", "E1", "E2", "E3"], "no floor means no floor"


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

def test_games_are_expanded_cheapest_first(monkeypatch):
    """Owner: "can it go with low numbers to high and push that anomalies/flags
    gradually". Ordering by market count ascending maximises the games examined
    per pass, which is what makes flags appear steadily rather than in one lump.

    This replaces soonest-kickoff-first, and the ceiling is what makes that
    safe: every game still in scope expands in ~0.70-0.75s, so the cost spread
    the kickoff ordering protected against no longer exists."""
    games = [_G(0, 480, hours=1), _G(1, 60, hours=9), _G(2, 300, hours=5)]
    _, expanded = _run(monkeypatch, games, min_markets=0, max_markets=500,
                       start_within_hours=48)
    assert expanded == ["E1", "E2", "E0"], "ascending market count, not kickoff"


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
