"""
Betlive prematch scraper — browser-free JSON, multi-sport.

Fourth book on the dashboard (CrystalBet, Pinnacle, Lider-Bet, Betlive). Full
protocol writeup: ../../docs/betlive.md. The sportsbook (sport.betlive.com) is
an Angular SPA behind Cloudflare backed by a plain REST API at
sportnew.betlive.com; curl_cffi Chrome impersonation passes Cloudflare with no
challenge. The catalog drill-down:

  GET /api/category/getCountryCategories?sportId=N   → nested country→league tree
  GET /api/event/getLeagueEvents?leagueIds=…&take=N  → events + curated markets, odds inline

Scope (list view, FT only): **moneyline** — 1X2 for soccer, 2-way for
basketball/tennis. The getLeagueEvents list prices only the headline markets
(moneyline, plus Double Chance / BTTS which we don't use); every total and
Asian-handicap market comes back as a null-outcome placeholder whose odds load
only via getPrematchEvent (Betlive's per-event "detail" call, like CrystalBet's
ExpandDetail). The total/spread classifier paths below are therefore wired and
ready but stay dormant on the list feed — turning on per-event detail fetches to
populate them is the obvious next enrichment. Sub-period markets are skipped.

sr_match_id: Betlive's providerEventId IS the bare SportRadar id when the event
comes from the SportRadar feed (providerId 14, verified 2026-06-15 — the same
number Lider-Bet ships as "sr:match:N"). We set sr_match_id ONLY for
providerId 14, so esports/virtual feeds (providerId 19) never produce a false
cross-book join. Names are emitted raw; the matcher normalizes.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from src.models import Odds

log = logging.getLogger(__name__)

BASE = "https://sportnew.betlive.com"
COUNTRIES_URL = f"{BASE}/api/category/getCountryCategories"
LEAGUE_EVENTS_URL = f"{BASE}/api/event/getLeagueEvents"
IMPERSONATE = "chrome124"

# Betlive sportId per dashboard sport name (verified 2026-06-15).
SPORT_ID = {"soccer": 1, "basketball": 6, "tennis": 5}

# providerId whose providerEventId is a real SportRadar match id.
SPORTRADAR_PROVIDER_IDS = frozenset({14})

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en",
    "Referer": "https://sport.betlive.com/",
    "Origin": "https://sport.betlive.com",
}

_LEAGUES_PER_CALL = 25
_EVENTS_PER_LEAGUE = 80
_HTTP_TIMEOUT = 30.0

# .NET DateTime.Ticks epoch: 100-ns intervals since 0001-01-01.
_DOTNET_EPOCH = datetime(1, 1, 1)

# Leaf-league name fragments that are esports/virtual/special shelves, not real
# fixtures — skipped so we don't pour synthetic events into the matcher.
_SKIP_LEAGUE = ("e-sports", "volta", "setka", "liga pro", "special", "srl ",
                "esoccer", "ebasket", "cyber")

_SUBPERIOD = ("half", "quarter", " set", "1st", "2nd", "3rd", "4th", "period", "map")
_PROP = ("home", "away", "team", "odd/even", "odd / even", "exact", "correct",
         "range", "race", "player", "both")


def ticks_to_utc(ticks) -> datetime | None:
    """.NET DateTime.Ticks → naive-UTC datetime (no tz attached upstream)."""
    try:
        return (_DOTNET_EPOCH + timedelta(microseconds=int(ticks) // 10)).replace(
            tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _classify_market(raw_name: str, sels: set[str]) -> tuple[str, int] | None:
    """Map a Betlive marketName (+ its selection labels) → (market_type, n_way),
    FT markets only. None for sub-period / prop / team markets."""
    name = re.sub(r"\s+", " ", (raw_name or "").strip().lower())
    if not name or any(tok in name for tok in _SUBPERIOD):
        return None
    if any(tok in name for tok in _PROP):
        return None
    if "handicap" in name and {"1", "2"} <= sels:
        return ("spread", 2)
    if "total" in name and {"Over", "Under"} <= sels:
        return ("total", 2)
    if name in ("fulltime result", "full time result", "1x2", "result"):
        return ("moneyline", 3 if "X" in sels else 2)
    if name in ("match winner", "match winner (ot)", "winner", "moneyline",
                "money line", "to win the match",
                # Betlive basketball's curated ML is literally named "12 (OT)"
                # (2-way incl overtime; outcomes "1"/"2") — found 2026-07-11
                # when basketball emitted 0 rows from 140 events.
                "12 (ot)", "12"):
        return ("moneyline", 2)
    return None


def _to_float(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _odds(value) -> float | None:
    v = _to_float(value)
    return v if v is not None and v > 1.0 else None


def _leaf_leagues(nodes: list) -> list[dict]:
    out: list[dict] = []
    for n in nodes:
        kids = n.get("children") or []
        if kids:
            out += _leaf_leagues(kids)
        elif n.get("eventCount", 0) > 0:
            out.append(n)
    return out


# A prematch getLeagueEvents response occasionally carries already-started /
# in-play fixtures. Betlive marks those with eventType == 2 (verified live
# 2026-07-11: eventType==2 exactly equalled the set of events whose startDate
# was in the past — soccer 6/6, basketball 0/0). We drop them two ways for
# safety: the explicit live marker AND a start-time-in-the-past check with a
# small grace for clock skew. Prematch odds must never carry a live game — its
# price has moved off the pre-game number and would fire phantom edges/arbs.
_LIVE_GRACE_SEC = 120


def _is_live(ev: dict, start_time: datetime | None, now: datetime) -> bool:
    if ev.get("eventType") == 2:
        return True
    if start_time is not None and start_time < now - timedelta(seconds=_LIVE_GRACE_SEC):
        return True
    return False


def _parse_event(ev: dict, sport_name: str, fetched_at: datetime) -> list[Odds]:
    home = (ev.get("homeTeamName") or "").strip()
    away = (ev.get("awayTeamName") or "").strip()
    if not home or not away:
        return []
    league = ev.get("leagueName")
    start_time = ticks_to_utc(ev.get("startDate"))
    if _is_live(ev, start_time, fetched_at):
        return []                       # in-play leak — not a prematch price
    event_id = ev.get("id")

    sr_match_id = None
    if ev.get("providerId") in SPORTRADAR_PROVIDER_IDS and ev.get("providerEventId"):
        sr_match_id = str(ev["providerEventId"])

    out: list[Odds] = []
    for mk in ev.get("markets") or []:
        ocs = mk.get("outcomes") or []
        if not ocs:
            continue
        sels = {(o.get("name") or "").strip() for o in ocs}
        cls = _classify_market(ocs[0].get("marketName", ""), sels)
        if cls is None:
            continue
        market_type, n_way = cls
        priced = {(o.get("name") or "").strip(): o for o in ocs}

        if market_type == "moneyline":
            sel = {"home": _odds((priced.get("1") or {}).get("odd")),
                   "away": _odds((priced.get("2") or {}).get("odd"))}
            if n_way == 3:
                sel["draw"] = _odds((priced.get("X") or {}).get("odd"))
            line = None
        elif market_type == "total":
            sel = {"over": _odds((priced.get("Over") or {}).get("odd")),
                   "under": _odds((priced.get("Under") or {}).get("odd"))}
            line = _line(priced.get("Over") or priced.get("Under") or {})
        else:  # spread — line is the HOME ("1") handicap, signed
            sel = {"home": _odds((priced.get("1") or {}).get("odd")),
                   "away": _odds((priced.get("2") or {}).get("odd"))}
            line = _line(priced.get("1") or {})

        row = _build(sport_name, home, away, market_type, sel, line,
                     league, start_time, event_id, sr_match_id, fetched_at)
        if row:
            out.append(row)
    return out


def _line(outcome: dict) -> float | None:
    """Line for a totals/handicap outcome: argumentX (number) or argumentString."""
    v = outcome.get("argumentX")
    if v not in (None, 0, 0.0):
        return _to_float(v)
    return _to_float(outcome.get("argumentString"))


def _build(sport_name, home, away, market_type, selections, line,
           league, start_time, event_id, sr_match_id, fetched_at) -> Odds | None:
    if any(v is None for v in selections.values()):
        return None
    if market_type in ("total", "spread") and line is None:
        return None
    try:
        return Odds(
            source="betlive", sport=sport_name, home=home, away=away,
            market_type=market_type, period="FT", selections=selections,
            fetched_at=fetched_at, line=line, start_time=start_time,
            league=league, raw_event_id=str(event_id) if event_id else None,
            sr_match_id=sr_match_id,
        )
    except ValueError as exc:
        log.debug("betlive Odds rejected: %s", exc)
        return None


def _fetch_sport_sync(sport_name: str) -> list[Odds]:
    from curl_cffi.requests import Session

    sport_id = SPORT_ID.get(sport_name)
    if sport_id is None:
        log.warning("betlive: unknown sport %r", sport_name)
        return []

    fetched_at = datetime.now(tz=timezone.utc)
    s = Session(impersonate=IMPERSONATE)

    cc = s.get(f"{COUNTRIES_URL}?sportId={sport_id}",
               headers=HEADERS, timeout=_HTTP_TIMEOUT).json()
    leagues = [l for l in _leaf_leagues(cc)
               if not any(tok in (l.get("name") or "").lower() for tok in _SKIP_LEAGUE)]
    league_ids = [str(l["id"]) for l in leagues]
    if not league_ids:
        log.info("betlive %s: no leagues with events", sport_name)
        return []

    rows: list[Odds] = []
    for i in range(0, len(league_ids), _LEAGUES_PER_CALL):
        chunk = ",".join(league_ids[i:i + _LEAGUES_PER_CALL])
        try:
            data = s.get(
                f"{LEAGUE_EVENTS_URL}?leagueIds={chunk}&page=0&take={_EVENTS_PER_LEAGUE}",
                headers=HEADERS, timeout=_HTTP_TIMEOUT).json()
        except Exception as exc:
            log.warning("betlive %s: getLeagueEvents chunk failed: %s", sport_name, exc)
            continue
        for wrapper in data or []:
            for ev in wrapper.get("events") or []:
                rows.extend(_parse_event(ev, sport_name, fetched_at))

    log.info("betlive %s: %d Odds rows from %d leagues",
             sport_name, len(rows), len(league_ids))
    return rows


async def fetch_betlive(sport_name: str) -> list[Odds]:
    """Async wrapper — runs the sync curl_cffi fetch in a worker thread."""
    return await asyncio.to_thread(_fetch_sport_sync, sport_name)


async def fetch_betlive_soccer() -> list[Odds]:
    return await fetch_betlive("soccer")


async def fetch_betlive_basketball() -> list[Odds]:
    return await fetch_betlive("basketball")


async def fetch_betlive_tennis() -> list[Odds]:
    return await fetch_betlive("tennis")


if __name__ == "__main__":   # smoke: python -m src.scrapers.betlive [sport]
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sport = sys.argv[1] if len(sys.argv) > 1 else "soccer"
    odds = asyncio.run(fetch_betlive(sport))
    print(f"\n{sport}: {len(odds)} Odds rows")
    by_mt: dict[str, int] = {}
    with_sr = 0
    for o in odds:
        by_mt[o.market_type] = by_mt.get(o.market_type, 0) + 1
        with_sr += o.sr_match_id is not None
    print("by market_type:", by_mt)
    print(f"with sr_match_id: {with_sr}/{len(odds)}")
    for o in odds[:6]:
        print(f"  {o.home} vs {o.away} | {o.market_type} {o.period} "
              f"line={o.line} {o.selections} sr={o.sr_match_id} @ {o.start_time}")
