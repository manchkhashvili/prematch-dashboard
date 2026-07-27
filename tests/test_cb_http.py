"""
Tests for src/scrapers/cb_http.py — the browser-free CB transport.

Fully offline: curl_cffi Session is replaced by a fake that replays canned
ASP.NET responses. Covers the wire-format helpers (delta walking, hidden-field
scraping), the session lifecycle (warm → list → expand → collapse), the
§5 hidden-input harvest trick, and the transport dispatch in crystalbet.py.

Run from prematch/:
    python -m pytest tests/ -v
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scrapers import cb_http  # noqa: E402
from src.scrapers.cb_http import (  # noqa: E402
    CbHttpSession,
    all_panels_html,
    apply_delta_hidden,
    hidden_fields,
    panel_html,
    walk_delta,
)


# ── Wire-format helpers ───────────────────────────────────────────────────────

def _seg(seg_type: str, seg_id: str, content: str) -> str:
    return f"{len(content)}|{seg_type}|{seg_id}|{content}|"


DELTA = (
    _seg("#", "", "1")
    + _seg("updatePanel", "ctl00_UpdatePanelsHolder", "<div id='holder'></div>")
    + _seg("updatePanel", "ctl00_RepeaterChampionatX",
           "<table class='game-details'><tr></tr></table>")
    + _seg("hiddenField", "__VIEWSTATE", "VS-NEW")
    + _seg("hiddenField", "__EVENTTARGET", "")
)


class TestWalkDelta:
    def test_yields_all_segments(self):
        segs = list(walk_delta(DELTA))
        assert ("updatePanel", "ctl00_UpdatePanelsHolder", "<div id='holder'></div>") in segs
        assert ("hiddenField", "__VIEWSTATE", "VS-NEW") in segs

    def test_content_with_pipes_survives(self):
        # length-prefixed format: content containing '|' must not break framing
        tricky = "a|b|c"
        delta = _seg("updatePanel", "P1", tricky) + _seg("hiddenField", "F", "v")
        segs = list(walk_delta(delta))
        assert ("updatePanel", "P1", tricky) in segs
        assert ("hiddenField", "F", "v") in segs

    def test_garbage_prefix_skipped(self):
        segs = list(walk_delta("notanumber|" + DELTA))
        assert any(s[0] == "hiddenField" for s in segs)


class TestDeltaHelpers:
    def test_apply_delta_hidden_updates_viewstate(self):
        fields = {"__VIEWSTATE": "OLD", "keep": "me"}
        apply_delta_hidden(fields, DELTA)
        assert fields["__VIEWSTATE"] == "VS-NEW"
        assert fields["keep"] == "me"

    def test_panel_html_matches_by_keyword(self):
        assert "game-details" in panel_html(DELTA, "RepeaterChampionat")
        assert panel_html(DELTA, "NoSuchPanel") == ""

    def test_all_panels_concatenates(self):
        blob = all_panels_html(DELTA)
        assert "holder" in blob and "game-details" in blob


class TestNormalizeHtml:
    def test_repairs_unclosed_cells_like_a_browser(self):
        # CB's raw panels leave <td>/<div> unclosed; html.parser then nests
        # each next cell INSIDE the previous one and selection harvesting
        # over-collects. After normalize_html the tds must be siblings.
        from bs4 import BeautifulSoup
        from src.scrapers.cb_http import normalize_html

        raw = ("<table class='game-details'><tr>"
               "<td class='sport_more_td1'>Title A"
               "<td class='sport_more_td2'><div class='sport_more_bt DetailSnatch'>"
               "<div class='sport_more_bt1'>1</div><div class='sport_more_bt2'>1.50</div></div>"
               "<td class='sport_more_td3'>Title B"
               "<td class='sport_more_td4'><div class='sport_more_bt DetailSnatch'>"
               "<div class='sport_more_bt1'>2</div><div class='sport_more_bt2'>2.50</div></div>"
               "</tr></table>")
        fixed = BeautifulSoup(normalize_html(raw), "html.parser")
        tds = fixed.select("tr > td")
        assert len(tds) == 4
        # td2 must contain ONLY its own snatch cell, not td3/td4's content
        td2 = fixed.select_one("td.sport_more_td2")
        assert len(td2.select("div.sport_more_bt.DetailSnatch")) == 1


class TestHiddenFields:
    def test_scrapes_name_value_pairs(self):
        html = (
            "<input type='hidden' name='__VIEWSTATE' value='abc'/>"
            '<input type="hidden" name="HiddenFieldExpandedGameId" value="123"/>'
            "<input type='text' name='not_hidden' value='x'/>"
            "<input type='hidden' name='empty'/>"
        )
        f = hidden_fields(html)
        assert f == {"__VIEWSTATE": "abc",
                     "HiddenFieldExpandedGameId": "123",
                     "empty": ""}


# ── Fake curl_cffi session ────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code
        self.cookies = {"ASP.NET_SessionId": "fake"}

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(f"status {self.status_code}")


GET_PAGE = (
    "<!DOCTYPE html><html><form>"
    "<input type='hidden' name='__VIEWSTATE' value='VS0'/>"
    "<input type='hidden' name='__EVENTVALIDATION' value='EV0'/>"
    "</form></html>"
)
ENGLISH_PAGE = GET_PAGE.replace("VS0", "VS1")

SPORT_DELTA = (
    _seg("updatePanel", "ctl00_UpdatePanelGames",
         "<input type='hidden' name='PanelOnlyField' value='ride-along'/>")
    + _seg("hiddenField", "__VIEWSTATE", "VS2")
)
LIST_DELTA = (
    _seg("updatePanel", "ctl00_UpdatePanelGames",
         "<div class='game-table'><div class='GContainerList' data-id='42'>"
         "</div></div>")
    + _seg("hiddenField", "__VIEWSTATE", "VS3")
)
EXPAND_DELTA = (
    _seg("updatePanel", "ctl00_UpdatePanelsHolder", "<div/>")
    + _seg("updatePanel", "ctl00_RepeaterChampionatA",
           "<div class='GContainerList' data-id='42'>"
           "<table class='game-details'></table></div>")
    + _seg("hiddenField", "__VIEWSTATE", "VS4")
)
COLLAPSE_DELTA = _seg("hiddenField", "__VIEWSTATE", "VS5")
EMPTY_LIST_DELTA = _seg("hiddenField", "__VIEWSTATE", "VS9")


class FakeSession:
    """Replays a scripted sequence of responses; records request bodies."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.posts: list[dict] = []
        self.cookies = {"ASP.NET_SessionId": "fake"}
        self.closed = False

    def get(self, url, **kw):
        return FakeResponse(GET_PAGE)

    def post(self, url, *, data="", **kw):
        from urllib.parse import parse_qs
        body = {k: v[0] for k, v in parse_qs(data, keep_blank_values=True).items()}
        self.posts.append(body)
        return FakeResponse(self.responses.pop(0))

    def close(self):
        self.closed = True


