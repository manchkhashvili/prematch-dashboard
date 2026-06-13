"""
Capital / PnL tracker: accounts + money ledger on top of the bet tracker.

Answers "where does the bankroll live and how is it doing" — per account
(a book, the bank, cash) and in total — using two sources that are never
double-counted:

  ledger  — money YOU moved: opening balances (starting capital), deposits,
            withdrawals, transfers between accounts, manual adjustments.
  bets    — money the BETS moved: every bet's stake leaves its account at
            placement; the payout returns on settlement. Computed live from
            the bets table (single source of truth), never copied into the
            ledger.

Balance per account = Σ ledger + Σ settled (payout − stake) − Σ open stakes.
Equity = Σ balances. Settled PnL = Σ (payout − stake) over settled bets —
deposits/withdrawals never bend the PnL number, only the balance.

Attribution: each bet flows through bets.account_id when set (the bets-page
picker). Legacy bets (NULL account_id) attribute via accounts.book_tag
matching bets.book ('cb' | 'pin' | 'other'). Bets that match neither are
aggregated under a virtual "(unassigned)" row so the totals always reconcile
with the bets table.

Deleting an account: hard delete only when nothing references it (no ledger
rows, no attributed bets — directly or via book_tag); otherwise it archives,
so history stays auditable. Archived accounts keep contributing to totals
(their balance is usually 0 after you withdraw/transfer the money out).

Shares the SQLite connection owned by src/bets.py (same file, bets.db) —
schema for these tables is created by bets._create_schema.
"""
from __future__ import annotations

import csv
import io
import logging
import os
from typing import Any, Optional

from src import bets as _bets
from src.bets import _now_iso, _require_conn, _conn_lock  # shared DB conn/lock

log = logging.getLogger(__name__)

LEDGER_KINDS = ("opening", "deposit", "withdraw", "transfer", "adjustment")

# Books charge a gross commission on withdrawals (the owner's books: ~6%).
# Applied to BOOK accounts (those with a book_tag) when valuing "what you'd
# actually have if you pulled the money out". The Bank (no tag) is fee-free —
# it's already your own money. Override with WITHDRAWAL_FEE_PCT.
WITHDRAWAL_FEE_PCT = float(os.environ.get("WITHDRAWAL_FEE_PCT", "6"))

# Seeded once into an empty accounts table: covers the three bets.book values
# (so every legacy bet attributes somewhere) + the bank. Rename/delete freely.
_DEFAULT_ACCOUNTS = (
    ("Bank", None),
    ("CrystalBet", "cb"),
    ("Pinnacle", "pin"),
    ("Other books", "other"),
)


def ensure_seed_accounts() -> None:
    """Create the default accounts if the table is empty. Idempotent."""
    conn = _require_conn()
    with _conn_lock:
        n = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        if n:
            return
        for name, tag in _DEFAULT_ACCOUNTS:
            conn.execute(
                "INSERT INTO accounts (name, book_tag, created_at) VALUES (?, ?, ?)",
                (name, tag, _now_iso()),
            )
    log.info("capital: seeded %d default accounts", len(_DEFAULT_ACCOUNTS))


# ── Accounts ──────────────────────────────────────────────────────────────────

def list_accounts(include_archived: bool = True) -> list[dict]:
    conn = _require_conn()
    with _conn_lock:
        rows = conn.execute(
            "SELECT * FROM accounts"
            + ("" if include_archived else " WHERE archived = 0")
            + " ORDER BY archived, id"
        ).fetchall()
    return [dict(r) for r in rows]


def add_account(name: str, book_tag: Optional[str] = None) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("account name required")
    if book_tag is not None and book_tag not in _bets.VALID_BOOKS:
        raise ValueError(f"book_tag must be one of {_bets.VALID_BOOKS} or null")
    conn = _require_conn()
    with _conn_lock:
        cur = conn.execute(
            "INSERT INTO accounts (name, book_tag, created_at) VALUES (?, ?, ?)",
            (name, book_tag, _now_iso()),
        )
        return cur.lastrowid


def rename_account(account_id: int, name: str) -> bool:
    name = (name or "").strip()
    if not name:
        raise ValueError("account name required")
    conn = _require_conn()
    with _conn_lock:
        cur = conn.execute(
            "UPDATE accounts SET name = ? WHERE id = ?", (name, account_id)
        )
        return cur.rowcount > 0


