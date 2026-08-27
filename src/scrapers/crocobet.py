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
  soccer (2026-08-13, from a SHAPE census of the board response we already
    fetch — 197 gameTypes per event, of which we had mapped three):
                  5 htft (the 9 cells '1/1'..'2/2', name 'taimboli ①')
                  3 moneyline H1(1/X/2)   111 moneyline H2(1/X/2)
                  -284 total H1           -30335 total H2
    Verified vs Pinnacle: H1 moneyline 0.56pp median (n=372), H1 total 1.20pp
    (n=1033). The grid has no Pinnacle counterpart, so it was verified by its
    own identity instead — rows sum to the H1 1X2 and columns to the FT 1X2 to
    0.69pp / 0.79pp median over 110 grids.
    TRAP: the ①/②/③ suffix on a gameName is the STATISTIC, not decoration
    (① goals, ② corners, ③ cards). '8 golebis r-ba ①' and
    '23 kutkhurebis r-ba ②' have an IDENTICAL outcome shape, so shape alone
    cannot map them; every code above was confirmed ① and then price-checked.
    Also excluded on purpose: 4 (double chance, 1X/12/X2 — three outcomes like
    a 1X2) and -6048 (3-way European handicap).
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
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from src import horizon
from src.models import Odds
from src.normalize import is_simulated_league, transliterate

log = logging.getLogger(__name__)

BASE = "https://sport-gateway.crocobet.com/sport/en/rest/market"
IMPERSONATE = "chrome124"
HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36"),
           "Accept": "application/json, text/plain, */*"}
TIMEOUT = 25.0
WORKERS = 8
# Full-ladder horizon: per-event detail calls only for games starting within
# this many hours; farther games use the board's inline main markets.
DETAIL_HOURS = float(os.environ.get("CROCOBET_DETAIL_HOURS", "24"))

# Read off the book's own /categories census (2026-08-27), not inferred from
# another book's numbering. Ice hockey is 4 — `ჰოკეი`, 732 events, NHL at the
# top. Note 3 is BASEBALL (`ბეისბოლი`) and tennis is really 5 (`ჩოგბურთი`);
# the "tennis": 3 entry below is therefore pointing at the wrong board, which
# has never mattered only because `_GAMETYPE["tennis"]` is empty and the guard
# in _fetch_sport_sync returns before any request is made. Left as found rather
# than silently corrected — fixing it is a tennis change, not a hockey one.
SPORT_ID = {"soccer": 1, "basketball": 2, "tennis": 3, "americanfootball": 16,
            "icehockey": 4}

# outcome-name (Georgian, stable) → canonical selection key
_OUT = {"1": "home", "2": "away", "X": "draw",
        "მეტი": "over", "ნაკლები": "under"}

