"""
CrystalBet whole-board collector — every sport, every league, every position.

Transport is the dashboard's proven browser-free session (`src/scrapers/
cb_http.py`); this module adds no protocol of its own. What it does add is a
parse that keeps everything, where the dashboard's parse keeps a classified
handful.

WHY A SEPARATE PARSER
---------------------
`src/scrapers/cb_detail.py` walks the same table but routes each market title
through a classifier that returns None for anything it does not recognise. Of
the ~525 market titles CrystalBet publishes per soccer game it emits maybe a
dozen. That is the right call for a pricing dashboard and the wrong one here:
the question this project exists to answer is whether a market moved when its
neighbours did, and a classifier decides in advance which neighbours are worth
having. So this parser keeps the book's own title and the book's own selection
label, unclassified, and stores them verbatim.

It is also ~15x faster, which matters at 4,242 games an hour. Measured
2026-08-26 on a 1.7 MB expand: BeautifulSoup 200-280 ms, this 17-20 ms. The
speedup is not cleverness — it is scoping the work to the one game's fragment
(a string slice) instead of building a DOM for the whole re-rendered panel.

THE MARKUP, AND THE TWO THINGS THAT LOOK OBVIOUS AND ARE WRONG
--------------------------------------------------------------
Each market is its own row:

    <tr id=3485587>
      <td class='sport_more_td1'>Exact number of goals <span ...></span></td>
      <td class='sport_more_td2'>
        <div id='S45217357950' class='sport_more_bt DetailSnatch' onclick='AS(...)'>
          <div class='sport_more_bt1'>0</div>
          <div class='sport_more_bt2'>9.90</div>
        </div>
        ...

The `<tr>` tags are not closed, so a DOM-shaped walk over `td1/td2` then
`td3/td4` silently drops half the board — the whitespace between one row's
closing `</td>` and the next row's opening `<td>` contains an unclosed `<tr>`.
Splitting on `<tr id=` instead is both correct and cheaper. (Measured: the
paired-td regex found 263 of 526 markets before this was understood.)

1. `tr id` is NOT a market key. 526 rows on one game carried 304 distinct ids —
   the three "1st Half - Corner Handicap" rungs at -2.5/-1.5/-0.5 share one,
   and so do player-prop families. The TITLE distinguishes them, and the title
   already carries the line ("hcp=-2.5"), so the title is the market name and
   `tr id` is stored only as a breadcrumb.

2. `div id='S...'` is NOT a position key either, even though it is stable —
   4,615 of 4,615 ids survived a 20-minute gap. 30 of them had a different LINE
   hanging off them afterwards ("21.5 Und" became "22.5 Und"). Keying on the id
   would book a line slide as a price move on a line the book no longer offers.
   The label is the key; the id goes in `positions.ref`.

Locked selections (`class='... EmptyDetailSnatch Snatch_Locked'`) render the
literal odds **1.01**. They are recorded as unpriced, not as 1.01 — see
store.py.

The line is deliberately left NULL for CrystalBet. The book writes it inside the
label ("Und 2.5", "hc1(-9.50)", "1 (2:0)") and parsing it out would be the first
step of exactly the normalisation this project is trying not to do. Two labels
differing by their line are two positions, which is the truth.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

# The collector lives inside the prematch tree and reuses its transport.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.scrapers import cb_http                      # noqa: E402
from sportsdatamovement.players import is_player_market  # noqa: E402

log = logging.getLogger(__name__)

BOOK = "crystalbet"

# CrystalBet ships its sport menu in Georgian. Discovered 2026-08-26 by
# scraping DoSportTypePostBack off the sports page; the ids are stable, the
# names are ours. An id that is not here still gets collected, under
# "sport_<id>" — an unnamed sport is a naming gap, not a reason to skip a board.
SPORT_NAMES: dict[int, str] = {
    16: "soccer", 17: "basketball", 18: "icehockey", 20: "handball",
    21: "volleyball", 22: "tennis", 23: "rugby", 24: "baseball",
    25: "fieldhockey", 26: "futsal", 27: "americanfootball", 28: "boxing",
    29: "waterpolo", 31: "aussierules", 32: "billiards", 33: "tabletennis",
    34: "formula1", 39: "darts", 61: "cricket", 62: "chess", 69: "mma",
    93: "cycling", 104: "gaelicfootball", 105: "hurling", 112: "basketball3x3",
    134: "esoccer", 190: "lacrosse", 227: "motorball", 228: "motorsport",
    235: "golf", 236: "entertainment", 239: "wintersports",
}

_HTTP_TIMEOUT = 60

# ── list-view scanning ────────────────────────────────────────────────────────
_RE_DATE = re.compile(r"x_loop_date'>.*?class='teams'>\s*-\s*(\d{2}/\d{2}/\d{4})", re.S)
_RE_GAME = re.compile(r"class='GContainerList[^']*'\s*data-id='(\d+)'")
_RE_TIME = re.compile(r"class='time'>(\d{2}:\d{2})<")
_RE_TEAMS = re.compile(r"class='teams_name'>(.*?)</div>", re.S)
_RE_LEAGUE = re.compile(r"class='game_hint'><label>(.*?)</label>", re.S)

# ── detail-table scanning ─────────────────────────────────────────────────────
_RE_ROW = re.compile(r"<tr id=(\d+)>")
_RE_TITLE = re.compile(r"sport_more_td[13]'>(.*?)</td>", re.S)
_RE_CELL = re.compile(
    r"<div id='S(\d+)'[^>]*class='([^']*)'[^>]*>\s*"
    r"<div class='sport_more_bt1'>(.*?)</div>\s*"
    r"<div class='sport_more_bt2'>(.*?)</div>", re.S)

_RE_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

TBILISI_UTC_OFFSET = timedelta(hours=4)


def _text(s: str) -> str:
    """Markup fragment -> its visible text, whitespace-collapsed."""
    return _WS.sub(" ", _RE_TAG.sub(" ", s)).strip()


def _odds(raw: str, css_class: str) -> Optional[float]:
    """Cell -> decimal odds, or None when the book is not taking the bet.

    A locked cell displays 1.01; see the module docstring. The `<= 1.0` guard
    below is belt-and-braces for anything else the book renders as unbettable.
    """
    if "Snatch_Locked" in css_class or "EmptyDetailSnatch" in css_class:
        return None
    try:
        v = float(_text(raw))
    except ValueError:
        return None
    return v if v > 1.0 else None


# ── list ──────────────────────────────────────────────────────────────────────

def parse_list(html: str) -> list[dict]:
    """Every game on a sport's list panel, in document order.

    CrystalBet groups games under a shared date header, so the date is carried
    forward across containers rather than read per game.
    """
    marks: list[tuple[int, str, str]] = []
    for m in _RE_DATE.finditer(html):
        marks.append((m.start(), "date", m.group(1)))
    for m in _RE_GAME.finditer(html):
        marks.append((m.start(), "game", m.group(1)))
    marks.sort()

    out: list[dict] = []
    date_str: Optional[str] = None
    for i, (pos, kind, value) in enumerate(marks):
        if kind == "date":
            date_str = value
            continue
        # The container runs until the next mark of any kind; that window holds
        # this game's time, teams and league and nothing else's.
        end = marks[i + 1][0] if i + 1 < len(marks) else len(html)
        block = html[pos:end]
        teams = _RE_TEAMS.search(block)
        if not teams:
            continue
        name = _text(teams.group(1))
        home, _, away = name.partition(" - ")
        league_m = _RE_LEAGUE.search(block)
        time_m = _RE_TIME.search(block)
        start_time = None
        if date_str and time_m:
            try:
                naive = datetime.strptime(f"{date_str} {time_m.group(1)}",
                                          "%d/%m/%Y %H:%M")
                start_time = (naive - TBILISI_UTC_OFFSET).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        out.append({
            "event_key": value,
            "home": home.strip() or name,
            "away": away.strip(),
            "league": _text(league_m.group(1)) if league_m else None,
            "start_time": start_time,
        })
    return out


# ── detail ────────────────────────────────────────────────────────────────────

def slice_detail(html: str, event_key: str) -> str:
    """The one game's `game-details` markup, or "" if it was not expanded.

    An expand re-renders the whole panel, and a previously-expanded game can
    still be in it, so the search is bounded to this game's own container. Left
    unbounded, a failed expand would silently harvest the NEXT game's markets
    and file them under this one.
    """
    i = html.find(f"data-id='{event_key}'")
    if i < 0:
        return ""
    nxt = html.find("GContainerList", i + 10)
    end = nxt if nxt > 0 else len(html)
    j = html.find("game-details", i)
    if j < 0 or j > end:
        return ""
    return html[j:end]


def parse_detail(frag: str, *, drop_players: bool = True
                 ) -> Iterator[tuple[str, str, str, str, Optional[float]]]:
    """-> (market_key, market_title, side_label, selection_ref, odds) per cell.

    A market's cells are laid out in visual GROUPS separated by
    `<div class='clear'>`, and the group is sometimes the only thing telling
    two bets apart. Measured 2026-08-26 on one soccer game, 20 cells per game
    collide without it:

        "Exact number of goals"  0..5+ three times over — total, then each
                                 team's, with identical labels;
        "Halftime/Fulltime and Total"  four groups, one per total line, and
                                 the line is missing from some labels
                                 ("2/X & Over" appears four times).

    So the group ordinal joins the market key, and the title gets a "#2"
    suffix when it is not the first group — without it the study would compare
    a team's exact-goals ladder against the match's and call the difference
    movement.
    """
    parts = _RE_ROW.split(frag)
    seen_titles: dict[str, int] = {}
    # split() gives [pre, id, body, id, body, ...]
    for n in range(1, len(parts) - 1, 2):
        body = parts[n + 1]
        t = _RE_TITLE.search(body)
        if not t:
            continue
        title = _text(t.group(1))
        if not title:
            continue
        # A title repeated across two <tr> rows of one game is rare (one per
        # game, measured) but has to stay separable.
        occurrence = seen_titles.get(title, 0)
        seen_titles[title] = occurrence + 1
        base = title if occurrence == 0 else f"{title} ({occurrence + 1})"
        # Walk cells and separators in document order so each cell knows which
        # group it fell in.
        group = 0
        pos = 0
        for m in _RE_CELL.finditer(body):
            group += body.count("<div class='clear'", pos, m.start())
            pos = m.start()
            sel_id, css, label, price = m.groups()
            lab = _text(label)
            if not lab:
                continue
            name = base if group == 0 else f"{base} #{group + 1}"
            if drop_players and is_player_market(market=name, side=lab):
                continue
            # The NAME is the market key. The `<tr id>` is tempting and wrong:
            # it is allocated per game, so keying on it turns `markets` from an
            # intern table of a few hundred titles into one row per
            # (game, market) — 56,460 rows and 16 % of the database after a
            # single pass, growing without bound as fixtures churn. It also
            # makes "how does Halftime/Fulltime behave across all games" a
            # question the schema cannot answer.
            yield (name, name, lab, sel_id, _odds(price, css))


# ── sport discovery ───────────────────────────────────────────────────────────

def discover_sports() -> list[tuple[int, str, int]]:
    """-> [(sport_id, name, game_count)] for every sport on the board.

    Uses its own throwaway session: a plain GET on Sports.aspx resets the
    server-side sport selection, which would leave a warmed collection session
    returning an empty list panel.
    """
    from curl_cffi.requests import Session
    s = Session(impersonate=cb_http.IMPERSONATE)
    try:
        html = s.get(cb_http.SPORTS_URL, headers=cb_http.HEADERS,
                     timeout=_HTTP_TIMEOUT).text
    finally:
        s.close()
    pat = re.compile(
        r"DoSportTypePostBack\((-?\d+)\).*?sport_menu_new_count'>([^<]*)<", re.S)
    out: list[tuple[int, str, int]] = []
    seen: set[int] = set()
    for m in pat.finditer(html):
        sid = int(m.group(1))
        if sid <= 0 or sid in seen:      # negative ids are TOP / LIVESTREAM tabs
            continue
        seen.add(sid)
        try:
            cnt = int(m.group(2).strip())
        except ValueError:
            cnt = 0
        if cnt <= 0:
            continue
        out.append((sid, SPORT_NAMES.get(sid, f"sport_{sid}"), cnt))
    return sorted(out, key=lambda r: -r[2])


# ── collection ────────────────────────────────────────────────────────────────

async def _list_with_retry(sport_id: int, attempts: int = 3) -> str:
    """Fetch the list panel, re-warming from scratch on a bounced session.

    CrystalBet answers a postback on a session it has dropped with a
    `pageRedirect` delta. Measured 2026-08-26 warming 33 sports in one pass at
    concurrency 4, seven of them were bounced on the first attempt — the
    dashboard never sees this because it warms four sessions and keeps them for
    an hour. A bounce is transient, so the fix is to throw the session away and
    warm a new one rather than lose the sport for the whole pass.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return await cb_http.fetch_list_raw(sport_id)
        except Exception as exc:
            last = exc
            cb_http.reset_session(sport_id)
            if attempt + 1 < attempts:
                delay = 2.0 * (attempt + 1)
                log.warning("cb sport=%d list attempt %d/%d failed (%s); "
                            "re-warming in %.0fs", sport_id, attempt + 1,
                            attempts, exc, delay)
                await asyncio.sleep(delay)
    raise last if last else RuntimeError("list failed")


