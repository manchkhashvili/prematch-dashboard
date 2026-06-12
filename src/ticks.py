"""
SQLite tick store — change-only odds history for both books.

Ported from prematch_v2/src/db.py (the v2 design, see docs/performance.md
"odds change rate"): lines are static most of the day, so a *tick* is stored
only when a selection's odds actually change. Between ticks the value is known
unchanged; charts render as step lines. A `latest` table mirrors the current
value per (market, selection) for restart recovery, and `poll_cycles` is the
heartbeat that distinguishes "unchanged" from "not polled". A market that
disappears from a healthy full cycle gets one NULL tick.

v1 adaptations vs v2:
  - rows come from the in-memory Odds model via rows_from_odds() — submarket
    (corners) folds into market_type as a prefix ("corners_total") so the
    market key stays 5 columns, exactly like v2's pin client did;
  - matching/link tables dropped — v1 matches in memory per request;
  - only the two MAIN pollers write (dashboard `_state` odds, post-merge).
    The anomaly scan's per-section permissive odds would alias the same
    market keys with different sections and ping-pong the ticks.

Writes are serialized with a lock; connections are short-lived per call.
The DB lives at data/ticks.db (gitignored), override with TICKS_DB_PATH.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from src.models import Odds

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "ticks.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  book TEXT NOT NULL,
  sport TEXT NOT NULL,
  source_event_id TEXT NOT NULL,
  home TEXT,
  away TEXT,
  league TEXT,
  start_time TEXT,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  UNIQUE(book, sport, source_event_id)
);

CREATE TABLE IF NOT EXISTS markets (
  id INTEGER PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events(id),
  market_type TEXT NOT NULL,
  period TEXT NOT NULL DEFAULT 'FT',
  line REAL,
  team_side TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_markets_key
  ON markets(event_id, market_type, period, COALESCE(line, -999.25), COALESCE(team_side, ''));

CREATE TABLE IF NOT EXISTS ticks (
  market_id INTEGER NOT NULL REFERENCES markets(id),
  selection TEXT NOT NULL,
  ts TEXT NOT NULL,
  odds REAL,
  meta TEXT,
  PRIMARY KEY (market_id, selection, ts)
);

CREATE TABLE IF NOT EXISTS latest (
  market_id INTEGER NOT NULL,
  selection TEXT NOT NULL,
  odds REAL,
  ts TEXT NOT NULL,
  meta TEXT,
  PRIMARY KEY (market_id, selection)
);

CREATE TABLE IF NOT EXISTS poll_cycles (
  id INTEGER PRIMARY KEY,
  book TEXT NOT NULL,
  sport TEXT NOT NULL,
  ts TEXT NOT NULL,
  ok INTEGER NOT NULL,
  n_events INTEGER NOT NULL DEFAULT 0,
  n_changes INTEGER NOT NULL DEFAULT 0,
  dur_ms INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rows_from_odds(odds: Iterable[Odds]) -> list[dict]:
    """Odds → tick-store rows. Submarket folds into market_type ('corners_total')
    so the storage key stays (market_type, period, line, team_side)."""
    rows = []
    for o in odds:
        mtype = f"{o.submarket}_{o.market_type}" if o.submarket else o.market_type
        rows.append({
            "source_event_id": o.raw_event_id or f"{o.home}|{o.away}",
            "home": o.home, "away": o.away, "league": o.league,
            "start_time": o.start_time.isoformat(timespec="seconds")
            if o.start_time else None,
            "market_type": mtype, "period": o.period, "line": o.line,
            "team_side": o.team_side,
            "selections": o.selections,
            "meta": {"max_stake": o.max_stake} if o.max_stake is not None else None,
        })
    return rows


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # caches: avoid a SELECT per row on the hot write path
        self._event_ids: dict[tuple, int] = {}
        self._market_ids: dict[tuple, int] = {}
        self._latest: dict[tuple[int, str], Optional[float]] = {}
        self._prev_keys: dict[tuple[str, str], set] = {}
        con = self._connect()
        try:
            con.executescript(SCHEMA)
            con.commit()
            self._load_caches(con)
        finally:
            con.close()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30)
        try:
            con.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        con.execute("PRAGMA synchronous=NORMAL")
        return con

    def _load_caches(self, con: sqlite3.Connection) -> None:
        for eid, book, sport, src in con.execute(
                "SELECT id, book, sport, source_event_id FROM events"):
            self._event_ids[(book, sport, src)] = eid
        for mid, eid, mtype, period, line, side in con.execute(
                "SELECT id, event_id, market_type, period, line, team_side FROM markets"):
            self._market_ids[(eid, mtype, period, line, side)] = mid
        for mid, sel, odds in con.execute(
                "SELECT market_id, selection, odds FROM latest"):
            self._latest[(mid, sel)] = odds

    # ── write path ────────────────────────────────────────────────────────────

    def record_cycle(self, book: str, sport: str, rows: Iterable[dict], *,
                     ok: bool = True, error: str | None = None,
                     dur_ms: int = 0, ts: str | None = None,
                     prune: bool = True) -> dict:
        """Persist one poll cycle (see module doc for row shape).
        Returns {"n_events": int, "n_changes": int}."""
        ts = ts or utcnow_iso()
        n_changes = 0
        event_srcs: set[str] = set()
        seen_keys: set[tuple[int, str]] = set()

        with self._lock:
            con = self._connect()
            try:
                cur = con.cursor()
                for row in rows:
                    src = str(row["source_event_id"])
                    event_srcs.add(src)
                    eid = self._upsert_event(cur, book, sport, src, row, ts)
                    mid = self._get_or_create_market(cur, eid, row)
                    meta = row.get("meta")
                    meta_json = json.dumps(meta, separators=(",", ":")) if meta else None
                    for sel, odds in (row.get("selections") or {}).items():
                        key = (mid, sel)
                        seen_keys.add(key)
                        if key not in self._latest or self._latest[key] != odds:
                            cur.execute(
                                "INSERT OR REPLACE INTO ticks (market_id, selection, ts, odds, meta)"
                                " VALUES (?,?,?,?,?)", (mid, sel, ts, odds, meta_json))
                            cur.execute(
                                "INSERT OR REPLACE INTO latest (market_id, selection, odds, ts, meta)"
                                " VALUES (?,?,?,?,?)", (mid, sel, odds, ts, meta_json))
                            self._latest[key] = odds
                            n_changes += 1
                        elif meta_json is not None:
                            # odds unchanged but limits may move — keep the
                            # snapshot's meta fresh without a tick
                            cur.execute("UPDATE latest SET meta=? WHERE market_id=?"
                                        " AND selection=?", (meta_json, mid, sel))

                # disappearance: only on a healthy, non-empty, FULL cycle — a
                # failed/empty fetch must not NULL the whole book.
                if prune and ok and seen_keys:
                    prev = self._prev_keys.get((book, sport), set())
                    for key in prev - seen_keys:
                        if self._latest.get(key) is not None:
                            cur.execute(
                                "INSERT OR REPLACE INTO ticks (market_id, selection, ts, odds, meta)"
                                " VALUES (?,?,?,NULL,NULL)", (key[0], key[1], ts))
                            cur.execute(
                                "INSERT OR REPLACE INTO latest (market_id, selection, odds, ts)"
                                " VALUES (?,?,NULL,?)", (key[0], key[1], ts))
                            self._latest[key] = None
                            n_changes += 1
                    self._prev_keys[(book, sport)] = seen_keys

                cur.execute(
                    "INSERT INTO poll_cycles (book, sport, ts, ok, n_events, n_changes, dur_ms, error)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (book, sport, ts, 1 if ok else 0, len(event_srcs), n_changes,
                     dur_ms, error))
                con.commit()
            finally:
                con.close()
        return {"n_events": len(event_srcs), "n_changes": n_changes}

    def _upsert_event(self, cur, book, sport, src, row, ts) -> int:
        key = (book, sport, src)
        eid = self._event_ids.get(key)
        if eid is None:
            cur.execute(
                "INSERT INTO events (book, sport, source_event_id, home, away,"
                " league, start_time, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(book, sport, source_event_id) DO NOTHING",
                (book, sport, src, row.get("home"), row.get("away"),
                 row.get("league"), row.get("start_time"), ts, ts))
            eid = cur.execute(
                "SELECT id FROM events WHERE book=? AND sport=? AND source_event_id=?",
                (book, sport, src)).fetchone()[0]
            self._event_ids[key] = eid
        else:
            cur.execute(
                "UPDATE events SET last_seen=?,"
                " home=COALESCE(?, home), away=COALESCE(?, away),"
                " league=COALESCE(?, league), start_time=COALESCE(?, start_time)"
                " WHERE id=?",
                (ts, row.get("home"), row.get("away"), row.get("league"),
                 row.get("start_time"), eid))
        return eid

    def _get_or_create_market(self, cur, eid: int, row: dict) -> int:
        mtype = row["market_type"]
        period = row.get("period") or "FT"
        line = row.get("line")
        side = row.get("team_side")
        key = (eid, mtype, period, line, side)
        mid = self._market_ids.get(key)
        if mid is None:
            cur.execute(
                "INSERT OR IGNORE INTO markets (event_id, market_type, period, line, team_side)"
                " VALUES (?,?,?,?,?)", (eid, mtype, period, line, side))
            mid = cur.execute(
                "SELECT id FROM markets WHERE event_id=? AND market_type=? AND period=?"
                " AND COALESCE(line,-999.25)=COALESCE(?,-999.25)"
                " AND COALESCE(team_side,'')=COALESCE(?,'')",
                (eid, mtype, period, line, side)).fetchone()[0]
            self._market_ids[key] = mid
        return mid

    # ── read path ─────────────────────────────────────────────────────────────

    def event_id(self, book: str, sport: str, source_event_id: str) -> Optional[int]:
        return self._event_ids.get((book, sport, str(source_event_id)))

    def series(self, event_id: int, market_type: str, period: str,
               line: float | None, team_side: str | None,
               since_ts: str, line_tolerance: float = 0.26) -> dict[str, list]:
        """Tick history for one market of one event: {selection: [[ts, odds]...]}.

        `line_tolerance` lets the chart follow the nearest line when the two
        books' lines differ slightly or the book re-centers its main line.
        """
        con = self._connect()
        try:
            mids = []
            for mid, mline in con.execute(
                    "SELECT id, line FROM markets WHERE event_id=? AND market_type=?"
                    " AND period=? AND COALESCE(team_side,'')=COALESCE(?,'')",
                    (event_id, market_type, period, team_side)):
                if line is None and mline is None:
                    mids.append(mid)
                elif line is not None and mline is not None \
                        and abs(mline - line) <= line_tolerance:
                    mids.append((abs(mline - line), mid))
            if not mids:
                return {}
            if line is not None:
                mids.sort()
                mid = mids[0][1]
            else:
                mid = mids[0]
            out: dict[str, list] = {}
            for sel, ts, odds in con.execute(
                    "SELECT selection, ts, odds FROM ticks WHERE market_id=?"
                    " AND ts>=? ORDER BY ts", (mid, since_ts)):
                out.setdefault(sel, []).append([ts, odds])
            # seed each series with the last value BEFORE the window so the
            # step line starts at the left edge, not at the first change
            for sel in list(out):
                row = con.execute(
                    "SELECT ts, odds FROM ticks WHERE market_id=? AND selection=?"
                    " AND ts<? ORDER BY ts DESC LIMIT 1", (mid, sel, since_ts)).fetchone()
                if row:
                    out[sel].insert(0, [since_ts, row[1]])
            # selections that ONLY have pre-window history
            for (sel,) in con.execute(
                    "SELECT DISTINCT selection FROM ticks WHERE market_id=?", (mid,)):
                if sel not in out:
                    row = con.execute(
                        "SELECT ts, odds FROM ticks WHERE market_id=? AND selection=?"
                        " ORDER BY ts DESC LIMIT 1", (mid, sel)).fetchone()
                    if row and row[1] is not None:
                        out[sel] = [[since_ts, row[1]]]
            return out
        finally:
            con.close()

    def cycle_status(self) -> list[dict]:
        """Most recent poll cycle per (book, sport)."""
        con = self._connect()
        try:
            q = """
            SELECT book, sport, ts, ok, n_events, n_changes, dur_ms, error
            FROM poll_cycles
            WHERE id IN (SELECT MAX(id) FROM poll_cycles GROUP BY book, sport)
            ORDER BY book, sport
            """
            cols = ["book", "sport", "ts", "ok", "n_events", "n_changes", "dur_ms", "error"]
            return [dict(zip(cols, r)) for r in con.execute(q)]
        finally:
            con.close()


# ── module-level singleton ────────────────────────────────────────────────────
_store: Store | None = None
_store_lock = threading.Lock()


def get_store() -> Store:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                path = os.environ.get("TICKS_DB_PATH") or DEFAULT_DB_PATH
                _store = Store(path)
    return _store


def _reset_for_tests() -> None:
    global _store
    with _store_lock:
        _store = None