def delete_account(account_id: int) -> str:
    """Delete or archive an account. Returns 'deleted' | 'archived'.
    Raises KeyError if the account doesn't exist."""
    conn = _require_conn()
    with _conn_lock:
        acct = conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if acct is None:
            raise KeyError(f"account {account_id} not found")
        n_ledger = conn.execute(
            "SELECT COUNT(*) FROM ledger WHERE account_id = ?", (account_id,)
        ).fetchone()[0]
        n_direct = conn.execute(
            "SELECT COUNT(*) FROM bets WHERE account_id = ?", (account_id,)
        ).fetchone()[0]
        n_legacy = 0
        if acct["book_tag"]:
            n_legacy = conn.execute(
                "SELECT COUNT(*) FROM bets WHERE account_id IS NULL AND book = ?",
                (acct["book_tag"],),
            ).fetchone()[0]
        if n_ledger or n_direct or n_legacy:
            conn.execute(
                "UPDATE accounts SET archived = 1 WHERE id = ?", (account_id,)
            )
            return "archived"
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        return "deleted"


def unarchive_account(account_id: int) -> bool:
    conn = _require_conn()
    with _conn_lock:
        cur = conn.execute(
            "UPDATE accounts SET archived = 0 WHERE id = ?", (account_id,)
        )
        return cur.rowcount > 0


# ── Ledger ────────────────────────────────────────────────────────────────────

