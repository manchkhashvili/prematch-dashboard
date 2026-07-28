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
    book              TEXT NOT NULL          free-form book/account tag (cb, pin, betlive, 1xbet, …)
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
from datetime import datetime, timedelta, timezone
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

# "cashout" (2026-06-12): settled early at a book-offered amount — payout is
# whatever the book paid, supplied by the user (no formula).
VALID_STATUSES = ("open", "won", "lost", "pushed", "void", "cashout")
# `book` is FREE-FORM: it's just the bet's account book_tag, so any account/book
# (betlive, 1xbet, a new one tomorrow) is first-class with zero code change.
# KNOWN_BOOKS is ONLY a suggestion list for the UI datalist + docs — NOT enforced.
KNOWN_BOOKS = ("cb", "pin", "liderbet", "betlive", "other")

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

        -- Parlay legs (2026-06-18): a parlay is ONE bets row (the header — one
        -- stake, combined odds = Π legs, rolled-up status/payout) plus N rows
        -- here, one per game. A single bet has zero leg rows and is unchanged.
        -- Keeping a parlay as one bets row is what keeps the capital invariant
        -- (one stake, one payout) correct with no double-counted exposure.
        CREATE TABLE IF NOT EXISTS bet_legs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bet_id INTEGER NOT NULL REFERENCES bets(id) ON DELETE CASCADE,
            leg_index INTEGER NOT NULL,
            sport TEXT,
            cb_event_id TEXT,
            match_label TEXT NOT NULL,
            period TEXT,
            market_type TEXT,
            line REAL,
            side TEXT NOT NULL,
            submarket TEXT,
            team_side TEXT,
            start_time TEXT,
            odds REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            UNIQUE(bet_id, leg_index)
        );
        CREATE INDEX IF NOT EXISTS idx_legs_bet ON bet_legs(bet_id);

        -- Capital tracker (src/capital.py): where the bankroll lives.
        -- accounts = places money sits (a book, the bank, cash). book_tag
        -- optionally links an account to the bets.book value so legacy bets
        -- (placed before bets.account_id existed) attribute automatically.
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            book_tag TEXT,
            created_at TEXT NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0,
            -- 1 = a "dividend" sink: profit pulled OUT of the operation. Excluded
            -- from working capital/equity; the Total is reduced by what sits here.
            is_dividend INTEGER NOT NULL DEFAULT 0,
            -- manually-tracked open-bet exposure (money tied up in unsettled bets
            -- you didn't log individually). Adds to open_stake; reduces free balance.
            manual_open_stake REAL NOT NULL DEFAULT 0
        );
        -- ledger = signed money movements that are NOT bet results:
        -- opening (starting capital), deposit, withdraw, transfer (paired
        -- rows), adjustment (manual correction / bonus). Bet PnL is computed
        -- from the bets table, never duplicated here.
        CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            kind TEXT NOT NULL,
            amount REAL NOT NULL,
            note TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ledger_account ON ledger(account_id);
        -- game_marks = "I have money on this GAME" (2026-07-28).
        --
        -- Deliberately NOT a bet: a bet is one position in one market, and the
        -- question this answers is "how much am I on this fixture in total",
        -- across however many positions and books. It records no odds, no side
        -- and no settlement, and never touches PnL.
        --
        -- Two keys, because no single identifier covers every page (measured
        -- live 2026-07-28):
        --   game_key — "pin:<pin_event_id>" where the row has one. This is the
        --     real cross-book identity: every book is matched against
        --     Pinnacle, so the same fixture priced by setanta, liderbet and
        --     betlive shares one pin_event_id even though all three spell the
        --     teams differently ("Mjallby AIF" / "Mjallby" / "Mjallby Aif").
        --     A name-only key gave that one game two different keys.
        --   alt_key  — the name key (sport|home|away, normalized) computed by
        --     static/marks.js. Consistency flags carry no pin_event_id at all,
        --     and 871/1592 matches rows have no Pinnacle counterpart, so the
        --     name key is the only handle those rows have.
        -- upsert matches on EITHER key so marking a game from Arbs and then
        -- from Anomalies updates one row instead of creating two.
        --
        -- Kickoff is in neither key: books disagree on start time by up to an
        -- hour (that disagreement is the whole matching problem). start_time
        -- is stored for display and pruning only.
        CREATE TABLE IF NOT EXISTS game_marks (
            game_key    TEXT PRIMARY KEY,
            alt_key     TEXT,
            sport       TEXT,
            match_label TEXT NOT NULL,
            start_time  TEXT,
            amount      REAL,               -- nullable: marking without an amount is fine
            note        TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_marks_start ON game_marks(start_time);
        -- idx_marks_alt is created after the migration block below: on a DB
        -- whose game_marks predates alt_key, indexing it here would fail
        -- before the ALTER has had a chance to add the column.
    """)
    # Migration: bets.account_id (nullable) — which account a bet's stake/payout
    # flows through. NULL = legacy row, attributed via accounts.book_tag.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(bets)")}
    if "account_id" not in cols:
        conn.execute("ALTER TABLE bets ADD COLUMN account_id INTEGER")
    # is_parlay (2026-06-18): 1 when this bets row is a parlay header (its legs
    # live in bet_legs). 0/absent = ordinary single bet.
    if "is_parlay" not in cols:
        conn.execute("ALTER TABLE bets ADD COLUMN is_parlay INTEGER NOT NULL DEFAULT 0")
    # is_dividend (2026-06-21): mark an account as a dividend sink (money pulled
    # out of the operation, excluded from working capital).
    acct_cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)")}
    if "is_dividend" not in acct_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN is_dividend INTEGER NOT NULL DEFAULT 0")
    # manual_open_stake (2026-07-01): manually-set open-bet exposure per account.
    if "manual_open_stake" not in acct_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN manual_open_stake REAL NOT NULL DEFAULT 0")
    # game_marks.alt_key (2026-07-28): the name-key alias, added a few hours
    # after the table itself. CREATE TABLE IF NOT EXISTS does not backfill a
    # column, so a DB created in between needs this ALTER or every mark write
    # fails on the missing column.
    mark_cols = {r[1] for r in conn.execute("PRAGMA table_info(game_marks)")}
    if mark_cols and "alt_key" not in mark_cols:
        conn.execute("ALTER TABLE game_marks ADD COLUMN alt_key TEXT")
    # Safe for both paths now that the column is guaranteed to exist.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_marks_alt ON game_marks(alt_key)")


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
    "edge_at_placement_pct", "status", "note", "start_time", "account_id",
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
    if not isinstance(fields["book"], str) or not fields["book"].strip():
        raise ValueError(f"book must be a non-empty string; got {fields['book']!r}")
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


def _range_sql(since: str | None, until: str | None, prefix: str = " WHERE") -> str:
    """Build the placed_at range predicate (empty string when unfiltered)."""
    parts = []
    if since is not None:
        parts.append("placed_at >= ?")
    if until is not None:
        parts.append("placed_at <= ?")
    return (prefix + " " + " AND ".join(parts)) if parts else ""


def _range_args(since: str | None, until: str | None) -> tuple:
    if until is not None and len(until) == 10:
        until = until + "T23:59:59.999999+00:00"
    return tuple(x for x in (since, until) if x is not None)


def list_bets(status: str | None = None, since: str | None = None,
              until: str | None = None) -> list[dict]:
    """List bets. status=None returns all; 'open' / 'settled' / a specific terminal
    state ('won','lost','pushed','void') filters accordingly.

    `since` / `until` are ISO-8601 dates or timestamps filtering on **placed_at**
    — when the bet was logged, not when the game runs. That is the axis the
    dashboard's date filter uses: picking a start date should show only the bets
    you have taken since then, so a fresh window reads zero across the board.
    Comparison is lexicographic, which is correct for ISO-8601; a bare date like
    "2026-08-01" therefore includes the whole of that day for `since`, and
    `until` is made inclusive by extending a bare date to end-of-day.
    """
    if until is not None and len(until) == 10:      # bare YYYY-MM-DD → inclusive
        until = until + "T23:59:59.999999+00:00"
    conn = _require_conn()
    with _conn_lock:
        if status is None or status == "all":
            rows = conn.execute(
                "SELECT * FROM bets" + _range_sql(since, until)
                + " ORDER BY placed_at DESC", _range_args(since, until)
            ).fetchall()
        elif status == "open":
            rows = conn.execute(
                "SELECT * FROM bets WHERE status = 'open'"
                + _range_sql(since, until, prefix=" AND")
                + " ORDER BY start_time ASC, placed_at DESC",
                _range_args(since, until)
            ).fetchall()
        elif status == "settled":
            rows = conn.execute(
                "SELECT * FROM bets WHERE status != 'open'"
                + _range_sql(since, until, prefix=" AND")
                + " ORDER BY settled_at DESC", _range_args(since, until)
            ).fetchall()
        elif status in VALID_STATUSES:
            rows = conn.execute(
                "SELECT * FROM bets WHERE status = ?"
                + _range_sql(since, until, prefix=" AND")
                + " ORDER BY settled_at DESC",
                (status, *_range_args(since, until)),
            ).fetchall()
        else:
            raise ValueError(f"unknown status filter {status!r}")
    return [dict(r) for r in rows]


def update_bet(bet_id: int, **fields: Any) -> bool:
    """Partial update — only the provided fields are set. Returns True if a row changed.

    2026-06-12: the editable set now covers the bet itself (stake, odds,
    market spec, account, …) so data-entry mistakes — e.g. the 100.01 stakes
    from the old stake-step bug — can be corrected in place instead of
    deleting and re-logging. Same validation rules as create_bet.
    """
    allowed = {
        "note", "pin_fair_closing", "status", "settled_at", "payout",
        "cb_event_id", "start_time",
        "sport", "match_label", "period", "market_type", "line", "side",
        "submarket", "team_side", "book", "account_id", "odds_taken", "stake",
    }
    bad = [k for k in fields if k not in allowed]
    if bad:
        raise ValueError(f"update_bet: cannot modify {bad}")
    if not fields:
        return False
    if "status" in fields and fields["status"] not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}; got {fields['status']!r}")
    if "book" in fields and (not isinstance(fields["book"], str) or not fields["book"].strip()):
        raise ValueError(f"book must be a non-empty string; got {fields['book']!r}")
    if fields.get("odds_taken") is not None and float(fields["odds_taken"]) <= 1.0:
        raise ValueError(f"odds_taken must be > 1.0; got {fields['odds_taken']}")
    if fields.get("stake") is not None and float(fields["stake"]) <= 0:
        raise ValueError(f"stake must be > 0; got {fields['stake']}")
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
    """Mark a bet won/lost/pushed/void/cashout, or revert it to open. If payout
    not given, computes:
        won     → stake × odds_taken
        lost    → 0
        pushed  → stake
        void    → stake
        cashout → no formula — the amount the book paid is REQUIRED
        open    → un-settle (clears payout + settled_at)
    Re-settling an ALREADY-settled bet is allowed (2026-06-13): the outcome
    is overwritten and payout recomputed — for fixing a mis-clicked result.
    Returns True if the bet existed and was updated.
    """
    if outcome not in ("won", "lost", "pushed", "void", "cashout", "open"):
        raise ValueError(
            f"settle outcome must be won|lost|pushed|void|cashout|open; got {outcome!r}"
        )
    bet = get_bet(bet_id)
    if not bet:
        return False
    if outcome == "open":
        # revert to open: drop payout + settled_at so PnL/balance back it out
        return update_bet(bet_id, status="open", payout=None, settled_at=None)
    if outcome == "cashout" and payout is None:
        raise ValueError("cashout needs the amount the book paid (payout)")
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


# ── Parlays (bet_legs) ────────────────────────────────────────────────────────
# A parlay is ONE bets row (header) + N bet_legs rows. The header's stake is the
# single real wager; its odds_taken/status/payout are DERIVED from the legs by
# _recompute_parlay. A single bet has no legs and is untouched by all of this.
_LEG_FIELDS = (
    "sport", "cb_event_id", "match_label", "period", "market_type", "line",
    "side", "submarket", "team_side", "start_time", "odds", "status",
)
_LEG_OUTCOMES = ("won", "lost", "pushed", "void", "open")


def list_legs(bet_id: int) -> list[dict]:
    conn = _require_conn()
    with _conn_lock:
        rows = conn.execute(
            "SELECT * FROM bet_legs WHERE bet_id = ? ORDER BY leg_index", (bet_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def _insert_leg(conn: sqlite3.Connection, bet_id: int, leg_index: int, leg: dict) -> int:
    payload = {k: leg.get(k) for k in _LEG_FIELDS}
    payload["status"] = leg.get("status") or "open"
    payload["bet_id"] = bet_id
    payload["leg_index"] = leg_index
    fields = ("bet_id", "leg_index", *_LEG_FIELDS)
    cols = ", ".join(fields)
    ph = ", ".join(f":{k}" for k in fields)
    return conn.execute(f"INSERT INTO bet_legs ({cols}) VALUES ({ph})", payload).lastrowid


def add_leg(bet_id: int, **leg: Any) -> int:
    """Add a game (leg) to a bet, turning it into a parlay. The bet must be OPEN.
    The first call migrates the bet's own market into leg 1, so combined odds =
    product of all legs. Stake/account are untouched — no new money. Returns the
    new leg's id."""
    for k in ("match_label", "side", "odds"):
        if leg.get(k) in (None, ""):
            raise ValueError(f"add_leg missing required leg field: {k}")
    if float(leg["odds"]) <= 1.0:
        raise ValueError(f"leg odds must be > 1.0; got {leg['odds']}")
    bet = get_bet(bet_id)
    if not bet:
        raise ValueError(f"bet {bet_id} not found")
    if bet["status"] != "open":
        raise ValueError("can only add a leg to an open bet")
    conn = _require_conn()
    with _conn_lock:
        existing = conn.execute(
            "SELECT COALESCE(MAX(leg_index), 0) FROM bet_legs WHERE bet_id = ?", (bet_id,)
        ).fetchone()[0]
        if not bet["is_parlay"]:
            # fold the header's own market down into leg 1
            _insert_leg(conn, bet_id, 1, {
                "sport": bet["sport"], "cb_event_id": bet["cb_event_id"],
                "match_label": bet["match_label"], "period": bet["period"],
                "market_type": bet["market_type"], "line": bet["line"],
                "side": bet["side"], "submarket": bet["submarket"],
                "team_side": bet["team_side"], "start_time": bet["start_time"],
                "odds": bet["odds_taken"], "status": "open",
            })
            conn.execute("UPDATE bets SET is_parlay = 1 WHERE id = ?", (bet_id,))
            existing = 1
        leg_id = _insert_leg(conn, bet_id, existing + 1, leg)
    _recompute_parlay(bet_id)
    return leg_id


def settle_leg(bet_id: int, leg_index: int, outcome: str) -> bool:
    """Set one leg's result (won|lost|pushed|void|open) and roll the parlay up.
    Returns True if a leg row changed."""
    if outcome not in _LEG_OUTCOMES:
        raise ValueError(f"leg outcome must be one of {_LEG_OUTCOMES}; got {outcome!r}")
    conn = _require_conn()
    with _conn_lock:
        changed = conn.execute(
            "UPDATE bet_legs SET status = ? WHERE bet_id = ? AND leg_index = ?",
            (outcome, bet_id, leg_index),
        ).rowcount > 0
    if changed:
        _recompute_parlay(bet_id)
    return changed


def remove_leg(bet_id: int, leg_index: int) -> bool:
    """Drop a leg. If the parlay falls to a single remaining leg, fold it back to
    an ordinary single bet (the remaining leg's market on the header)."""
    conn = _require_conn()
    with _conn_lock:
        changed = conn.execute(
            "DELETE FROM bet_legs WHERE bet_id = ? AND leg_index = ?",
            (bet_id, leg_index),
        ).rowcount > 0
        if changed:                       # re-index the survivors 1..N
            rows = conn.execute(
                "SELECT id FROM bet_legs WHERE bet_id = ? ORDER BY leg_index", (bet_id,)
            ).fetchall()
            for i, r in enumerate(rows, 1):
                conn.execute("UPDATE bet_legs SET leg_index = ? WHERE id = ?", (i, r["id"]))
            n = len(rows)
    if not changed:
        return False
    if n <= 1:
        _fold_to_single(bet_id)
    else:
        _recompute_parlay(bet_id)
    return True


def _fold_to_single(bet_id: int) -> None:
    """Collapse a 1-leg (or 0-leg) parlay back to a plain single bet."""
    conn = _require_conn()
    with _conn_lock:
        leg = conn.execute(
            "SELECT * FROM bet_legs WHERE bet_id = ? ORDER BY leg_index LIMIT 1", (bet_id,)
        ).fetchone()
        if leg is not None:
            conn.execute(
                "UPDATE bets SET is_parlay = 0, sport = ?, cb_event_id = ?, "
                "match_label = ?, period = ?, market_type = ?, line = ?, side = ?, "
                "submarket = ?, team_side = ?, start_time = ?, odds_taken = ? WHERE id = ?",
                (leg["sport"], leg["cb_event_id"], leg["match_label"], leg["period"],
                 leg["market_type"], leg["line"], leg["side"], leg["submarket"],
                 leg["team_side"], leg["start_time"], leg["odds"], bet_id),
            )
            conn.execute("DELETE FROM bet_legs WHERE bet_id = ?", (bet_id,))
        else:
            conn.execute("UPDATE bets SET is_parlay = 0 WHERE id = ?", (bet_id,))


def _recompute_parlay(bet_id: int) -> None:
    """Derive the parlay header (odds_taken, status, payout, settled_at) from its
    legs. No-op for a bet with no legs. Roll-up:
      any leg lost           → lost,  payout 0
      all settled, ≥1 won    → won,   payout = stake × Π(won-leg odds)
      all settled, none won  → pushed (full refund)
      otherwise              → open
    pushed/void legs drop out of every product (factor 1.0)."""
    conn = _require_conn()
    with _conn_lock:
        legs = conn.execute(
            "SELECT odds, status FROM bet_legs WHERE bet_id = ? ORDER BY leg_index",
            (bet_id,),
        ).fetchall()
        if not legs:
            return
        row = conn.execute("SELECT stake FROM bets WHERE id = ?", (bet_id,)).fetchone()
        if row is None:
            return
        stake = row["stake"]
        statuses = [l["status"] for l in legs]
        combined = 1.0                    # header odds = product of legs still standing
        for l in legs:
            if l["status"] not in ("pushed", "void"):
                combined *= l["odds"]
        now = _now_iso()
        if "lost" in statuses:
            status, payout, settled_at = "lost", 0.0, now
        elif all(s in ("won", "pushed", "void") for s in statuses):
            won_odds = 1.0
            for l in legs:
                if l["status"] == "won":
                    won_odds *= l["odds"]
            if "won" in statuses:
                status, payout = "won", stake * won_odds
            else:                         # every leg pushed/void → stake back
                status, payout = "pushed", stake
            settled_at = now
        else:
            status, payout, settled_at = "open", None, None
        conn.execute(
            "UPDATE bets SET is_parlay = 1, odds_taken = ?, status = ?, "
            "payout = ?, settled_at = ? WHERE id = ?",
            (combined, status, payout, settled_at, bet_id),
        )


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


# ── Game marks ───────────────────────────────────────────────────────────────
# "I have money on this game" — see the game_marks DDL for why this is not a
# bet. Marks are display state: they never enter PnL, capital or CLV.

MARK_PRUNE_DAYS = 2.0


def list_marks() -> list[dict]:
    conn = _require_conn()
    with _conn_lock:
        rows = conn.execute(
            "SELECT * FROM game_marks ORDER BY COALESCE(start_time, updated_at)"
        ).fetchall()
    return [dict(r) for r in rows]


def _find_mark(conn, keys: list[str]):
    """The existing mark reachable by any of `keys`, on either key column.

    This is what makes a mark one-per-GAME: marking from Arbs stores a
    pin-event key, marking the same fixture from Anomalies offers only a name
    key, and either must find the other.
    """
    ks = [k for k in keys if k]
    if not ks:
        return None
    q = ",".join("?" * len(ks))
    return conn.execute(
        f"SELECT * FROM game_marks WHERE game_key IN ({q}) OR alt_key IN ({q})",
        ks + ks,
    ).fetchone()


def upsert_mark(game_key: str, *, match_label: str, alt_key: str | None = None,
                sport: str | None = None, start_time: str | None = None,
                amount: float | None = None, note: str | None = None) -> dict:
    """Create or update one mark. Re-marking an existing game updates the
    amount rather than stacking a second row — the mark is per game, and its
    amount is the total you have on that fixture."""
    if not game_key or not str(game_key).strip():
        raise ValueError("game_key is required")
    if not match_label or not str(match_label).strip():
        raise ValueError("match_label is required")
    if amount is not None and (amount < 0 or amount != amount):   # NaN-safe
        raise ValueError("amount must be >= 0")
    now = datetime.now(tz=timezone.utc).isoformat()
    conn = _require_conn()
    with _conn_lock:
        existing = _find_mark(conn, [game_key, alt_key])
        if existing is not None:
            conn.execute(
                """UPDATE game_marks SET
                       alt_key     = COALESCE(?, alt_key),
                       sport       = COALESCE(?, sport),
                       match_label = ?,
                       start_time  = COALESCE(?, start_time),
                       amount      = ?,
                       note        = ?,
                       updated_at  = ?
                   WHERE game_key = ?""",
                (alt_key, sport, match_label, start_time, amount, note, now,
                 existing["game_key"]),
            )
            found = existing["game_key"]
        else:
            conn.execute(
                """INSERT INTO game_marks
                       (game_key, alt_key, sport, match_label, start_time,
                        amount, note, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (game_key, alt_key, sport, match_label, start_time, amount,
                 note, now, now),
            )
            found = game_key
        conn.commit()
        row = conn.execute(
            "SELECT * FROM game_marks WHERE game_key = ?", (found,)
        ).fetchone()
    return dict(row)


def delete_mark(game_key: str) -> bool:
    """Delete by either key — the caller may only hold the name key."""
    conn = _require_conn()
    with _conn_lock:
        cur = conn.execute(
            "DELETE FROM game_marks WHERE game_key = ? OR alt_key = ?",
            (game_key, game_key))
        conn.commit()
    return cur.rowcount > 0


def prune_marks(days: float = MARK_PRUNE_DAYS) -> int:
    """Drop marks for games that kicked off more than `days` ago.

    A mark with no start_time is KEPT — we cannot tell whether it is stale, and
    silently deleting the user's own annotation is worse than leaving it.
    """
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat()
    conn = _require_conn()
    with _conn_lock:
        cur = conn.execute(
            "DELETE FROM game_marks WHERE start_time IS NOT NULL "
            "AND start_time != '' AND start_time < ?", (cutoff,))
        conn.commit()
    return cur.rowcount


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