def make_warmed(monkeypatch, extra_responses) -> tuple[CbHttpSession, FakeSession]:
    fake = FakeSession([ENGLISH_PAGE, SPORT_DELTA, *extra_responses])
    monkeypatch.setattr(
        "curl_cffi.requests.Session", lambda *a, **kw: fake,
    )
    sess = CbHttpSession(17)
    sess.warm()
    return sess, fake


# ── Session lifecycle ─────────────────────────────────────────────────────────

class TestWarm:
    def test_warm_sequence_and_viewstate(self, monkeypatch):
        sess, fake = make_warmed(monkeypatch, [])
        # english flip is a FULL postback: no __ASYNCPOST, target = ImageButtonEn
        flip = fake.posts[0]
        assert flip["__EVENTTARGET"] == "ctl00$ctl00$ImageButtonEn"
        assert "__ASYNCPOST" not in flip
        # sport select is async with the sport id as argument
        sel = fake.posts[1]
        assert sel["__EVENTARGUMENT"] == "17"
        assert sel["__ASYNCPOST"] == "true"
        # viewstate refreshed from the sport-select delta
        assert sess.fields["__VIEWSTATE"] == "VS2"

    def test_panel_hidden_inputs_harvested(self, monkeypatch):
        # the §5 trick: inputs rendered inside panels (not hiddenField segs)
        # must ride along on subsequent POSTs
        sess, fake = make_warmed(monkeypatch, [LIST_DELTA])
        assert sess.fields["PanelOnlyField"] == "ride-along"
        sess.fetch_list_html()
        assert fake.posts[-1]["PanelOnlyField"] == "ride-along"


