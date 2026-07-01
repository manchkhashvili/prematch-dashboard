"""
Browser-free CrystalBet transport — plain HTTP ASP.NET postbacks via curl_cffi.

Drop-in TRANSPORT replacement for the Playwright layer in crystalbet.py:
it produces the same two HTML artifacts the Playwright path produces —
  1. the list-view HTML (the UpdatePanelGames panel after
     SelectAllChampionats:<sport_id>), consumed by
     crystalbet._extract_games_from_list_html unchanged, and
  2. the per-game detail HTML (the updatePanel segments of an
     ExpandDetail:<game_id> delta), consumed by cb_detail.parse_detail_page
     (scope_to_event=True) unchanged.
All parsing stays in the existing modules; this file only moves bytes.

Protocol (verified live 2026-06-12, see scripts/probe_cb_http.py and
prematch_v2/docs/crystalbet.md for the wire-format reference):

  * A scrape session is: GET Sports.aspx (cookies + ~6 KB __VIEWSTATE) →
    full-postback English flip → DoSportTypePostBack(sport_id) →
    then one DoChampionatPostBack("SelectAllChampionats:<sport_id>") per
    list refresh. SelectAllChampionats re-selects ALL championships every
    POST, so new championships are picked up each cycle automatically —
    same semantics as the Playwright path clicking the same button.
  * UpdatePanel POSTs send the full hidden-field set + the
    MasteScriptManager control pair + __ASYNCPOST=true, and get back the
    delta format `len|type|id|content|...`. __VIEWSTATE must be refreshed
    from the delta's hiddenField segments before the next POST.
  * THE one trick (v2's headline finding): panels carry NEW hidden
    <input>s that are NOT in the hiddenField delta segments (e.g.
    HiddenFieldExpandedGameId). A real browser form-serializes the live
    DOM so they ride along; we must re-scan hidden inputs out of every
    returned updatePanel's HTML and merge them into the field set, or
    ExpandDetail returns a near-empty delta.
  * ExpandDetail's detail table comes back inside the RepeaterChampionat
    updatePanel (NOT UpdatePanelGames) — we return all updatePanel
    segments concatenated and let parse_detail_page scope by event id.
  * After parsing a detail we POST CollapseDetail so the server-side view
    (and therefore every later response) stays small.

Sessions are per-sport (mirrors the one-BrowserContext-per-sport rule:
CB's session-bound currentSport stomps itself otherwise) and re-warm
hourly or on failure. curl_cffi's Session is sync; callers get async
wrappers via asyncio.to_thread. Per-sport serialization is inherited from
crystalbet.py's sport locks (ASP.NET serializes same-session requests
server-side anyway).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Iterator, Optional
from urllib.parse import urlencode

log = logging.getLogger(__name__)

BASE = "https://www.crystalbet.com"
SPORTS_URL = f"{BASE}/Pages/Sports.aspx"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
POST_HEADERS = {
    **HEADERS,
    "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
    "X-MicrosoftAjax": "Delta=true",
}

SM = "ctl00$ctl00$MasteScriptManager"  # CB's own typo, not ours
PREFIX = "ctl00$ctl00$ContentPlaceHolder1$ContentPlaceHolder2"
UPDATE_SPORT_TYPES = f"{PREFIX}$UpdateSportTypes"
UPDATE_GAMES = f"{PREFIX}$UpdateGames"
UPDATE_PANELS_HOLDER = f"{PREFIX}$UpdatePanelsHolder"
BTN_CHAMP = f"{PREFIX}$ButtonUpdateChampionats"
HF_CHAMP_PARAM = f"{PREFIX}$HiddenFieldUpdateChampionatsParam"

REWARM_AFTER_SEC = 3600.0   # shed ViewState/session age hourly
GET_TIMEOUT = 60
POST_TIMEOUT = 120
IMPERSONATE = "chrome124"

# English-flip verification. A correctly flipped game panel has ~0 Georgian
# (Mkhedruli) characters; an unflipped one has hundreds. When several sport
# sessions warm at once (boot), CB's ImageButtonEn flip occasionally doesn't
# take and the session would otherwise stay stuck serving Georgian team/league
# names until the next hourly re-warm. fetch_list_html checks the panel and
# re-warms once if it's Georgian.
_GEORGIAN_RE = re.compile(r"[Ⴀ-ჿ]")
_GEORGIAN_OK_MAX = 30


def georgian_chars(html: str) -> int:
    return len(_GEORGIAN_RE.findall(html))


# ── ASP.NET wire-format helpers (pure functions) ──────────────────────────────

_RE_HIDDEN = re.compile(r'<input[^>]+type=["\']hidden["\'][^>]*>', re.I)
_RE_NAME = re.compile(r'name=["\']([^"\']+)["\']')
_RE_VALUE = re.compile(r'value=["\']([^"\']*)["\']')


def hidden_fields(html: str) -> dict[str, str]:
    """All hidden <input> name/value pairs in an HTML fragment."""
    out: dict[str, str] = {}
    for tag in _RE_HIDDEN.finditer(html):
        nm = _RE_NAME.search(tag.group())
        vm = _RE_VALUE.search(tag.group())
        if nm:
            out[nm.group(1)] = vm.group(1) if vm else ""
    return out


def walk_delta(delta: str) -> Iterator[tuple[str, str, str]]:
    """Yield (segment_type, segment_id, content) over an UpdatePanel delta."""
    pos = 0
    while pos < len(delta):
        bar = delta.find("|", pos)
        if bar < 0:
            break
        try:
            length = int(delta[pos:bar])
        except ValueError:
            pos = bar + 1
            continue
        te = delta.find("|", bar + 1)
        if te < 0:
            break
        ie = delta.find("|", te + 1)
        if ie < 0:
            break
        yield delta[bar + 1:te], delta[te + 1:ie], delta[ie + 1:ie + 1 + length]
        pos = ie + 1 + length + 1


def apply_delta_hidden(fields: dict[str, str], delta: str) -> None:
    """Refresh __VIEWSTATE & co. from a delta's hiddenField segments."""
    for seg_type, seg_id, content in walk_delta(delta):
        if seg_type == "hiddenField":
            fields[seg_id] = content


