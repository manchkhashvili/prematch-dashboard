"""
SQLite store for hourly whole-board snapshots of Lider-Bet and CrystalBet.

WHY THIS IS NOT A SNAPSHOT TABLE
--------------------------------
The obvious schema — one row per (position, snapshot) — is the schema that
produced the 32 GB `liveanomalies.db` we deleted on 2026-07-27. Measured on
2026-08-26, one snapshot of the two boards is roughly:

    CrystalBet   4,242 games x ~4,900 selections   ~= 20.8 M positions
    Lider-Bet    2,857 matches x ~1,700 outcomes   ~=  4.9 M positions

Call it ~26 M positions. At 24 snapshots/day that is 620 M rows/day. There is
no row encoding that makes that survivable.

So the store is CHANGE-ONLY: a row lands in `odds` when a position's price
DIFFERS from the last price we recorded for it, and at no other time. The
`snapshots` table is the heartbeat that makes this lossless — it records that a
(book, sport) board was successfully read at time T, which is what lets a reader
distinguish "unchanged since the last tick" from "we never looked". Together
they reconstruct any snapshot exactly (see `snapshot_at`), so nothing the naive
schema would have stored is lost. Measured change rate on a near-kickoff soccer
game was 3.8 % over 20 minutes; far-out games move far less.

That is also, conveniently, the primary output of the project. A change-only
table IS the movement log: "which positions moved between 14:00 and 15:00" is
`SELECT ... FROM odds WHERE snapshot_id = ?`, and "which stayed stale while a
related market moved" is that set's complement within the same event.

IDENTITY, AND THE TRAP UNDER IT
-------------------------------
A position is keyed by (event, market, side label, line) using the books' OWN
names — no normalisation, no allowlist. This project exists to compare a market
against its neighbours, and any mapping layer would decide in advance which
neighbours are worth keeping. Both books' full market sets are captured raw.

CrystalBet publishes a stable internal selection id (`<div id='S45217357861'>`)
and it is tempting to key on that. Do not. Measured on 2026-08-26 over a 20-
minute gap, all 4,615 ids on a game survived — but 30 of them had a DIFFERENT
line hanging off them afterwards ("21.5 Und" became "22.5 Und"). Keying on the
id would have booked a line slide as a price move on the old line, which is the
exact opposite of the truth. The id is kept in `positions.ref` as an
informational breadcrumb; the label is what identifies the bet.

The books' locked selections are a second trap: CrystalBet renders a suspended
cell as `class='... Snatch_Locked'` displaying **1.01**. Storing that literally
would log every suspension as a crash to 1.01 and every unsuspension as a
recovery. A locked cell is stored as price NULL — the same encoding used when a
position leaves the board entirely — because in both cases the true statement is
"not bettable at this instant", not "bettable at 1.01".

Prices are stored as INTEGER milli-odds (1.85 -> 1850): SQLite varints make that
two bytes where a REAL is eight.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "snapshots.db"

# Positions and prices are interned/encoded rather than stored verbatim; see the
# module docstring. -999.25 is the "no line" sentinel inside the unique index
# because SQLite treats every NULL as distinct, so a NULL line would let the
# same position be inserted an unbounded number of times.
NO_LINE = -999.25

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  book TEXT NOT NULL,
  sport TEXT NOT NULL,
  event_key TEXT NOT NULL,
  league TEXT,
  home TEXT,
  away TEXT,
  start_time TEXT,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  UNIQUE(book, sport, event_key)
);
CREATE INDEX IF NOT EXISTS idx_events_book_sport ON events(book, sport);

-- Interning tables. `markets` and `sides` hold a few thousand rows between
-- them; `positions` holds tens of millions, so every string moved out of it
-- pays for itself many times over.
CREATE TABLE IF NOT EXISTS markets (
  id INTEGER PRIMARY KEY,
  book TEXT NOT NULL,
  market_key TEXT NOT NULL,
  name TEXT NOT NULL,
  UNIQUE(book, market_key, name)
);

CREATE TABLE IF NOT EXISTS sides (
  id INTEGER PRIMARY KEY,
  label TEXT NOT NULL UNIQUE
);

-- `first_snapshot` is an id, not a timestamp: 26 M positions x 20 bytes of ISO
-- text is half a gigabyte spent restating something the snapshot row already
-- knows. `ref` holds the book's own selection handle and is stored as an
-- INTEGER where the book uses digits (CrystalBet), which SQLite packs into a
-- few bytes instead of eleven.
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events(id),
  market_id INTEGER NOT NULL REFERENCES markets(id),
  side_id INTEGER NOT NULL REFERENCES sides(id),
  line REAL,
  ref,
  first_snapshot INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_key
  ON positions(event_id, market_id, side_id, COALESCE(line, -999.25));

CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY,
  book TEXT NOT NULL,
  sport TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  ok INTEGER NOT NULL DEFAULT 0,
  n_events INTEGER NOT NULL DEFAULT 0,
  n_positions INTEGER NOT NULL DEFAULT 0,
  n_new INTEGER NOT NULL DEFAULT 0,
  n_moved INTEGER NOT NULL DEFAULT 0,
  n_gone INTEGER NOT NULL DEFAULT 0,
  n_dupes INTEGER NOT NULL DEFAULT 0,
  bytes INTEGER NOT NULL DEFAULT 0,
  dur_ms INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_book ON snapshots(book, sport, started_at);

CREATE TABLE IF NOT EXISTS odds (
  position_id INTEGER NOT NULL,
  snapshot_id INTEGER NOT NULL,
  price INTEGER,
  PRIMARY KEY (position_id, snapshot_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_odds_snapshot ON odds(snapshot_id);

CREATE TABLE IF NOT EXISTS latest (
  position_id INTEGER PRIMARY KEY,
  price INTEGER,
  snapshot_id INTEGER NOT NULL
) WITHOUT ROWID;
"""

