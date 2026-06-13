"""
Tests for src.capital — accounts, ledger, balances, PnL, CSV export.

Same temp-DB pattern as test_bets.py: capital shares bets.db, so the
clean_db fixture isolates everything.
"""
from __future__ import annotations

import pytest

from src import bets, capital


@pytest.fixture
def clean_db(tmp_path):
    bets._reset_for_tests()
    db_path = tmp_path / "test_bets.db"
    bets.init_db(db_path)
    yield db_path
    bets._reset_for_tests()


def _bet(**over):
    fields = dict(
        sport="basketball", match_label="H vs A", period="FT",
        market_type="moneyline", side="home", book="cb",
        odds_taken=2.0, stake=100.0, bankroll_at_time=500.0,
    )
    fields.update(over)
    return bets.create_bet(**fields)


# ── accounts ──────────────────────────────────────────────────────────────────

def test_seed_accounts_once(clean_db):
    capital.ensure_seed_accounts()
    n = len(capital.list_accounts())
    assert n == 4
    capital.ensure_seed_accounts()  # idempotent
    assert len(capital.list_accounts()) == n


def test_add_rename_account(clean_db):
    aid = capital.add_account("1xbet", "other")
    assert capital.rename_account(aid, "1xBet GE")
    accts = {a["id"]: a for a in capital.list_accounts()}
    assert accts[aid]["name"] == "1xBet GE"
    assert accts[aid]["book_tag"] == "other"


def test_add_account_validates(clean_db):
    with pytest.raises(ValueError):
        capital.add_account("   ")
    with pytest.raises(ValueError):
        capital.add_account("X", "nope")


def test_delete_empty_account_hard_deletes(clean_db):
    aid = capital.add_account("Empty")
    assert capital.delete_account(aid) == "deleted"
    assert aid not in {a["id"] for a in capital.list_accounts()}


def test_delete_account_with_ledger_archives(clean_db):
    aid = capital.add_account("Bank")
    capital.add_entry(aid, "opening", 1000)
    assert capital.delete_account(aid) == "archived"
    acct = next(a for a in capital.list_accounts() if a["id"] == aid)
    assert acct["archived"] == 1
    assert capital.unarchive_account(aid)


def test_delete_account_with_direct_bets_archives(clean_db):
    aid = capital.add_account("Pin")
    _bet(account_id=aid)
    assert capital.delete_account(aid) == "archived"


def test_delete_account_with_legacy_book_bets_archives(clean_db):
    aid = capital.add_account("CB", "cb")
    _bet()  # book='cb', account_id NULL → attributes via book_tag
    assert capital.delete_account(aid) == "archived"


def test_delete_missing_account_raises(clean_db):
    with pytest.raises(KeyError):
        capital.delete_account(999)


# ── ledger ────────────────────────────────────────────────────────────────────

def test_entry_signs_fixed_by_kind(clean_db):
    aid = capital.add_account("Bank")
    capital.add_entry(aid, "opening", 1000)
    capital.add_entry(aid, "deposit", -50)     # sign coerced to +
    capital.add_entry(aid, "withdraw", 200)    # sign coerced to −
    capital.add_entry(aid, "adjustment", -25)  # kept as given
    amounts = {e["kind"]: e["amount"] for e in capital.list_entries(aid)}
    assert amounts == {"opening": 1000, "deposit": 50,
                       "withdraw": -200, "adjustment": -25}


def test_entry_validation(clean_db):
    aid = capital.add_account("Bank")
    with pytest.raises(ValueError):
        capital.add_entry(aid, "bogus", 10)
    with pytest.raises(ValueError):
        capital.add_entry(aid, "deposit", 0)
    with pytest.raises(KeyError):
        capital.add_entry(999, "deposit", 10)


def test_transfer_creates_balanced_pair(clean_db):
    a = capital.add_account("Bank")
    b = capital.add_account("CB", "cb")
    capital.transfer(a, b, 300, note="topup")
    entries = capital.list_entries()
    assert len(entries) == 2
    assert sum(e["amount"] for e in entries) == 0
    by_acct = {e["account_id"]: e for e in entries}
    assert by_acct[a]["amount"] == -300 and "to CB" in by_acct[a]["note"]
    assert by_acct[b]["amount"] == 300 and "from Bank" in by_acct[b]["note"]


def test_transfer_rejects_same_account(clean_db):
    a = capital.add_account("Bank")
    with pytest.raises(ValueError):
        capital.transfer(a, a, 100)


def test_delete_entry(clean_db):
    aid = capital.add_account("Bank")
    eid = capital.add_entry(aid, "deposit", 10)
    assert capital.delete_entry(eid)
    assert not capital.delete_entry(eid)