def add_entry(
    account_id: int,
    kind: str,
    amount: float,
    note: Optional[str] = None,
    ts: Optional[str] = None,
) -> int:
    """One signed ledger row. The user always types a positive number; the
    kind fixes the sign (withdraw = out). 'adjustment' keeps the sign given
    (corrections go both ways). 'transfer' rows are made by transfer()."""
    if kind not in LEDGER_KINDS:
        raise ValueError(f"kind must be one of {LEDGER_KINDS}; got {kind!r}")
    amount = float(amount)
    if amount == 0:
        raise ValueError("amount must be non-zero")
    if kind in ("opening", "deposit"):
        amount = abs(amount)
    elif kind == "withdraw":
        amount = -abs(amount)
    conn = _require_conn()
    with _conn_lock:
        exists = conn.execute(
            "SELECT 1 FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if not exists:
            raise KeyError(f"account {account_id} not found")
        cur = conn.execute(
            "INSERT INTO ledger (ts, account_id, kind, amount, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts or _now_iso(), account_id, kind, amount, note),
        )
        return cur.lastrowid


def transfer(
    from_id: int, to_id: int, amount: float, note: Optional[str] = None,
) -> tuple[int, int]:
    """Move money between two accounts — a paired ledger row on each side."""
    amount = abs(float(amount))
    if amount == 0:
        raise ValueError("amount must be non-zero")
    if from_id == to_id:
        raise ValueError("transfer needs two different accounts")
    conn = _require_conn()
    with _conn_lock:
        names = {
            r["id"]: r["name"] for r in conn.execute(
                "SELECT id, name FROM accounts WHERE id IN (?, ?)",
                (from_id, to_id),
            ).fetchall()
        }
        if from_id not in names or to_id not in names:
            raise KeyError("both transfer accounts must exist")
        ts = _now_iso()
        base = f" — {note}" if note else ""
        out_id = conn.execute(
            "INSERT INTO ledger (ts, account_id, kind, amount, note) VALUES (?, ?, 'transfer', ?, ?)",
            (ts, from_id, -amount, f"to {names[to_id]}{base}"),
        ).lastrowid
        in_id = conn.execute(
            "INSERT INTO ledger (ts, account_id, kind, amount, note) VALUES (?, ?, 'transfer', ?, ?)",
            (ts, to_id, amount, f"from {names[from_id]}{base}"),
        ).lastrowid
        return out_id, in_id


def list_entries(account_id: Optional[int] = None) -> list[dict]:
    conn = _require_conn()
    with _conn_lock:
        if account_id is None:
            rows = conn.execute(
                "SELECT l.*, a.name AS account_name FROM ledger l "
                "JOIN accounts a ON a.id = l.account_id ORDER BY l.ts DESC, l.id DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT l.*, a.name AS account_name FROM ledger l "
                "JOIN accounts a ON a.id = l.account_id "
                "WHERE l.account_id = ? ORDER BY l.ts DESC, l.id DESC",
                (account_id,),
            ).fetchall()
    return [dict(r) for r in rows]


def delete_entry(entry_id: int) -> bool:
    conn = _require_conn()
    with _conn_lock:
        cur = conn.execute("DELETE FROM ledger WHERE id = ?", (entry_id,))
        return cur.rowcount > 0


def update_entry(
    entry_id: int,
    amount: Optional[float] = None,
    note: Optional[str] = None,
    ts: Optional[str] = None,
) -> bool:
    """Edit a ledger entry in place (fix typos / round amounts without
    deleting history). The amount keeps the row's kind semantics:
    opening/deposit stay positive, withdraw stays negative, transfer keeps
    its original direction; adjustment takes the sign as typed."""
    conn = _require_conn()
    with _conn_lock:
        row = conn.execute(
            "SELECT kind, amount FROM ledger WHERE id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            return False
        sets, params = [], []
        if amount is not None:
            amount = float(amount)
            if amount == 0:
                raise ValueError("amount must be non-zero")
            kind = row["kind"]
            if kind in ("opening", "deposit"):
                amount = abs(amount)
            elif kind == "withdraw":
                amount = -abs(amount)
            elif kind == "transfer":
                amount = abs(amount) * (1 if row["amount"] >= 0 else -1)
            sets.append("amount = ?")
            params.append(amount)
        if note is not None:
            sets.append("note = ?")
            params.append(note)
        if ts is not None:
            sets.append("ts = ?")
            params.append(ts)
        if not sets:
            return False
        params.append(entry_id)
        conn.execute(f"UPDATE ledger SET {', '.join(sets)} WHERE id = ?", params)
        return True


# ── Balances / summary ────────────────────────────────────────────────────────

def _attribute(bet: dict, tag_to_id: dict[str, int]) -> Optional[int]:
    """Which account a bet's money flows through. None = unassigned."""
    if bet.get("account_id") is not None:
        return bet["account_id"]
    return tag_to_id.get(bet.get("book"))


def capital_summary(since: Optional[str] = None) -> dict:
    """Everything the capital UI needs in one call: per-account balances,
    totals, and the cumulative settled-PnL curve.

    `since` (ISO ts) is a time filter for PERFORMANCE only — settled PnL,
    turnover, yield, ROI and the curve count only bets settled on/after it.
    Balances, equity, open exposure and per-account columns stay all-time /
    current (where the money is now doesn't depend on the lookback window).
    """
    accounts = list_accounts()
    entries = list_entries()
    all_bets = _bets.list_bets()

    tag_to_id = {
        a["book_tag"]: a["id"] for a in accounts
        if a["book_tag"] and not a["archived"]
    }
    per: dict[Optional[int], dict[str, float]] = {}

    def bucket(acct_id: Optional[int]) -> dict[str, float]:
        return per.setdefault(acct_id, {
            "opening": 0.0, "ledger_net": 0.0,
            "bet_pnl": 0.0, "open_stake": 0.0, "total_stake": 0.0,
            "n_bets": 0, "n_open": 0,
        })

    for e in entries:
        b = bucket(e["account_id"])
        b["ledger_net"] += e["amount"]
        if e["kind"] == "opening":
            b["opening"] += e["amount"]

    # (settled_at, id, pnl, stake) — windowed perf is filtered from this.
    settled_events: list[tuple[str, int, float, float]] = []
    total_bets = total_open = 0
    for bet in all_bets:
        b = bucket(_attribute(bet, tag_to_id))
        b["n_bets"] += 1
        b["total_stake"] += bet["stake"]
        total_bets += 1
        if bet["status"] == "open":
            b["open_stake"] += bet["stake"]
            b["n_open"] += 1
            total_open += 1
        elif bet["payout"] is not None:
            pnl = bet["payout"] - bet["stake"]
            b["bet_pnl"] += pnl     # all-time, drives the balance
            # bet id breaks same-second settled_at ties deterministically
            settled_events.append(
                (bet["settled_at"] or bet["placed_at"], bet["id"], pnl, bet["stake"])
            )

    rows = []
    for a in accounts:
        b = bucket(a["id"])
        rows.append({
            **a,
            "opening": round(b["opening"], 2),
            "ledger_net": round(b["ledger_net"], 2),
            "bet_pnl": round(b["bet_pnl"], 2),
            "open_stake": round(b["open_stake"], 2),
            "total_stake": round(b["total_stake"], 2),
            "n_bets": int(b["n_bets"]),
            "n_open": int(b["n_open"]),
            "balance": round(b["ledger_net"] + b["bet_pnl"] - b["open_stake"], 2),
        })
    un = per.get(None)
    if un and (un["n_bets"] or un["ledger_net"]):
        rows.append({
            "id": None, "name": "(unassigned)", "book_tag": None,
            "created_at": None, "archived": 0,
            "opening": round(un["opening"], 2),
            "ledger_net": round(un["ledger_net"], 2),
            "bet_pnl": round(un["bet_pnl"], 2),
            "open_stake": round(un["open_stake"], 2),
            "total_stake": round(un["total_stake"], 2),
            "n_bets": int(un["n_bets"]),
            "n_open": int(un["n_open"]),
            "balance": round(un["ledger_net"] + un["bet_pnl"] - un["open_stake"], 2),
        })

    # "Current capital": realistic cash-out value =
    #   bank balance + (book balance + open bets) * (1 - fee)
    # Book money — both the free balance AND the stakes tied up in open bets —
    # loses the gross withdrawal fee when pulled out; the Bank / untagged
    # accounts are fee-free. Open stakes count at face (what you put in), and
    # (balance + open_stake) == ledger_net + bet_pnl (all the account's money,
    # open bets valued at stake). Negative totals take no fee.
    fee = WITHDRAWAL_FEE_PCT / 100.0
    for r in rows:
        gross = r["balance"] + r["open_stake"]   # free cash + open-bet stakes
        if r.get("book_tag") and gross > 0:
            r["current_value"] = round(gross * (1.0 - fee), 2)
        else:
            r["current_value"] = round(gross, 2)

    starting = sum(r["opening"] for r in rows)
    open_exposure = sum(r["open_stake"] for r in rows)
    total_stake_all = sum(r["total_stake"] for r in rows)
    equity = sum(r["balance"] for r in rows)
    current_capital = sum(r["current_value"] for r in rows)

    # Performance (windowed by `since`): settled PnL, turnover, yield, ROI and
    # the curve count only bets settled in the window. Deposits/withdrawals are
    # excluded from the curve — it shows betting results, balances show cash.
    settled_events.sort(key=lambda t: (t[0], t[1]))
    curve, acc = [], 0.0
    win_pnl = win_turnover = 0.0
    for ts, _bid, pnl, stake in settled_events:
        if since is not None and ts < since:
            continue
        win_pnl += pnl
        win_turnover += stake
        acc += pnl
        curve.append({"ts": ts, "pnl": round(acc, 2)})

    return {
        "accounts": rows,
        "totals": {
            "starting_capital": round(starting, 2),
            "equity": round(equity, 2),
            # Total = all money valuing open bets at stake = starting + PnL
            # (gross, no withdrawal fee). current_capital nets the book fee.
            "total_gross": round(equity + open_exposure, 2),
            "current_capital": round(current_capital, 2),
            "withdrawal_fee_pct": WITHDRAWAL_FEE_PCT,
            "settled_pnl": round(win_pnl, 2),
            "open_exposure": round(open_exposure, 2),
            "total_stake": round(total_stake_all, 2),
            # yield = pnl / turnover (settled stakes in window); the standard
            # betting yield. ROI = pnl / starting capital — return on the
            # bankroll you put in (total, not per book).
            "yield_pct": round(win_pnl / win_turnover * 100.0, 2)
            if win_turnover else None,
            "roi_pct": round(win_pnl / starting * 100.0, 2)
            if starting else None,
            # back-compat alias used by older pnl.html
            "growth_pct": round(win_pnl / starting * 100.0, 2)
            if starting else None,
            "n_bets": total_bets,
            "n_open": total_open,
            "since": since,
        },
        "pnl_curve": curve,
    }


# ── CSV export ────────────────────────────────────────────────────────────────

def _csv(headers: list[str], rows: list[list[Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue()


def export_csv(what: str) -> str:
    """CSV text for 'summary' | 'ledger' | 'bets' — spreadsheet-ready."""
    if what == "summary":
        s = capital_summary()
        rows = [[a["name"], a["book_tag"] or "", "yes" if a["archived"] else "",
                 a["opening"], a["ledger_net"], a["bet_pnl"], a["open_stake"],
                 a["balance"], a["n_bets"]] for a in s["accounts"]]
        t = s["totals"]
        rows.append([])
        rows.append(["TOTAL", "", "", t["starting_capital"], "", t["settled_pnl"],
                     t["open_exposure"], t["equity"], ""])
        return _csv(["account", "book_tag", "archived", "opening", "ledger_net",
                     "bet_pnl", "open_stake", "balance", "n_bets"], rows)
    if what == "ledger":
        return _csv(
            ["ts", "account", "kind", "amount", "note"],
            [[e["ts"], e["account_name"], e["kind"], e["amount"], e["note"] or ""]
             for e in list_entries()],
        )
    if what == "bets":
        accounts = list_accounts()
        by_id = {a["id"]: a["name"] for a in accounts}
        tag_to_id = {a["book_tag"]: a["id"] for a in accounts
                     if a["book_tag"] and not a["archived"]}
        cols = ["id", "placed_at", "start_time", "sport", "match_label",
                "period", "market_type", "line", "side", "odds_taken",
                "stake", "status", "settled_at", "payout",
                "edge_at_placement_pct", "note"]
        rows = []
        for b in _bets.list_bets():
            acct_id = _attribute(b, tag_to_id)
            account = by_id.get(acct_id) or b.get("book") or ""
            pnl = (round(b["payout"] - b["stake"], 2)
                   if b["status"] != "open" and b["payout"] is not None else "")
            row = [b.get(c) for c in cols]
            row.insert(cols.index("side") + 1, account)  # account after side
            rows.append(row + [pnl])
        headers = cols[:cols.index("side") + 1] + ["account"] \
            + cols[cols.index("side") + 1:] + ["pnl"]
        return _csv(headers, rows)
    raise ValueError(f"unknown export {what!r} — use summary|ledger|bets")
