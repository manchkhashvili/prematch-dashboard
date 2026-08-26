"""
The movement dashboard — a read-only view over the change log.

Its own FastAPI app on its own port, deliberately not a tab on the main
dashboard: this project has its own database and its own lifecycle, and the
scanner has enough to do without serving analytical queries that scan millions
of rows.

THE THREE QUESTIONS IT ANSWERS
------------------------------
    /api/movers    what moved, sortable by how far it moved
    /api/event     for one event: which markets moved in which pass, as a grid
    /api/markets   which market TYPES move and which sit still

The middle one is the point of the whole project. A row per market, a column
per hourly pass, a filled cell where the price changed: the main result moving
while HT/FT 1/1 stays blank for six hours is visible at a glance, and no
statistic has to be trusted to see it.

READING A CHANGE LOG BACKWARDS
------------------------------
`odds` holds only changes, so "what did this move FROM" is not in the row —
it is the previous row for the same position. That is a correlated lookup, and
it is why every endpoint here is bounded by a snapshot window and a limit:
computing the from-price for a whole pass of CrystalBet soccer is ~50 k index
seeks. Bounded, it is a second; unbounded it would be a minute and would block
the collector's commit.

Prices are milli-odds in the database (see store.py); everything crossing the
API boundary is decimal.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import FastAPI, HTTPException, Query          # noqa: E402
from fastapi.responses import FileResponse                 # noqa: E402

from sportsdatamovement.store import Store, get_store      # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="sports data movement")
_store: Optional[Store] = None


def store() -> Store:
    global _store
    if _store is None:
        _store = get_store()
    return _store


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    con = store()._connect()
    try:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(sql, params)]
    finally:
        con.close()


def _price(v) -> Optional[float]:
    return None if v is None else round(v / 1000.0, 4)


# ── shared filters ────────────────────────────────────────────────────────────

def _scope(book: Optional[str], sport: Optional[str]) -> tuple[str, list]:
    where, params = [], []
    if book:
        where.append("e.book = ?")
        params.append(book)
    if sport:
        where.append("e.sport = ?")
        params.append(sport)
    return (" AND ".join(where) if where else "1=1"), params


@app.get("/api/meta")
def meta() -> dict:
    """Everything the filters need, plus the pass history."""
    books = _rows("SELECT DISTINCT book, sport, COUNT(*) AS events FROM events"
                  " GROUP BY book, sport ORDER BY events DESC")
    passes = _rows(
        "SELECT id, book, sport, started_at, finished_at, ok, n_events,"
        " n_positions, n_new, n_moved, n_gone, n_dupes, bytes, dur_ms, error"
        " FROM snapshots ORDER BY id DESC LIMIT 400")
    return {"scopes": books, "passes": passes, "db": store().stats()}


@app.get("/api/movers")
def movers(book: Optional[str] = None, sport: Optional[str] = None,
           snapshot: Optional[int] = None, since_passes: int = 1,
           search: str = "", min_pct: float = 0.0, max_odds: float = 0.0,
           sort: str = "abs_pp", limit: int = Query(300, le=5000)) -> dict:
    """Price changes, with what each one moved FROM.

    `since_passes` counts back over the snapshots of the selected scope, so
    "the last 3 passes" means three of THIS board's passes rather than three
    rows of a global table interleaved across 57 sports.

    Two measures of "how far", and the default is deliberately not the obvious
    one. Percentage move ranks a longshot drifting 40.0 -> 60.0 as +67 %, ahead
    of a favourite going 2.00 -> 1.10 at -45 % — but the first is a 0.8-point
    shift in implied probability and the second is 41 points. Sorted by
    percentage the whole first page is longshot noise, so `pp` (the change in
    implied probability) is what ranks by default.
    """
    scope, params = _scope(book, sport)
    snaps = _rows(
        f"SELECT id FROM snapshots WHERE ok=1"
        f"{' AND book=?' if book else ''}{' AND sport=?' if sport else ''}"
        f" ORDER BY id DESC LIMIT ?",
        tuple(params + [max(1, since_passes)]))
    if not snaps:
        return {"rows": [], "snapshot_ids": []}
    ids = [r["id"] for r in snaps]
    if snapshot is not None:
        ids = [snapshot]
    placeholders = ",".join("?" * len(ids))

    order = {
        "abs_pp": "ABS(pp) DESC",
        "pp": "pp DESC",
        "pp_asc": "pp ASC",
        "abs_pct": "ABS(pct) DESC",
        "pct": "pct DESC",
        "pct_asc": "pct ASC",
        "time": "snapshot_id DESC",
        "odds": "odds DESC",
    }.get(sort, "ABS(pp) DESC")

    like = f"%{search.strip()}%" if search.strip() else None
    sql = f"""
    SELECT * FROM (
      SELECT sn.started_at AS ts, o.snapshot_id AS snapshot_id,
             e.book, e.sport, e.league, e.home, e.away, e.start_time AS kickoff,
             m.name AS market, s.label AS side, p.line,
             o.position_id AS position_id,
             o.price AS now_price,
             (SELECT price FROM odds o2
               WHERE o2.position_id = o.position_id
                 AND o2.snapshot_id < o.snapshot_id
               ORDER BY o2.snapshot_id DESC LIMIT 1) AS was_price
        FROM odds o
        JOIN positions p ON p.id = o.position_id
        JOIN markets   m ON m.id = p.market_id
        JOIN sides     s ON s.id = p.side_id
        JOIN events    e ON e.id = p.event_id
        JOIN snapshots sn ON sn.id = o.snapshot_id
       WHERE o.snapshot_id IN ({placeholders}) AND {scope}
         {"AND (e.home LIKE ? OR e.away LIKE ? OR m.name LIKE ? OR e.league LIKE ?)" if like else ""}
    )
    """
    args = list(ids) + params
    if like:
        args += [like, like, like, like]
    # The from-price is only known after the subquery, so the derived columns
    # and the magnitude filter have to sit outside it.
    sql = f"""
    SELECT ts, snapshot_id, book, sport, league, home, away, kickoff,
           market, side, line, position_id,
           now_price / 1000.0 AS odds,
           was_price / 1000.0 AS was,
           CASE WHEN was_price IS NULL OR was_price = 0 THEN NULL
                ELSE (now_price - was_price) * 100.0 / was_price END AS pct,
           CASE WHEN was_price IS NULL OR was_price = 0 OR now_price = 0 THEN NULL
                ELSE 100000.0 / now_price - 100000.0 / was_price END AS pp
      FROM ({sql})
     WHERE was_price IS NOT NULL AND now_price IS NOT NULL
       AND ABS((now_price - was_price) * 100.0 / was_price) >= ?
       AND (? <= 0 OR now_price <= ? * 1000.0)
     ORDER BY {order} LIMIT ?"""
    args += [min_pct, max_odds, max_odds, limit]
    return {"rows": _rows(sql, tuple(args)), "snapshot_ids": ids}


@app.get("/api/events")
def events(book: Optional[str] = None, sport: Optional[str] = None,
           search: str = "", sort: str = "moves",
           since_passes: int = 6, limit: int = Query(150, le=1000)) -> dict:
    """Events ranked by how much of their board moved recently."""
    scope, params = _scope(book, sport)
    snaps = _rows(
        f"SELECT id FROM snapshots WHERE ok=1"
        f"{' AND book=?' if book else ''}{' AND sport=?' if sport else ''}"
        f" ORDER BY id DESC LIMIT ?",
        tuple(params + [max(1, since_passes)]))
    ids = [r["id"] for r in snaps] or [0]
    placeholders = ",".join("?" * len(ids))
    like = f"%{search.strip()}%" if search.strip() else None
    order = {"moves": "moves DESC", "positions": "positions DESC",
             "kickoff": "kickoff ASC"}.get(sort, "moves DESC")
    sql = f"""
    SELECT e.id AS event_id, e.book, e.sport, e.league, e.home, e.away,
           e.start_time AS kickoff,
           COUNT(DISTINCT p.id) AS positions,
           SUM(CASE WHEN o.snapshot_id IN ({placeholders}) AND o.price IS NOT NULL
                    THEN 1 ELSE 0 END) AS moves
      FROM events e
      JOIN positions p ON p.event_id = e.id
      LEFT JOIN odds o ON o.position_id = p.id
     WHERE {scope} {"AND (e.home LIKE ? OR e.away LIKE ? OR e.league LIKE ?)" if like else ""}
     GROUP BY e.id
     ORDER BY {order} LIMIT ?"""
    args = list(ids) + params + ([like, like, like] if like else []) + [limit]
    return {"rows": _rows(sql, tuple(args))}


@app.get("/api/event/{event_id}")
def event(event_id: int, passes: int = Query(24, le=200)) -> dict:
    """One event as a grid: a row per position, a column per pass.

    This is the view the project exists for. `cells` gives the price recorded
    in each pass where the position changed; a gap means "unchanged since the
    last filled cell", which is exactly what the change log asserts.
    """
    head = _rows("SELECT id, book, sport, league, home, away, start_time,"
                 " first_seen, last_seen FROM events WHERE id=?", (event_id,))
    if not head:
        raise HTTPException(404, "no such event")
    ev = head[0]
    cols = _rows(
        "SELECT id, started_at FROM snapshots WHERE book=? AND sport=? AND ok=1"
        " ORDER BY id DESC LIMIT ?", (ev["book"], ev["sport"], passes))
    cols.reverse()
    ids = [c["id"] for c in cols] or [0]
    placeholders = ",".join("?" * len(ids))

    rows = _rows(f"""
        SELECT p.id AS position_id, m.name AS market, s.label AS side, p.line,
               l.price AS latest,
               (SELECT COUNT(*) FROM odds o WHERE o.position_id=p.id) AS n_moves,
               ls.started_at AS last_move
          FROM positions p
          JOIN markets m ON m.id = p.market_id
          JOIN sides   s ON s.id = p.side_id
          LEFT JOIN latest l ON l.position_id = p.id
          LEFT JOIN snapshots ls ON ls.id = l.snapshot_id
         WHERE p.event_id = ?
         ORDER BY m.name, s.label""", (event_id,))

    cells = _rows(f"""
        SELECT o.position_id, o.snapshot_id, o.price
          FROM odds o JOIN positions p ON p.id = o.position_id
         WHERE p.event_id = ? AND o.snapshot_id IN ({placeholders})""",
        tuple([event_id] + ids))

    grid: dict[int, dict[int, Optional[float]]] = {}
    for c in cells:
        grid.setdefault(c["position_id"], {})[c["snapshot_id"]] = _price(c["price"])
    for r in rows:
        r["latest"] = _price(r["latest"])
        r["cells"] = grid.get(r["position_id"], {})
    return {"event": ev, "columns": cols, "rows": rows}


@app.get("/api/history/{position_id}")
def history(position_id: int) -> dict:
    """Every recorded price for one position — the input to the price chart.

    Step lines, not a curve: between two ticks the price is *known* to have
    been the earlier value, which is the whole premise of the store.
    """
    head = _rows("""
        SELECT m.name AS market, s.label AS side, p.line, p.ref,
               e.book, e.sport, e.league, e.home, e.away, e.start_time AS kickoff
          FROM positions p
          JOIN markets m ON m.id=p.market_id
          JOIN sides   s ON s.id=p.side_id
          JOIN events  e ON e.id=p.event_id
         WHERE p.id=?""", (position_id,))
    if not head:
        raise HTTPException(404, "no such position")
    pts = _rows("""
        SELECT sn.started_at AS ts, o.snapshot_id, o.price
          FROM odds o JOIN snapshots sn ON sn.id=o.snapshot_id
         WHERE o.position_id=? ORDER BY o.snapshot_id""", (position_id,))
    for p in pts:
        p["odds"] = _price(p.pop("price"))
    return {"position": head[0], "points": pts}


@app.get("/api/markets")
def markets(book: Optional[str] = None, sport: Optional[str] = None,
            since_passes: int = 12, sort: str = "rate",
            limit: int = Query(400, le=2000)) -> dict:
    """Which market types move, and which sit still.

    `rate` is moves per position per pass. A market at 0.00 over a dozen passes
    while its neighbours run at 0.05 is the shape this project is looking for —
    though a market can also sit still because nothing on its events moved,
    which is what the event grid is for.
    """
    scope, params = _scope(book, sport)
    snaps = _rows(
        f"SELECT id FROM snapshots WHERE ok=1"
        f"{' AND book=?' if book else ''}{' AND sport=?' if sport else ''}"
        f" ORDER BY id DESC LIMIT ?",
        tuple(params + [max(1, since_passes)]))
    ids = [r["id"] for r in snaps] or [0]
    n_passes = len(ids)
    placeholders = ",".join("?" * len(ids))
    order = {"rate": "rate DESC", "rate_asc": "rate ASC",
             "positions": "positions DESC",
             "moves": "moves DESC"}.get(sort, "rate DESC")
    sql = f"""
    SELECT market, book, sport, positions, moves,
           CAST(moves AS REAL) / (positions * ?) AS rate
      FROM (
        SELECT m.name AS market, e.book, e.sport,
               COUNT(DISTINCT p.id) AS positions,
               SUM(CASE WHEN o.snapshot_id IN ({placeholders})
                        AND o.price IS NOT NULL THEN 1 ELSE 0 END) AS moves
          FROM positions p
          JOIN markets m ON m.id = p.market_id
          JOIN events  e ON e.id = p.event_id
          LEFT JOIN odds o ON o.position_id = p.id
         WHERE {scope}
         GROUP BY m.name, e.book, e.sport
      ) WHERE positions > 0
     ORDER BY {order} LIMIT ?"""
    args = [max(1, n_passes)] + list(ids) + params + [limit]
    return {"rows": _rows(sql, tuple(args)), "passes": n_passes}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


def serve(host: str = "127.0.0.1", port: int = 8100) -> None:
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")
