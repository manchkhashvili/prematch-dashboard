"""
Live probe: can v1's CB scrape run browser-free (curl_cffi), feeding v1's
UNTOUCHED parsers?

Answers three questions against the live site:
  1. Does v1's single-postback league load (SelectAllChampionats:<sport_id>)
     work over plain HTTP, and does it return the full board in one delta?
  2. Does v1's _extract_games_from_list_html() parse the raw UpdatePanelGames
     panel HTML identically to a rendered page (dates, teams, loadinfo)?
  3. Does ExpandDetail over HTTP + v1's cb_detail.parse_detail_page
     (scope_to_event=True) yield detail Odds?

Usage:
  .venv/bin/python scripts/probe_cb_http.py [--sport basketball] [--expand N]

ASP.NET plumbing adapted from prematch_v2 (docs/crystalbet.md, src/cb_client.py).
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from curl_cffi.requests import Session

from src.scrapers import cb_detail
from src.scrapers.crystalbet import _extract_games_from_list_html, _SPORT_MODULES

BASE = "https://www.crystalbet.com"
SPORTS_URL = f"{BASE}/Pages/Sports.aspx"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
POST_HEADERS = {**HEADERS,
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                "X-MicrosoftAjax": "Delta=true"}

SM = "ctl00$ctl00$MasteScriptManager"
PREFIX = "ctl00$ctl00$ContentPlaceHolder1$ContentPlaceHolder2"
UPDATE_SPORT_TYPES = f"{PREFIX}$UpdateSportTypes"
UPDATE_GAMES = f"{PREFIX}$UpdateGames"
UPDATE_PANELS_HOLDER = f"{PREFIX}$UpdatePanelsHolder"
BTN_CHAMP = f"{PREFIX}$ButtonUpdateChampionats"
HF_CHAMP_PARAM = f"{PREFIX}$HiddenFieldUpdateChampionatsParam"

RE_HIDDEN = re.compile(r'<input[^>]+type=["\']hidden["\'][^>]*>', re.I)
RE_NAME = re.compile(r'name=["\']([^"\']+)["\']')
RE_VALUE = re.compile(r'value=["\']([^"\']*)["\']')


def hidden_fields(html: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for tag in RE_HIDDEN.finditer(html):
        nm = RE_NAME.search(tag.group())
        vm = RE_VALUE.search(tag.group())
        if nm:
            out[nm.group(1)] = vm.group(1) if vm else ""
    return out


def walk_delta(delta: str):
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


def apply_delta_hidden(fields: dict, delta: str) -> None:
    for seg_type, seg_id, content in walk_delta(delta):
        if seg_type == "hiddenField":
            fields[seg_id] = content


def panel_html(delta: str, keyword: str) -> str:
    for seg_type, seg_id, content in walk_delta(delta):
        if seg_type == "updatePanel" and keyword in seg_id:
            return content
    return ""


class Probe:
    def __init__(self):
        self.s = Session(impersonate="chrome124")
        self.fields: dict[str, str] = {}
        self.cookies: dict[str, str] = {}

    def _post(self, body: dict, *, asyncpost: bool = True) -> str:
        hdrs = POST_HEADERS if asyncpost else {
            **HEADERS,
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
        t0 = time.time()
        r = self.s.post(SPORTS_URL, data=urlencode(body), headers=hdrs,
                        cookies=self.cookies, timeout=120)
        dt = time.time() - t0
        txt = r.text
        print(f"    POST {body.get('__EVENTTARGET', '?').split('$')[-1]}"
              f" arg={body.get('__EVENTARGUMENT', '')[:40]!r}"
              f" -> {r.status_code}, {len(txt):,}B in {dt:.1f}s")
        if r.status_code != 200:
            raise RuntimeError(f"status={r.status_code}")
        if "pageRedirect" in txt[:160]:
            raise RuntimeError("pageRedirect — session died")
        if asyncpost:
            apply_delta_hidden(self.fields, txt)
        return txt

    def harvest_panels(self, delta: str) -> None:
        ph = panel_html(delta, "UpdatePanelsHolder") + panel_html(delta, "UpdatePanelGames")
        self.fields.update(hidden_fields(ph))

    def warm(self, sport_id: int) -> None:
        print("  GET Sports.aspx ...")
        t0 = time.time()
        r0 = self.s.get(SPORTS_URL, headers=HEADERS, timeout=60)
        r0.raise_for_status()
        print(f"    {len(r0.text):,}B in {time.time()-t0:.1f}s; cookies: "
              f"{sorted(dict(r0.cookies))}")
        self.fields = hidden_fields(r0.text)
        self.cookies = dict(r0.cookies)

        print("  English flip (full postback) ...")
        body = {**self.fields, "__EVENTTARGET": "ctl00$ctl00$ImageButtonEn",
                "__EVENTARGUMENT": ""}
        r1 = self._post(body, asyncpost=False)
        if not r1.lstrip().lower().startswith("<!doctype"):
            print("    WARN: English flip did not return a full page")
        self.fields = hidden_fields(r1)
        self.cookies.update(dict(self.s.cookies))

        print(f"  DoSportTypePostBack({sport_id}) ...")
        body = {**self.fields, SM: f"{UPDATE_GAMES}|{UPDATE_SPORT_TYPES}",
                "__EVENTTARGET": UPDATE_SPORT_TYPES,
                "__EVENTARGUMENT": str(sport_id), "__ASYNCPOST": "true"}
        delta = self._post(body)
        self.harvest_panels(delta)

    def select_all_champs(self, sport_id: int) -> str:
        print(f"  DoChampionatPostBack('SelectAllChampionats:{sport_id}') ...")
        body = {**self.fields, SM: f"{UPDATE_PANELS_HOLDER}|{BTN_CHAMP}",
                "__EVENTTARGET": BTN_CHAMP, "__EVENTARGUMENT": "",
                HF_CHAMP_PARAM: f"SelectAllChampionats:{sport_id}",
                "__ASYNCPOST": "true"}
        delta = self._post(body)
        self.harvest_panels(delta)
        return delta

    def expand_detail(self, game_id: str) -> str:
        body = {**self.fields, SM: f"{UPDATE_PANELS_HOLDER}|{UPDATE_GAMES}",
                "__EVENTTARGET": UPDATE_GAMES,
                "__EVENTARGUMENT": f"ExpandDetail:{game_id}",
                "__ASYNCPOST": "true"}
        delta = self._post(body)
        self.harvest_panels(delta)
        return delta


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="basketball",
                    choices=sorted(_SPORT_MODULES))
    ap.add_argument("--expand", type=int, default=2,
                    help="how many games to ExpandDetail-probe")
    ap.add_argument("--save", default="", help="save panel HTML here")
    args = ap.parse_args()

    sport = _SPORT_MODULES[args.sport]
    fetched_at = datetime.now(tz=timezone.utc)

    print(f"== probe: {args.sport} (sport_id={sport.SPORT_ID}) ==")
    p = Probe()
    p.warm(sport.SPORT_ID)
    delta = p.select_all_champs(sport.SPORT_ID)
    panel = panel_html(delta, "UpdatePanelGames")
    print(f"  UpdatePanelGames panel: {len(panel):,}B")

    n_containers = len(re.findall(r"GContainerList", panel))
    print(f"  GContainerList mentions in raw panel: {n_containers}")
    print(f"  'game-table' divs in raw panel: "
          f"{len(re.findall(r'game-table', panel))}")
    print(f"  'x_loop_title_block' in raw panel: "
          f"{len(re.findall(r'x_loop_title_block', panel))}")
    print(f"  date-pattern dd/mm/yyyy hits: "
          f"{len(re.findall(r'\\d{2}[./]\\d{2}[./]\\d{4}', panel))}")

    if args.save:
        Path(args.save).write_text(panel, encoding="utf-8")
        print(f"  saved panel -> {args.save}")

    # ── v1's UNTOUCHED list parser on the raw panel HTML ──
    games = _extract_games_from_list_html(panel, fetched_at, sport=sport)
    n_with_start = sum(1 for g in games if g.start_time is not None)
    n_with_li = sum(1 for g in games if g.loadinfo)
    n_list_odds = sum(len(g.list_odds) for g in games)
    print(f"\n  v1 _extract_games_from_list_html on RAW PANEL:")
    print(f"    games={len(games)}, with_start_time={n_with_start}, "
          f"with_loadinfo={n_with_li}, list_odds_rows={n_list_odds}")
    for g in games[:5]:
        print(f"      {g.event_id} | {g.home} vs {g.away} | {g.league} "
              f"| start={g.start_time} | list_odds={len(g.list_odds)}")

    if not games:
        print("  !! 0 games parsed — raw panel structure differs from "
              "rendered DOM; port needs a shim. Dumping first 2KB:")
        print(panel[:2000])
        return

    # ── ExpandDetail probe ──
    candidates = [g for g in games
                  if "HasAdditionalOdds\":\"True\"" in g.loadinfo
                  or "HasAdditionalOdds':'True'" in g.loadinfo
                  or 'HasAdditionalOdds": "True"' in g.loadinfo] or games
    for g in candidates[:args.expand]:
        print(f"\n  ExpandDetail {g.event_id} ({g.home} vs {g.away}) ...")
        d2 = p.expand_detail(g.event_id)
        pg = panel_html(d2, "UpdatePanelGames")
        holder = panel_html(d2, "UpdatePanelsHolder")
        blob = pg or holder
        has_table = "game-details" in blob
        print(f"    delta {len(d2):,}B; games-panel {len(pg):,}B; "
              f"holder {len(holder):,}B; game-details present: {has_table}")
        odds = cb_detail.parse_detail_page(
            blob, event_id=g.event_id, home=g.home, away=g.away,
            league=g.league, start_time=g.start_time, fetched_at=fetched_at,
            sport_name=sport.SPORT_NAME, classify=sport.classify_market_title,
            scope_to_event=True,
        )
        by_type: dict[str, int] = {}
        for o in odds:
            k = f"{o.market_type}:{o.period}"
            by_type[k] = by_type.get(k, 0) + 1
        print(f"    v1 cb_detail.parse_detail_page -> {len(odds)} Odds: {by_type}")

    print("\n== probe complete ==")


if __name__ == "__main__":
    main()