class TestFetchListHtml:
    def test_returns_games_panel(self, monkeypatch):
        sess, fake = make_warmed(monkeypatch, [LIST_DELTA])
        panel = sess.fetch_list_html()
        # returns an html5lib soup now (cb_http.normalize_soup) — the parsers
        # consume it directly instead of re-parsing a serialized string
        assert panel.select_one("div.GContainerList") is not None
        body = fake.posts[-1]
        hf = "ctl00$ctl00$ContentPlaceHolder1$ContentPlaceHolder2$HiddenFieldUpdateChampionatsParam"
        assert body[hf] == "SelectAllChampionats:17"
        assert sess.fields["__VIEWSTATE"] == "VS3"

    def test_raises_when_no_games_panel(self, monkeypatch):
        sess, _ = make_warmed(monkeypatch, [EMPTY_LIST_DELTA])
        with pytest.raises(RuntimeError, match="no.*UpdatePanelGames"):
            sess.fetch_list_html()

    def test_raises_on_page_redirect(self, monkeypatch):
        sess, _ = make_warmed(monkeypatch, ["1|#||pageRedirect||/Login.aspx|"])
        with pytest.raises(RuntimeError, match="pageRedirect"):
            sess.fetch_list_html()


class TestExpandDetail:
    def test_returns_panels_and_collapses(self, monkeypatch):
        sess, fake = make_warmed(monkeypatch, [EXPAND_DELTA, COLLAPSE_DELTA])
        blob = sess.expand_detail_html("42")
        assert blob.select_one("table.game-details") is not None
        expand_body, collapse_body = fake.posts[-2], fake.posts[-1]
        assert expand_body["__EVENTARGUMENT"] == "ExpandDetail:42"
        assert collapse_body["__EVENTARGUMENT"] == "CollapseDetail:42"
        assert sess.fields["__VIEWSTATE"] == "VS5"

    def test_collapse_failure_is_swallowed(self, monkeypatch):
        sess, fake = make_warmed(monkeypatch, [EXPAND_DELTA])

        real_post = sess._post
        calls = {"n": 0}

        def flaky(body, **kw):
            calls["n"] += 1
            if "CollapseDetail" in body.get("__EVENTARGUMENT", ""):
                raise RuntimeError("boom")
            return real_post(body, **kw)

        sess._post = flaky
        blob = sess.expand_detail_html("42")
        assert blob.select_one("table.game-details") is not None


# ── Module-level wrappers ─────────────────────────────────────────────────────