async def collect_sport(writer, sport_id: int, *, max_start_days: float = 0.0,
                        max_games: int = 0, drop_players: bool = True,
                        progress_every: int = 200) -> dict:
    """Expand every game of one sport into `writer`.

    Expands run sequentially on the sport's single session: CrystalBet
    serialises concurrent postbacks within an ASP.NET session anyway
    (docs/performance.md), so parallelism has to come from running several
    SPORTS at once, which the caller does.
    """
    t0 = time.monotonic()
    raw = await _list_with_retry(sport_id)
    writer.count_bytes(len(raw))
    listed = parse_list(raw)

    # EVERY game the list showed is registered, including the ones this pass is
    # not going to expand. That is what stops a truncated pass from concluding
    # the rest of the board has left the book: an event that is listed but
    # unread is left exactly as it was.
    #
    # Getting this wrong is not a small error. Measured 2026-08-27, a
    # `--max-events 2` smoke run over 57 boards nulled 2,547,857 positions in
    # one pass, because the 2-event cap was applied before the events were
    # registered and the pass still reported a complete list read.
    for g in listed:
        writer.add_event(
            g["event_key"], league=g["league"], home=g["home"], away=g["away"],
            start_time=g["start_time"].isoformat(timespec="seconds")
            if g["start_time"] else None)

    games = listed
    if max_start_days > 0:
        cut = datetime.now(timezone.utc) + timedelta(days=max_start_days)
        games = [g for g in games
                 if g["start_time"] is None or g["start_time"] <= cut]
    if max_games > 0:
        games = games[:max_games]

    n_expanded = n_failed = n_cells = n_rewarm = 0
    consecutive = 0
    for i, g in enumerate(games):
        try:
            html = await cb_http.expand_detail_raw(sport_id, g["event_key"])
        except Exception as exc:
            n_failed += 1
            consecutive += 1
            log.warning("cb sport=%d game=%s expand failed: %s",
                        sport_id, g["event_key"], exc)
            # A session bounced mid-sport fails every remaining expand. Three
            # in a row is the signal to re-warm rather than write off the rest
            # of the board.
            if consecutive >= 3 and n_rewarm < 3:
                n_rewarm += 1
                consecutive = 0
                log.warning("cb sport=%d: re-warming after %d consecutive "
                            "failures", sport_id, 3)
                try:
                    await _list_with_retry(sport_id)
                except Exception as exc2:
                    log.error("cb sport=%d: re-warm failed (%s); giving up on "
                              "the rest of this sport", sport_id, exc2)
                    break
            continue
        consecutive = 0
        writer.count_bytes(len(html))
        frag = slice_detail(html, g["event_key"])
        if not frag:
            n_failed += 1
            continue
        n_expanded += 1
        # Only now is this event's market list known in full, which is what
        # licenses concluding that a position missing from it was pulled.
        writer.mark_read(g["event_key"])
        for mkey, title, label, ref, odds in parse_detail(
                frag, drop_players=drop_players):
            n_cells += 1
            writer.add(g["event_key"], mkey, title, label, odds, ref=ref)
        if progress_every and (i + 1) % progress_every == 0:
            log.info("cb sport=%d %d/%d games, %d cells, %.0fs",
                     sport_id, i + 1, len(games), n_cells, time.monotonic() - t0)

    # Free the session's ViewState and cookies between sports: 33 warmed
    # sessions held open for a whole pass is memory we never need at once.
    cb_http.reset_session(sport_id)
    return {"games": len(games), "expanded": n_expanded, "failed": n_failed,
            "rewarmed": n_rewarm, "cells": n_cells,
            "sec": round(time.monotonic() - t0, 1)}


