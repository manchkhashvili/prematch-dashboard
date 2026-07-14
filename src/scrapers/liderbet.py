"""
Lider-Bet prematch scraper — browser-free JSON, multi-sport.

Transport + parse for the third book on the dashboard (alongside CrystalBet and
Pinnacle). Full protocol writeup: ../../docs/liderbet.md. In short, two GETs:

  GET /services/pre/m1/api/sport/menu?lang=en      → sport→country→tournament tree
  GET /services/pre/m4/api/sport/matchData?tourIds=…&lang=en
                                                   → matches + odds, inline

No session, no ViewState, no auth — plain idempotent GETs over curl_cffi.
curl_cffi's Session is sync; callers get async wrappers (asyncio.to_thread),
mirroring cb_http.py.

Scope (list view, FT only): moneyline (1X2 for soccer, 2-way for basketball /
tennis), total (O/U, every line the list ships), spread (Asian handicap). That
is exactly the market set the matcher joins against Pinnacle. Sub-period markets
(1st half / quarter / set) are deliberately skipped here — they can follow the
same pattern later.

Every Odds row carries `sr_match_id` (the bare SportRadar id from
meta.matchProvider.matchId, e.g. "71792526") when present, so the matcher can
join Lider↔Betlive EXACTLY for cross-book arbs. Names are emitted raw; the
matcher's normalizer handles Lider's occasional Cyrillic competitor names via
transliteration (Pinnacle exposes no SportRadar id, so that leg is name-only).
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from src.models import Odds
from src.normalize import is_simulated_league, transliterate

log = logging.getLogger(__name__)

BASE = "https://sports.lider-bet.com/services"
MENU_URL = f"{BASE}/pre/m1/api/sport/menu"
MATCHDATA_URL = f"{BASE}/pre/m4/api/sport/matchData"
IMPERSONATE = "chrome124"

# Lider sport "section" id per dashboard sport name (verified 2026-06-15).
SECTION = {"soccer": "s:16", "basketball": "s:2", "tennis": "s:13"}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en",
    "Referer": "https://sports.lider-bet.com/",
    "Origin": "https://sports.lider-bet.com",
}

_TOURS_PER_CALL = 40          # batch tournaments into one matchData GET
_HTTP_TIMEOUT = 30.0

# Strip the circled/superscript variant glyphs Lider appends to market names
# ("Handicap ①", "1st Half Total ②") before classifying.
_DECOR = re.compile(r"[①-⓿⁰-₟⅐-↏]")
_SUBPERIOD = ("half", "quarter", " set", "{")  # → not a full-match market


def _classify_market(raw_name: str) -> tuple[str, int] | None:
    """Map a Lider marketType name → (market_type, n_way) for FT markets only.

    Returns None for sub-period markets (1st half / quarter / set) and anything
    that isn't a plain FT moneyline / total / handicap.
    """
    name = _DECOR.sub("", raw_name or "").strip().lower()
    name = re.sub(r"\s+", " ", name)
    if any(tok in name for tok in _SUBPERIOD):
        return None
    if name == "full time result":
        return ("moneyline", 3)
    if name in ("winner", "winner (ot)", "match winner", "money line", "result"):
        return ("moneyline", 2)
    if name in ("total", "total (ot)", "total games"):
        return ("total", 2)
    if name in ("handicap", "handicap (ot)", "asian handicap"):
        return ("spread", 2)
    return None


def _menu_nodes(menu: dict) -> list[dict]:
    return [it for v in menu.values() if isinstance(v, list)
            for it in v if isinstance(it, dict)]


def _to_float(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _spec_line(specifier: dict | None) -> float | None:
    """Pull the numeric line out of a market/outcome specifier."""
    if not isinstance(specifier, dict):
        return None
    for key in ("total", "handicap", "hcp", "special"):
        if key in specifier:
            v = _to_float(specifier[key])
            if v is not None:
                return v
    return None


def _odds(value) -> float | None:
    """Decimal odds, or None if missing/suspended (<= 1.0)."""
    v = _to_float(value)
    return v if v is not None and v > 1.0 else None


def _parse_match(
    m: dict, ancestors: dict, market_types: dict, sport_name: str, fetched_at: datetime,
) -> list[Odds]:
    home = (ancestors.get(m.get("homeId"), {}) or {}).get("name") or ""
    away = (ancestors.get(m.get("awayId"), {}) or {}).get("name") or ""
    # Romanize for display — Lider ships some names in Russian/Georgian even at
    # lang=en (no English exists in the feed). transliterate() is a no-op on the
    # Latin majority. The matcher re-normalizes anyway, so this is display-safe.
    home, away = transliterate(home.strip()), transliterate(away.strip())
    if not home or not away:
        return []
    league = transliterate((ancestors.get(m.get("tourId"), {}) or {}).get("name") or "") or None

    start_time = None
    st = m.get("startTime")
    if st:
        try:
            start_time = datetime.fromisoformat(st).replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    sr = (m.get("meta", {}) or {}).get("matchProvider", {}) or {}
    sr_id = sr.get("matchId") or ""
    sr_match_id = sr_id[len("sr:match:"):] if sr_id.startswith("sr:match:") else None

    event_id = m.get("id")
    out: list[Odds] = []

    for mk in (m.get("markets") or {}).values():
        tp = market_types.get(mk.get("typeId"), {})
        cls = _classify_market(tp.get("name", ""))
        if cls is None:
            continue
        market_type, n_way = cls

        # outcomeType id → label ("1"/"X"/"2"/"Over"/"Under")
        label = {ot["id"]: (ot.get("name") or "").strip()
                 for ot in tp.get("outcomeTypes", [])}
        priced = {label.get(otid, ""): o for otid, o in (mk.get("outcomes") or {}).items()}

        if market_type == "moneyline":
            sel = {"home": _odds((priced.get("1") or {}).get("value")),
                   "away": _odds((priced.get("2") or {}).get("value"))}
            if n_way == 3:
                sel["draw"] = _odds((priced.get("X") or {}).get("value"))
            row = _build(sport_name, home, away, "moneyline", "FT", sel,
                         None, league, start_time, event_id, sr_match_id, fetched_at)
            if row:
                out.append(row)

        elif market_type == "total":
            line = _spec_line(mk.get("specifier"))
            sel = {"over": _odds((priced.get("Over") or {}).get("value")),
                   "under": _odds((priced.get("Under") or {}).get("value"))}
            row = _build(sport_name, home, away, "total", "FT", sel,
                         line, league, start_time, event_id, sr_match_id, fetched_at)
            if row:
                out.append(row)

        elif market_type == "spread":
            home_oc = priced.get("1") or {}
            line = _spec_line(home_oc.get("specifier")) or _spec_line(mk.get("specifier"))
            sel = {"home": _odds(home_oc.get("value")),
                   "away": _odds((priced.get("2") or {}).get("value"))}
            row = _build(sport_name, home, away, "spread", "FT", sel,
                         line, league, start_time, event_id, sr_match_id, fetched_at)
            if row:
                out.append(row)

    return out


def _build(sport_name, home, away, market_type, period, selections, line,
           league, start_time, event_id, sr_match_id, fetched_at) -> Odds | None:
    if any(v is None for v in selections.values()):
        return None
    if market_type in ("total", "spread") and line is None:
        return None
    try:
        return Odds(
            source="liderbet", sport=sport_name, home=home, away=away,
            market_type=market_type, period=period, selections=selections,
            fetched_at=fetched_at, line=line, start_time=start_time,
            league=league, raw_event_id=str(event_id) if event_id else None,
            sr_match_id=sr_match_id,
        )
    except ValueError as exc:               # odds <= 1.0 slipped through
        log.debug("liderbet Odds rejected: %s", exc)
        return None


def _fetch_sport_sync(sport_name: str) -> list[Odds]:
    from curl_cffi.requests import Session

    section = SECTION.get(sport_name)
    if section is None:
        log.warning("liderbet: unknown sport %r", sport_name)
        return []

    fetched_at = datetime.now(tz=timezone.utc)
    s = Session(impersonate=IMPERSONATE)

    menu = s.get(f"{MENU_URL}?lang=en&marketFilter=true",
                 headers=HEADERS, timeout=_HTTP_TIMEOUT).json()["menu"]
    # The menu is a flat adjacency map {parentNodeId: [childNodes]}. Simulated
    # football hides under real-looking TOURNAMENT names ("World Cup",
    # "Champions League") whose PARENT category is the tell — e.g. the sim
    # "World Cup" (t:69953) sits under c:17065 "Simulated Reality League",
    # while the REAL c:22319 "World Cup 2026" is separate. So we drop a
    # tournament when its own name OR its parent category name looks simulated
    # (fixes the England-v-Argentina phantom edges, 2026-07-14).
    name_by_id = {n["id"]: (n.get("name") or "")
                  for v in menu.values() if isinstance(v, list)
                  for n in v if isinstance(n, dict) and n.get("id")}
    parent_of: dict[str, str] = {}
    for parent_id, children in menu.items():
        if not isinstance(children, list):
            continue
        for n in children:
            if isinstance(n, dict) and n.get("id"):
                parent_of[n["id"]] = parent_id
    tour_ids = [
        n["id"] for n in _menu_nodes(menu)
        if n.get("id", "").startswith("t:")
        and n.get("sectionId") == section and n.get("cnt", 0) > 0
        and not is_simulated_league(n.get("name"))
        and not is_simulated_league(name_by_id.get(parent_of.get(n["id"], "")))
    ]
    if not tour_ids:
        log.info("liderbet %s: no tournaments with games", sport_name)
        return []

    rows: list[Odds] = []
    for i in range(0, len(tour_ids), _TOURS_PER_CALL):
        chunk = ",".join(tour_ids[i:i + _TOURS_PER_CALL])
        try:
            data = s.get(f"{MATCHDATA_URL}?tourIds={chunk}&lang=en&marketFilter=true",
                         headers=HEADERS, timeout=_HTTP_TIMEOUT).json()["data"]
        except Exception as exc:
            log.warning("liderbet %s: matchData chunk failed: %s", sport_name, exc)
            continue
        anc, mts = data.get("ancestors", {}), data.get("marketTypes", {})
        for m in (data.get("matches") or {}).values():
            rows.extend(_parse_match(m, anc, mts, sport_name, fetched_at))

    log.info("liderbet %s: %d Odds rows from %d tournaments",
             sport_name, len(rows), len(tour_ids))
    return rows


async def fetch_liderbet(sport_name: str) -> list[Odds]:
    """Async wrapper — runs the sync curl_cffi fetch in a worker thread."""
    return await asyncio.to_thread(_fetch_sport_sync, sport_name)


async def fetch_liderbet_soccer() -> list[Odds]:
    return await fetch_liderbet("soccer")


async def fetch_liderbet_basketball() -> list[Odds]:
    return await fetch_liderbet("basketball")


async def fetch_liderbet_tennis() -> list[Odds]:
    return await fetch_liderbet("tennis")


if __name__ == "__main__":   # smoke: python -m src.scrapers.liderbet [sport]
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sport = sys.argv[1] if len(sys.argv) > 1 else "soccer"
    odds = asyncio.run(fetch_liderbet(sport))
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