def panel_html(delta: str, keyword: str) -> str:
    """Content of the first updatePanel segment whose id contains keyword."""
    for seg_type, seg_id, content in walk_delta(delta):
        if seg_type == "updatePanel" and keyword in seg_id:
            return content
    return ""


def all_panels_html(delta: str) -> str:
    """All updatePanel segments' content, concatenated."""
    return "\n".join(
        content for seg_type, _sid, content in walk_delta(delta)
        if seg_type == "updatePanel"
    )


def normalize_html(html: str) -> str:
    """Re-serialize raw ASP.NET panel HTML through an HTML5 tree builder.

    CB's raw panels carry heavily unclosed markup (measured live: 515 <td>
    opens vs 259 closes in one detail panel). A real browser repairs that
    during HTML5 tree construction, and page.content() returns the REPAIRED
    serialization — which is what v1's parsers were written against. Parsing
    the raw bytes with html.parser instead mis-nests every following sibling
    into the open cell and the detail parser over-collects selections
    (verified live 2026-06-12: 679 vs 237 markets on the same game).

    html5lib implements the same WHATWG algorithm browsers use, so this
    yields a browser-equivalent document (offline-verified: identical Odds
    to a Chromium capture of the same game, 0 structural / 0 price diffs).
    lxml does NOT qualify — its libxml2 recovery diverges from browsers on
    exactly this markup.
    """
    from bs4 import BeautifulSoup

    return str(BeautifulSoup(html, "html5lib"))


# ── Per-sport session ─────────────────────────────────────────────────────────

