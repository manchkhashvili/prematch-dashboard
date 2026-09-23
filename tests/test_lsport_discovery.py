"""Covering ALL LSport matches — discovery + the generic sport (2026-09-22).

A fixed list of sports cannot cover all LSport matches: CB's nav moved by four
sports between two days, and LSport's coverage moves with the calendar. So
the New inconsistencies cycle sweeps the live nav, counts LSport matches on
every sport it is not already scanning, and scans any that has some through
a title-only generic classifier. These tests pin:

  * the English nav parse (a regex over the raw page read 5 sports with empty
    labels; the DOM walk reads all of them), and the slug that names a sport;
  * the generic classifier: what it reads, what it refuses (any "period" /
    set / frame title, since H1 there could be a set or a third), n_way=0
    AUTO resolving to 3-way only when an X label is present;
  * registration: a discovered sport lands in the module registry and in
    consistency.GENERIC_SPORTS, a dedicated module for the same id wins, and
    the parse-pool worker resolves the generic classifier by MODE (it never
    sees the main process's registrations);
  * the discovery sweep against a stubbed nav + lists: sports with LSport
    matches are activated, quiet ones drop their session, a broken sport is
    reported and does not stop the sweep;
  * badge-less games keep their list rows without an ExpandDetail;
  * the API census fields and the config knob.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import consistency as C
from src import lsport_scan
from src.scrapers import cb_detail, cb_http, cb_parse_pool
from src.scrapers import crystalbet as CB   # read CB._SPORT_MODULES at call time: test_cb_http reloads the module
from src.scrapers.sports import lsport_generic as G

NOW = datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parent.parent


# ── nav + slug ───────────────────────────────────────────────────────────────

def test_nav_parse_reads_every_sport_from_saved_markup():
    html = (ROOT / "data/raw/cb_sports_nav.html").read_text(encoding="utf-8", errors="replace")
    nav = cb_http.parse_nav_sports(html)
    assert len(nav) >= 30
    assert nav[33].endswith("Table Tennis") and nav[16].endswith("Football")
    assert all(k > 0 for k in nav), "TOP / LIVE pseudo-tabs must be dropped"


def test_nav_parse_on_the_live_anchor_shape():
    html = ("<a href='javascript:' onclick='DoSportTypePostBack(87)' class='spt_button_87'>"
            "<span class='SportTypeIcon'><span class='SportType87'></span>"
            "<span class='sport_menu_new_count'>8</span></span>"
            "<div class='sport_menu_new_title'>SUMO</div> </a>"
            "<a onclick='DoSportTypePostBack(-169)'>TOP</a>")
    assert cb_http.parse_nav_sports(html) == {87: "8 SUMO"}


@pytest.mark.parametrize("label,sid,expected", [
    ("232 Table Tennis", 33, "tabletennis"), ("8 SUMO", 87, "sumo"),
    ("12 Aus. Football", 31, "ausfootball"), ("286 კალათბურთი", 17, "sport17"), ("", 5, "sport5"),
])
def test_slug(label, sid, expected):
    assert G.slug(label, sid) == expected


# ── the generic classifier ───────────────────────────────────────────────────

@pytest.mark.parametrize("title,mt,per,n,side", [
    ("Winner", "moneyline", "FT", 0, None),
    ("Main result", "moneyline", "FT", 0, None),
    ("1st Half Result", "moneyline", "H1", 0, None),
    ("1st. Quarter - 1x2", "moneyline", "Q1", 0, None),
    ("Draw No Bet", "moneyline", "FT", 2, None),
    ("Asian Handicap", "spread", "FT", 2, None),
    ("2nd Half - Handicap", "spread", "H2", 2, None),
    ("Total Points", "total", "FT", 2, None),
    ("Under/Over 1st Half", "total", "H1", 2, None),
    ("Under/Over - Home Team", "team_total", "FT", 2, "home"),
    ("HT/FT", "htft", "FT", 2, None),
    ("Halftime/Fulltime", "htft", "FT", 2, None),
])
def test_generic_reads_lsport_vocabulary(title, mt, per, n, side):
    c = G.classify(title)
    assert c is not None, title
    assert (c.market_type, c.period, c.n_way, c.team_side) == (mt, per, n, side)


@pytest.mark.parametrize("title", [
    "1st Period Winner", "Under/Over 1st Period", "Asian Handicap 1st Period",   # a period is ambiguous
    "Set Handicap", "1st Set - Winner", "Total sets", "Frame Handicap", "Map 1 Winner",
    "Handicap(1X2)*", "European Handicap", "Odd/Even", "Race To 10 points",
    "Winning margin", "Correct Score", "Both Teams To Score", "Double Chance",
    "Winner & round range", "Will the fight go the distance", "Highest Scoring Half", "",
])
def test_generic_refuses_what_it_cannot_place(title):
    assert G.classify(title) is None, title


def _cell(lab, o):
    return (f'<div class="sport_more_bt DetailSnatch"><div class="sport_more_bt1">{lab}</div>'
            f'<div class="sport_more_bt2">{o}</div></div>')


def _page(markets, eid="1"):
    rows = "".join(
        f'<tr><td class="sport_more_td1">{t}</td><td class="sport_more_td2">'
        f'<div class="sport_more_td_div">{"".join(_cell(l, o) for l, o in sels)}</div></td></tr>'
        for t, sels in markets.items())
    return f'<div class="GContainerList" data-id="{eid}"><table class="game-details">{rows}</table></div>'


def _parse(markets, sport="bandy"):
    return cb_detail.parse_detail_page(
        _page(markets), event_id="1", home="H", away="A", league="L", start_time=NOW,
        fetched_at=NOW, sport_name=sport, classify=G.classify, scope_to_event=True, per_section=True)


def test_auto_n_way_reads_a_1x2_as_three_way_and_a_winner_as_two_way():
    odds = _parse({"Main result": [("1", "2.10"), ("X", "3.40"), ("2", "3.20")],
                   "Winner": [("1", "1.60"), ("2", "2.30")]})
    by = {o.section: o.selections for o in odds}
    assert set(by["Main result"]) == {"home", "draw", "away"}
    assert set(by["Winner"]) == {"home", "away"}


def test_generic_ladders_reach_the_detector():
    from src.anomalies import find_ladder_anomalies
    odds = _parse({"Asian Handicap": [("1(-2.5)", "1.80"), ("2(+2.5)", "1.90"),
                                      ("1(-1.5)", "1.95"), ("2(+1.5)", "1.75"),
                                      ("1(-0.5)", "1.60"), ("2(+0.5)", "2.20")]})
    assert [a.event_id for a in find_ladder_anomalies(odds, markets=("spread", "total"), min_pct=0.0)]


# ── registration ─────────────────────────────────────────────────────────────

@pytest.fixture
def clean_registry():
    reg = CB._SPORT_MODULES
    before = dict(reg); before_g = set(C.GENERIC_SPORTS)
    yield
    for k in list(reg):
        if k not in before:
            del reg[k]
    C.GENERIC_SPORTS.clear(); C.GENERIC_SPORTS.update(before_g)


def test_register_creates_once_and_never_shadows_a_dedicated_module(clean_registry):
    m1 = G.register(999, "5 Bandy")
    m2 = G.register(999, "5 Bandy")
    assert m1 is m2 and m1.SPORT_NAME == "bandy" and m1.GENERIC
    assert CB._SPORT_MODULES["bandy"] is m1 and "bandy" in C.GENERIC_SPORTS
    # same id as a dedicated module, different label → the dedicated one
    from src.scrapers.sports import soccer
    assert G.register(soccer.SPORT_ID, "1239 Football") is soccer
    assert "football" not in CB._SPORT_MODULES


def test_worker_resolves_the_generic_classifier_by_mode_not_registry():
    assert cb_parse_pool._classifier("anything", "generic") is G.classify
    job = {"html": "<div class='GContainerList' data-id='1'><div class='game-table'></div></div>",
           "sport_name": "sumo", "fetched_at": NOW}
    assert cb_parse_pool.parse_list_job(job) == []       # unknown name → generic, no KeyError


def test_generic_sport_is_consistency_eligible(clean_registry):
    G.register(998, "7 Floorball")
    from src.models import Odds
    rows = [Odds(source="crystalbet", sport="floorball", home="H", away="A", market_type="moneyline",
                 period="FT", selections={"home": 1.5, "away": 2.5}, fetched_at=NOW, raw_event_id="1")]
    C.find_consistency_flags(rows)          # must not be filtered out (no assertion on flags)


# ── the sweep, stubbed ───────────────────────────────────────────────────────

def _list_html(games):
    blocks = []
    for eid, code in games:
        blocks.append(f"""
        <div class="GContainerList" data-id="{eid}">
          <div class="game-row" id="G{eid}" data-game-code="{code}">
            <div class="game_hint"><label>Some League</label></div>
            <div class="teams_name">Home {eid} - Away {eid}</div><span class="time">12:00</span></div>
          <div class="game_loading" data-loadinfo='[{{"name":"1","bet":"1.80","handicap":""}},
            {{"name":" 2","bet":"2.05","handicap":""}}]'></div>
        </div>""")
    return ('<html><body><div class="game-table"><div class="x_loop_title_block">'
            + (NOW + timedelta(hours=1)).strftime("%d.%m.%Y") + '</div>' + "".join(blocks) + "</div></body></html>")


def test_discover_activates_only_sports_with_lsport_matches(monkeypatch, clean_registry):
    nav = {16: "1239 Football", 87: "8 SUMO", 38: "3 Bandy", 60: "7 Floorball"}
    lists = {87: _list_html([("a", 20270001), ("b", 20270002)]),
             38: _list_html([("c", 70000001)]),
             60: None}                                   # broken sport
    closed = []
    monkeypatch.setattr(lsport_scan.cb_http, "fetch_nav_sports", lambda: nav)

    async def fake_list(sid):
        if lists[sid] is None:
            raise RuntimeError("boom")
        return lists[sid]
    monkeypatch.setattr(lsport_scan, "_list_raw", fake_list)
    monkeypatch.setattr(lsport_scan, "reset_session", lambda sid: closed.append(sid))

    census, active = asyncio.run(lsport_scan.discover(known_ids={16}, horizon_h=48))
    assert active == ["sumo"]
    assert census["sumo"]["in_horizon"] == 2 and census["sumo"]["generic"] is True
    assert census["bandy"]["in_horizon"] == 0 and census["bandy"]["other"] == 1
    assert census["floorball"]["error"] and "boom" in census["floorball"]["error"]
    assert "football" not in census, "a sport the cycle already scans is skipped by id"
    assert sorted(closed) == [38, 60], "quiet and broken sports drop their session"
    assert "sumo" in CB._SPORT_MODULES and "sumo" in C.GENERIC_SPORTS


def test_badgeless_games_keep_list_rows_without_a_post(monkeypatch, clean_registry):
    """A sumo bout has one 2-way price and no '+N' badge; expanding it can only
    fail. The pass keeps its list rows and never POSTs."""
    G.register(87, "8 SUMO")
    posted = []

    class Sess:
        s = object()
        def fetch_list_raw(self):
            return _list_html([("a", 20270001)])           # no badge in this fixture
        def expand_detail_raw(self, eid):
            posted.append(eid); raise AssertionError("must not expand")
        def close(self): pass
    lsport_scan._sessions.clear()
    monkeypatch.setattr(lsport_scan.cb_http, "CbHttpSession", lambda sid: Sess())
    odds, stats = asyncio.run(lsport_scan.scan_sport("sumo", horizon_h=48))
    lsport_scan._sessions.clear()
    assert posted == [] and stats.list_only == 1 and stats.expanded == 0 and stats.failed == 0
    assert odds and odds[0].sport == "sumo" and odds[0].market_type == "moneyline"


# ── wiring ───────────────────────────────────────────────────────────────────

def test_api_exposes_the_census(monkeypatch):
    from fastapi.testclient import TestClient
    import src.app as app
    monkeypatch.setattr(app, "_lsport_census", {"sumo": {"sport_id": 87, "in_horizon": 8, "generic": True}})
    monkeypatch.setattr(app, "_lsport_census_at", NOW)
    monkeypatch.setattr(app, "_lsport_generic_active", ["sumo"])
    j = TestClient(app.app).get("/api/new_inconsistencies/status").json()
    assert j["census"]["sumo"]["in_horizon"] == 8 and j["generic_active"] == ["sumo"]
    assert j["census_at"] == NOW.isoformat() and j["discover_sec"] >= 300


def test_discover_cadence_is_configurable(tmp_path, monkeypatch):
    from src import runtime_config
    monkeypatch.setattr(runtime_config, "CONFIG_PATH", tmp_path / "rc.json")
    runtime_config.load(force=True)
    assert runtime_config.get()["cadence"]["lsport_discover_sec"] == 1800
    assert runtime_config.update({"cadence": {"lsport_discover_sec": 10}})["cadence"]["lsport_discover_sec"] == 300
    runtime_config.load(force=True)


def test_tab_shows_the_census_line():
    t = (ROOT / "static/new_inconsistencies.html").read_text(encoding="utf-8")
    assert "LSport elsewhere" in t and "meta.census" in t