class TestModuleWrappers:
    def setup_method(self):
        cb_http._sessions.clear()

    def test_fetch_list_html_warms_then_fetches(self, monkeypatch):
        fake = FakeSession([ENGLISH_PAGE, SPORT_DELTA, LIST_DELTA])
        monkeypatch.setattr("curl_cffi.requests.Session", lambda *a, **kw: fake)
        panel = asyncio.run(cb_http.fetch_list_html(17))
        assert panel.select_one("div.GContainerList") is not None

    def test_expand_requires_warm_session(self):
        with pytest.raises(RuntimeError, match="not warmed"):
            asyncio.run(cb_http.expand_detail_html(17, "42"))

    def test_reset_session_closes_and_drops(self, monkeypatch):
        fake = FakeSession([ENGLISH_PAGE, SPORT_DELTA, LIST_DELTA])
        monkeypatch.setattr("curl_cffi.requests.Session", lambda *a, **kw: fake)
        asyncio.run(cb_http.fetch_list_html(17))
        cb_http.reset_session(17)
        assert fake.closed
        assert 17 not in cb_http._sessions

    def test_rewarm_after_expiry(self, monkeypatch):
        fake = FakeSession([ENGLISH_PAGE, SPORT_DELTA, LIST_DELTA,
                            ENGLISH_PAGE, SPORT_DELTA, LIST_DELTA])
        monkeypatch.setattr("curl_cffi.requests.Session", lambda *a, **kw: fake)
        asyncio.run(cb_http.fetch_list_html(17))
        sess = cb_http._sessions[17]
        sess.warmed_at -= cb_http.REWARM_AFTER_SEC + 1
        asyncio.run(cb_http.fetch_list_html(17))
        # 2 warm flows of 2 POSTs each + 2 list POSTs
        assert len(fake.posts) == 6


# ── crystalbet.py transport dispatch ──────────────────────────────────────────

class TestTransportDispatch:
    def test_default_transport_is_playwright(self):
        from src.scrapers import crystalbet
        assert crystalbet.CB_TRANSPORT == "playwright"
        assert crystalbet._USE_HTTP_TRANSPORT is False

    def test_expand_game_routes_to_http(self, monkeypatch):
        from datetime import datetime, timezone
        from src.scrapers import crystalbet
        from src.scrapers.crystalbet import _GameOnList
        from src.scrapers.sports import basketball

        monkeypatch.setattr(crystalbet, "_USE_HTTP_TRANSPORT", True)

        async def fake_expand(sport_id, game_id):
            assert sport_id == basketball.SPORT_ID
            assert game_id == "42"
            return ("<div class='GContainerList' data-id='42'>"
                    "<table class='game-details'></table></div>")

        monkeypatch.setattr(cb_http, "expand_detail_html", fake_expand)
        game = _GameOnList(event_id="42", home="A", away="B", league="L",
                           start_time=None, loadinfo="", list_odds=[])
        odds = asyncio.run(crystalbet._expand_game(
            game, datetime.now(tz=timezone.utc), basketball, None,
        ))
        assert odds == []  # empty table parses to no Odds, but no exception

    def test_expand_game_http_raises_without_detail_table(self, monkeypatch):
        from datetime import datetime, timezone
        from src.scrapers import crystalbet
        from src.scrapers.crystalbet import _GameOnList
        from src.scrapers.sports import basketball

        monkeypatch.setattr(crystalbet, "_USE_HTTP_TRANSPORT", True)

        async def fake_expand(sport_id, game_id):
            return "<div>quickbet only</div>"

        monkeypatch.setattr(cb_http, "expand_detail_html", fake_expand)
        game = _GameOnList(event_id="42", home="A", away="B", league="L",
                           start_time=None, loadinfo="", list_odds=[])
        with pytest.raises(RuntimeError, match="no detail table"):
            asyncio.run(crystalbet._expand_game(
                game, datetime.now(tz=timezone.utc), basketball, None,
            ))

    def test_refresh_list_html_http_retries_once(self, monkeypatch):
        from src.scrapers import crystalbet

        monkeypatch.setattr(crystalbet, "_USE_HTTP_TRANSPORT", True)
        calls = {"fetch": 0, "reset": 0}

        async def flaky_fetch(sport_id):
            calls["fetch"] += 1
            if calls["fetch"] == 1:
                raise RuntimeError("transient")
            return "<html/>"

        def fake_reset(sport_id):
            calls["reset"] += 1

        monkeypatch.setattr(cb_http, "fetch_list_html", flaky_fetch)
        monkeypatch.setattr(cb_http, "reset_session", fake_reset)
        page, html = asyncio.run(crystalbet._refresh_list_html_for_sport(
            17, "basketball", headed=False,
        ))
        assert page is None
        assert html == "<html/>"
        assert calls == {"fetch": 2, "reset": 1}