async def collect(store, *, sports: Optional[list[int]] = None,
                  concurrency: int = 3, max_start_days: float = 0.0,
                  max_games: int = 0, drop_players: bool = True) -> list[dict]:
    """One full CrystalBet pass. Each sport is its own snapshot row."""
    if sports is None:
        found = await asyncio.to_thread(discover_sports)
        sports = [sid for sid, _n, _c in found]
        log.info("cb: %d sports with games: %s", len(found),
                 ", ".join(f"{n}({c})" for _s, n, c in found[:12]))

    sem = asyncio.Semaphore(max(1, concurrency))
    results: list[dict] = []

    async def one(sport_id: int) -> None:
        name = SPORT_NAMES.get(sport_id, f"sport_{sport_id}")
        async with sem:
            try:
                with store.snapshot(BOOK, name) as w:
                    stats = await collect_sport(
                        w, sport_id, max_start_days=max_start_days,
                        max_games=max_games, drop_players=drop_players)
                    out = w.commit()
                out.update(stats)
                out["sport"] = name
                results.append(out)
                log.info("cb %s: %s", name, out)
            except Exception as exc:
                log.error("cb %s failed: %s", name, exc)
                results.append({"sport": name, "error": str(exc)})

    await asyncio.gather(*(one(s) for s in sports))
    return results