# HT/FT cells arrive already labelled "1/1" … "2/2", so no translation table is
# needed — but the set is fixed and a partial grid must be rejected rather than
# half-read (the consistency check reasons about the grid as a distribution).
_HTFT_CELLS = ("1/1", "1/X", "1/2", "X/1", "X/X", "X/2", "2/1", "2/X", "2/2")

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
        # 2026-08-13. The board response we ALREADY fetch carries 197 gameTypes
        # per soccer event and we were reading three of them. These five are the
        # inputs the consistency engine needs, identified by market SHAPE
        # (outcome count + labels) rather than by the Georgian name, and then
        # price-verified against Pinnacle — see notes/build_log.md.
        #
        # The ①/②/③ suffix on a gameName is the STATISTIC, not decoration:
        # ① goals, ② corners, ③ cards. `23 kutkhurebis r-ba ②` is a corners
        # total with exactly the same outcome shape as a goals total, so shape
        # alone is not enough to map on — every code below was confirmed ① and
        # then checked on price.
        5:      ("htft", "FT", 9, None),        # 'taimboli ①' — the 9-cell grid
        3:      ("moneyline", "H1", 3, None),   # 'I taimis shedegi ①'
        111:    ("moneyline", "H2", 3, None),   # 'II taimis shedegi ①'
        -284:   ("total", "H1", 2, None),       # 'I taimis totali ①'
        -30335: ("total", "H2", 2, None),       # 'II taimis totali ①'
    },
    "tennis": {},   # no remoteId on tennis events → unverifiable via SR join yet
    # American football (2026-08-12). Three of the five codes are basketball's,
    # but the TOTAL is not: AF uses -30172, not basketball's -2966. Copying the
    # basketball table wholesale would therefore have emitted zero totals and
    # silently mis-typed nothing else — the kind of miss that only a live
    # gameType census catches. All five carry the "(OT)" suffix in their
    # Georgian names, matching CB/Pinnacle's incl-overtime convention.
    # Ice hockey (2026-08-27, 244 events / 8 distinct gameTypes on the board).
    # Two of the codes ARE soccer's — 1 for the 3-way result and 8 for the goals
    # total — which is no coincidence: hockey's regulation board has soccer's
    # shape, a 3-way result over 60 minutes. The handicap -458 is soccer's too.
    #
    # What is NOT here matters more than what is. Crocobet ships NO incl-OT
    # market for hockey at all: every name above is bare, with none of the
    # "(OT)" suffixes that basketball and American football carry. So these are
    # REGULATION prices and they are emitted at period REG. Filing them at FT
    # would score a 60-minute price against Pinnacle's period-0 incl-OT
    # moneyline on every event.
    #
    # Deliberately excluded:
    #   190   'გამარჯვებული ①' — an OUTRIGHT. Its outcomes are team names
    #         (four of them), not 1/X/2; it is the tournament winner market
    #         riding on the same board.
    #   93    'ფრეზე გაყრა ∪ ①' — draw no bet, outcomes '(0)1'/'(0)2'.
    #         Pinnacle prices nothing against it.
    #   -273  'I პერიოდი - შედეგი ①' — the opening period result, and it is
    #         3-WAY. Pinnacle's period-1 hockey moneyline is 2-way, and
    #         _find_pin_match would have paired them before the selection-shape
    #         guard landed. Left out until there is a 2-way to pair it with.
    "icehockey": {
        1:    ("moneyline", "REG", 3, None),   # 'ძირითადი შედეგი  ∪ ①'
        8:    ("total", "REG", 2, None),       # 'გოლების რ-ბა 2.5  ① $'
        -458: ("spread", "REG", 2, None),      # 'ფორა -3.5 / +3.5 ① €'
        -329: ("spread", "P1", 2, None),       # 'I პერიოდი - ფორა -0.5 / +0.5'
        66:   ("total", "P1", 2, None),        # 'I პერიოდის გოლების რ-ბა 1.5'
    },
    "americanfootball": {
        -2527:  ("moneyline", "FT", 2, None),      # '1 2 ∪ ① (OT)'
        -2950:  ("spread", "FT", 2, None),         # 'ფორა -7.5 / +7.5 (OT)'
        -30172: ("total", "FT", 2, None),          # 'ქულების რაოდენობა 37.5 (OT)'
        182:    ("team_total", "FT", 2, "home"),   # 'I გუნდის ქულების რ-ბა (OT)'
        183:    ("team_total", "FT", 2, "away"),   # 'II გუნდის ქულების რ-ბა (OT)'
    },
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
        if market_type == "htft":
            grid = {}
            for o in g.get("outcomes") or []:
                nm = (o.get("outcomeName") or "").strip()
                od = o.get("outcomeOdds")
                if nm in _HTFT_CELLS and isinstance(od, (int, float)) and od > 1.0:
                    grid[nm] = float(od)
            if len(grid) != len(_HTFT_CELLS):
                continue                      # all nine cells or nothing
            try:
                rows.append(Odds(
                    source="crocobet", sport=sport, home=home, away=away,
                    market_type="htft", period="FT", selections=grid,
                    fetched_at=fetched_at, line=None, start_time=start_time,
                    league=league, raw_event_id=str(ev.get("eventId")),
                    sr_match_id=sr_match_id))
            except ValueError as e:
                log.debug("crocobet htft rejected: %s", e)
            continue
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
            and (c.get("eventsCount") or 0) > 0
            and not is_simulated_league(c.get("categoryName"))]
    if not leaf:
        return []
    ids = ",".join(str(c["categoryId"]) for c in leaf)
    board = _get(s, f"categories/multi/{ids}/events?lang=en").get("data") or []
    now_ms = fetched_at.timestamp() * 1000
    # prematch only — drop anything already started (grace 120s for clock skew)
    board = [e for e in board
             if not e.get("eventStart") or e["eventStart"] > now_ms - 120_000]
    # Global data horizon (src/horizon.py), applied to the BOARD so a far event
    # is dropped before the near/far split below — which means it costs neither
    # a per-event ladder call nor a parse of its inline mains.
    horizon_ms = horizon.cutoff_ts()
    if horizon_ms is not None:
        n_all = len(board)
        board = [e for e in board
                 if not e.get("eventStart") or e["eventStart"] <= horizon_ms * 1000]
        if len(board) != n_all:
            log.debug("crocobet %s: %d events beyond the %.1f-day horizon skipped",
                      sport, n_all - len(board), horizon.max_start_days())

    # Energy budget (2026-07-11, the "PC runs hot" fix): the board response
    # already carries the 3 MAIN markets inline for EVERY event (one call for
    # the whole board), so we only open per-event full ladders for games
    # starting within DETAIL_HOURS. Far-out games keep ML/total/spread from
    # the board (matching + arbs unaffected); extended depth appears as
    # kickoff approaches. This cut a ~950-call/~90 MB soccer+bball sweep per
    # cycle to board-size + near-game ladders.
    cutoff_ms = now_ms + DETAIL_HOURS * 3600_000
    near = [e for e in board if (e.get("eventStart") or 0) <= cutoff_ms]
    far = [e for e in board if (e.get("eventStart") or 0) > cutoff_ms]

    rows: list[Odds] = []
    for ev in far:                     # inline mains only — zero extra calls
        rows.extend(_parse_event(ev, ev.get("eventGames") or [], sport, fetched_at))

    def one(ev):
        try:
            full = _get(s, f"events/{ev['eventId']}?lang=en").get("data") or {}
            return _parse_event(ev, full.get("eventGames") or [], sport, fetched_at)
        except Exception as e:
            log.debug("crocobet event %s failed: %s", ev.get("eventId"), e)
            return []

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for chunk in ex.map(one, near):
            rows.extend(chunk)
    log.info("crocobet %s: %d Odds rows (%d near events full ladder, %d far "
             "board-inline)", sport, len(rows), len(near), len(far))
    return rows


async def fetch_crocobet(sport: str) -> list[Odds]:
    return await asyncio.to_thread(_fetch_sport_sync, sport)


async def fetch_crocobet_soccer() -> list[Odds]:
    return await fetch_crocobet("soccer")


async def fetch_crocobet_basketball() -> list[Odds]:
    return await fetch_crocobet("basketball")


async def fetch_crocobet_tennis() -> list[Odds]:
    return await fetch_crocobet("tennis")


async def fetch_crocobet_icehockey() -> list[Odds]:
    return await fetch_crocobet("icehockey")


async def fetch_crocobet_americanfootball() -> list[Odds]:
    return await fetch_crocobet("americanfootball")


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
