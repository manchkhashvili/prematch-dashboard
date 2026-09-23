"""Futsal and handball — soccer-shaped LSport-only sports (2026-09-22).

Both ride sports/soccerlike.py: the soccer classifier for the shared titles,
a prelude for LSport's period vocabulary, and the list view trimmed to the
3-way moneyline (soccer's loadinfo layout misreads futsal's second block as a
total at 1.7, which total_additivity would then compare against real half
totals). The identity checks that fire on LSport soccer — the pick'em trio,
the HT/FT grid vs its legs, ladders, halves vs the match — run on them as-is;
the soccer goal model stays soccer-only.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src import consistency as C
from src.scrapers import cb_detail
from src.scrapers.sports import futsal, handball, soccerlike

NOW = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("title,mt,per,n,side", [
    ("1st Period Winner", "moneyline", "H1", 3, None),
    ("2nd Period Winner", "moneyline", "H2", 3, None),
    ("2nd Half 3 Way", "moneyline", "H2", 3, None),
    ("Asian Handicap 1st Period", "spread", "H1", 2, None),
    ("Asian Handicap 2nd Period", "spread", "H2", 2, None),
    ("Under/Over - Home Team", "team_total", "FT", 2, "home"),
    ("Under/Over 1st Period - Away Team", "team_total", "H1", 2, "away"),
    ("1 period - Home Team total", "team_total", "H1", 2, "home"),
    # shared vocabulary, via the soccer classifier
    ("Main result", "moneyline", "FT", 3, None),
    ("1st Half Result", "moneyline", "H1", 3, None),
    ("draw no bet", "moneyline", "FT", 2, None),
    ("Asian Handicap", "spread", "FT", 2, None),
    ("HT/FT", "htft", "FT", 2, None),
    ("Halftime/fulltime", "htft", "FT", 2, None),
    ("Total goals", "total", "FT", 2, None),
    ("Under/Over 1st Period", "total", "H1", 2, None),
    ("1st. Half Total goals", "total", "H1", 2, None),
])
def test_lsport_and_shared_titles_classify(title, mt, per, n, side):
    for mod in (futsal, handball):
        c = mod.classify_market_title(title)
        assert c is not None, (mod.SPORT_NAME, title)
        assert (c.market_type, c.period, c.n_way, c.team_side) == (mt, per, n, side)


@pytest.mark.parametrize("title", ["Double Chance", "Double Chance 1st Period",
                                   "Both Teams To Score", "1st Period Race To 3.0",
                                   "Odd/Even", "Winning margins", "Correct Score 1st Period", ""])
def test_unrepresentable_titles_stay_unclassified(title):
    assert soccerlike.classify(title) is None


def test_ids_and_names():
    assert (futsal.SPORT_ID, futsal.SPORT_NAME) == (26, "futsal")
    assert (handball.SPORT_ID, handball.SPORT_NAME) == (20, "handball")


def test_list_view_keeps_only_the_three_way_moneyline():
    from src.scrapers.crystalbet import _extract_games_from_list_html
    # Iraero Irkutsk v Tyumen as CB listed it: 1X2 then a block soccer's layout
    # reads as "total 1.7 over 7.0 under 1.95" (it is a handicap).
    loadinfo = ('[{"name":"1","bet":"2.10","handicap":""},{"name":"X","bet":"4.45","handicap":""},'
                '{"name":" 2","bet":"2.40","handicap":""},{"name":"1","bet":"7.00","handicap":""},'
                '{"name":"Handicap","bet":"","handicap":"handicap 1.7"},{"name":"2","bet":"1.95","handicap":""}]')
    html = ('<html><body><div class="game-table"><div class="x_loop_title_block">22.09.2026</div>'
            '<div class="GContainerList" data-id="5"><div class="game-row" data-game-code="20270001">'
            '<div class="game_hint"><label>Russia, Superliga</label></div>'
            '<div class="teams_name">Iraero Irkutsk - Tyumen</div><span class="time">14:00</span></div>'
            f'<div class="game_loading" data-loadinfo=\'{loadinfo}\'></div></div></div></body></html>')
    games = _extract_games_from_list_html(html, NOW, sport=futsal)
    assert len(games) == 1 and games[0].provider == "lsport"
    kinds = {(o.sport, o.market_type) for o in games[0].list_odds}
    assert kinds <= {("futsal", "moneyline")}, kinds


# ── detail pages through the engine ──────────────────────────────────────────

def _cell(lab, o):
    return (f'<div class="sport_more_bt DetailSnatch"><div class="sport_more_bt1">{lab}</div>'
            f'<div class="sport_more_bt2">{o}</div></div>')


def _page(markets, eid="1"):
    rows = "".join(
        f'<tr><td class="sport_more_td1">{t}</td><td class="sport_more_td2">'
        f'<div class="sport_more_td_div">{"".join(_cell(l, o) for l, o in sels)}</div></td></tr>'
        for t, sels in markets.items())
    return f'<div class="GContainerList" data-id="{eid}"><table class="game-details">{rows}</table></div>'


def _flags(mod, markets, home="H", away="A"):
    odds = cb_detail.parse_detail_page(
        _page(markets), event_id="1", home=home, away=away, league="L",
        start_time=NOW, fetched_at=NOW, sport_name=mod.SPORT_NAME,
        classify=mod.classify_market_title_permissive, scope_to_event=True, per_section=True)
    assert odds
    return odds, C.find_consistency_flags(odds)


def test_futsal_htft_combo_reads_the_lsport_period_titles():
    """Chom Bueng McRu v Acsat Ssvit W (Thai women's league) as posted:
    HT/FT 2/2 @1.55 vs the correlation-fair from H1 away @1.40 and FT away
    @1.15. The H1 leg is `1st Period Winner`, which the soccer classifier
    alone does not read — without the prelude this check could not run."""
    m = {
        "Main result": [("1", "8.50"), ("X", "6.00"), ("2", "1.15")],
        "1st Period Winner": [("1", "5.50"), ("X", "3.40"), ("2", "1.40")],
        "HT/FT": [("1/1", "14.0"), ("1/X", "26.0"), ("1/2", "10.0"), ("X/1", "21.0"),
                  ("X/X", "9.50"), ("X/2", "3.40"), ("2/1", "34.0"), ("2/X", "21.0"), ("2/2", "1.55")],
    }
    odds, flags = _flags(futsal, m, "Chom Bueng McRu", "Acsat Ssvit W")
    assert {(o.market_type, o.period) for o in odds} >= {("moneyline", "H1"), ("htft", "FT")}
    combo = [f for f in flags if f.kind == "htft_combo"]
    assert len(combo) == 1 and combo[0].outcome == "2/2" and combo[0].odds == 1.55
    assert combo[0].sport == "futsal"


def test_handball_pickem_trio():
    """DNB and the 0.0 rung are the same bet; a book quoting them apart
    enough to cover under 1.0 is locked. Handball's LSport page posts all
    three legs (Main result, draw no bet, Asian Handicap with a 0.0 rung)."""
    m = {
        "Main result": [("1", "1.55"), ("X", "10.0"), ("2", "3.10")],
        "draw no bet": [("1", "1.55"), ("2", "2.10")],
        "Asian Handicap": [("1(0.0)", "1.20"), ("2(0.0)", "3.50"), ("1(-3.5)", "1.90"), ("2(+3.5)", "1.85")],
    }
    odds, flags = _flags(handball, m)
    kinds = {f.kind for f in flags}
    assert "pickem_duplicate" in kinds or "pickem_dominance" in kinds, kinds
    assert all(f.sport == "handball" for f in flags)


def test_the_soccer_goal_model_stays_soccer_only():
    """half_result_vs_ft fits a ~2.7-goal Poisson; a 55-goal handball half
    must never be scored against it."""
    m = {
        "Main result": [("1", "1.10"), ("X", "12.0"), ("2", "9.00")],
        "1st Half Result": [("1", "1.90"), ("X", "3.20"), ("2", "3.50")],   # Belarus-shaped gap
        "2nd Half 3 Way": [("1", "1.90"), ("X", "3.20"), ("2", "3.50")],
    }
    _, flags = _flags(handball, m)
    assert not [f for f in flags if f.kind == "half_result_vs_ft"]


def test_halves_do_sum_to_the_match_here():
    """Unlike volleyball's sets, futsal's halves ARE halves — additivity applies."""
    # the centre of a ladder is where P(over) crosses 0.5, so each period
    # needs two rungs straddling it: match ~7.5, halves ~0.5 each (sum 1.0,
    # 6.5 pts short of the match — past the 5-pt bar).
    m = {
        "Main result": [("1", "2.00"), ("X", "4.00"), ("2", "3.00")],
        "Total goals": [("Under 7.5", "1.90"), ("Over 7.5", "1.90"),
                        ("Under 8.5", "1.40"), ("Over 8.5", "2.80")],
        "Under/Over 1st Period": [("Under 0.5", "1.90"), ("Over 0.5", "1.90"),
                                  ("Under 1.5", "1.35"), ("Over 1.5", "3.00")],
        "Under/Over 2nd Period": [("Under 0.5", "1.90"), ("Over 0.5", "1.90"),
                                  ("Under 1.5", "1.35"), ("Over 1.5", "3.00")],
    }
    _, flags = _flags(futsal, m)
    assert [f for f in flags if f.kind == "total_additivity"]


def test_registered_everywhere_the_cycle_looks():
    from src.scrapers.crystalbet import _SPORT_MODULES
    from src.scrapers import cb_parse_pool
    import src.app as app
    for mod in (futsal, handball):
        assert _SPORT_MODULES[mod.SPORT_NAME] is mod
        assert cb_parse_pool._classifier(mod.SPORT_NAME, "permissive") is mod.classify_market_title_permissive
        assert mod.SPORT_NAME in C.CONSISTENCY_SPORTS
        assert mod.SPORT_NAME in app._lsport_sports()
        assert mod.SPORT_NAME not in app.SPORT_NAMES
        assert mod.SPORT_NAME not in C.SETS_AS_PERIODS
