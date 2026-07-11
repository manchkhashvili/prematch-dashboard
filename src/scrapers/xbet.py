"""1xbet prematch scraper — the v2 reference book (replaces pinnacle.py).

Drop-in interface match for the old Pinnacle scraper: async
`fetch_xbet_<sport>() -> list[Odds]` with source="xbet". The poll loop, the
matcher, edge math, bets/CLV and every page keep working unchanged — they
only ever consumed Odds rows (state slot / DB columns still say "pin"; that
naming is internal).

Protocol: docs/1xbet-prematch.md. Stateless JSON on
`1xbet-ge.com/service-api/LineFeed/`. Hard-won rules:
- QUERY-PARAM ORDER IS ENFORCED (tf right after lng; wrong order → 406).
  URLs below replicate the SPA's canonical strings — never rebuild from dicts.
- DNS can wedge on gambling domains → raw-UDP resolve + curl pin (ported
  from live's probe_1xbet_live.py).
- The feed times out in bursts → one same-call retry on a fresh session.

Market codes — ONLY price-verified mappings are emitted (cross-checked vs
Lider-Bet / CrystalBet on live matched games; notes/build_log.md):

  basketball (verified 2026-07-10, 0.4–2.8pp devigged agreement):
    G=101 T401/402  moneyline (incl OT)         → moneyline FT
    G=17  T9/T10@P  total (incl OT)             → total FT
    G=2   T7@P=+L pairs T8@P=−L                 → spread FT (line = home line)
    G=15  T11/12@P home total, G=62 T13/14@P    → team_total FT
    sub-games: "1 Half"→H1, quarters→Q1..Q4 (same G codes inside; "2 Half"
    is REGULAR time — no H2 period in the v1 model, so it is not emitted)
  soccer (verified vs CB 2026-06-17, live docs):
    G=1 T1/T2/T3 1X2 → moneyline FT (3-way), G=17 totals, G=2 spread
    sub-game "1st half"→H1
  tennis: G=1 T1/T3 moneyline; G=17 GAMES total; G=2 GAMES handicap
    (side-signed) — verified vs Lider by name+time 2026-07-11
    (ML 1.32pp / total 1.45pp / handicap 0.99pp medians).
  mma: sport id resolved from the sports tree by name; G=1 2-way moneyline.

Handicap pairing self-check: basketball lists each side at its OWN signed
line (T7@+L with T8@−L); other sports may list both sides at one P. We build
both pairings and keep whichever yields more complete lines for that event —
data decides, not assumption.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlparse

from src.models import Odds

log = logging.getLogger(__name__)

BASE = "https://1xbet-ge.com/service-api/LineFeed"
IMPERSONATE = "chrome124"
HEADERS = {"Accept": "application/json, text/plain, */*", "Accept-Language": "en",
           "Referer": "https://1xbet-ge.com/en/line"}
TIMEOUT = 15
MAX_WORKERS = 8
ENUM_TTL = 1800.0            # champ/board enumeration cache (fixtures churn slowly)

SPORT_ID = {"basketball": 3, "soccer": 1, "tennis": 4}   # mma resolved by name

# Energy budget (2026-07-11, the "PC runs hot" fix): pricing the WHOLE board
# every cycle was ~4,400 GetGameZip calls + ~60 MB JSON parse per 2 min across
# 3 sports. Ladders are now fetched only for events starting within
# HORIZON_HOURS, and half/quarter sub-game ladders only within SUBGAME_HOURS
# (prematch period markets barely move days out). Enumeration still covers the
# full board, so nothing is lost from matching — far events simply have no
# 1xbet price until they enter the horizon.
HORIZON_HOURS = float(os.environ.get("XBET_HORIZON_HOURS", "36"))
SUBGAME_HOURS = float(os.environ.get("XBET_SUBGAME_HOURS", "18"))
_MMA_NAMES = ("mma", "martial arts", "ufc")

# sub-game panel name → v1 period, per sport (verified live)
SUBGAME_PERIODS = {
    "basketball": {"1 Half": "H1", "1st quarter": "Q1", "2nd quarter": "Q2",
                   "3rd quarter": "Q3", "4th quarter": "Q4"},
    "soccer": {"1st half": "H1"},
}

# ── DNS pin (ported from live probe; see module docstring) ───────────────────
_HOST = urlparse(BASE).hostname
_PORT = urlparse(BASE).port or 443
_dns_cache = {"ip": None, "ts": 0.0}
_DNS_TTL = 600.0
_tls = threading.local()


def _nameservers():
    servers = []
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                if line.startswith("nameserver"):
                    servers.append(line.split()[1])
    except Exception:
        pass
    for pub in ("1.1.1.1", "8.8.8.8"):
        if pub not in servers:
            servers.append(pub)
    return servers


def _dns_a(server, host, timeout=3.0):
    q = struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
    for part in host.split("."):
        q += bytes([len(part)]) + part.encode()
    q += b"\x00" + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(q, (server, 53))
        data, _ = s.recvfrom(2048)
    finally:
        s.close()
    ancount = struct.unpack(">H", data[6:8])[0]
    i = 12
    while data[i]:
        i += 1 + data[i]
    i += 5
    for _ in range(ancount):
        i += 2
        typ, _cls, _ttl, rdl = struct.unpack(">HHIH", data[i:i + 10]); i += 10
        if typ == 1 and rdl == 4:
            return ".".join(str(b) for b in data[i:i + 4])
        i += rdl
    return None


def _resolve_ip():
    now = time.time()
    if _dns_cache["ip"] and now - _dns_cache["ts"] < _DNS_TTL:
        return _dns_cache["ip"]
    for srv in _nameservers():
        try:
            ip = _dns_a(srv, _HOST)
            if ip:
                _dns_cache.update(ip=ip, ts=now)
                return ip
        except Exception:
            continue
    return _dns_cache["ip"]


def _pin(s):
    ip = _resolve_ip()
    if not ip:
        return
    try:
        from curl_cffi.const import CurlOpt
        prev = getattr(_tls, "pinned_ip", None)
        entries = [f"{_HOST}:{_PORT}:{ip}"]
        if prev and prev != ip:
            entries.insert(0, f"-{_HOST}:{_PORT}")
        s.curl.setopt(CurlOpt.RESOLVE, entries)
        _tls.pinned_ip = ip
    except Exception:
        pass


def _session():
    s = getattr(_tls, "session", None)
    if s is None:
        from curl_cffi.requests import Session
        s = _tls.session = Session(impersonate=IMPERSONATE)
    return s


def _get(path: str, _retry: bool = True) -> dict:
    s = _session()
    _pin(s)
    try:
        r = s.get(f"{BASE}/{path}", headers=HEADERS, timeout=TIMEOUT)
    except Exception:
        if _retry:                       # bursty-timeout feed: fresh session, once
            _tls.session = None
            return _get(path, _retry=False)
        raise
    if r.status_code == 406:
        log.error("1xbet 406 NotAcceptable — param-order bug in URL: %s", path)
    r.raise_for_status()
    return r.json()


# ── canonical-order URLs — do not reorder params ─────────────────────────────
def _sports_tree() -> list[dict]:
    d = _get("GetSportsZip?lng=en&gr=1232&country=61&tf=2200000&tz=4"
             "&partner=151&virtualSports=true")
    return d.get("Value") or []


def _champs(sport_id: int) -> list[dict]:
    d = _get(f"GetChampsZip?sport={sport_id}&lng=en&tf=2200000&tz=4&country=61"
             f"&partner=151&virtualSports=true&gr=1232")
    return d.get("Value") or []


def _champ_games(champ_li: int) -> list[dict]:
    d = _get(f"GetChampZip?champ={champ_li}&lng=en&tf=2200000&tz=4&country=61"
             f"&mode=4&getEmpty=true&antisports=188&partner=151&gr=1232")
    return ((d.get("Value") or {}).get("G")) or []


def _game_zip(event_id: int) -> dict:
    d = _get(f"GetGameZip?id={event_id}&lng=en&tf=2200000&tz=4&gr=1232&country=61"
             f"&partner=151&grMode=4&isSubGames=true&GroupEvents=true&countevents=250")
    return d.get("Value") or {}


# ── board enumeration (cached) ────────────────────────────────────────────────
_enum_cache: dict[str, tuple[float, list[dict]]] = {}
_mma_sport_id: list[int | None] = [None]


def _resolve_mma_id() -> int | None:
    if _mma_sport_id[0] is None:
        for sp in _sports_tree():
            name = (sp.get("N") or "").lower()
            if any(t in name for t in _MMA_NAMES):
                _mma_sport_id[0] = sp.get("I")
                break
        else:
            _mma_sport_id[0] = -1
    return _mma_sport_id[0] if _mma_sport_id[0] != -1 else None


def _enumerate(sport_name: str) -> list[dict]:
    now = time.time()
    ts, cached = _enum_cache.get(sport_name, (0.0, []))
    if cached and now - ts < ENUM_TTL:
        return cached
    sid = SPORT_ID.get(sport_name) or (_resolve_mma_id() if sport_name == "mma" else None)
    if sid is None:
        log.warning("xbet: no sport id for %r", sport_name)
        return cached
    champs = [c for c in _champs(sid) if (c.get("GC") or 0) > 0]
    games: list[dict] = []

    def one(c):
        out = []
        for g in _champ_games(c.get("LI")):
            if g.get("I") and g.get("O1") and g.get("O2"):
                g["_league"] = g.get("L") or c.get("L")
                out.append(g)
        return out

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for chunk in ex.map(one, champs):
            games.extend(chunk)
    _enum_cache[sport_name] = (now, games)
    log.info("xbet %s: enumerated %d events in %d champs",
             sport_name, len(games), len(champs))
    return games


# ── GameZip → Odds rows ───────────────────────────────────────────────────────
def _flat_groups(val: dict) -> dict[int, list[dict]]:
    ge: dict[int, list[dict]] = {}
    for g in (val.get("GE") or []):
        bucket = ge.setdefault(g.get("G"), [])
        for col in (g.get("E") or []):
            bucket.extend(col if isinstance(col, list) else [col])
    return ge


def _price(o) -> float | None:
    c = o.get("C")
    return float(c) if isinstance(c, (int, float)) and c > 1.0 else None


def _spread_rows(entries) -> dict[float, tuple[float, float]]:
    """{home_line: (home_odds, away_odds)} — self-detecting pairing mode."""
    home = {float(o["P"]): _price(o) for o in entries
            if o.get("T") == 7 and o.get("P") is not None and _price(o)}
    away = {float(o["P"]): _price(o) for o in entries
            if o.get("T") == 8 and o.get("P") is not None and _price(o)}
    neg = {l: (h, away[-l]) for l, h in home.items() if -l in away}
    same = {l: (h, away[l]) for l, h in home.items() if l in away}
    return neg if len(neg) >= len(same) else same


def _ou_map(entries, t_over: int, t_under: int) -> dict[float, tuple[float, float]]:
    per: dict[float, dict[int, float]] = {}
    for o in entries:
        p = _price(o)
        if p and o.get("P") is not None:
            per.setdefault(float(o["P"]), {})[o.get("T")] = p
    return {l: (ts[t_over], ts[t_under]) for l, ts in per.items()
            if t_over in ts and t_under in ts}


def _mk(game, sport, market_type, period, sels, line=None, team_side=None,
        fetched_at=None) -> Odds | None:
    try:
        return Odds(
            source="xbet", sport=sport,
            home=(game.get("O1") or "").strip(), away=(game.get("O2") or "").strip(),
            market_type=market_type, period=period, selections=sels,
            fetched_at=fetched_at, line=line,
            start_time=datetime.fromtimestamp(game["S"], tz=timezone.utc)
            if game.get("S") else None,
            league=game.get("_league") or game.get("L"),
            raw_event_id=str(game.get("I")), team_side=team_side)
    except (ValueError, KeyError, TypeError):
        return None


def _parse_zip(val: dict, game: dict, sport: str, period: str,
               fetched_at: datetime) -> list[Odds]:
    ge = _flat_groups(val)
    rows: list[Odds] = []

    if period == "FT":
        if sport == "soccer":                      # 1X2, G=1 T1/T2/T3
            ml = {o.get("T"): _price(o) for o in ge.get(1, []) if o.get("P") is None}
            if ml.get(1) and ml.get(2) and ml.get(3):
                rows.append(_mk(game, sport, "moneyline", "FT",
                                {"home": ml[1], "draw": ml[2], "away": ml[3]},
                                fetched_at=fetched_at))
        elif sport == "basketball":                # G=101 T401/402
            ml = {o.get("T"): _price(o) for o in ge.get(101, []) if o.get("P") is None}
            if ml.get(401) and ml.get(402):
                rows.append(_mk(game, sport, "moneyline", "FT",
                                {"home": ml[401], "away": ml[402]},
                                fetched_at=fetched_at))
        else:                                      # tennis / mma: G=1 T1/T3 2-way
            ml = {o.get("T"): _price(o) for o in ge.get(1, []) if o.get("P") is None}
            if ml.get(1) and ml.get(3):
                rows.append(_mk(game, sport, "moneyline", "FT",
                                {"home": ml[1], "away": ml[3]},
                                fetched_at=fetched_at))

    if sport in ("basketball", "soccer", "tennis"):
        # tennis: G=17 = GAMES total (1.45pp median vs Lider 'Total', n=41),
        # G=2 = GAMES handicap side-signed (0.99pp, n=64) — verified 2026-07-11
        # by name+time pairing vs Lider (1xbet has no SR ids).
        for line, (h, a) in _spread_rows(ge.get(2, [])).items():
            rows.append(_mk(game, sport, "spread", period,
                            {"home": h, "away": a}, line=line, fetched_at=fetched_at))
        for line, (o_, u) in _ou_map(ge.get(17, []), 9, 10).items():
            rows.append(_mk(game, sport, "total", period,
                            {"over": o_, "under": u}, line=line, fetched_at=fetched_at))

    if sport == "basketball":                      # team totals: T11-14 (not 9/10!)
        for line, (o_, u) in _ou_map(ge.get(15, []), 11, 12).items():
            rows.append(_mk(game, sport, "team_total", period,
                            {"over": o_, "under": u}, line=line,
                            team_side="home", fetched_at=fetched_at))
        for line, (o_, u) in _ou_map(ge.get(62, []), 13, 14).items():
            rows.append(_mk(game, sport, "team_total", period,
                            {"over": o_, "under": u}, line=line,
                            team_side="away", fetched_at=fetched_at))

    return [r for r in rows if r is not None]


def _fetch_event(game: dict, sport: str, periods: bool,
                 fetched_at: datetime) -> list[Odds]:
    rows = _parse_zip(_game_zip(int(game["I"])), game, sport, "FT", fetched_at)
    if periods and (game.get("S") or 0) <= time.time() + SUBGAME_HOURS * 3600:
        pmap = SUBGAME_PERIODS.get(sport) or {}
        subs = {(x.get("PN") or "").strip(): x.get("I")
                for x in (game.get("SG") or []) if x.get("I")}
        for pn, period in pmap.items():
            sub_id = subs.get(pn)
            if not sub_id:
                continue
            try:
                rows += _parse_zip(_game_zip(int(sub_id)), game, sport,
                                   period, fetched_at)
            except Exception as e:
                log.debug("xbet sub-game %s/%s: %s", game.get("I"), pn, e)
    return rows


def _fetch_sport_sync(sport: str, periods: bool) -> list[Odds]:
    games = _enumerate(sport)
    fetched_at = datetime.now(tz=timezone.utc)
    horizon = time.time() + HORIZON_HOURS * 3600
    skipped = sum(1 for g in games if (g.get("S") or 0) > horizon)
    games = [g for g in games if (g.get("S") or 0) <= horizon]
    if skipped:
        log.debug("xbet %s: %d events beyond %.0fh horizon skipped",
                  sport, skipped, HORIZON_HOURS)
    rows: list[Odds] = []
    errors = 0

    def one(g):
        nonlocal errors
        try:
            return _fetch_event(g, sport, periods, fetched_at)
        except Exception as e:
            errors += 1
            if errors <= 3:
                log.warning("xbet event %s failed: %s", g.get("I"), e)
            return []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for chunk in ex.map(one, games):
            rows.extend(chunk)
    if errors:
        log.info("xbet %s: %d/%d events failed this cycle", sport, errors, len(games))
    return rows


# ── public interface (pinnacle-compatible) ────────────────────────────────────
async def fetch_xbet_basketball(*, concurrency: int = 10) -> list[Odds]:
    return await asyncio.to_thread(_fetch_sport_sync, "basketball", True)


async def fetch_xbet_soccer(*, concurrency: int = 10) -> list[Odds]:
    return await asyncio.to_thread(_fetch_sport_sync, "soccer", True)


async def fetch_xbet_tennis(*, concurrency: int = 10) -> list[Odds]:
    return await asyncio.to_thread(_fetch_sport_sync, "tennis", False)


async def fetch_xbet_mma(*, concurrency: int = 10) -> list[Odds]:
    return await asyncio.to_thread(_fetch_sport_sync, "mma", False)


if __name__ == "__main__":   # smoke: python -m src.scrapers.xbet [sport]
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sport = sys.argv[1] if len(sys.argv) > 1 else "basketball"
    fn = {"basketball": fetch_xbet_basketball, "soccer": fetch_xbet_soccer,
          "tennis": fetch_xbet_tennis, "mma": fetch_xbet_mma}[sport]
    odds = asyncio.run(fn())
    print(f"\n{sport}: {len(odds)} Odds rows")
    by = {}
    for o in odds:
        by[(o.market_type, o.period)] = by.get((o.market_type, o.period), 0) + 1
    for k, v in sorted(by.items()):
        print(f"  {k}: {v}")
    for o in odds[:6]:
        print(f"  {o.home} v {o.away} | {o.market_type} {o.period} line={o.line} "
              f"{o.selections} @ {o.start_time:%m-%d %H:%M}")
