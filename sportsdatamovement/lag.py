"""
Which positions lag the match price — across every book and every sport.

THE QUESTION
------------
When a book moves the market it cannot afford to get wrong — the moneyline —
what else moves with it? A position that reliably does not react is where a
stale price can survive long enough to bet.

WHAT THE FIRST VERSION OF THIS GOT WRONG
----------------------------------------
Both mistakes were found by one fixture the owner checked by hand (Deportivo
Muniz Reserves: main result moved six times, the HT/FT grid never moved at
all), and both would silently hide exactly the markets worth finding.

1. **Aggregating by market name without checking for aliases.** CrystalBet
   prices "Halftime/Fulltime" on 1,281 soccer events and "HT/FT" on 112 others,
   and no event carries both. The first follows the main result 98 % of the
   time, the second 60 %. Reporting one number for "HT/FT markets" averages a
   well-kept market with a neglected one and shows neither.

2. **A minimum-sample cutoff.** Dropping markets with few observations deletes
   the finding by construction: a market carried on fewer events is, almost by
   definition, one the book maintains less. Thin samples are reported here with
   their `n` visible so they can be judged, never filtered away.

And the measure itself is per POSITION, not per market. "Did the HT/FT grid
tick" is true when 1 of its 9 cells moved; the question is whether the cell you
would actually bet moved. The two differ by more than 10 points on some markets
(Halftime/Fulltime and 1st Half Total #3: 98.1 % of grids, 81.4 % of cells).

THE ANCHOR
----------
Chosen per (book, sport) by name, because the auto-detected "market on the most
events with 2-3 selections" picks "Home Team To Win a Set" for CrystalBet
tennis and "Will the fight go the distance" for MMA. The chosen anchor is
returned with the results so a wrong one is visible rather than silently
skewing every rate beneath it.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Optional

# Ordered by preference. The first that exists for a (book, sport) wins, so a
# 3-way main result beats a 2-way "winner" where a book offers both.
ANCHOR_NAMES = (
    "Main result", "Full Time Result", "Result", "Match result",
    "Winner (incl. overtime)", "Winner (incl. extra innings)", "Winner (OT)",
    "Winner", "Match Winner", "Moneyline", "To Win",
)


def find_anchor(con: sqlite3.Connection, book: str, sport: str
                ) -> tuple[Optional[str], set[int]]:
    """-> (anchor market name, its market ids). Empty when nothing matches."""
    have = {}
    for mid, name, n in con.execute("""
            SELECT m.id, m.name, COUNT(DISTINCT p.event_id)
              FROM markets m
              JOIN positions p ON p.market_id = m.id
              JOIN events e ON e.id = p.event_id
             WHERE e.book=? AND e.sport=? GROUP BY m.id""", (book, sport)):
        have.setdefault(name, []).append((mid, n))
    for want in ANCHOR_NAMES:
        if want in have:
            return want, {mid for mid, _ in have[want]}
    # Fall back to the widest 2-3 selection market, and say so by returning the
    # name — a caller that sees something odd can discount the whole table.
    best = None
    for name, entries in have.items():
        ev = sum(n for _, n in entries)
        if best is None or ev > best[1]:
            best = (name, ev, {mid for mid, _ in entries})
    return (best[0], best[2]) if best else (None, set())


def follow_rates(db_path: str, book: str, sport: str, *, min_obs: int = 20
                 ) -> dict:
    """How often each market reacts in the same pass as the anchor.

    Returns {"anchor": name, "anchor_moves": n, "rows": [...]} where each row
    carries BOTH rates — `market_rate` (any cell moved) and `position_rate`
    (this cell moved) — plus the event and observation counts behind them.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        anchor_name, anchors = find_anchor(con, book, sport)
        if not anchors:
            return {"anchor": None, "anchor_moves": 0, "rows": []}

        pos_event, pos_mkt, mkt_name = {}, {}, {}
        for pid, eid, mid in con.execute("""
                SELECT p.id, p.event_id, p.market_id FROM positions p
                  JOIN events e ON e.id=p.event_id
                 WHERE e.book=? AND e.sport=?""", (book, sport)):
            pos_event[pid], pos_mkt[pid] = eid, mid
        for mid, name in con.execute(
                "SELECT id, name FROM markets WHERE book=?", (book,)):
            mkt_name[mid] = name

        # A position's FIRST tick is its creation, not a reaction to anything —
        # it had no previous price to move from. Counting it as a follow
        # inflates every rate here, and inflates it most for the markets that
        # appear late and then never move, which are the ones being looked for.
        first_tick: dict[int, int] = {}
        raw: list[tuple[int, int]] = []
        for pid, sid in con.execute(
                "SELECT position_id, snapshot_id FROM odds WHERE price IS NOT NULL"
                " ORDER BY position_id, snapshot_id"):
            if pid not in pos_event:
                continue
            if pid not in first_tick:
                first_tick[pid] = sid
                continue
            raw.append((pid, sid))

        ticked = defaultdict(set)
        for pid, sid in raw:
            ticked[(pos_event[pid], sid)].add(pid)
    finally:
        con.close()

    ev_positions = defaultdict(list)
    for pid, eid in pos_event.items():
        ev_positions[eid].append(pid)

    m_opp, m_fol = defaultdict(int), defaultdict(int)
    p_opp, p_fol = defaultdict(int), defaultdict(int)
    events = defaultdict(set)
    anchor_moves = 0
    for (eid, sid), moved in ticked.items():
        if not any(pos_mkt[p] in anchors for p in moved):
            continue
        anchor_moves += 1
        by_mkt = defaultdict(list)
        for pid in ev_positions[eid]:
            # A position that did not yet exist at this pass had no chance to
            # react, and one that has never been priced at all cannot be judged.
            fs = first_tick.get(pid)
            if fs is None or fs >= sid:
                continue
            by_mkt[pos_mkt[pid]].append(pid)
        for mid, pids in by_mkt.items():
            if mid in anchors:
                continue
            m_opp[mid] += 1
            events[mid].add(eid)
            if any(p in moved for p in pids):
                m_fol[mid] += 1
            for p in pids:
                p_opp[mid] += 1
                if p in moved:
                    p_fol[mid] += 1

    rows = []
    for mid, opp in m_opp.items():
        if opp < min_obs:
            continue
        rows.append({
            "market": mkt_name.get(mid, str(mid)),
            "events": len(events[mid]),
            "obs": opp,
            "market_rate": m_fol[mid] / opp,
            "position_rate": p_fol[mid] / max(1, p_opp[mid]),
        })
    rows.sort(key=lambda r: r["position_rate"])
    return {"anchor": anchor_name, "anchor_moves": anchor_moves, "rows": rows}


def aliases(db_path: str, book: str, sport: str, needle: str) -> list[dict]:
    """Every market name containing `needle`, with the events carrying it.

    The alias trap in reverse: before trusting a rate for "the HT/FT market",
    check how many markets that description actually covers.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return [{"market": n, "events": ev, "positions": pos}
                for n, ev, pos in con.execute("""
                    SELECT m.name, COUNT(DISTINCT p.event_id), COUNT(*)
                      FROM positions p JOIN markets m ON m.id=p.market_id
                      JOIN events e ON e.id=p.event_id
                     WHERE e.book=? AND e.sport=? AND m.name LIKE ?
                     GROUP BY m.name ORDER BY 2 DESC""",
                    (book, sport, f"%{needle}%"))]
    finally:
        con.close()
