"""
Lider-Bet whole-board collector — every sport, every league, every position.

Same protocol as `src/scrapers/liderbet.py` (menu -> matchData -> matchData/
details, plain idempotent GETs over curl_cffi; see docs/liderbet.md), with two
deliberate differences.

NO ALLOWLIST
------------
The dashboard scraper reads eight typeIds. It has to: it feeds a matcher that
prices markets against other books, and reading `mt:16:1079` "Handicap  ①"
(3-way) as `mt:16:501` "Handicap ①" (2-way) invents a spread nobody quoted —
that trap is documented at length in the scraper it belongs to. Here nothing is
interpreted. A market is stored under its own typeId and its own outcome
labels, which are the feed's own dictionary and therefore cannot be confused
with each other: the two Handicaps differ by typeId even though their names
differ only by a double space.

That is what makes this collector safe without the allowlist, and it is also
why the typeId — not the name — is the market key.

EVERY SPORT, NOT FOUR
---------------------
The dashboard maps four sections. The menu publishes 25 (measured 2026-08-26:
soccer 1,595 matches, table tennis 431, tennis 231, esports 103, ice hockey 75,
and a long tail down to a single chess match). Sections are enumerated from the
menu rather than hardcoded, so a sport CrystalBet or Lider adds next month is
collected without a code change.

LINES LIVE ON THE POSITION, NOT IN THE NAME
-------------------------------------------
Lider gives the line as a structured `specifier` rather than baking it into the
label, so it is stored in its own column and the market name keeps the book's
template ("Total {total}"). Handicaps carry the specifier on the OUTCOME, not
the market — the two sides of an Asian line are +0.5 and -0.5 of the same
market — so the outcome's specifier wins where it exists.

Full board cost, measured 2026-08-26: 143 detail calls of 20 matches each,
~0.6 s and ~5.7 MB per call, so about 90 seconds and 820 MB of transport for
2,857 matches carrying ~1,700 priced outcomes each on soccer.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.normalize import is_simulated_league, transliterate   # noqa: E402
from sportsdatamovement.common import slug                     # noqa: E402
from sportsdatamovement.players import is_player_market        # noqa: E402

log = logging.getLogger(__name__)

BOOK = "liderbet"

BASE = "https://sports.lider-bet.com/services"
MENU_URL = f"{BASE}/pre/m1/api/sport/menu"
MATCHDATA_URL = f"{BASE}/pre/m4/api/sport/matchData"
IMPERSONATE = "chrome124"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en",
    "Referer": "https://sports.lider-bet.com/",
    "Origin": "https://sports.lider-bet.com",
}

_TOURS_PER_CALL = 40
_MATCHES_PER_DETAIL = 20
_HTTP_TIMEOUT = 30.0

# Circled/superscript variant glyphs Lider appends to market names
# ("Handicap ①"). Stripped for readability; the typeId already distinguishes
# the variants, so nothing is lost.
_DECOR = re.compile(r"[①-⓿⁰-₟⅐-↏]")
_WS = re.compile(r"\s+")


def _clean(name: str) -> str:
    return _WS.sub(" ", _DECOR.sub("", name or "")).strip()


def _to_float(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


_LINE_KEYS = ("total", "handicap", "hcp", "special")

# A specifier entry whose KEY is an opaque id is a lookup table, not a
# discriminator. Lider's player-prop markets ship the whole squad this way:
#
#   "Player {count}+ shots (SuperSub)"
#     {"sr:player:1243608": "Hatate, Reo", ... 46 of them ...,
#      "count": "4", "special": "sr:player:918650"}
#
# Only `count` and `special` say which bet this is; the other 46 entries are
# there so the id in `special` can be turned into a name. Treating them as part
# of the identity puts ~1.5 KB of roster into every market key, and — worse —
# means one substitution or transfer re-keys every position on the event.
_DICT_KEY = re.compile(r"^(sr|lb):", re.I)


def split_specifier(specifier) -> tuple[Optional[float], str]:
    """A Lider specifier -> (numeric line, canonical rest).

    The obvious reading — "pull the number out of `total`/`hcp` and ignore the
    rest" — merges positions that are not the same bet, and the merge is
    invisible because both halves are real prices. Measured 2026-08-26 on a
    first-ever pass, which by definition cannot contain a price move: 1,856 of
    5,571 positions were reported as having MOVED. They were collisions.

    Two shapes cause it:

        mt:16:1102  European Handicap   {"special": "(0:1)", "hcp": "0:1"}
        mt:16:2839  Total Goals (agg.)  {"variant": "sr:point_range:6+"}

    `hcp` is the string "0:1", not a number, so float() fails and all four
    rungs of the market land on line=None under one key — and the last one
    written wins, at a price belonging to a different handicap.

    So: whatever is numeric becomes the line, and EVERYTHING else is kept as a
    discriminator. A specifier key nobody has seen before ends up in the rest
    and keeps its positions apart, which is the safe direction to fail.
    """
    if not isinstance(specifier, dict) or not specifier:
        return None, ""
    line: Optional[float] = None
    used: Optional[str] = None
    for key in _LINE_KEYS:
        if key in specifier:
            v = _to_float(specifier[key])
            if v is not None:
                line, used = v, key
                break
    rest = {k: v for k, v in specifier.items()
            if k != used and not _DICT_KEY.match(k)}
    canon = ";".join(f"{k}={rest[k]}" for k in sorted(rest))
    return line, canon


def _spec_line(specifier) -> Optional[float]:
    return split_specifier(specifier)[0]


def _line_key(specifier) -> Optional[str]:
    """Which specifier key `split_specifier` turned into the numeric line."""
    if not isinstance(specifier, dict):
        return None
    for key in _LINE_KEYS:
        if key in specifier and _to_float(specifier[key]) is not None:
            return key
    return None


_PLACEHOLDER = re.compile(r"\{[!#]?([A-Za-z_][A-Za-z0-9_]*)\}")
# Values that identify a market to Lider's own systems but say nothing to a
# reader: "sr:player:2735979", "sr:point_range:6+".
_OPAQUE = re.compile(r"^(sr|lb):", re.I)


def _readable(value: str) -> bool:
    """Is this specifier value worth showing to a human?"""
    v = value.strip()
    if not v or _OPAQUE.match(v):
        return False
    # Bare numbers are ordinals ("goalnr": "1") or the line, both of which are
    # already visible elsewhere.
    return not v.replace(".", "").replace("-", "").replace(":", "").isdigit()


def fill_name(name: str, specifier, *, skip: Optional[str] = None) -> str:
    """Make a market name identify the bet a reader is looking at.

    Lider ships names as templates — "1X2 / Anytime goalscorer {lb_br_player}",
    "{#setnr} set - Winner" — with the values in the specifier alongside the
    line:

        {"special": "sr:player:2735979", "lb_br_player": "Donovan, Colby",
         "goalnr": "1", "player": "Donovan, Colby"}

    Two things go wrong if that is left alone. Templates display literally, so
    every goalscorer market on an event is the same string and eight players'
    prices cannot be told apart by eye. And some names have no slot at all —
    "1st goalscorer" is the whole title, with the player only in the specifier —
    so substitution alone is not enough. Anything readable the template had no
    room for is appended in brackets.

    `skip` is the key that became `positions.line`, and is deliberately left
    alone in both halves: "Total {total}" must stay a template so `markets`
    holds one row per market rather than one per rung.

    None of this can grow the `markets` table. Every value used here is already
    part of the market key (that is what `split_specifier` put there), so the
    name is a function of the key rather than a new dimension.
    """
    if not isinstance(specifier, dict) or not specifier:
        return name

    def resolve(value) -> str:
        """An opaque id that the specifier itself can translate — the roster
        entries exist for exactly this."""
        v = str(value).strip()
        if _OPAQUE.match(v) and v in specifier:
            return str(specifier[v]).strip()
        return v

    def rep(m: "re.Match") -> str:
        key = m.group(1)
        if key == skip or key not in specifier:
            return m.group(0)
        return resolve(specifier[key])

    out = _WS.sub(" ", _PLACEHOLDER.sub(rep, name)).strip()
    extra = []
    for key in sorted(specifier):
        if key == skip or _DICT_KEY.match(key):
            continue
        value = resolve(specifier[key])
        if _readable(value) and value not in out:
            extra.append(value)
    if extra:
        out = f"{out} [{'; '.join(dict.fromkeys(extra))}]"
    return out


def _odds(value) -> Optional[float]:
    """Decimal odds, or None when the book is not taking it.

    Lider zeroes or floors a suspended outcome rather than dropping it, so
    <= 1.0 means unpriced — the same "not bettable at this instant" that
    CrystalBet writes as a locked cell.
    """
    v = _to_float(value)
    return v if v is not None and v > 1.0 else None


def _start_time(m: dict) -> Optional[datetime]:
    st = m.get("startTime")
    if not st:
        return None
    try:
        return datetime.fromisoformat(st).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


# ── menu ──────────────────────────────────────────────────────────────────────

def _menu_nodes(menu: dict) -> list[dict]:
    return [it for v in menu.values() if isinstance(v, list)
            for it in v if isinstance(it, dict)]


def read_menu(session, *, skip_simulated: bool = True) -> dict[str, dict]:
    """-> {section_id: {"name", "slug", "tours": [...], "matches": int}}.

    Simulated / "Simulated Reality League" shelves are dropped by default. They
    are algorithmic markets that reprice every few minutes by construction, so
    including them would swamp the movement log with the one kind of movement
    that carries no information. The tell is often the PARENT category rather
    than the tournament — the simulated "World Cup" sits under "Simulated
    Reality League" while the real World Cup does not — so both are checked.
    """
    menu = session.get(f"{MENU_URL}?lang=en&marketFilter=true",
                       headers=HEADERS, timeout=_HTTP_TIMEOUT).json()["menu"]
    nodes = _menu_nodes(menu)
    name_by_id = {n["id"]: (n.get("name") or "") for n in nodes if n.get("id")}
    parent_of: dict[str, str] = {}
    for parent_id, children in menu.items():
        if isinstance(children, list):
            for n in children:
                if isinstance(n, dict) and n.get("id"):
                    parent_of[n["id"]] = parent_id

    sections: dict[str, dict] = {}
    for n in nodes:
        nid = n.get("id", "")
        if nid.startswith("s:"):
            nm = n.get("name") or nid
            sections.setdefault(nid, {"name": nm, "slug": slug(nm),
                                      "tours": [], "matches": 0})
    for n in nodes:
        nid = n.get("id", "")
        if not nid.startswith("t:") or n.get("cnt", 0) <= 0:
            continue
        sec = n.get("sectionId")
        if sec not in sections:
            continue
        if skip_simulated and (
                is_simulated_league(n.get("name"))
                or is_simulated_league(name_by_id.get(parent_of.get(nid, "")))):
            continue
        sections[sec]["tours"].append(nid)
        sections[sec]["matches"] += n.get("cnt", 0)
    return {k: v for k, v in sections.items() if v["tours"]}


# ── parse ─────────────────────────────────────────────────────────────────────

def parse_match(m: dict, ancestors: dict, market_types: dict, *,
                drop_players: bool = True) -> dict:
    """One match -> {"event": {...}, "rows": [(market_key, name, side, line,
    odds, ref)]}.

    Every market and every outcome, save for player props when `drop_players`
    (a third of Lider's soccer board — see players.py).
    """
    home = _clean(transliterate(
        (ancestors.get(m.get("homeId"), {}) or {}).get("name") or ""))
    away = _clean(transliterate(
        (ancestors.get(m.get("awayId"), {}) or {}).get("name") or ""))
    league = _clean(transliterate(
        (ancestors.get(m.get("tourId"), {}) or {}).get("name") or "")) or None

    rows: list[tuple] = []
    for mk in (m.get("markets") or {}).values():
        type_id = mk.get("typeId")
        if not type_id:
            continue
        tp = market_types.get(type_id, {})
        title = _clean(tp.get("name", "")) or str(type_id)
        # The feed ships its own outcome dictionary per market type, so the
        # labels are authoritative even where the market NAME is ambiguous.
        labels = {ot["id"]: _clean(ot.get("name") or "")
                  for ot in tp.get("outcomeTypes", []) if ot.get("id")}
        # Anything in the market specifier that is not the line distinguishes
        # this market from its siblings under the same typeId — see
        # split_specifier.
        spec = mk.get("specifier")
        # Decided on the RAW title and specifier, before the name is filled in:
        # substitution puts the player's name into the title, so testing
        # afterwards would work for the wrong reason and would keep working if
        # the structural tell ever disappeared.
        if drop_players and is_player_market(market=title, specifier=spec):
            continue
        market_line, market_rest = split_specifier(spec)
        market_key = f"{type_id}|{market_rest}" if market_rest else str(type_id)
        title = fill_name(title, spec, skip=_line_key(spec))
        for oid, oc in (mk.get("outcomes") or {}).items():
            if not isinstance(oc, dict):
                continue
            label = labels.get(oid) or str(oid)
            # Asian sides carry their own specifier: the two halves of one
            # handicap market are +0.5 and -0.5, not one line.
            line, out_rest = split_specifier(oc.get("specifier"))
            if line is None:
                line = market_line
            side = f"{label}|{out_rest}" if out_rest else label
            rows.append((market_key, title, side, line,
                         _odds(oc.get("value")), str(oid)))

    return {
        "event": {
            "event_key": str(m.get("id") or ""),
            "home": home, "away": away, "league": league,
            "start_time": _start_time(m),
        },
        "rows": rows,
    }


# ── collection ────────────────────────────────────────────────────────────────

def collect_section_sync(writer, session, section: dict, *,
                         max_start_days: float = 0.0,
                         max_matches: int = 0,
                         drop_players: bool = True) -> dict:
    """Pull one sport's whole board into `writer`. Runs in a worker thread."""
    t0 = time.monotonic()
    cut = (datetime.now(timezone.utc) + timedelta(days=max_start_days)
           if max_start_days > 0 else None)

    tours = section["tours"]
    match_ids: list[str] = []
    listed: list[tuple] = []          # (event_key, league, home, away, start)
    ancestors: dict = {}
    n_bytes = 0

    for i in range(0, len(tours), _TOURS_PER_CALL):
        chunk = ",".join(tours[i:i + _TOURS_PER_CALL])
        try:
            r = session.get(
                f"{MATCHDATA_URL}?tourIds={chunk}&lang=en&marketFilter=true",
                headers=HEADERS, timeout=_HTTP_TIMEOUT)
            n_bytes += len(r.content)
            data = r.json()["data"]
        except Exception as exc:
            log.warning("lider %s: matchData chunk failed: %s",
                        section["slug"], exc)
            continue
        ancestors.update(data.get("ancestors", {}))
        for m in (data.get("matches") or {}).values():
            if not m.get("id"):
                continue
            listed.append((str(m["id"]), _start_time(m)))
            st = _start_time(m)
            if cut is not None and st is not None and st > cut:
                continue
            match_ids.append(m["id"])

    # Register every match the list carried, including the ones this pass will
    # not pull detail for. An event that is listed but unread must be left
    # alone; without this a truncated pass reads as "the rest of the board has
    # left the book" and nulls it — 2,547,857 positions in one measured case.
    for event_key, st in listed:
        writer.add_event(
            event_key,
            start_time=st.isoformat(timespec="seconds") if st else None)

    if max_matches > 0:
        match_ids = match_ids[:max_matches]

    n_rows = n_matches = 0
    for i in range(0, len(match_ids), _MATCHES_PER_DETAIL):
        batch = match_ids[i:i + _MATCHES_PER_DETAIL]
        ids = ",".join(b if str(b).startswith("pr:m:") else f"pr:m:{b}"
                       for b in batch)
        try:
            r = session.get(f"{MATCHDATA_URL}/details?matchIds={ids}&lang=en",
                            headers=HEADERS, timeout=_HTTP_TIMEOUT * 2)
            n_bytes += len(r.content)
            data = r.json()["data"]
        except Exception as exc:
            log.warning("lider %s: details batch failed: %s",
                        section["slug"], exc)
            continue
        anc = {**ancestors, **(data.get("ancestors") or {})}
        mts = data.get("marketTypes", {})
        matches = data.get("matches") or {}
        for mid in batch:
            m = matches.get(mid) or matches.get(str(mid))
            if not m:
                continue
            parsed = parse_match(m, anc, mts, drop_players=drop_players)
            ev = parsed["event"]
            if not ev["event_key"]:
                continue
            n_matches += 1
            # The details payload is this match's whole market list in one
            # response, so having parsed it we know what is on the board.
            writer.mark_read(ev["event_key"])
            writer.add_event(
                ev["event_key"], league=ev["league"], home=ev["home"],
                away=ev["away"],
                start_time=ev["start_time"].isoformat(timespec="seconds")
                if ev["start_time"] else None)
            for mkey, title, label, line, odds, ref in parsed["rows"]:
                n_rows += 1
                writer.add(ev["event_key"], mkey, title, label, odds,
                           line=line, ref=ref)

    writer.count_bytes(n_bytes)
    return {"matches": n_matches, "listed": len(match_ids), "cells": n_rows,
            "sec": round(time.monotonic() - t0, 1)}


async def collect(store, *, skip_simulated: bool = True,
                  max_start_days: float = 0.0, max_matches: int = 0,
                  drop_players: bool = True,
                  sections: Optional[list[str]] = None) -> list[dict]:
    """One full Lider-Bet pass. Each sport is its own snapshot row."""
    from curl_cffi.requests import Session

    session = Session(impersonate=IMPERSONATE)
    try:
        found = await asyncio.to_thread(read_menu, session,
                                        skip_simulated=skip_simulated)
        log.info("lider: %d sports with games: %s", len(found),
                 ", ".join(f"{v['slug']}({v['matches']})" for v in
                           sorted(found.values(), key=lambda s: -s["matches"])[:12]))
        results: list[dict] = []
        for sec_id, sec in sorted(found.items(),
                                  key=lambda kv: -kv[1]["matches"]):
            if sections and sec_id not in sections:
                continue
            try:
                with store.snapshot(BOOK, sec["slug"]) as w:
                    stats = await asyncio.to_thread(
                        collect_section_sync, w, session, sec,
                        max_start_days=max_start_days, max_matches=max_matches,
                        drop_players=drop_players)
                    out = w.commit()
                out.update(stats)
                out["sport"] = sec["slug"]
                results.append(out)
                log.info("lider %s: %s", sec["slug"], out)
            except Exception as exc:
                log.error("lider %s failed: %s", sec["slug"], exc)
                results.append({"sport": sec["slug"], "error": str(exc)})
        return results
    finally:
        session.close()
