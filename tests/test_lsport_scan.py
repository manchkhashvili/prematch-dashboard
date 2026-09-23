"""New inconsistencies — the LSport-only CrystalBet cycle (2026-09-21).

CrystalBet prices its board from two feeds; the owner knows from bet tickets
that the mistakes live in LSport's. The feed is readable off the list view:
`data-game-code` on the game-row title block is the provider's own fixture id,
and the two id spaces do not overlap (LSport ~20 M, the other feed ~63–75 M —
see src/scrapers/cb_provider.py for the measurement). These tests pin:

  * the id → provider reading, and that the list parser stamps it on the game
    AND on its list-view rows;
  * the game filter the cycle runs on (LSport only, in horizon, soonest first);
  * one scan pass against a stubbed session: expansions parsed with the
    permissive/per-section classifier, provider stamped on every row, budget
    and switch-off truncation keeping the tail's list-view rows;
  * the /api/new_inconsistencies endpoints serving the cycle's OWN stores and
    never the Anomalies tab's;
  * the runtime-config keys and the tab/nav wiring.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from src import lsport_scan, runtime_config
from src.models import Odds
from src.scrapers import cb_provider
from src.scrapers import crystalbet as CB

NOW = datetime(2026, 9, 21, 18, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"


# ── provider from the fixture id ─────────────────────────────────────────────

@pytest.mark.parametrize("code,expected", [
    (20_264_337, "lsport"),      # measured LSport range 19.5–20.3 M
    (19_586_954, "lsport"),
    (39_999_999, "lsport"),
    (40_000_000, "other"),
    (68_932_806, "other"),       # measured other-feed range 63–75 M
    (74_933_034, "other"),
    (None, None),
    (0, None),
])
def test_provider_from_game_code(code, expected):
    assert cb_provider.provider_of(code) == expected


def _container(inner: str, event_id: str = "3235754054"):
    return BeautifulSoup(
        f'<div class="GContainerList" data-id="{event_id}">{inner}</div>',
        "html.parser").select_one("div.GContainerList")


def test_game_code_is_read_off_the_title_block():
    c = _container('<div class="game-row x_loop_game_title_block" id="G3235754054" '
                   'data-game-code="20264337"></div>')
    assert cb_provider.game_code_of(c) == 20264337


def test_a_game_with_no_code_is_unknown_not_guessed():
    assert cb_provider.game_code_of(_container("<div class='game-row'></div>")) is None
    assert cb_provider.game_code_of(_container(
        '<div class="game-row" data-game-code=""></div>')) is None
    assert cb_provider.game_code_of(_container(
        '<div class="game-row" data-game-code="hAfMDh4C"></div>')) is None


# ── the list parser stamps it ────────────────────────────────────────────────

def _list_page(games: list[tuple[str, str | None]]) -> str:
    blocks = []
    for eid, code in games:
        code_attr = f' data-game-code="{code}"' if code is not None else ""
        blocks.append(f"""
        <div class="GContainerList" data-id="{eid}">
          <div class="game-row x_loop_game_title_block" id="G{eid}"{code_attr}>
            <div class="game_hint"><label>Estonia, Youth League. Under 19</label></div>
            <div class="teams_name">Home {eid} - Away {eid}</div>
            <span class="time">21:00</span>
          </div>
          <div class="x_loop_game_active_add">+117</div>
          <div class="game_loading" data-loadinfo='[{{"name":"1","bet":"1.80","handicap":""}},
            {{"name":" 2","bet":"2.05","handicap":""}}]'></div>
        </div>""")
    return ('<html><body><div class="game-table">'
            '<div class="x_loop_title_block">21.09.2026</div>'
            + "".join(blocks) + "</div></body></html>")


def test_list_parser_populates_game_code_and_provider():
    html = _list_page([("1", "20264337"), ("2", "68932806"), ("3", None)])
    games = CB._extract_games_from_list_html(html, NOW)
    by = {g.event_id: g for g in games}
    assert (by["1"].game_code, by["1"].provider) == (20264337, "lsport")
    assert (by["2"].game_code, by["2"].provider) == (68932806, "other")
    assert (by["3"].game_code, by["3"].provider) == (None, None)


def test_list_view_rows_carry_the_provider():
    """A game that is never expanded still says which feed priced it."""
    html = _list_page([("1", "20264337"), ("2", "68932806")])
    games = CB._extract_games_from_list_html(html, NOW)
    for g in games:
        assert g.list_odds, "fixture should parse a moneyline row"
        assert {o.provider for o in g.list_odds} == {g.provider}


def test_odds_provider_defaults_to_none_everywhere_else():
    o = Odds(source="pinnacle", sport="soccer", home="A", away="B",
             market_type="moneyline", period="FT",
             selections={"home": 1.9, "away": 2.0}, fetched_at=NOW)
    assert o.provider is None


# ── the filter ───────────────────────────────────────────────────────────────

def _game(eid, provider, hours, code=None):
    return CB._GameOnList(
        event_id=eid, home=f"H{eid}", away=f"A{eid}", league="L",
        start_time=NOW + timedelta(hours=hours) if hours is not None else None,
        loadinfo="", list_odds=[], market_count=100,
        game_code=code, provider=provider)


def test_select_games_keeps_lsport_in_horizon_soonest_first():
    games = [_game("a", "other", 1), _game("b", "lsport", 30), _game("c", "lsport", 2),
             _game("d", "lsport", 100), _game("e", None, 1), _game("f", "lsport", None)]
    picked, census = lsport_scan.select_games(games, NOW, horizon_h=48)
    assert [g.event_id for g in picked] == ["c", "b"]
    assert census == {"other": 1, "lsport": 4, "unknown": 1}


def test_select_games_unknown_provider_is_not_lsport():
    picked, _ = lsport_scan.select_games([_game("e", None, 1)], NOW, 48)
    assert picked == []


# ── one pass against a stubbed session ───────────────────────────────────────

def _lsport_list_html(n: int) -> str:
    """Basketball list format (a 2-entry loadinfo parses to one moneyline row),
    so every game has list-view rows to fall back on."""
    games = [(str(1000 + i), str(20_200_000 + i)) for i in range(n)]
    games.append(("9999", "70000000"))     # one other-feed game, never expanded
    return _list_page(games)


def _detail_blob(event_id: str) -> str:
    """A minimal ExpandDetail panel: one 2-way handicap ladder with a crossing
    rung (home gets MORE points yet its odds get LONGER), so the pass has an
    anomaly to find."""
    def cell(lab, o):
        return (f'<div class="sport_more_bt DetailSnatch">'
                f'<div class="sport_more_bt1">{lab}</div>'
                f'<div class="sport_more_bt2">{o}</div></div>')
    sels = (cell("1(-2.5)", "1.80") + cell("2(+2.5)", "1.90")
            + cell("1(-1.5)", "1.95") + cell("2(+1.5)", "1.75")
            + cell("1(-0.5)", "1.60") + cell("2(+0.5)", "2.20"))
    return (f'<div class="GContainerList" data-id="{event_id}">'
            f'<table class="game-details"><tr>'
            f'<td class="sport_more_td1">Asian Handicap</td>'
            f'<td class="sport_more_td2"><div class="sport_more_td_div">{sels}</div></td>'
            f'</tr></table></div>')


class _FakeSession:
    """Stands in for cb_http.CbHttpSession: records what was asked."""
    def __init__(self, sport_id, *, list_html, fail_ids=(), slow_sec=0.0):
        self.sport_id = sport_id
        self.s = None
        self.list_html = list_html
        self.fail_ids = set(fail_ids)
        self.slow_sec = slow_sec
        self.expanded: list[str] = []
        self.closed = False

    def fetch_list_raw(self):
        self.s = object()          # "warmed"
        return self.list_html

    def expand_detail_raw(self, event_id):
        import time
        self.expanded.append(event_id)
        if self.slow_sec:
            time.sleep(self.slow_sec)
        if event_id in self.fail_ids:
            raise RuntimeError("boom")
        return _detail_blob(event_id)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_session(monkeypatch):
    holder = {}

    def factory(list_html, **kw):
        def _make(sport_id):
            holder["sess"] = _FakeSession(sport_id, list_html=list_html, **kw)
            return holder["sess"]
        lsport_scan._sessions.clear()
        monkeypatch.setattr(lsport_scan.cb_http, "CbHttpSession", _make)
        return holder
    yield factory
    lsport_scan._sessions.clear()


def test_scan_expands_only_lsport_games_and_stamps_the_provider(fake_session):
    holder = fake_session(_lsport_list_html(3))
    odds, stats = asyncio.run(lsport_scan.scan_sport("basketball", horizon_h=48))
    sess = holder["sess"]
    assert sorted(sess.expanded) == ["1000", "1001", "1002"]
    assert "9999" not in sess.expanded
    assert stats.board == 4 and stats.lsport == 3 and stats.in_horizon == 3
    assert stats.expanded == 3 and stats.failed == 0 and stats.truncated_at is None
    assert stats.by_provider == {"lsport": 3, "other": 1}
    assert odds and all(o.provider == "lsport" for o in odds)
    assert all(o.source == "crystalbet" for o in odds)
    # per-section ladder mode: the section title rides on every row
    assert {o.section for o in odds} == {"Asian Handicap"}
    # and the detector the tab runs finds the crossing rung on every game
    anoms, flags = lsport_scan.detect(odds)
    assert {a.event_id for a in anoms} == {"1000", "1001", "1002"}


def test_a_failed_expansion_keeps_the_games_list_view_rows(fake_session):
    fake_session(_lsport_list_html(2), fail_ids={"1001"})
    odds, stats = asyncio.run(lsport_scan.scan_sport("basketball", horizon_h=48))
    assert stats.expanded == 1 and stats.failed == 1
    kept = [o for o in odds if o.raw_event_id == "1001"]
    assert kept and all(o.section is None for o in kept), "list-view rows, not detail"
    assert all(o.provider == "lsport" for o in kept)


def test_the_budget_truncates_and_the_tail_keeps_list_view_rows(fake_session):
    holder = fake_session(_lsport_list_html(4), slow_sec=0.05)
    odds, stats = asyncio.run(lsport_scan.scan_sport("basketball", horizon_h=48, max_sec=0.08))
    assert stats.truncated_at is not None and stats.truncated_at < 4
    assert len(holder["sess"].expanded) == stats.expanded
    ids = {o.raw_event_id for o in odds}
    assert ids == {"1000", "1001", "1002", "1003"}, "the tail is not dropped"


def test_switching_the_scan_off_stops_the_pass(fake_session):
    holder = fake_session(_lsport_list_html(4))
    calls = {"n": 0}

    def should_continue():
        calls["n"] += 1
        return calls["n"] <= 2          # allow two games, then "off"
    odds, stats = asyncio.run(lsport_scan.scan_sport(
        "basketball", horizon_h=48, should_continue=should_continue))
    assert stats.expanded == 2 and stats.truncated_at == 2
    assert len(holder["sess"].expanded) == 2


def test_progress_callback_publishes_partial_boards(fake_session, monkeypatch):
    monkeypatch.setattr(lsport_scan, "PROGRESS_EVERY", 2)
    fake_session(_lsport_list_html(5))
    seen = []

    async def on_progress(partial, done, total):
        seen.append((len({o.raw_event_id for o in partial}), done, total))
    asyncio.run(lsport_scan.scan_sport("basketball", horizon_h=48, on_progress=on_progress))
    assert seen == [(2, 2, 5), (4, 4, 5)]


def test_the_cycle_owns_its_session_not_the_shared_one(fake_session):
    """The shared per-sport session is guarded by crystalbet's sport lock; the
    cycle must never touch it."""
    from src.scrapers import cb_http
    fake_session(_lsport_list_html(1))
    shared_before = dict(cb_http._sessions)     # whatever other tests left there
    asyncio.run(lsport_scan.scan_sport("basketball", horizon_h=48))
    assert 17 in lsport_scan._sessions
    assert cb_http._sessions == shared_before, "the shared store must be untouched"
    assert lsport_scan._sessions[17] is not cb_http._sessions.get(17)
    lsport_scan.close_all()
    assert lsport_scan._sessions == {}


# ── the endpoints serve the cycle's own stores ───────────────────────────────

@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    import src.app as app
    return TestClient(app.app), app


def _row(sport, eid, pct=5.0):
    return {"book": "cb", "sport": sport, "league": "L", "match_label": f"H{eid} — A{eid}",
            "home": f"H{eid}", "away": f"A{eid}", "cb_event_id": eid, "start_time": None,
            "period": "FT", "market_type": "spread", "submarket": None, "section": "AH",
            "market": "AH", "side": "home", "line_lo": -1.5, "line_hi": -0.5,
            "odds_lo": 1.9, "odds_hi": 2.1, "expected": "down", "pct": pct, "delta": 0.2,
            "bet_line": -0.5, "bet_odds": 2.1}


def _flag(sport, eid, sev):
    return {"book": "cb", "sport": sport, "league": "L", "match_label": f"H{eid} — A{eid}",
            "home": f"H{eid}", "away": f"A{eid}", "cb_event_id": eid, "book_event_id": eid,
            "start_time": None, "kind": "pickem_dominance", "periods": "FT",
            "detail": "d", "severity": sev, "outcome": "home", "odds": 2.0}


def test_endpoint_serves_only_the_lsport_stores(client, monkeypatch):
    c, app = client
    monkeypatch.setattr(app, "_lsport_anomalies", {"soccer": [_row("soccer", "1"), _row("soccer", "2", 0.2)]})
    monkeypatch.setattr(app, "_lsport_consistency", {"soccer": [_flag("soccer", "1", 9.0), _flag("soccer", "2", 1.0)]})
    monkeypatch.setattr(app, "_lsport_odds", {"soccer": []})
    monkeypatch.setattr(app, "_lsport_at", {"soccer": NOW.isoformat()})
    monkeypatch.setattr(app, "_lsport_stats", {"soccer": {"board": 4, "lsport": 3}})
    # The Anomalies tab's stores hold something else entirely — it must NOT leak.
    monkeypatch.setattr(app, "_recent_anomalies", [_row("basketball", "X", 50.0)])
    monkeypatch.setattr(app, "_recent_consistency", [_flag("basketball", "X", 99.0)])
    j = c.get("/api/new_inconsistencies", params={"min_pct": 0.5, "min_severity": 2.0}).json()
    assert [r["cb_event_id"] for r in j["anomalies"]] == ["1"]
    assert [f["cb_event_id"] for f in j["consistency"]] == ["1"]
    assert j["count"] == 1 and j["consistency_count"] == 1
    assert j["computed_at"] == NOW.isoformat()
    assert j["stats"]["soccer"]["lsport"] == 3
    assert "scan_sec" in j and "horizon_h" in j and "sports" in j
    # and the Anomalies tab did not pick up the LSport rows either
    ja = c.get("/api/anomalies").json()
    assert "1" not in {r["cb_event_id"] for r in ja["anomalies"]}


def test_status_and_alert_feeds_mirror_the_shapes(client, monkeypatch):
    c, app = client
    monkeypatch.setattr(app, "_lsport_anomalies", {"tennis": [_row("tennis", "7", 12.0)]})
    monkeypatch.setattr(app, "_lsport_consistency", {"tennis": [_flag("tennis", "7", 3.0)]})
    monkeypatch.setattr(app, "_lsport_at", {"tennis": NOW.isoformat()})
    st = c.get("/api/new_inconsistencies/status").json()
    assert st["anomalies"] == 1 and st["consistency"] == 1
    al = c.get("/api/new_inconsistencies/alerts").json()
    assert set(al) >= {"enabled", "computed_at", "ladders", "consistency"}
    assert al["ladders"][0]["event_id"] == "7" and al["ladders"][0]["pct"] == 12.0
    assert al["consistency"][0]["event_id"] == "7" and al["consistency"][0]["kind"] == "pickem_dominance"
    # the compact builders default to the Anomalies stores when given no source
    assert app._ladder_alert_rows() == app._ladder_alert_rows(None)


def test_enrichment_uses_the_cycles_own_odds(client, monkeypatch):
    """Pin context on the new tab must be computed against the LSport
    snapshot, not the anomaly scans' odds — a row's event lives only there."""
    c, app = client
    seen = {}

    def fake_attach(rows, cb_odds, pin_odds, moves):
        seen["cb_odds"] = cb_odds
        return rows
    monkeypatch.setattr(app, "_attach_pin_to_anomalies", fake_attach)
    marker = [Odds(source="crystalbet", sport="soccer", home="H1", away="A1",
                   market_type="spread", period="FT", selections={"home": 1.9, "away": 2.1},
                   fetched_at=NOW, line=-0.5, raw_event_id="1", provider="lsport")]
    monkeypatch.setattr(app, "_lsport_anomalies", {"soccer": [_row("soccer", "1")]})
    monkeypatch.setattr(app, "_lsport_consistency", {})
    monkeypatch.setattr(app, "_lsport_odds", {"soccer": marker})
    monkeypatch.setattr(app, "_extra_anom_odds", {"soccer": []})
    c.get("/api/new_inconsistencies")
    assert seen["cb_odds"] is marker


# ── config + wiring ──────────────────────────────────────────────────────────

def test_runtime_config_has_the_cycles_knobs(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_config, "CONFIG_PATH", tmp_path / "rc.json")
    runtime_config.load(force=True)
    assert "lsport_scan" in runtime_config.SCANS
    cfg = runtime_config.get()
    assert "lsport_scan" in cfg["scans"]
    assert cfg["cadence"]["lsport_scan_sec"] == 180
    assert cfg["limits"]["lsport_horizon_h"] == 48.0
    assert cfg["limits"]["lsport_max_sec"] == 120.0
    out = runtime_config.update({"cadence": {"lsport_scan_sec": 5},
                                 "limits": {"lsport_horizon_h": 1000, "lsport_max_sec": -3}})
    assert out["cadence"]["lsport_scan_sec"] == 30        # floor
    assert out["limits"]["lsport_horizon_h"] == 240.0     # ceiling
    assert out["limits"]["lsport_max_sec"] == 0.0         # floor
    runtime_config.load(force=True)


def test_the_tab_points_at_its_own_endpoint_and_shares_no_alert_keys():
    t = (STATIC / "new_inconsistencies.html").read_text(encoding="utf-8")
    assert "/api/new_inconsistencies?" in t
    assert "/api/anomalies" not in t
    # The Anomalies tab's alert panel writes localStorage keys the shared
    # alerts.js reads (anom_*alert*). This page must not write them — two
    # panels editing one setting would be a silent fight.
    assert not re.search(r'"anom_[a-z_]*alert[a-z_]*"', t)
    assert "newinc_lad_min_odds" in t


def test_every_navigating_page_links_the_new_tab():
    pages = [p for p in STATIC.glob("*.html")
             if '<a href="/anomalies.html"' in p.read_text(encoding="utf-8")]
    assert pages
    for p in pages:
        assert 'href="/new_inconsistencies.html"' in p.read_text(encoding="utf-8"), p.name


def test_config_tab_describes_the_new_knobs():
    t = (STATIC / "config.html").read_text(encoding="utf-8")
    for key in ("lsport_scan:", "lsport_scan_sec:", "lsport_horizon_h:", "lsport_max_sec:"):
        assert key in t, key


def test_the_loop_is_registered_at_startup():
    src = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
    assert 'name="lsport_scan_loop"' in src
    assert "_ls.close_all()" in src
