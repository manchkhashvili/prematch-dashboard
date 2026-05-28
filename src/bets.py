"""
Bet tracker: SQLite persistence + DAO functions.

Phase 3.5–3.6 (2026-05-27). The bet tracker is the first piece of the prematch
dashboard that needs durable cross-restart state, so it gets its own SQLite
file at `prematch/data/bets.db` rather than piggybacking on the in-memory
state or the JSON cache_persistence used for scraper warm-up.

Schema
------
Two tables, one foreign-key relationship:

  bets — one row per placed wager.
    id                INTEGER PK AUTOINCREMENT
    placed_at         TEXT NOT NULL          ISO-8601 UTC
    sport             TEXT NOT NULL
    cb_event_id       TEXT                   nullable; off-platform bets have no event id
    match_label       TEXT NOT NULL          "Home vs Away" snapshot for display
    period            TEXT NOT NULL          "FT" | "H1" | "Q1"... (matches Odds.period)
    market_type       TEXT NOT NULL          moneyline | spread | total | team_total | corners
    line              REAL                   nullable; null for ML
    side              TEXT NOT NULL          home | away | draw | over | under
    submarket         TEXT                   corners | None (for soccer corner markets)
    team_side         TEXT                   home | away | None (for team_total)
    book              TEXT NOT NULL          cb | pin | other
    odds_taken        REAL NOT NULL          decimal
    stake             REAL NOT NULL
    bankroll_at_time  REAL NOT NULL
    pin_fair_at_placement REAL               snapshot Pin no-vig fair at bet time
    cb_fair_at_placement  REAL               snapshot CB odds at bet time (== odds_taken if book=cb)
    edge_at_placement_pct REAL               snapshot edge at bet time
    status            TEXT NOT NULL DEFAULT 'open'   open | won | lost | pushed | void
    settled_at        TEXT                   ISO-8601 UTC; nullable until settled
    payout            REAL                   final payout (stake×odds, 0, or stake)
    pin_fair_closing  REAL                   Pin no-vig fair at kickoff; set automatically
    note              TEXT
    start_time        TEXT                   match start_time ISO; for CLV cutoff & ordering

  bet_odds_history — periodic snapshots of CB + Pin fair for each OPEN bet.
    Populated by `record_history_snapshot()` after every Pinnacle poll cycle.
    Stops once a bet is settled.
    id                INTEGER PK AUTOINCREMENT
    bet_id            INTEGER NOT NULL REFERENCES bets(id) ON DELETE CASCADE
    recorded_at       TEXT NOT NULL          ISO-8601 UTC
    cb_decimal        REAL                   current CB odds for the bet's market+side
    pin_fair_decimal  REAL                   current Pin no-vig fair for same
    UNIQUE(bet_id, recorded_at)

Concurrency
-----------
SQLite is opened with `check_same_thread=False` and a single connection per
process, guarded by a threading.Lock on every write. Reads are also locked
because SQLite's cursors aren't fully thread-safe under WAL anyway. This is
plenty for a single-user local dashboard — bets are placed manually so the
write rate is essentially zero.

Public API
----------
  init_db(path=None)                      Create tables if missing. Idempotent.
  create_bet(**fields) -> int             Insert + return new id.
  list_bets(status=None) -> list[dict]    Filter by 'open'/'settled'/'all' (None = all).
  get_bet(bet_id) -> dict | None
  update_bet(bet_id, **fields) -> bool    Partial update.
  settle_bet(bet_id, outcome, payout=None) Mark won/lost/pushed/void + payout.
  delete_bet(bet_id) -> bool              Hard delete (also kills history rows).
  record_history_snapshot(bet_id, ...)    Insert one history row.
  get_history(bet_id) -> list[dict]       All history for a bet, oldest first.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Path resolution: caller-provided > env override > default. The env knob lets
# tests and ops point at a different file without touching code. Set via
# `BETS_DB_PATH=/tmp/bets.db` before launching uvicorn.
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "bets.db"


def _resolve_db_path(path: Path | None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get("BETS_DB_PATH")
    if env:
        return Path(env)
    return DEFAULT_DB_PATH

VALID_STATUSES = ("open", "won", "lost", "pushed", "void")
VALID_BOOKS = ("cb", "pin", "other")

_conn: sqlite3.Connection | None = None
_conn_lock = threading.Lock()
_current_db_path: Path | None = None


# ── Connection management ─────────────────────────────────────────────────────
def init_db(path: Path | None = None) -> None:
    """Open (or re-open) the connection at `path` and create tables if missing.

    Idempotent — safe to call repeatedly. Tests use this with a temp path to
    isolate from the real bets.db.
    """
    global _conn, _current_db_path
    db_path = _resolve_db_path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _conn_lock:
        if _conn is not None and _current_db_path == db_path:
            return  # already open at this path
        if _conn is not None:
            _conn.close()
        _conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,  # autocommit; we use explicit transactions where needed
        )
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA foreign_keys = ON")
        # WAL is best-effort: it works on local APFS/ext4 but some FUSE mounts
        # (CI sandboxes, network shares) don't support the shared-memory mmap
        # WAL needs. Fall back silently to the default journal mode.
        try:
            _conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError as e:
            log.debug("WAL not available (%s) — using default journal mode", e)
        _current_db_path = db_path
        _create_schema(_conn)


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            placed_at TEXT NOT NULL,
            sport TEXT NOT NULL,
            cb_event_id TEXT,
            match_label TEXT NOT NULL,
            period TEXT NOT NULL,
            market_type TEXT NOT NULL,
            line REAL,
            side TEXT NOT NULL,
            submarket TEXT,
            team_side TEXT,
            book TEXT NOT NULL,
            odds_taken REAL NOT NULL,
            stake REAL NOT NULL,
            bankroll_at_time REAL NOT NULL,
            pin_fair_at_placement REAL,
            cb_fair_at_placement REAL,
            edge_at_placement_pct REAL,
            status TEXT NOT NULL DEFAULT 'open',
            settled_at TEXT,
            payout REAL,
            pin_fair_closing REAL,
            note TEXT,
            start_time TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_bets_status ON bets(status);
        CREATE INDEX IF NOT EXISTS idx_bets_placed_at ON bets(placed_at);
        CREATE INDEX IF NOT EXISTS idx_bets_event ON bets(cb_event_id);

        CREATE TABLE IF NOT EXISTS bet_odds_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bet_id INTEGER NOT NULL REFERENCES bets(id) ON DELETE CASCADE,
            recorded_at TEXT NOT NULL,
            cb_decimal REAL,
            pin_fair_decimal REAL,
            UNIQUE(bet_id, recorded_at)
        );
        CREATE INDEX IF NOT EXISTS idx_history_bet ON bet_odds_history(bet_id);
    """)


