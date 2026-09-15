"""
Live-board duplicate listings — the same in-play match listed twice by one
book. CrystalBet and Lider-Bet, every sport.

The live twin of `duplicate_fixture` in consistency.py. Prematch anchors a
duplicate on kickoff time; a live board carries something better — the game
state. Two listings with the same two teams, at the same score, in the same
period, on the same board at the same instant are one match. Two genuinely
different games between the same teams cannot share all three.

This is a TRIPWIRE, not a feed: it polls slowly (runtime_config
`live_dup_sec`, default 300 s) and reads only the enumeration boards — one GET
for Lider-Bet (the whole live board, ~4 MB), three requests for CrystalBet
(GET, the English flip, one ShowAllStarted postback — scores only, no prices,
~500 KB). Neither opens a single match. Transport is lifted from the live
probes (live/probe_cb_live.py, live/probe_liderbet_live.py), verified against
both boards on 2026-09-15; the CB markup in live/docs was stale (the rows are
`div.d_row` under a `sportTypeId` container, not `div.game_info`) and this
parser follows what the board actually serves.

Flags are emitted as consistency-tab rows with a flat severity of 100 —
the same convention as the prematch duplicate — under kind `duplicate_live`.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

log = logging.getLogger(__name__)

# ── canonical sport names ────────────────────────────────────────────────────
# CB sport ids, verified from the live tree's sport tabs. Simulated ("e-")
# and esports feeds are listed so they can be SKIPPED: those fixtures repeat
# by design and drowned the prematch scan with 16k events.
CB_SPORTS = {
    16: "soccer", 17: "basketball", 18: "icehockey", 20: "handball",
    21: "volleyball", 22: "tennis", 24: "baseball", 25: "fieldhockey",
    26: "futsal", 32: "snooker", 33: "tabletennis", 37: "badminton",
    39: "darts", 76: "squash", 112: "basketball3x3",
}
CB_SIM_SPORTS = {80, 133, 134, 135, 216}
LIDER_SPORTS = {
    "football": "soccer", "basketball": "basketball", "tennis": "tennis",
    "ice hockey": "icehockey", "table tennis": "tabletennis",
    "volleyball": "volleyball", "handball": "handball", "snooker": "snooker",
    "baseball": "baseball", "darts": "darts", "futsal": "futsal",
    "badminton": "badminton", "squash": "squash",
}
DUP_NAME_SCORE = 80.0


@dataclass
class LiveMatch:
    book: str
    sport: str
    event_id: str
    home: str
    away: str
    league: str
    home_score: int | None
    away_score: int | None
    period: str                 # clock stripped: "2nd half", "4th quarter", "Game 5"


def _period_label(s: str) -> str:
    """'2nd half 58'' -> '2nd half'; '1st set 12:34' -> '1st set'; 'Game 5'
    stays. Only the trailing CLOCK is stripped — the ordinal IS the period. (A
    first cut stripped every digit and turned '4th set' and '5th set' into the
    same label.)"""
    s = str(s or "")
    s = re.sub(r"\s*\d+(?::\d+)?'\s*$", "", s)      # 58'  or  12:34'
    s = re.sub(r"\s*\d+:\d+\s*$", "", s)             # 12:34
    return s.strip().lower()


# ── CrystalBet ───────────────────────────────────────────────────────────────
_CB_LIVE_URL = "https://www.crystalbet.com/Pages/LiveBetting.aspx"
_CB_T_TREE = "ctl00$ctl00$ContentPlaceHolder1$ContentPlaceHolder2$UpdateLiveTree"
_CB_EN = "ctl00$ctl00$ImageButtonEn"
_RE_CB_SPORT_BLOCK = re.compile(
    r"<div class='started-games-by-country[^']*'\s+sportTypeId='(\d+)'\s*>(.*?)(?=<div class='started-games-by-country|\Z)",
    re.S)
_RE_CB_LEAGUE = re.compile(r"league-title'>([^<]*)<")
_RE_CB_ROW = re.compile(
    r"doGameOpenPost\((\d+)\).*?team1-title'>([^<]*)<.*?team2-title'>([^<]*)<"
    r".*?started-game-image'></span><span>([^<]*)</span>"
    r".*?team1-result'>([^<]*)<.*?team2-result'>([^<]*)<", re.S)


def _int(s) -> int | None:
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return None


def parse_cb_live_board(delta: str) -> list[LiveMatch]:
    out = []
    for sid, block in _RE_CB_SPORT_BLOCK.findall(delta):
        sid = int(sid)
        if sid in CB_SIM_SPORTS:
            continue
        sport = CB_SPORTS.get(sid)
        if not sport:
            continue
        # a sport container holds several leagues; split at each league head
        parts = re.split(r"(?=<div class='league-head')", block)
        for part in parts:
            lg = _RE_CB_LEAGUE.search(part)
            league = lg.group(1).strip() if lg else ""
            for gid, h, a, per, hs, as_ in _RE_CB_ROW.findall(part):
                out.append(LiveMatch("cb", sport, gid, h.strip(), a.strip(), league,
                                     _int(hs), _int(as_), _period_label(per)))
    return out


def fetch_cb_live() -> list[LiveMatch]:
    """GET → English flip → ShowAllStarted. Sync; call from a thread."""
    from curl_cffi.requests import Session
    from src.scrapers.cb_http import (HEADERS, POST_HEADERS, IMPERSONATE, SM,
                                      hidden_fields, apply_delta_hidden,
                                      all_panels_html, _PIN)
    s = Session(impersonate=IMPERSONATE)
    _PIN.pin(s)
    r0 = s.get(_CB_LIVE_URL, headers=HEADERS, timeout=60)
    fields = hidden_fields(r0.text)
    cookies = dict(r0.cookies)
    # English names (full postback)
    _PIN.pin(s)
    r1 = s.post(_CB_LIVE_URL, data=urlencode({**fields, "__EVENTTARGET": _CB_EN,
                                              "__EVENTARGUMENT": ""}),
                headers={**HEADERS, "Content-Type":
                         "application/x-www-form-urlencoded; charset=utf-8"},
                cookies=cookies, timeout=30)
    fields = hidden_fields(r1.text)
    cookies.update(dict(s.cookies))
    # the whole in-play board, scores only
    _PIN.pin(s)
    r2 = s.post(_CB_LIVE_URL,
                data=urlencode({**fields, SM: f"{_CB_T_TREE}|{_CB_T_TREE}",
                                "__EVENTTARGET": _CB_T_TREE,
                                "__EVENTARGUMENT": "ShowAllStarted",
                                "__ASYNCPOST": "true"}),
                headers=POST_HEADERS, cookies=cookies, timeout=30)
    if r2.status_code != 200:
        raise RuntimeError(f"CB live board status={r2.status_code}")
    apply_delta_hidden(fields, r2.text)
    return parse_cb_live_board(r2.text)


# ── Lider-Bet ────────────────────────────────────────────────────────────────
_LIDER_LIVE_MENU = ("https://sports.lider-bet.com/services/br/api/v1/menu"
                    "?lang=en&producers=br:1,br:3,bg:1&xlabels=qw-line:main")


def parse_lider_live_board(data: dict) -> list[LiveMatch]:
    from src.normalize import is_simulated_league
    anc = data.get("ancestors") or {}
    nm = lambda i: ((anc.get(i) or {}).get("name") or "").strip()
    out = []
    for m in (data.get("matches") or {}).values():
        sport = LIDER_SPORTS.get(nm(m.get("sportId")).lower())
        if not sport:                      # eSoccer, eBasketball, LoL, CS, Dota…
            continue
        league = nm(m.get("tourId"))
        if is_simulated_league(league):
            continue
        pr = m.get("props") or {}
        sc = (pr.get("scores") or {}).get("main") or {}
        st = (pr.get("status") or {}).get("matchStatus") or ""
        out.append(LiveMatch("liderbet", sport, str(m.get("id", "")),
                             nm(m.get("homeId")), nm(m.get("awayId")), league,
                             _int(sc.get("home")), _int(sc.get("away")),
                             _period_label(st)))
    return out


def fetch_liderbet_live() -> list[LiveMatch]:
    """One GET — the whole live board. Sync; call from a thread."""
    from curl_cffi.requests import Session
    from src.scrapers.liderbet import HEADERS, IMPERSONATE
    s = Session(impersonate=IMPERSONATE, headers=HEADERS, timeout=25)
    r = s.get(_LIDER_LIVE_MENU)
    if r.status_code != 200:
        raise RuntimeError(f"Lider live board status={r.status_code}")
    return parse_lider_live_board(r.json())


# ── the detector ─────────────────────────────────────────────────────────────
def find_live_duplicates(matches: list[LiveMatch], ts_iso: str | None = None) -> list[dict]:
    """Consistency-tab rows for every duplicated pair. One row per pair, on
    the event id that sorts first, naming the twin."""
    from rapidfuzz import fuzz
    from src.normalize import normalize_team, normalize_tennis_name
    ts_iso = ts_iso or datetime.now(tz=timezone.utc).isoformat()
    cache: dict = {}

    def norm(name, sport):
        k = (name, sport)
        if k not in cache:
            try:
                cache[k] = (normalize_tennis_name(name) if sport == "tennis"
                            else normalize_team(name)) if name else ""
            except Exception:
                cache[k] = str(name or "").lower()
        return cache[k]

    groups: dict[tuple, list[LiveMatch]] = {}
    for m in matches:
        if m.home_score is None or m.away_score is None:
            continue                       # not started / no state to anchor on
        groups.setdefault((m.book, m.sport), []).append(m)
    rows = []
    for (book, sport), grp in groups.items():
        grp = sorted(grp, key=lambda m: m.event_id)
        for i, a in enumerate(grp):
            ah, aa = norm(a.home, sport), norm(a.away, sport)
            for b in grp[i + 1:]:
                if a.event_id == b.event_id:
                    continue
                bh, ba = norm(b.home, sport), norm(b.away, sport)
                if not (ah and aa and bh and ba):
                    continue
                direct = min(fuzz.token_set_ratio(ah, bh), fuzz.token_set_ratio(aa, ba))
                swap = min(fuzz.token_set_ratio(ah, ba), fuzz.token_set_ratio(aa, bh))
                if max(direct, swap) < DUP_NAME_SCORE:
                    continue
                flipped = swap > direct
                same = ((a.home_score == b.away_score and a.away_score == b.home_score)
                        if flipped else
                        (a.home_score == b.home_score and a.away_score == b.away_score))
                if not same or a.period != b.period:
                    continue
                rows.append({
                    "book": book, "sport": sport, "league": a.league,
                    "match_label": f"{a.home} — {a.away}", "home": a.home, "away": a.away,
                    "cb_event_id": a.event_id, "book_event_id": a.event_id,
                    "start_time": None, "kind": "duplicate_live", "periods": "LIVE",
                    "outcome": None, "odds": None, "severity": 100.0,
                    "first_seen": ts_iso,
                    "detail": (f"same LIVE match listed twice — {a.event_id} ({a.league}) "
                               f"and {b.event_id} ({b.league})"
                               f"{' with sides flipped' if flipped else ''}; both "
                               f"{a.home_score}-{a.away_score}, {a.period or 'in play'}"),
                })
    return rows
