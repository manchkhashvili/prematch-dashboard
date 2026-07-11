"""Crocobet prematch scraper — browser-free JSON, gameType-keyed.

Fifth book on the dashboard (CrystalBet, Pinnacle, Lider-Bet, Betlive,
Crocobet). Crocobet runs its own REST gateway — no auth, no session, plain
GETs over curl_cffi:

  GET /categories?lang=en                          → sport→league tree
  GET /categories/multi/{catIds}/events?lang=en    → board, main markets inline
  GET /events/{eventId}?lang=en                    → full market ladder

Base: https://sport-gateway.crocobet.com/sport/en/rest/market

WHY gameType, not names (the "avoid confusion" rule): the /sport/en/ path
STILL returns Georgian market + outcome names ("ქულების რ-ბა" = points total,
"ნაკლები/მეტი" = under/over). Never classify on the localized string. Every
market carries a STABLE numeric `gameType`; we map on that, exactly like
1xbet's G/T and Lider's typeId. Verified live 2026-07-11 by joining Crocobet
`remoteId` (== SportRadar match id) to Lider's `sr:match` id (91 common
basketball events) and comparing devigged prices — the mappings below agreed
to a **0.00pp median** gap. Outcome labels are mapped by their own Georgian
text (also stable), never by array position (which varies across events).

Verified gameType → market (FT = full match, incl OT for basketball). Every
code below cross-priced vs Lider via SR-id join at 0.00pp median (2026-07-11):
  basketball FT : -2527 moneyline(1/2)   -2966 total   -2950 spread
                  182 team_total home    183 team_total away
  basketball H1 : -2541 total            -6008 spread
  basketball Qn : 33/-421/-422/-423 totals Q1..Q4
                  -430/-2502/-5004/-5006 spreads Q1..Q4
  soccer FT     : 1 moneyline(1/X/2)     8 total       -458 spread
Known-but-not-shipped: -2542/-6009 = 2nd-half total/handicap REGULAR TIME
(verified 0.00pp, but the v1 Period model has no H2 — add if H2 lands).
Tennis has NO verified codes (its events carry no remoteId → SR join
impossible; needs a name+time verification pass) — the scraper returns []
for tennis WITHOUT fetching anything.

sr_match_id = the event's `remoteId` (bare SR id) → exact cross-book join to
Lider/Betlive. Home/away come from `participants` (number 1 = home, 2 = away).
The gateway serves names in GEORGIAN only (the site's EN toggle translates
client-side; no server English exists), so we romanize to Latin via the shared
transliterate() — readable English-ish names that also fuzzy-match the
reference books.
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from src.models import Odds
from src.normalize import transliterate

log = logging.getLogger(__name__)

BASE = "https://sport-gateway.crocobet.com/sport/en/rest/market"
IMPERSONATE = "chrome124"
HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36"),
           "Accept": "application/json, text/plain, */*"}
TIMEOUT = 25.0
WORKERS = 8

SPORT_ID = {"soccer": 1, "basketball": 2, "tennis": 3}

# outcome-name (Georgian, stable) → canonical selection key
_OUT = {"1": "home", "2": "away", "X": "draw",
        "მეტი": "over", "ნაკლები": "under"}

# gameType → (market_type, period, n_way, team_side). VERIFIED subset only
# (docstring). team_side is None except for team totals.
_GAMETYPE = {
    "basketball": {
        -2527: ("moneyline", "FT", 2, None),
        -2966: ("total", "FT", 2, None),
        -2950: ("spread", "FT", 2, None),
        182:   ("team_total", "FT", 2, "home"),
        183:   ("team_total", "FT", 2, "away"),
        -2541: ("total", "H1", 2, None),
        -6008: ("spread", "H1", 2, None),
        33:    ("total", "Q1", 2, None),
        -421:  ("total", "Q2", 2, None),
        -422:  ("total", "Q3", 2, None),
        -423:  ("total", "Q4", 2, None),
        -430:  ("spread", "Q1", 2, None),
        -2502: ("spread", "Q2", 2, None),
        -5004: ("spread", "Q3", 2, None),
        -5006: ("spread", "Q4", 2, None),
    },
    "soccer": {
        1:    ("moneyline", "FT", 3, None),
        8:    ("total", "FT", 2, None),
        -458: ("spread", "FT", 2, None),
    },
    "tennis": {},   # no remoteId on tennis events → unverifiable via SR join yet
}


def _session():
    from curl_cffi.requests import Session
    return Session(impersonate=IMPERSONATE)


def _get(s, path: str):
    r = s.get(f"{BASE}/{path}", headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _prices(game: dict) -> dict[str, float]:
    """outcomeName → decimal, dropping suspended (<= 1.0) prices."""
    out = {}
    for o in game.get("outcomes") or []:
        od = o.get("outcomeOdds")
        nm = _OUT.get(o.get("outcomeName"))
        if nm and isinstance(od, (int, float)) and od > 1.0:
            out[nm] = float(od)
    return out


def _parse_event(ev: dict, games: list[dict], sport: str,
                 fetched_at: datetime) -> list[Odds]:
    parts = {p.get("number"): p for p in ev.get("participants") or []}
    # Crocobet's sport gateway serves names in GEORGIAN only (the site's EN
    # toggle translates client-side via ngx-translate; no server English
    # exists — verified: /sport/{ka,en,ru,tr}/ all return Georgian). We
    # romanize to Latin with the shared transliterate() so names read in
    # English and fuzzy-match the reference books. (mkhedruli → national
    # romanization, e.g. "მემფისი" → "mempisi".)
    home = transliterate(((parts.get(1) or {}).get("name") or "").strip())
    away = transliterate(((parts.get(2) or {}).get("name") or "").strip())
    if not home or not away:
        return []
    start = ev.get("eventStart")
    start_time = (datetime.fromtimestamp(start / 1000, tz=timezone.utc)
                  if start else None)
    league = transliterate(ev.get("category3Name") or ev.get("category2Name") or "") or None
    sr = ev.get("remoteId")
    sr_match_id = str(sr) if sr else None
    table = _GAMETYPE.get(sport, {})

    rows: list[Odds] = []
    for g in games:
        hit = table.get(g.get("gameType"))
        if hit is None:
            continue
        market_type, period, n_way, team_side = hit
        p = _prices(g)
        if market_type == "moneyline":
            need = {"home", "draw", "away"} if n_way == 3 else {"home", "away"}
            if not need <= set(p):
                continue
            sel = {k: p[k] for k in need}
            line = None
        elif market_type in ("total", "team_total"):
            if not {"over", "under"} <= set(p):
                continue
            sel = {"over": p["over"], "under": p["under"]}
            line = g.get("argument")
        else:  # spread — argument is the HOME line, signed
            if not {"home", "away"} <= set(p):
                continue
            sel = {"home": p["home"], "away": p["away"]}
            line = g.get("argument")
        if market_type != "moneyline" and line is None:
            continue
        try:
            rows.append(Odds(
                source="crocobet", sport=sport, home=home, away=away,
                market_type=market_type, period=period, selections=sel,
                fetched_at=fetched_at,
                line=float(line) if line is not None else None,
                start_time=start_time, league=league, team_side=team_side,
                raw_event_id=str(ev.get("eventId")), sr_match_id=sr_match_id))
        except ValueError as e:
            log.debug("crocobet Odds rejected: %s", e)
    return rows


def _fetch_sport_sync(sport: str) -> list[Odds]:
    sport_id = SPORT_ID.get(sport)
    if sport_id is None:
        log.warning("crocobet: unknown sport %r", sport)
        return []
    if not _GAMETYPE.get(sport):
        # No verified market codes for this sport — do NOT waste a board +
        # per-event ladder sweep to emit nothing (tennis was costing ~75
        # ladder calls/cycle for zero rows before this guard).
        return []
    s = _session()
    fetched_at = datetime.now(tz=timezone.utc)
    cats = _get(s, "categories?lang=en").get("data") or []
    leaf = [c for c in cats if c.get("sportId") == sport_id
            and (c.get("eventsCount") or 0) > 0]
    if not leaf:
        return []
    ids = ",".join(str(c["categoryId"]) for c in leaf)
    board = _get(s, f"categories/multi/{ids}/events?lang=en").get("data") or []
    now_ms = fetched_at.timestamp() * 1000
    # prematch only — drop anything already started (grace 120s for clock skew)
    board = [e for e in board
             if not e.get("eventStart") or e["eventStart"] > now_ms - 120_000]

    rows: list[Odds] = []

    def one(ev):
        try:
            full = _get(s, f"events/{ev['eventId']}?lang=en").get("data") or {}
            return _parse_event(ev, full.get("eventGames") or [], sport, fetched_at)
        except Exception as e:
            log.debug("crocobet event %s failed: %s", ev.get("eventId"), e)
            return []

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for chunk in ex.map(one, board):
            rows.extend(chunk)
    log.info("crocobet %s: %d Odds rows from %d events", sport, len(rows), len(board))
    return rows


async def fetch_crocobet(sport: str) -> list[Odds]:
    return await asyncio.to_thread(_fetch_sport_sync, sport)


async def fetch_crocobet_soccer() -> list[Odds]:
    return await fetch_crocobet("soccer")


async def fetch_crocobet_basketball() -> list[Odds]:
    return await fetch_crocobet("basketball")


async def fetch_crocobet_tennis() -> list[Odds]:
    return await fetch_crocobet("tennis")


if __name__ == "__main__":   # smoke: python -m src.scrapers.crocobet [sport]
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sp = sys.argv[1] if len(sys.argv) > 1 else "basketball"
    odds = asyncio.run(fetch_crocobet(sp))
    print(f"\n{sp}: {len(odds)} Odds rows")
    by = {}
    for o in odds:
        by[(o.market_type, o.period)] = by.get((o.market_type, o.period), 0) + 1
    for k, v in sorted(by.items()):
        print(f"  {k}: {v}")
    with_sr = sum(1 for o in odds if o.sr_match_id)
    print(f"with sr_match_id: {with_sr}/{len(odds)}")
    for o in odds[:5]:
        print(f"  {o.home} v {o.away} | {o.market_type} {o.period} line={o.line} "
              f"{o.selections} sr={o.sr_match_id}")