# ── balances / summary ────────────────────────────────────────────────────────

def test_balance_combines_ledger_and_bets(clean_db):
    cb = capital.add_account("CB", "cb")
    capital.add_entry(cb, "opening", 500)
    won = _bet(account_id=cb, stake=100, odds_taken=2.0)
    bets.settle_bet(won, "won")          # +100 pnl
    lost = _bet(account_id=cb, stake=50)
    bets.settle_bet(lost, "lost")        # −50 pnl
    _bet(account_id=cb, stake=30)        # open → −30 from balance
    s = capital.capital_summary()
    acct = next(a for a in s["accounts"] if a["id"] == cb)
    assert acct["bet_pnl"] == 50.0
    assert acct["open_stake"] == 30.0
    assert acct["balance"] == 500 + 50 - 30
    assert s["totals"]["settled_pnl"] == 50.0
    assert s["totals"]["open_exposure"] == 30.0
    assert s["totals"]["equity"] == 520.0
    assert s["totals"]["starting_capital"] == 500.0
    # yield = 50 pnl / 150 settled stakes
    assert s["totals"]["yield_pct"] == pytest.approx(33.33, abs=0.01)


def test_open_and_total_bet_counts(clean_db):
    cb = capital.add_account("CB", "cb")
    w = _bet(account_id=cb); bets.settle_bet(w, "won")
    _bet(account_id=cb)   # open
    _bet(account_id=cb)   # open
    s = capital.capital_summary()
    acct = next(a for a in s["accounts"] if a["id"] == cb)
    assert acct["n_bets"] == 3 and acct["n_open"] == 2
    assert s["totals"]["n_bets"] == 3 and s["totals"]["n_open"] == 2


def test_legacy_bets_attribute_via_book_tag(clean_db):
    cb = capital.add_account("CB", "cb")
    bid = _bet()  # account_id NULL, book='cb'
    bets.settle_bet(bid, "won")
    s = capital.capital_summary()
    acct = next(a for a in s["accounts"] if a["id"] == cb)
    assert acct["bet_pnl"] == 100.0
    assert not any(a["name"] == "(unassigned)" for a in s["accounts"])


def test_unmatched_bets_fall_into_unassigned(clean_db):
    capital.add_account("Bank")  # no book_tag anywhere
    bid = _bet(book="pin")
    bets.settle_bet(bid, "won")
    s = capital.capital_summary()
    un = next(a for a in s["accounts"] if a["name"] == "(unassigned)")
    assert un["bet_pnl"] == 100.0
    # totals still reconcile
    assert s["totals"]["settled_pnl"] == 100.0


def test_push_and_void_are_pnl_neutral(clean_db):
    cb = capital.add_account("CB", "cb")
    capital.add_entry(cb, "opening", 100)
    b1 = _bet(account_id=cb)
    bets.settle_bet(b1, "pushed")
    b2 = _bet(account_id=cb)
    bets.settle_bet(b2, "void")
    s = capital.capital_summary()
    assert s["totals"]["settled_pnl"] == 0.0
    assert s["totals"]["equity"] == 100.0


def test_pnl_curve_is_cumulative_and_ignores_deposits(clean_db):
    cb = capital.add_account("CB", "cb")
    capital.add_entry(cb, "deposit", 10_000)  # must not bend the curve
    b1 = _bet(account_id=cb, stake=100, odds_taken=2.0)
    bets.settle_bet(b1, "won")
    b2 = _bet(account_id=cb, stake=40)
    bets.settle_bet(b2, "lost")
    curve = capital.capital_summary()["pnl_curve"]
    assert [p["pnl"] for p in curve] == [100.0, 60.0]


def test_deposits_move_balance_not_pnl(clean_db):
    cb = capital.add_account("CB", "cb")
    capital.add_entry(cb, "deposit", 200)
    capital.add_entry(cb, "withdraw", 80)
    s = capital.capital_summary()
    assert s["totals"]["settled_pnl"] == 0.0
    assert s["totals"]["equity"] == 120.0
    assert s["totals"]["starting_capital"] == 0.0


# ── CSV export ────────────────────────────────────────────────────────────────

