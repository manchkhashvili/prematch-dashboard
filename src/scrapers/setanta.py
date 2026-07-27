"""Setanta (setanta.bet) prematch scraper — SignalR/MessagePack push feed.

Sixth book on the dashboard (CrystalBet, Pinnacle, Lider-Bet, Betlive,
Crocobet, Setanta). Setanta is the odd one out: **it has no REST odds
endpoint at all**. The sportsbook is a white-label "apg" SPA iframed from
`sport-iframe.ukyuku.xyz`, and every price rides a SignalR hub speaking the
MessagePack protocol:

  GET  /en/                              → iframe shell; carries apiKeySdk + brand
  GET  /api/v0/sport/feed/schema         → the wire schema (field order per model)
  wss  /direct-feed/feed?brand=&X-Api-Key=  → the hub; all odds

No auth, no cookie, no negotiate step (`skipNegotiation`), 0.08 s handshake.
Full protocol write-up + energy numbers: `docs/setanta.md`. Reproducible probe:
`scripts/probe_setanta.py`.

WHY numeric codes, not names (the "avoid confusion" rule): payloads are
positional arrays with no field names, and market/outcome identity is the
tuple `(sport, resultKind, marketType, period, marketParameters, outcomeType)`.
We map on those numbers, exactly like Crocobet's gameType, 1xbet's G/T and
Lider's typeId. Names exist only in a separate dictionary and are
condition-guarded — resolving them naively gives WRONG names (matching only
on sport labelled a football Total as "Total kicks", the penalty-shootout
variant), so we never classify on a string.

Verified 2026-07-26 against the live board + the book's own market dictionary
(`/apg/v0/sport/feed/localization/markets?lang=en`), reading each entry's
`condition` as `sport:resultKind:period:…`:

  soccer (F)     : mt 2  moneyline 3-way (0=1, 1=X, 3=2)
                   mt 5  total (4=Over, 5=Under), line = marketParameters[0]
                   mt 4  spread/handicap (86=home, 87=away), line = HOME, signed
                   mt 7  team total (37=Over, 38=Under), params [team, line]
  basketball (B) : mt 145 moneyline INCL OT, 2-way (0=1, 3=2)  ← the CB/Pinnacle
                   convention. mt 2 for basketball is "3-way betting (REGULAR
                   time)" — a different market; using it as the moneyline would
                   silently mis-pair against every other book.
                   mt 5 total, mt 4 spread, mt 7 team total (as soccer)
  tennis (T)     : mt 1  moneyline 2-way (0=1, 3=2)   ← NOT mt 2
                   mt 5  total (games), mt 4 spread (games)

PERIOD CODES ARE SPORT-SPECIFIC — the trap in this feed. `0` is FT everywhere,
but for basketball `1..4` are QUARTERS and the halves live at 4010/4011:
  soccer     : 0=FT, 1=H1, 2=H2(unsupported by the v1 Period model → dropped)
  basketball : 0=FT, 4010=H1, 1=Q1, 2=Q2, 3=Q3, 4=Q4
               (4011=H2, 4012=H2 incl OT → dropped, no H2 in the model)
  tennis     : 0=FT, 1..5=sets → only FT is emitted (no Set period in the model)
Mapping basketball period 1 to "H1" would pair a quarter against a half.

`resultKind` is the STATISTIC (1=Goals/Points, 4=Corners, 8=Yellow cards,
32=Penalties, …). Only resultKind 1 is emitted — corners/cards would need the
`submarket` model and a separate verification pass.

`subPeriod` is a MINUTE/GAME WINDOW inside the period, and it must be null for
a row to be usable: (period=1, subPeriod=15) is "first 15 minutes", not the
first half. Its 3-way prices look nothing alike (draw 1.22 vs 1.94), so
treating it as H1 generated 130 %+ phantom edges against Pinnacle's real H1
on the first end-to-end run.

NO SportRadar id: `hasBetradarMapping` was false on 383/383 events sampled
across 10 sports and no competitor carries `extraData`, so `sr_match_id` is
always None and Setanta joins on name+time like CrystalBet — unlike
Lider/Betlive/Crocobet, which hand us an SR id.

Energy: enumeration of the whole football board is ~0.3 s; full ladders for
the board are ~8 MB / 4-7 s. To keep the per-cycle cost (and the decode CPU)
in line with Crocobet's DETAIL_HOURS budget, we pull FULL ladders only for
games starting within SETANTA_DETAIL_HOURS and the curated main-market tier
for everything farther out.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import struct
import time
from datetime import datetime, timezone

from src.models import Odds
from src.normalize import is_simulated_league

log = logging.getLogger(__name__)

IFRAME = "https://sport-iframe.ukyuku.xyz"
IMPERSONATE = "chrome124"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
TIMEOUT = 25.0
PREMATCH = 1                     # stage enum: 0 Default, 1 Prematch, 2 Live
BATCH = 200                      # event ids per subscription (measured optimum)
# Full-ladder horizon, mirroring CROCOBET_DETAIL_HOURS: per-event ladders only
# for games starting within this many hours; farther games use the cheap
# main-market tier (whole board 415 KB / 0.3 s).
DETAIL_HOURS = float(os.environ.get("SETANTA_DETAIL_HOURS", "24"))
MAIN_PROFILE = "pro_main_period"

SPORT_CODE = {"soccer": "F", "basketball": "B", "tennis": "T"}

# (marketType, sport) → (market_type, n_way). period comes from _PERIOD.
_MARKET = {
    "F": {2: ("moneyline", 3), 5: ("total", 2), 4: ("spread", 2), 7: ("team_total", 2)},
    "B": {145: ("moneyline", 2), 5: ("total", 2), 4: ("spread", 2), 7: ("team_total", 2)},
    "T": {1: ("moneyline", 2), 5: ("total", 2), 4: ("spread", 2)},
}
# feed period code → v1 Period, per sport (see module docstring)
_PERIOD = {
    "F": {0: "FT", 1: "H1"},
    "B": {0: "FT", 4010: "H1", 1: "Q1", 2: "Q2", 3: "Q3", 4: "Q4"},
    "T": {0: "FT"},
}
# outcomeType → canonical selection key
_OUT_ML = {0: "home", 1: "draw", 3: "away"}
_OUT_TOTAL = {4: "over", 5: "under"}
_OUT_TEAM_TOTAL = {37: "over", 38: "under"}
_OUT_SPREAD = {86: "home", 87: "away"}
_TEAM_PARAM = {"1": "home", "2": "away"}


# ── minimal MessagePack + SignalR framing (no new dependency for one book) ────

def _dec(b, i):
    c = b[i]; i += 1
    if c <= 0x7F: return c, i
    if c >= 0xE0: return c - 0x100, i
    if 0x80 <= c <= 0x8F: return _dec_map(b, i, c & 0xF)
    if 0x90 <= c <= 0x9F: return _dec_arr(b, i, c & 0xF)
    if 0xA0 <= c <= 0xBF:
        n = c & 0x1F; return b[i:i + n].decode("utf-8", "replace"), i + n
    if c == 0xC0: return None, i
    if c == 0xC2: return False, i
    if c == 0xC3: return True, i
    if c == 0xCA: return struct.unpack_from(">f", b, i)[0], i + 4
    if c == 0xCB: return struct.unpack_from(">d", b, i)[0], i + 8
    if c == 0xCC: return b[i], i + 1
    if c == 0xCD: return struct.unpack_from(">H", b, i)[0], i + 2
    if c == 0xCE: return struct.unpack_from(">I", b, i)[0], i + 4
    if c == 0xCF: return struct.unpack_from(">Q", b, i)[0], i + 8
    if c == 0xD0: return struct.unpack_from(">b", b, i)[0], i + 1
    if c == 0xD1: return struct.unpack_from(">h", b, i)[0], i + 2
    if c == 0xD2: return struct.unpack_from(">i", b, i)[0], i + 4
    if c == 0xD3: return struct.unpack_from(">q", b, i)[0], i + 8
    if c == 0xD9:
        n = b[i]; return b[i + 1:i + 1 + n].decode("utf-8", "replace"), i + 1 + n
    if c == 0xDA:
        n = struct.unpack_from(">H", b, i)[0]
        return b[i + 2:i + 2 + n].decode("utf-8", "replace"), i + 2 + n
    if c == 0xDB:
        n = struct.unpack_from(">I", b, i)[0]
        return b[i + 4:i + 4 + n].decode("utf-8", "replace"), i + 4 + n
    if c == 0xDC: return _dec_arr(b, i + 2, struct.unpack_from(">H", b, i)[0])
    if c == 0xDD: return _dec_arr(b, i + 4, struct.unpack_from(">I", b, i)[0])
    if c == 0xDE: return _dec_map(b, i + 2, struct.unpack_from(">H", b, i)[0])
    if c == 0xDF: return _dec_map(b, i + 4, struct.unpack_from(">I", b, i)[0])
    raise ValueError(f"unsupported msgpack byte {c:#x}")


def _dec_arr(b, i, n):
    out = []
    for _ in range(n):
        v, i = _dec(b, i); out.append(v)
    return out, i


def _dec_map(b, i, n):
    out = {}
    for _ in range(n):
        k, i = _dec(b, i); v, i = _dec(b, i); out[k] = v
    return out, i


def _packb(o):
    if o is None: return b"\xc0"
    if o is True: return b"\xc3"
    if o is False: return b"\xc2"
    if isinstance(o, int):
        if 0 <= o <= 0x7F: return bytes([o])
        if -32 <= o < 0: return bytes([o + 0x100])
        if 0 <= o <= 0xFFFF: return b"\xcd" + struct.pack(">H", o)
        return b"\xce" + struct.pack(">I", o)
    if isinstance(o, str):
        e = o.encode()
        if len(e) < 32: return bytes([0xA0 | len(e)]) + e
        if len(e) < 256: return b"\xd9" + bytes([len(e)]) + e
        return b"\xda" + struct.pack(">H", len(e)) + e
    if isinstance(o, (list, tuple)):
        h = bytes([0x90 | len(o)]) if len(o) < 16 else b"\xdc" + struct.pack(">H", len(o))
        return h + b"".join(_packb(x) for x in o)
    if isinstance(o, dict):
        h = bytes([0x80 | len(o)]) if len(o) < 16 else b"\xde" + struct.pack(">H", len(o))
        return h + b"".join(_packb(k) + _packb(v) for k, v in o.items())
    raise TypeError(type(o))


def _frames(payload: bytes) -> list:
    """Split one SignalR binary payload (varint-prefixed msgpack messages)."""
    out, i = [], 0
    while i < len(payload):
        n, sh = 0, 0
        while True:
            c = payload[i]; i += 1
            n |= (c & 0x7F) << sh; sh += 7
            if not c & 0x80:
                break
        out.append(_dec(payload[i:i + n], 0)[0]); i += n
    return out


def _frame(msg) -> bytes:
    p = _packb(msg)
    n, out = len(p), bytearray()
    while True:
        c = n & 0x7F; n >>= 7
        out.append(c | 0x80 if n else c)
        if not n:
            return bytes(out) + p


# ── schema-driven decode ──────────────────────────────────────────────────────

class _Wire:
    """Turns positional arrays back into dicts using the book's own schema.

    The field order of an object spec never changes, but `dec` is called once
    per decoded object — ~390 k times for a whole-board sweep. Sorting the
    fields on every call cost ~25 % of this scraper's CPU, so the order is
    memoised per spec (keyed by id(), which is stable: the schema dicts are
    held for the lifetime of the _Wire).
    """

    def __init__(self, schemas: list):
        self.s = {x["name"]: x for x in schemas}
        self._order: dict[int, list] = {}
        self._schemas = schemas          # keep alive so id() stays unique

    def _fields(self, spec):
        key = id(spec)
        got = self._order.get(key)
        if got is None:
            got = [(n, f["type"]) for n, f in
                   sorted(spec.items(), key=lambda kv: kv[1]["index"])]
            self._order[key] = got
        return got

    def dec(self, spec, val):
        if val is None or spec == "raw" or isinstance(spec, str):
            return val
        if isinstance(spec, list):
            return [self.dec(spec[0], v) for v in val]
        if isinstance(spec, dict) and "index" in spec and "type" in spec:
            return self.dec(spec["type"], val)
        if isinstance(spec, dict):
            n_val = len(val)
            return {n: (self.dec(t, val[i]) if i < n_val else None)
                    for i, (n, t) in enumerate(self._fields(spec))}
        return val

    def rows(self, msg, model):
        batch = self.dec(self.s["batch"]["valueSchema"], msg[3])
        sch = self.s[model]
        out = []
        for raw in batch["data"]:
            fd = self.dec(self.s["feedData"]["valueSchema"], raw)
            key = fd["key"]
            if isinstance(sch.get("keySchema"), dict):
                key = self.dec(sch["keySchema"], key)
            out.append((key, self.dec(sch["valueSchema"], fd["value"])
                        if fd["value"] is not None else None))
        return batch["isInitialBatch"], out


# ── bootstrap (brand / api key / schema come off the wire, never hardcoded) ───

_BOOT: dict | None = None
_BOOT_AT: float = 0.0
_BOOT_TTL = 3600.0


def _bootstrap_sync() -> dict:
    """apiKeySdk + brand from the iframe shell, plus the wire schema."""
    global _BOOT, _BOOT_AT
    if _BOOT and time.time() - _BOOT_AT < _BOOT_TTL:
        return _BOOT
    from curl_cffi.requests import Session
    s = Session(impersonate=IMPERSONATE)
    html = s.get(f"{IFRAME}/en/", headers={"User-Agent": UA}, timeout=TIMEOUT).text
    key = re.search(r'"apiKeySdk":"([0-9a-f-]+)"', html)
    brand = re.search(r"/content/uploads/icons/([A-Z0-9]+)/", html)
    if not key or not brand:
        raise RuntimeError("setanta: could not read apiKeySdk/brand from iframe shell")
    # NB: the schema lives under /api/v0/…, NOT the /apg/v0/… prefix every other
    # feed call uses — /apg/v0/sport/feed/schema returns 400.
    schemas = s.get(f"{IFRAME}/api/v0/sport/feed/schema",
                    headers={"User-Agent": UA}, timeout=TIMEOUT).json()
    _BOOT = {"key": key.group(1), "brand": brand.group(1), "schemas": schemas}
    _BOOT_AT = time.time()
    log.info("setanta bootstrap: brand=%s, %d wire models", _BOOT["brand"], len(schemas))
    return _BOOT


# ── hub client ────────────────────────────────────────────────────────────────

class _Hub:
    def __init__(self, ws, wire: _Wire, ctx: list):
        self.ws, self.wire, self.ctx, self.n = ws, wire, ctx, 0

    async def sweep(self, invocations, model, timeout=90.0):
        """Fire every subscription, then read until each delivered its initial
        batch. Pipelining is the whole game here: awaiting one stream at a time
        takes the football enumeration from 0.3 s to 21.7 s."""
        want, t0 = set(), time.time()
        for target, args in invocations:
            iid = str(self.n); self.n += 1; want.add(iid)
            await self.ws.send(_frame([4, {}, iid, target, args]))
        rows, done = [], set()
        while done < want and time.time() - t0 < timeout:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), 15)
            except (asyncio.TimeoutError, TimeoutError):
                break
            if isinstance(raw, str):
                continue
            for m in _frames(raw):
                if not isinstance(m, list) or m[0] == 6 or str(m[2]) not in want:
                    continue
                if m[0] == 2:
                    init, r = self.wire.rows(m, model)
                    rows += r
                    if init:
                        done.add(str(m[2]))
                elif m[0] == 3:
                    done.add(str(m[2]))
        # CancelInvocation — a stream keeps pushing deltas until told to stop,
        # and leftovers steal decode time from the next sweep.
        for iid in want:
            try:
                await self.ws.send(_frame([5, {}, iid]))
            except Exception:
                break
        if done < want:
            log.warning("setanta: %d/%d subscriptions returned in time",
                        len(done), len(want))
        return rows


# ── parsing ───────────────────────────────────────────────────────────────────

def _line_of(params, market_type):
    """marketParameters → (line, team_side)."""
    if market_type == "team_total":
        if len(params) < 2:
            return None, None
        return params[1], _TEAM_PARAM.get(str(params[0]))
    return (params[0] if params else None), None


def _selections(market_type, n_way, outcomes):
    want = (_OUT_ML if market_type == "moneyline"
            else _OUT_TEAM_TOTAL if market_type == "team_total"
            else _OUT_TOTAL if market_type == "total" else _OUT_SPREAD)
    sel = {}
    for o in outcomes:
        if o.get("isRemoved") or o.get("isFrozen"):
            continue
        side = want.get((o.get("key") or {}).get("type"))
        odd = o.get("odd")
        if side and isinstance(odd, int) and odd > 100:   # odds are decimal ×100
            sel[side] = odd / 100.0
    if market_type == "moneyline":
        need = {"home", "draw", "away"} if n_way == 3 else {"home", "away"}
    elif market_type in ("total", "team_total"):
        need = {"over", "under"}
    else:
        need = {"home", "away"}
    return sel if need <= set(sel) else None


def _parse_markets(markets, events, sport, sport_code, fetched_at) -> list[Odds]:
    table = _MARKET[sport_code]
    periods = _PERIOD[sport_code]
    rows: list[Odds] = []
    for key, value in markets:
        if not value or not isinstance(key, dict):
            continue
        if key.get("resultKind") != 1:          # 1 = Goals/Points; corners etc. skipped
            continue
        # subPeriod is a MINUTE/GAME WINDOW inside the period, not a sub-market:
        # (period=1, subPeriod=15) is "first 15 minutes", which prices nothing
        # like a first half (draw ~1.22 vs ~1.94). Emitting it as H1 produced
        # 130%+ phantom edges against Pinnacle's real H1. Only the whole period
        # has a slot in the v1 Period model.
        if key.get("subPeriod"):
            continue
        hit = table.get(key.get("marketType"))
        if hit is None:
            continue
        period = periods.get(key.get("period"))
        if period is None:
            continue
        ev = events.get(key.get("eventId"))
        if ev is None:
            continue
        market_type, n_way = hit
        for item in value.get("marketItems") or []:
            if item.get("isRemoved"):
                continue
            sel = _selections(market_type, n_way, item.get("outcomes") or [])
            if not sel:
                continue
            line, team_side = _line_of((item.get("key") or {}).get("marketParameters") or [],
                                       market_type)
            if market_type != "moneyline" and line is None:
                continue
            if market_type == "team_total" and team_side is None:
                continue
            try:
                rows.append(Odds(
                    source="setanta", sport=sport,
                    home=ev["home"], away=ev["away"],
                    market_type=market_type, period=period, selections=sel,
                    fetched_at=fetched_at,
                    line=float(line) if line is not None else None,
                    start_time=ev["start_time"], league=ev["league"],
                    team_side=team_side, raw_event_id=key["eventId"],
                    sr_match_id=None))       # feed exposes no SportRadar id
            except (ValueError, TypeError) as e:
                log.debug("setanta Odds rejected: %s", e)
    return rows


def _event_meta(key, value, fetched_at) -> dict | None:
    """richEvent → the fields Odds needs, or None if unusable/not prematch."""
    if not value or value.get("stage") != PREMATCH:
        return None
    if value.get("tradingStatus") != 1:              # 1=Opened, 2=Suspended, 3=Removed
        return None
    comp = value.get("competitors") or []
    if len(comp) < 2:
        return None
    home = (comp[0].get("name") or "").strip()
    away = (comp[1].get("name") or "").strip()
    if not home or not away:
        return None
    start = value.get("startTime")
    league = value.get("tournamentName") or value.get("categoryName") or None
    return {"home": home, "away": away, "league": league,
            "start_time": (datetime.fromtimestamp(start, tz=timezone.utc)
                           if start else None)}


def _is_virtual(name: str | None) -> bool:
    """Simulated/virtual competitions — 16 % margins, no real-world counterpart."""
    if not name:
        return False
    low = name.lower()
    return ("virtual" in low or "esportsbattle" in low or "e-basketball" in low
            or is_simulated_league(name))


# ── fetch ─────────────────────────────────────────────────────────────────────

async def _fetch_sport(sport: str) -> list[Odds]:
    sport_code = SPORT_CODE.get(sport)
    if sport_code is None:
        log.warning("setanta: unknown sport %r", sport)
        return []
    import websockets

    boot = await asyncio.to_thread(_bootstrap_sync)
    fetched_at = datetime.now(tz=timezone.utc)
    url = (f"wss://sport-iframe.ukyuku.xyz/direct-feed/feed"
           f"?brand={boot['brand']}&X-Api-Key={boot['key']}")
    ctx = ["en", "MOBILE_WEB", boot["brand"], "", "GEL"]

    # ping_interval=None: decoding a whole-board initial batch starves the
    # event loop long enough that the client's own keepalive fails and tears
    # the connection down. The hub runs SignalR-level pings itself.
    async with websockets.connect(
            url, additional_headers={"Origin": IFRAME, "User-Agent": UA},
            max_size=None, ping_interval=None, open_timeout=TIMEOUT) as ws:
        await ws.send('{"protocol":"messagepack","version":1}\x1e')
        await asyncio.wait_for(ws.recv(), TIMEOUT)
        hub = _Hub(ws, _Wire(boot["schemas"]), ctx)

        tours = await hub.sweep(
            [("GetTournamentsBySport", [sport_code, PREMATCH, ctx])], "tournament")
        tids = [k for k, v in tours
                if v and (v.get("prematchEventsCount") or 0) > 0
                and not _is_virtual(v.get("name"))]
        if not tids:
            return []

        ev_rows = await hub.sweep(
            [("GetRichEventsByTournamentIdAndStage", [t, PREMATCH, ctx]) for t in tids],
            "richEvent")
        now = fetched_at.timestamp()
        events: dict[str, dict] = {}
        for key, value in ev_rows:
            meta = _event_meta(key, value, fetched_at)
            if meta is None or _is_virtual(meta["league"]):
                continue
            # prematch only — drop anything already started (120 s clock grace)
            if meta["start_time"] and meta["start_time"].timestamp() < now - 120:
                continue
            events[key] = meta
        if not events:
            return []

        cutoff = now + DETAIL_HOURS * 3600
        near, far = [], []
        for eid, m in events.items():
            st = m["start_time"]
            (near if st and st.timestamp() <= cutoff else far).append(eid)

        markets = []
        if near:
            markets += await hub.sweep(
                [("GetMarketsByEventIds", [near[i:i + BATCH], None, ctx])
                 for i in range(0, len(near), BATCH)], "market", timeout=120)
        if far:
            markets += await hub.sweep(
                [("GetMainMarketsByProfileAndEventIds",
                  [MAIN_PROFILE, far[i:i + BATCH], 3, 3, ctx])
                 for i in range(0, len(far), BATCH)], "market")

    rows = _parse_markets(markets, events, sport, sport_code, fetched_at)
    log.info("setanta %s: %d Odds rows (%d near events full ladder, %d far "
             "main-markets, %d tournaments)", sport, len(rows), len(near),
             len(far), len(tids))
    return rows


async def fetch_setanta(sport: str) -> list[Odds]:
    try:
        return await _fetch_sport(sport)
    except Exception as e:
        log.warning("setanta %s failed: %s: %s", sport, type(e).__name__, e)
        return []


async def fetch_setanta_soccer() -> list[Odds]:
    return await fetch_setanta("soccer")


async def fetch_setanta_basketball() -> list[Odds]:
    return await fetch_setanta("basketball")


async def fetch_setanta_tennis() -> list[Odds]:
    return await fetch_setanta("tennis")


if __name__ == "__main__":   # smoke: python -m src.scrapers.setanta [sport]
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sp = sys.argv[1] if len(sys.argv) > 1 else "soccer"
    t0 = time.time()
    odds = asyncio.run(fetch_setanta(sp))
    print(f"\n{sp}: {len(odds)} Odds rows in {time.time()-t0:.1f}s")
    by: dict = {}
    for o in odds:
        by[(o.market_type, o.period)] = by.get((o.market_type, o.period), 0) + 1
    for k, v in sorted(by.items(), key=lambda kv: str(kv[0])):
        print(f"  {k}: {v}")
    for o in odds[:5]:
        print(f"  {o.home} v {o.away} | {o.market_type} {o.period} line={o.line} "
              f"{o.selections}")