def _require_conn() -> sqlite3.Connection:
    if _conn is None:
        init_db()
    assert _conn is not None
    return _conn


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


# ── CRUD: bets ────────────────────────────────────────────────────────────────
_BET_INSERT_FIELDS = (
    "placed_at", "sport", "cb_event_id", "match_label", "period", "market_type",
    "line", "side", "submarket", "team_side", "book", "odds_taken", "stake",
    "bankroll_at_time", "pin_fair_at_placement", "cb_fair_at_placement",
    "edge_at_placement_pct", "status", "note", "start_time",
)


def create_bet(**fields: Any) -> int:
    """Insert a new bet. Required: sport, match_label, period, market_type, side, book,
    odds_taken, stake, bankroll_at_time. Returns the new row id."""
    required = (
        "sport", "match_label", "period", "market_type", "side", "book",
        "odds_taken", "stake", "bankroll_at_time",
    )
    missing = [k for k in required if k not in fields or fields[k] is None]
    if missing:
        raise ValueError(f"create_bet missing required fields: {missing}")
    if fields["book"] not in VALID_BOOKS:
        raise ValueError(f"book must be one of {VALID_BOOKS}; got {fields['book']!r}")
    if float(fields["odds_taken"]) <= 1.0:
        raise ValueError(f"odds_taken must be > 1.0; got {fields['odds_taken']}")
    if float(fields["stake"]) <= 0:
        raise ValueError(f"stake must be > 0; got {fields['stake']}")

    payload = {k: fields.get(k) for k in _BET_INSERT_FIELDS}
    payload["placed_at"] = payload.get("placed_at") or _now_iso()
    payload["status"] = payload.get("status") or "open"
    if payload["status"] not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}; got {payload['status']!r}")

    cols = ", ".join(_BET_INSERT_FIELDS)
    placeholders = ", ".join(f":{k}" for k in _BET_INSERT_FIELDS)
    conn = _require_conn()
    with _conn_lock:
        cur = conn.execute(f"INSERT INTO bets ({cols}) VALUES ({placeholders})", payload)
        return cur.lastrowid


def get_bet(bet_id: int) -> dict | None:
    conn = _require_conn()
    with _conn_lock:
        row = conn.execute("SELECT * FROM bets WHERE id = ?", (bet_id,)).fetchone()
    return dict(row) if row else None