def test_export_csv_shapes(clean_db):
    cb = capital.add_account("CB", "cb")
    capital.add_entry(cb, "opening", 500)
    bid = _bet(account_id=cb)
    bets.settle_bet(bid, "won")

    summary = capital.export_csv("summary")
    assert summary.splitlines()[0].startswith("account,")
    assert "TOTAL" in summary and "CB" in summary

    ledger = capital.export_csv("ledger")
    assert "opening" in ledger and "500" in ledger

    bets_csv = capital.export_csv("bets")
    lines = bets_csv.splitlines()
    header = lines[0].split(",")
    assert header[-1] == "pnl"
    assert "account" in header and "start_time" in header
    assert lines[1].endswith(",100.0")  # won 100 stake at 2.0 → +100 pnl
    # the account column carries the NAME, not the cb/pin tag or numeric id
    assert ",CB," in lines[1]

    with pytest.raises(ValueError):
        capital.export_csv("nope")


def test_export_bets_csv_resolves_legacy_book_to_account_name(clean_db):
    capital.add_account("CrystalBet", "cb")
    _bet()  # legacy-style: book='cb', no account_id
    lines = capital.export_csv("bets").splitlines()
    assert ",CrystalBet," in lines[1]


# ── bet-create derivation (merged Book/Account picker, 2026-06-12) ────────────

def _api_payload(**over):
    p = dict(sport="basketball", match_label="X vs Y", period="FT",
             market_type="moneyline", side="home", odds_taken=2.0, stake=25)
    p.update(over)
    return p


def test_api_create_bet_derives_book_and_bankroll(clean_db):
    import asyncio
    from src import app as appmod
    cb = capital.add_account("CB", "cb")
    capital.add_entry(cb, "opening", 300)
    b = asyncio.run(appmod.api_create_bet(_api_payload(account_id=cb)))
    assert b["book"] == "cb"            # derived from the account's tag
    assert b["bankroll_at_time"] == 300.0  # equity stamped automatically
    assert b["account_id"] == cb


def test_api_create_bet_untagged_account_books_as_other(clean_db):
    import asyncio
    from src import app as appmod
    bank = capital.add_account("Bank")
    b = asyncio.run(appmod.api_create_bet(_api_payload(account_id=bank)))
    assert b["book"] == "other"


def test_existing_db_gains_capital_tables_on_init(clean_db, tmp_path):
    # Simulate an old DB: drop the new tables + column, re-init, verify
    # the migration recreates them. (Cheap proxy for upgrading prod bets.db.)
    conn = bets._require_conn()
    conn.executescript("DROP TABLE ledger; DROP TABLE accounts;")
    bets._reset_for_tests()
    bets.init_db(clean_db)
    capital.ensure_seed_accounts()
    assert len(capital.list_accounts()) == 4
    cols = {r[1] for r in bets._require_conn().execute("PRAGMA table_info(bets)")}
    assert "account_id" in cols


# ── ledger editing + cashout flow (2026-06-12) ────────────────────────────────

def test_update_entry_keeps_kind_sign(clean_db):
    aid = capital.add_account("Bank")
    e_dep = capital.add_entry(aid, "deposit", 100.01)
    e_wd = capital.add_entry(aid, "withdraw", 50.01)
    assert capital.update_entry(e_dep, amount=100)     # rounding the typo
    assert capital.update_entry(e_wd, amount=-50)      # sign ignored, kind wins
    amounts = {e["id"]: e["amount"] for e in capital.list_entries(aid)}
    assert amounts[e_dep] == 100.0
    assert amounts[e_wd] == -50.0


def test_update_entry_transfer_keeps_direction(clean_db):
    a = capital.add_account("Bank")
    b = capital.add_account("CB", "cb")
    out_id, in_id = capital.transfer(a, b, 300.01)
    assert capital.update_entry(out_id, amount=300)
    assert capital.update_entry(in_id, amount=300)
    entries = {e["id"]: e["amount"] for e in capital.list_entries()}
    assert entries[out_id] == -300.0 and entries[in_id] == 300.0


def test_update_entry_note_and_missing(clean_db):
    aid = capital.add_account("Bank")
    eid = capital.add_entry(aid, "deposit", 10)
    assert capital.update_entry(eid, note="fixed")
    assert capital.list_entries(aid)[0]["note"] == "fixed"
    assert not capital.update_entry(999, amount=5)
    with pytest.raises(ValueError):
        capital.update_entry(eid, amount=0)


def test_cashout_pnl_flows_into_balance(clean_db):
    cb = capital.add_account("CB", "cb")
    capital.add_entry(cb, "opening", 200)
    bid = _bet(account_id=cb, stake=50, odds_taken=3.0)
    bets.settle_bet(bid, "cashout", payout=120)   # cashed out before the end
    s = capital.capital_summary()
    assert s["totals"]["settled_pnl"] == 70.0     # 120 − 50
    assert s["totals"]["equity"] == 270.0
    assert s["pnl_curve"][-1]["pnl"] == 70.0