class CbHttpSession:
    """One warmed ASP.NET session, pinned to one sport.

    All methods are SYNC (curl_cffi Session is sync) — call through the
    module-level async wrappers from asyncio code.
    """

    def __init__(self, sport_id: int):
        self.sport_id = sport_id
        self.s = None                       # curl_cffi Session
        self.fields: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self.warmed_at = 0.0

    # ── low level ──

    def _post(self, body: dict, *, asyncpost: bool = True) -> str:
        hdrs = POST_HEADERS if asyncpost else {
            **HEADERS,
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        }
        r = self.s.post(SPORTS_URL, data=urlencode(body), headers=hdrs,
                        cookies=self.cookies, timeout=POST_TIMEOUT)
        txt = r.text
        if r.status_code != 200:
            raise RuntimeError(f"CB postback status={r.status_code}")
        if "pageRedirect" in txt[:160]:
            raise RuntimeError("CB postback pageRedirect — session died")
        if asyncpost:
            apply_delta_hidden(self.fields, txt)
        return txt

    def _harvest_panels(self, delta: str) -> None:
        # Merge hidden inputs rendered inside ANY returned panel — they are
        # not in the hiddenField segments but a browser would submit them.
        self.fields.update(hidden_fields(all_panels_html(delta)))

    # ── lifecycle ──

    def warm(self) -> None:
        """GET → English flip → sport select. Raises on any failure."""
        from curl_cffi.requests import Session

        self.close()
        self.s = Session(impersonate=IMPERSONATE)
        r0 = self.s.get(SPORTS_URL, headers=HEADERS, timeout=GET_TIMEOUT)
        r0.raise_for_status()
        self.fields = hidden_fields(r0.text)
        self.cookies = dict(r0.cookies)

        # Full (non-async) postback: returns a complete English page and the
        # language sticks to the session. Re-scrape hidden fields from it.
        body = {**self.fields, "__EVENTTARGET": "ctl00$ctl00$ImageButtonEn",
                "__EVENTARGUMENT": ""}
        full = self._post(body, asyncpost=False)
        self.fields = hidden_fields(full)
        self.cookies.update(dict(self.s.cookies))

        body = {**self.fields, SM: f"{UPDATE_GAMES}|{UPDATE_SPORT_TYPES}",
                "__EVENTTARGET": UPDATE_SPORT_TYPES,
                "__EVENTARGUMENT": str(self.sport_id), "__ASYNCPOST": "true"}
        delta = self._post(body)
        self._harvest_panels(delta)
        self.warmed_at = time.time()
        log.info("CB http: session warmed for sport_id=%d", self.sport_id)

    def fetch_list_html(self) -> str:
        """SelectAllChampionats → the UpdatePanelGames panel HTML (full board).

        Verifies the session is actually in English; if the panel comes back
        Georgian (a transient flip miss at warm time), re-warm once and refetch
        so names never get stuck in Georgian."""
        panel = self._list_panel()
        if georgian_chars(panel) > _GEORGIAN_OK_MAX:
            log.warning(
                "CB http: sport_id=%d list came back Georgian (%d glyphs) — "
                "re-warming to re-apply the English flip",
                self.sport_id, georgian_chars(panel),
            )
            self.warm()
            panel = self._list_panel()
            if georgian_chars(panel) > _GEORGIAN_OK_MAX:
                log.error("CB http: sport_id=%d still Georgian after re-warm",
                          self.sport_id)
        return normalize_html(panel)

    def _list_panel(self) -> str:
        body = {**self.fields, SM: f"{UPDATE_PANELS_HOLDER}|{BTN_CHAMP}",
                "__EVENTTARGET": BTN_CHAMP, "__EVENTARGUMENT": "",
                HF_CHAMP_PARAM: f"SelectAllChampionats:{self.sport_id}",
                "__ASYNCPOST": "true"}
        delta = self._post(body)
        self._harvest_panels(delta)
        panel = panel_html(delta, "UpdatePanelGames")
        if not panel:
            raise RuntimeError(
                f"CB http: SelectAllChampionats:{self.sport_id} returned no "
                f"UpdatePanelGames panel ({len(delta):,}B delta)"
            )
        return panel

    def expand_detail_html(self, game_id: str) -> str:
        """ExpandDetail → all updatePanel segments (detail table rides in the
        RepeaterChampionat panel). Collapses afterward, best-effort, so the
        server-side view stays small for subsequent responses."""
        body = {**self.fields, SM: f"{UPDATE_PANELS_HOLDER}|{UPDATE_GAMES}",
                "__EVENTTARGET": UPDATE_GAMES,
                "__EVENTARGUMENT": f"ExpandDetail:{game_id}",
                "__ASYNCPOST": "true"}
        delta = self._post(body)
        self._harvest_panels(delta)
        blob = normalize_html(all_panels_html(delta))
        try:
            collapse = {**self.fields, SM: f"{UPDATE_PANELS_HOLDER}|{UPDATE_GAMES}",
                        "__EVENTTARGET": UPDATE_GAMES,
                        "__EVENTARGUMENT": f"CollapseDetail:{game_id}",
                        "__ASYNCPOST": "true"}
            cdelta = self._post(collapse)
            self._harvest_panels(cdelta)
        except Exception as e:
            log.debug("CB http: CollapseDetail %s failed (harmless): %s",
                      game_id, e)
        return blob

    def close(self) -> None:
        if self.s is not None:
            try:
                self.s.close()
            except Exception:
                pass
            self.s = None


# ── Module-level per-sport singletons + async wrappers ────────────────────────

_sessions: dict[int, CbHttpSession] = {}


def _get_session(sport_id: int) -> CbHttpSession:
    if sport_id not in _sessions:
        _sessions[sport_id] = CbHttpSession(sport_id)
    return _sessions[sport_id]


def _ensure_warm_sync(sess: CbHttpSession) -> None:
    if sess.s is None or (time.time() - sess.warmed_at) > REWARM_AFTER_SEC:
        sess.warm()


def reset_session(sport_id: int) -> None:
    """Drop a sport's session so the next call re-warms from scratch.
    The transport-level analogue of crystalbet._close_page_internal."""
    sess = _sessions.pop(sport_id, None)
    if sess is not None:
        sess.close()


async def fetch_list_html(sport_id: int) -> str:
    """Async: warmed-session SelectAllChampionats list snapshot."""
    sess = _get_session(sport_id)

    def run() -> str:
        _ensure_warm_sync(sess)
        return sess.fetch_list_html()

    return await asyncio.to_thread(run)


async def expand_detail_html(sport_id: int, game_id: str) -> str:
    """Async: ExpandDetail for one game on the sport's warmed session."""
    sess = _get_session(sport_id)

    def run() -> str:
        if sess.s is None:
            raise RuntimeError(
                f"CB http: session for sport_id={sport_id} not warmed — "
                "fetch_list_html must run first in this cycle"
            )
        return sess.expand_detail_html(game_id)

    return await asyncio.to_thread(run)


async def close_all() -> None:
    """Teardown — close every sport session (app shutdown)."""
    for sport_id in list(_sessions):
        reset_session(sport_id)
    log.info("CB http: all sessions closed")