def list_bets(status: str | None = None) -> list[dict]:
    """List bets. status=None returns all; 'open' / 'settled' / a specific terminal
    state ('won','lost','pushed','void') filters accordingly."""
    conn = _require_conn()
    with _conn_lock:
        if status is None or status == "all":
            rows = conn.execute(
                "SELECT * FROM bets ORDER BY placed_at DESC"
            ).fetchall()
        elif status == "open":
            rows = conn.execute(
                "SELECT * FROM bets WHERE status = 'open' ORDER BY start_time ASC, placed_at DESC"
            ).fetchall()
        elif status == "settled":
            rows = conn.execute(
                "SELECT * FROM bets WHERE status != 'open' ORDER BY settled_at DESC"
            ).fetchall()
        elif status in VALID_STATUSES:
            rows = conn.execute(
                "SELECT * FROM bets WHERE status = ? ORDER BY settled_at DESC",
                (status,),
            ).fetchall()
        else:
            raise ValueError(f"unknown status filter {status!r}")
    return [dict(r) for r in rows]


def update_bet(bet_id: int, **fields: Any) -> bool:
    """Partial update — only the provided fields are set. Returns True if a row changed."""
    allowed = {
        "note", "pin_fair_closing", "status", "settled_at", "payout",
        "cb_event_id", "start_time",
    }
    bad = [k for k in fields if k not in allowed]
    if bad:
        raise ValueError(f"update_bet: cannot modify {bad}")
    if not fields:
        return False
    if "status" in fields and fields["status"] not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}; got {fields['status']!r}")
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    fields["id"] = bet_id
    conn = _require_conn()
    with _conn_lock:
        cur = conn.execute(f"UPDATE bets SET {sets} WHERE id = :id", fields)
        return cur.rowcount > 0


def settle_bet(
    bet_id: int,
    outcome: str,
    payout: float | None = None,
) -> bool:
    """Mark a bet won/lost/pushed/void. If payout not given, computes from outcome:
        won    → stake × odds_taken
        lost   → 0
        pushed → stake
        void   → stake
    Returns True if the bet existed and was updated.
    """
    if outcome not in ("won", "lost", "pushed", "void"):
        raise ValueError(f"settle outcome must be won|lost|pushed|void; got {outcome!r}")
    bet = get_bet(bet_id)
    if not bet:
        return False
    if bet["status"] != "open":
        raise ValueError(f"bet {bet_id} already settled as {bet['status']}")
    if payout is None:
        stake = bet["stake"]
        odds = bet["odds_taken"]
        if outcome == "won":
            payout = stake * odds
        elif outcome == "lost":
            payout = 0.0
        else:  # push or void
            payout = stake
    return update_bet(
        bet_id,
        status=outcome,
        payout=float(payout),
        settled_at=_now_iso(),
    )


def delete_bet(bet_id: int) -> bool:
    conn = _require_conn()
    with _conn_lock:
        cur = conn.execute("DELETE FROM bets WHERE id = ?", (bet_id,))
        return cur.rowcount > 0


# ── History tracking ─────────────────────────────────────────────────────────
def record_history_snapshot(
    bet_id: int,
    cb_decimal: float | None,
    pin_fair_decimal: float | None,
    recorded_at: str | None = None,
) -> None:
    """Insert one history row. Silently no-ops if both cb and pin are None
    (nothing to record) or if a row at this timestamp already exists for the
    bet (the UNIQUE constraint enforces this)."""
    if cb_decimal is None and pin_fair_decimal is None:
        return
    ts = recorded_at or _now_iso()
    conn = _require_conn()
    with _conn_lock:
        try:
            conn.execute(
                "INSERT INTO bet_odds_history (bet_id, recorded_at, cb_decimal, pin_fair_decimal) "
                "VALUES (?, ?, ?, ?)",
                (bet_id, ts, cb_decimal, pin_fair_decimal),
            )
        except sqlite3.IntegrityError:
            # Duplicate timestamp for this bet — ignore. Happens if the
            # recorder is triggered twice in the same second.
            pass


def get_history(bet_id: int) -> list[dict]:
    conn = _require_conn()
    with _conn_lock:
        rows = conn.execute(
            "SELECT recorded_at, cb_decimal, pin_fair_decimal "
            "FROM bet_odds_history WHERE bet_id = ? ORDER BY recorded_at ASC",
            (bet_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def open_bet_ids() -> list[int]:
    """Convenience: all currently-open bet ids. Used by the history-recorder hook."""
    conn = _require_conn()
    with _conn_lock:
        rows = conn.execute(
            "SELECT id FROM bets WHERE status = 'open'"
        ).fetchall()
    return [r["id"] for r in rows]


# ── Test/dev helpers ─────────────────────────────────────────────────────────
def _reset_for_tests() -> None:
    """Force-close the connection so the next init_db() call opens a fresh DB.
    Test fixtures call this between cases."""
    global _conn, _current_db_path
    with _conn_lock:
        if _conn is not None:
            _conn.close()
        _conn = None
        _current_db_path = None