# The flat shape the study asks for: sportsbook, sport, league, position, side,
# record_time. Only rows where the price actually moved appear — that is the
# point — so reconstructing a full board at an instant goes through
# `snapshot_at` instead.
VIEW = """
CREATE VIEW IF NOT EXISTS movements AS
SELECT sn.started_at        AS record_time,
       e.book               AS sportsbook,
       e.sport              AS sport,
       e.league             AS league,
       e.home || ' - ' || e.away AS event,
       e.start_time         AS kickoff,
       m.name               AS position,
       s.label              AS side,
       p.line               AS line,
       CAST(o.price AS REAL) / 1000.0 AS odds,
       o.position_id        AS position_id,
       o.snapshot_id        AS snapshot_id
FROM odds o
JOIN positions p ON p.id = o.position_id
JOIN markets   m ON m.id = p.market_id
JOIN sides     s ON s.id = p.side_id
JOIN events    e ON e.id = p.event_id
JOIN snapshots sn ON sn.id = o.snapshot_id;
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encode_price(odds: Optional[float]) -> Optional[int]:
    """Decimal odds -> milli-odds, or None for "not bettable at this instant".

    Anything at or below 1.0 is not a price a book can be held to, and
    CrystalBet's locked cells render as 1.01, so the caller is expected to have
    already passed None for those (see the module docstring). This is the
    last-resort guard, not the policy.
    """
    if odds is None:
        return None
    try:
        v = float(odds)
    except (TypeError, ValueError):
        return None
    if v <= 1.0:
        return None
    return int(round(v * 1000.0))


def decode_price(price: Optional[int]) -> Optional[float]:
    return None if price is None else price / 1000.0


def _pack_ref(ref):
    """A book's selection handle, as compactly as SQLite will hold it.

    CrystalBet's are eleven-digit numbers ("45217357861") and there is one per
    position, so at 26 M positions the difference between TEXT and INTEGER is
    hundreds of megabytes. Lider's ("ot:16:1651") stay text.
    """
    if isinstance(ref, str) and ref.isdigit():
        try:
            return int(ref)
        except ValueError:
            return ref
    return ref


class Store:
    """One SQLite file. Thread-safe via a single write lock.

    Reads open their own short-lived connections, so an analysis query never
    blocks a collection pass beyond SQLite's own WAL semantics.
    """

    def __init__(self, path: str | Path = DEFAULT_DB_PATH):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        con = self._connect()
        try:
            con.executescript(SCHEMA)
            con.executescript(VIEW)
            con.commit()
        finally:
            con.close()

    # ── connection ────────────────────────────────────────────────────────────

    def _connect(self, *, autocommit: bool = False) -> sqlite3.Connection:
        """`autocommit=True` hands transaction control to the caller.

        The writer needs it: sqlite3 opens an implicit transaction on the first
        INSERT, including into a TEMP table, and then refuses the explicit
        BEGIN IMMEDIATE that the commit-time diff needs to take the write lock
        once rather than per statement.
        """
        # check_same_thread=False: a snapshot writer is opened on the event
        # loop and then filled from whichever worker thread the collector runs
        # in (Lider's whole pass is one `asyncio.to_thread`), before being
        # committed back on the loop. Those uses are strictly sequential — a
        # writer is never touched by two threads at once — which is exactly the
        # case the flag exists for.
        con = sqlite3.connect(self.path, timeout=60, check_same_thread=False,
                              isolation_level=None if autocommit else "")
        try:
            con.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        con.execute("PRAGMA synchronous=NORMAL")
        # A whole-board pass inserts millions of rows; the default 2 MB page
        # cache turns that into a seek storm.
        con.execute("PRAGMA cache_size=-131072")   # 128 MB
        # Staging goes to a temp FILE, not memory. The whole point of the
        # staging design is that a board of any size costs the same RAM.
        con.execute("PRAGMA temp_store=FILE")
        return con

    # ── write path ────────────────────────────────────────────────────────────

    @contextmanager
    def snapshot(self, book: str, sport: str) -> Iterator["SnapshotWriter"]:
        """Open a snapshot, stream rows into it, close it.

        The snapshot row is INSERTed before any collection happens and only
        flipped to ok=1 at the end, so a crashed or half-finished pass is
        visibly incomplete rather than indistinguishable from a clean one. That
        distinction is load-bearing here: a reader treats `ok=1` as "the whole
        board was seen at this instant", which is what licenses the inference
        "no tick means unchanged".
        """
        started = utcnow_iso()
        with self._lock:
            con = self._connect()
            try:
                cur = con.execute(
                    "INSERT INTO snapshots (book, sport, started_at, ok)"
                    " VALUES (?,?,?,0)", (book, sport, started))
                snap_id = cur.lastrowid
                con.commit()
            finally:
                con.close()
        writer = SnapshotWriter(self, snap_id, book, sport, started)
        try:
            yield writer
        except BaseException as exc:
            writer.fail(f"{type(exc).__name__}: {exc}")
            raise
        else:
            if not writer.closed:
                writer.commit()

    # ── read path ─────────────────────────────────────────────────────────────

    def snapshot_at(self, book: str, sport: str, when: str | None = None, *,
                    snapshot_id: int | None = None) -> list[dict]:
        """The full board as it stood at `when` — the row set the naive
        snapshot schema would have stored, rebuilt from the change log.

        `when` is an ISO timestamp; the board is the last successful snapshot at
        or before it. Pass `snapshot_id` instead to name a pass exactly, which
        is what analysis wants and what side-steps the ambiguity when two passes
        of one board share a second.

        Every position of that (book, sport) takes its most recent price at or
        before the chosen snapshot; positions whose most recent value is NULL
        (gone or suspended) are omitted.
        """
        con = self._connect()
        try:
            if snapshot_id is not None:
                row = con.execute(
                    "SELECT id, started_at FROM snapshots WHERE id=?",
                    (snapshot_id,)).fetchone()
            else:
                row = con.execute(
                    "SELECT id, started_at FROM snapshots"
                    " WHERE book=? AND sport=? AND ok=1 AND started_at<=?"
                    " ORDER BY started_at DESC, id DESC LIMIT 1",
                    (book, sport, when or "9999")).fetchone()
            if row is None:
                return []
            snap_id = row[0]
            q = """
            SELECT e.league, e.home, e.away, e.start_time,
                   m.name, s.label, p.line, o.price
              FROM positions p
              JOIN events  e ON e.id = p.event_id
              JOIN markets m ON m.id = p.market_id
              JOIN sides   s ON s.id = p.side_id
              JOIN odds    o ON o.position_id = p.id
             WHERE e.book = ? AND e.sport = ?
               AND o.snapshot_id = (SELECT MAX(o2.snapshot_id) FROM odds o2
                                     WHERE o2.position_id = p.id
                                       AND o2.snapshot_id <= ?)
               AND o.price IS NOT NULL
            """
            cols = ("league", "home", "away", "start_time", "position", "side",
                    "line", "price")
            out = []
            for r in con.execute(q, (book, sport, snap_id)):
                d = dict(zip(cols, r))
                d["odds"] = decode_price(d.pop("price"))
                d["book"], d["sport"] = book, sport
                d["record_time"] = row[1]
                out.append(d)
            return out
        finally:
            con.close()

    def recent_snapshots(self, limit: int = 20) -> list[dict]:
        con = self._connect()
        try:
            cols = ("id", "book", "sport", "started_at", "finished_at", "ok",
                    "n_events", "n_positions", "n_new", "n_moved", "n_gone",
                    "n_dupes", "bytes", "dur_ms", "error")
            q = f"SELECT {','.join(cols)} FROM snapshots ORDER BY id DESC LIMIT ?"
            return [dict(zip(cols, r)) for r in con.execute(q, (limit,))]
        finally:
            con.close()

    def last_snapshot_start(self, book: str, sport: str) -> Optional[str]:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT started_at FROM snapshots WHERE book=? AND sport=?"
                " ORDER BY id DESC LIMIT 1", (book, sport)).fetchone()
            return row[0] if row else None
        finally:
            con.close()

    def stats(self) -> dict:
        con = self._connect()
        try:
            def one(q: str, *a):
                return con.execute(q, a).fetchone()[0]
            size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
            return {
                "db_bytes": size,
                "events": one("SELECT COUNT(*) FROM events"),
                "positions": one("SELECT COUNT(*) FROM positions"),
                "markets": one("SELECT COUNT(*) FROM markets"),
                "sides": one("SELECT COUNT(*) FROM sides"),
                "odds_rows": one("SELECT COUNT(*) FROM odds"),
                "snapshots": one("SELECT COUNT(*) FROM snapshots"),
                "snapshots_ok": one("SELECT COUNT(*) FROM snapshots WHERE ok=1"),
            }
        finally:
            con.close()

    # ── retention ─────────────────────────────────────────────────────────────

    def drop_snapshots(self, ids: Iterable[int]) -> dict:
        """Undo one or more passes: delete their ticks and rebuild `latest`.

        A pass that read the board wrongly does not just add noise, it moves
        the baseline — `latest` is what the NEXT pass diffs against, so a bad
        pass makes the following one wrong too. Removing the ticks is therefore
        only half the job; `latest` has to be recomputed from whatever the most
        recent surviving tick for each position now is.

        Positions whose only ticks came from the dropped passes lose their
        `latest` row entirely, which is correct: as far as the store is
        concerned they have never been priced.
        """
        ids = [int(i) for i in ids]
        if not ids:
            return {"snapshots": 0, "odds": 0, "latest": 0}
        marks = ",".join("?" * len(ids))
        with self._lock:
            con = self._connect()
            try:
                touched = [r[0] for r in con.execute(
                    f"SELECT DISTINCT position_id FROM odds"
                    f" WHERE snapshot_id IN ({marks})", ids)]
                n_odds = con.execute(
                    f"SELECT COUNT(*) FROM odds WHERE snapshot_id IN ({marks})",
                    ids).fetchone()[0]
                con.execute(f"DELETE FROM odds WHERE snapshot_id IN ({marks})", ids)
                con.execute(f"DELETE FROM snapshots WHERE id IN ({marks})", ids)
                con.execute(
                    "CREATE TEMP TABLE _touch (id INTEGER PRIMARY KEY)")
                con.executemany("INSERT INTO _touch (id) VALUES (?)",
                                [(p,) for p in touched])
                con.execute("DELETE FROM latest WHERE position_id IN"
                            " (SELECT id FROM _touch)")
                con.execute("""
                    INSERT INTO latest (position_id, price, snapshot_id)
                    SELECT o.position_id, o.price, o.snapshot_id FROM odds o
                     WHERE o.position_id IN (SELECT id FROM _touch)
                       AND o.snapshot_id = (SELECT MAX(o2.snapshot_id) FROM odds o2
                                             WHERE o2.position_id = o.position_id)""")
                n_latest = con.execute(
                    "SELECT COUNT(*) FROM latest WHERE position_id IN"
                    " (SELECT id FROM _touch)").fetchone()[0]
                con.execute("DROP TABLE _touch")
                con.commit()
            finally:
                con.close()
        return {"snapshots": len(ids), "odds": n_odds,
                "positions_touched": len(touched), "latest": n_latest}

    def purge_players(self) -> dict:
        """Delete player-scoped positions and their history.

        The collectors skip these at source, so this is for a database that was
        filled before that — half the CrystalBet soccer board — rather than
        something the loop needs. It reuses the same detector the collectors
        use, registered as a SQLite function, so the two can never drift.

        A market or a side is enough on its own: `is_player_market` ORs the
        two, and evaluating them separately over the dimension tables is a few
        thousand calls instead of one per position.
        """
        from sportsdatamovement.players import is_player_market

        with self._lock:
            con = self._connect()
            try:
                con.create_function(
                    "sdm_is_player", 2,
                    lambda m, s: 1 if is_player_market(market=m or "",
                                                       side=s or "") else 0)
                con.execute("CREATE TEMP TABLE _pm AS SELECT id FROM markets"
                            " WHERE sdm_is_player(name, '')")
                con.execute("CREATE TEMP TABLE _ps AS SELECT id FROM sides"
                            " WHERE sdm_is_player('', label)")
                con.execute(
                    "CREATE TEMP TABLE _dead AS SELECT id FROM positions"
                    " WHERE market_id IN (SELECT id FROM _pm)"
                    "    OR side_id IN (SELECT id FROM _ps)")
                n_pos = con.execute("SELECT COUNT(*) FROM _dead").fetchone()[0]
                n_odds = con.execute(
                    "SELECT COUNT(*) FROM odds WHERE position_id IN"
                    " (SELECT id FROM _dead)").fetchone()[0]
                con.execute("DELETE FROM odds WHERE position_id IN (SELECT id FROM _dead)")
                con.execute("DELETE FROM latest WHERE position_id IN (SELECT id FROM _dead)")
                con.execute("DELETE FROM positions WHERE id IN (SELECT id FROM _dead)")
                n_mkt = con.execute(
                    "DELETE FROM markets WHERE id NOT IN"
                    " (SELECT DISTINCT market_id FROM positions)").rowcount
                n_side = con.execute(
                    "DELETE FROM sides WHERE id NOT IN"
                    " (SELECT DISTINCT side_id FROM positions)").rowcount
                for t in ("_pm", "_ps", "_dead"):
                    con.execute(f"DROP TABLE {t}")
                con.commit()
            finally:
                con.close()
        return {"positions": n_pos, "odds": n_odds,
                "markets": max(0, n_mkt), "sides": max(0, n_side)}

    def prune(self, keep_days: float) -> dict:
        """Drop events whose last_seen is older than `keep_days`, and everything
        hanging off them.

        Events churn: roughly 2,000 new fixtures a day arrive and the same
        number kick off and vanish. Without this the positions table grows
        without bound even though the live board does not.
        """
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=keep_days)).isoformat(timespec="seconds")
        with self._lock:
            con = self._connect()
            try:
                ev = [r[0] for r in con.execute(
                    "SELECT id FROM events WHERE last_seen < ?", (cutoff,))]
                if not ev:
                    return {"events": 0, "positions": 0, "odds": 0,
                            "markets": 0, "sides": 0}
                con.execute("CREATE TEMP TABLE _dead (id INTEGER PRIMARY KEY)")
                con.executemany("INSERT INTO _dead (id) VALUES (?)",
                                [(e,) for e in ev])
                pos = [r[0] for r in con.execute(
                    "SELECT id FROM positions WHERE event_id IN (SELECT id FROM _dead)")]
                con.execute("CREATE TEMP TABLE _deadp (id INTEGER PRIMARY KEY)")
                con.executemany("INSERT INTO _deadp (id) VALUES (?)",
                                [(p,) for p in pos])
                n_odds = con.execute(
                    "SELECT COUNT(*) FROM odds WHERE position_id IN"
                    " (SELECT id FROM _deadp)").fetchone()[0]
                con.execute("DELETE FROM odds WHERE position_id IN (SELECT id FROM _deadp)")
                con.execute("DELETE FROM latest WHERE position_id IN (SELECT id FROM _deadp)")
                con.execute("DELETE FROM positions WHERE id IN (SELECT id FROM _deadp)")
                con.execute("DELETE FROM events WHERE id IN (SELECT id FROM _dead)")
                con.execute("DROP TABLE _dead")
                con.execute("DROP TABLE _deadp")
                # Orphaned dimension rows. Both books encode participants into
                # the market identity — CrystalBet in the title ("Anytime
                # goalscorer & correct score Soula, Mazire (PFC Levski Sofia)"),
                # Lider in the specifier ("player=sr:player:12345") — so
                # `markets` carries tens of thousands of rows that are tied to
                # specific fixtures in practice. Without this they outlive the
                # events forever.
                n_mkt = con.execute(
                    "DELETE FROM markets WHERE id NOT IN"
                    " (SELECT DISTINCT market_id FROM positions)").rowcount
                n_side = con.execute(
                    "DELETE FROM sides WHERE id NOT IN"
                    " (SELECT DISTINCT side_id FROM positions)").rowcount
                con.commit()
            finally:
                con.close()
        return {"events": len(ev), "positions": len(pos), "odds": n_odds,
                "markets": max(0, n_mkt), "sides": max(0, n_side)}


class SnapshotWriter:
    """One board pass, staged to disk and diffed in SQL.

    WHY NOT IN MEMORY
    -----------------
    The obvious writer accumulates the pass as Python objects and diffs it
    against a dict of the current board. Both halves of that are unaffordable at
    this scale: CrystalBet's soccer board alone is ~3.4 M positions, so the row
    buffer is gigabytes and the board dict is gigabytes again — per sport, with
    three sports collected concurrently. It works beautifully on a 2-game smoke
    run and dies at 3am on the real board.

    So rows stream into a TEMP table as they arrive (temp_store=FILE, so the
    staging cost is disk, not RAM) and the entire diff — intern the dimensions,
    create missing positions, find what changed, find what vanished — runs as a
    handful of set-based statements. Memory is flat in the size of the board,
    and the whole thing is one transaction: a pass either lands or it doesn't.

    The staging tables are TEMP, which in SQLite means private to this
    connection and dropped with it. That is also what lets several sports commit
    concurrently without stepping on each other: staging touches no shared page,
    so only the final transaction serialises.
    """

    BATCH = 20_000

    def __init__(self, store: Store, snap_id: int, book: str, sport: str,
                 started_at: str):
        self.store = store
        self.id = snap_id
        self.book = book
        self.sport = sport
        self.started_at = started_at
        self.closed = False
        self.bytes = 0
        self.n_listed = 0
        self._buf: list[tuple] = []
        self._con = store._connect(autocommit=True)
        self._con.executescript("""
            CREATE TEMP TABLE _stage (
              event_key TEXT, market_key TEXT, market TEXT, side TEXT,
              line REAL, price INTEGER, ref
            );
            CREATE TEMP TABLE _ev (
              event_key TEXT PRIMARY KEY, league TEXT, home TEXT, away TEXT,
              start_time TEXT, was_read INTEGER NOT NULL DEFAULT 0
            );
        """)

    # ── input ─────────────────────────────────────────────────────────────────

    def add_event(self, event_key: str, *, league: str | None = None,
                  home: str | None = None, away: str | None = None,
                  start_time: str | None = None) -> None:
        self._con.execute(
            "INSERT INTO _ev (event_key, league, home, away, start_time)"
            " VALUES (?,?,?,?,?) ON CONFLICT(event_key) DO UPDATE SET"
            " league=COALESCE(excluded.league, league),"
            " home=COALESCE(excluded.home, home),"
            " away=COALESCE(excluded.away, away),"
            " start_time=COALESCE(excluded.start_time, start_time)",
            (str(event_key), league, home, away, start_time))

    def mark_read(self, event_key: str) -> None:
        """This event's market list was read in full.

        Only events marked here take part in the disappearance diff. A
        CrystalBet expand that fails — and several percent do when the session
        is bounced mid-pass — leaves an event listed but unread, and without
        this every one of its positions would be booked as pulled from the board
        and then re-created on the next pass. That is thousands of phantom
        movements per failure, in the exact table the study reads.
        """
        self._con.execute(
            "INSERT INTO _ev (event_key, was_read) VALUES (?,1)"
            " ON CONFLICT(event_key) DO UPDATE SET was_read=1", (str(event_key),))

    def add(self, event_key: str, market_key: str, market: str, side: str,
            odds: Optional[float], *, line: Optional[float] = None,
            ref: str | None = None) -> None:
        """One priced (or suspended) position. `odds=None` means not bettable —
        see the module docstring on locked cells."""
        self._buf.append((str(event_key), str(market_key), market, side, line,
                          encode_price(odds), _pack_ref(ref)))
        if len(self._buf) >= self.BATCH:
            self._flush()

    def _flush(self) -> None:
        if not self._buf:
            return
        self._con.executemany(
            "INSERT INTO _stage (event_key, market_key, market, side, line,"
            " price, ref) VALUES (?,?,?,?,?,?,?)", self._buf)
        self._buf.clear()

    def count_bytes(self, n: int) -> None:
        """Transport cost, recorded so the snapshot's true price is visible."""
        self.bytes += int(n)

    # ── terminal ──────────────────────────────────────────────────────────────

    def _dur_ms(self) -> int:
        try:
            t0 = datetime.fromisoformat(self.started_at)
        except ValueError:
            return 0
        return int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)

    def _close_con(self) -> None:
        try:
            self._con.close()
        except Exception:
            pass

    def fail(self, error: str) -> None:
        if self.closed:
            return
        self.closed = True
        self._close_con()
        with self.store._lock:
            con = self.store._connect()
            try:
                con.execute(
                    "UPDATE snapshots SET finished_at=?, ok=0, error=?,"
                    " dur_ms=? WHERE id=?",
                    (utcnow_iso(), str(error)[:500], self._dur_ms(), self.id))
                con.commit()
            finally:
                con.close()

    def commit(self, *, ok: bool = True, prune_missing: bool = True) -> dict:
        """Diff against the previous pass and persist only what changed.

        `ok` says the LIST was read in full, which is what licenses concluding
        that an event absent from it has left the board. It is separate from
        whether each event's markets were read — see `mark_read`.
        """
        if self.closed:
            raise RuntimeError("snapshot already closed")
        self.closed = True
        ts = utcnow_iso()
        con = self._con
        try:
            self._flush()
            with self.store._lock:
                con.execute("BEGIN IMMEDIATE")
                stats = self._diff(con, ts, ok=ok, prune_missing=prune_missing)
                con.execute(
                    "UPDATE snapshots SET finished_at=?, ok=?, n_events=?,"
                    " n_positions=?, n_new=?, n_moved=?, n_gone=?, n_dupes=?,"
                    " bytes=?, dur_ms=? WHERE id=?",
                    (ts, 1 if ok else 0, stats["n_events"], stats["n_positions"],
                     stats["n_new"], stats["n_moved"], stats["n_gone"],
                     stats["n_dupes"], self.bytes, self._dur_ms(), self.id))
                con.execute("COMMIT")
        finally:
            self._close_con()
        stats["snapshot_id"] = self.id
        stats["bytes"] = self.bytes
        return stats

    # ── the diff ──────────────────────────────────────────────────────────────

    def _diff(self, con, ts: str, *, ok: bool, prune_missing: bool) -> dict:
        book, sport, snap = self.book, self.sport, self.id
        ex = con.execute

        # 1. Events. INSERT then UPDATE rather than upsert-with-COALESCE so an
        #    event that vanishes from the feed's metadata keeps the names we
        #    already had.
        ex("INSERT OR IGNORE INTO events (book, sport, event_key, league, home,"
           " away, start_time, first_seen, last_seen)"
           " SELECT ?,?,event_key,league,home,away,start_time,?,? FROM _ev",
           (book, sport, ts, ts))
        ex("UPDATE events SET last_seen=?,"
           "  league=COALESCE((SELECT league FROM _ev v WHERE v.event_key=events.event_key), league),"
           "  home=COALESCE((SELECT home FROM _ev v WHERE v.event_key=events.event_key), home),"
           "  away=COALESCE((SELECT away FROM _ev v WHERE v.event_key=events.event_key), away),"
           "  start_time=COALESCE((SELECT start_time FROM _ev v WHERE v.event_key=events.event_key), start_time)"
           " WHERE book=? AND sport=? AND event_key IN (SELECT event_key FROM _ev)",
           (ts, book, sport))

        # 2. Dimensions.
        ex("INSERT OR IGNORE INTO markets (book, market_key, name)"
           " SELECT DISTINCT ?, market_key, market FROM _stage", (book,))
        ex("INSERT OR IGNORE INTO sides (label)"
           " SELECT DISTINCT side FROM _stage")

        # 3. Resolve every staged row to a position, creating the ones that are
        #    new. The two-step (create, then resolve) is what lets this be set
        #    based — there is no per-row SELECT anywhere.
        ex("""CREATE TEMP TABLE _res AS
              SELECT st.rowid AS srow, e.id AS eid, m.id AS mid, s.id AS sid,
                     st.line AS line, st.price AS price, st.ref AS ref
                FROM _stage st
                JOIN events  e ON e.book=? AND e.sport=? AND e.event_key=st.event_key
                JOIN markets m ON m.book=? AND m.market_key=st.market_key
                               AND m.name=st.market
                JOIN sides   s ON s.label=st.side""",
           (book, sport, book))
        ex("INSERT OR IGNORE INTO positions (event_id, market_id, side_id, line,"
           " ref, first_snapshot)"
           " SELECT eid, mid, sid, line, ref, ? FROM _res", (snap,))
        ex("""CREATE TEMP TABLE _seen AS
              SELECT p.id AS pid, r.price AS price, MIN(r.srow) AS srow
                FROM _res r
                JOIN positions p ON p.event_id=r.eid AND p.market_id=r.mid
                                AND p.side_id=r.sid
                                AND COALESCE(p.line,?)=COALESCE(r.line,?)
               GROUP BY p.id""", (NO_LINE, NO_LINE))
        ex("CREATE UNIQUE INDEX _seen_pid ON _seen(pid)")

        n_positions = ex("SELECT COUNT(*) FROM _seen").fetchone()[0]
        n_staged = ex("SELECT COUNT(*) FROM _res").fetchone()[0]
        # Two staged rows resolving to one position is a collector key bug, not
        # a price move. Left alone it books the difference between two unrelated
        # bets as movement — which is how a first-ever Lider pass came to report
        # 1,856 moves out of 5,571 positions (see liderbet.split_specifier).
        # GROUP BY above keeps the first; this counts what it dropped.
        n_dupes = n_staged - n_positions

        # 4. What changed. A position seen for the first time only earns a row
        #    if it is actually priced — one that shows up already suspended has
        #    no price history to start.
        n_new = ex(
            "SELECT COUNT(*) FROM _seen s LEFT JOIN latest l"
            " ON l.position_id=s.pid"
            " WHERE l.position_id IS NULL AND s.price IS NOT NULL").fetchone()[0]
        n_moved = ex(
            "SELECT COUNT(*) FROM _seen s JOIN latest l ON l.position_id=s.pid"
            " WHERE l.price IS NOT s.price AND s.price IS NOT NULL").fetchone()[0]
        n_pulled = ex(
            "SELECT COUNT(*) FROM _seen s JOIN latest l ON l.position_id=s.pid"
            " WHERE l.price IS NOT s.price AND s.price IS NULL").fetchone()[0]
        ex("INSERT OR REPLACE INTO odds (position_id, snapshot_id, price)"
           " SELECT s.pid, ?, s.price FROM _seen s"
           " LEFT JOIN latest l ON l.position_id=s.pid"
           " WHERE (l.position_id IS NULL AND s.price IS NOT NULL)"
           "    OR (l.position_id IS NOT NULL AND l.price IS NOT s.price)",
           (snap,))

        # 5. Disappearance, at two granularities — see mark_read.
        #
        #      read events    a position missing from an event whose market
        #                     list we read in full really was pulled;
        #      listed events  an event absent from a COMPLETE list read has
        #                     left the board, and everything on it goes too;
        #      everything else (listed but unread, or any event at all when the
        #                     list read failed) is left exactly as it was.
        #                     Not read is not gone.
        n_gone = n_pulled
        if prune_missing and n_positions:
            gone_sql = """
                SELECT l.position_id FROM latest l
                  JOIN positions p ON p.id=l.position_id
                  JOIN events e ON e.id=p.event_id
                 WHERE e.book=? AND e.sport=? AND l.price IS NOT NULL
                   AND l.position_id NOT IN (SELECT pid FROM _seen)
                   AND (e.event_key IN (SELECT event_key FROM _ev WHERE was_read=1)
                        OR (? AND e.event_key NOT IN (SELECT event_key FROM _ev)))
            """
            args = (book, sport, 1 if ok else 0)
            n_gone += ex(f"SELECT COUNT(*) FROM ({gone_sql})", args).fetchone()[0]
            ex(f"INSERT OR REPLACE INTO odds (position_id, snapshot_id, price)"
               f" SELECT position_id, ?, NULL FROM ({gone_sql})", (snap,) + args)

        # 6. `latest` mirrors whatever this snapshot wrote.
        ex("INSERT OR REPLACE INTO latest (position_id, price, snapshot_id)"
           " SELECT position_id, price, snapshot_id FROM odds WHERE snapshot_id=?",
           (snap,))

        n_events = ex("SELECT COUNT(*) FROM _ev").fetchone()[0]
        ex("DROP TABLE _res")
        ex("DROP TABLE _seen")
        return {"n_events": n_events, "n_positions": n_positions,
                "n_new": n_new, "n_moved": n_moved, "n_gone": n_gone,
                "n_dupes": n_dupes}


# ── module-level singleton ────────────────────────────────────────────────────
_store: Optional[Store] = None
_store_lock = threading.Lock()


def get_store(path: str | Path | None = None) -> Store:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = Store(path or os.environ.get("SDM_DB_PATH")
                               or DEFAULT_DB_PATH)
    return _store


def _reset_for_tests() -> None:
    global _store
    with _store_lock:
        _store = None
