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
    assert n == len(capital._DEFAULT_ACCOUNTS)
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
        capital.add_account("   ")          # empty name still rejected
    # book_tag is free-form now — any tag is first-class, no allowlist
    aid = capital.add_account("My 1xbet", "1xbet")
    assert {a["id"]: a for a in capital.list_accounts()}[aid]["book_tag"] == "1xbet"
    # blank/whitespace tag normalises to NULL (the bank / untagged case)
    aid2 = capital.add_account("Bank2", "  ")
    assert {a["id"]: a for a in capital.list_accounts()}[aid2]["book_tag"] is None


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


def test_transfer_book_to_bank_charges_gross_commission(clean_db):
    # Withdraw 100 from a book to the bank → 6% GROSS commission grosses up to
    # 6.38 on 100. The transfer pair is balanced; the 6.38 is its own
    # 'commission' row, and the bank nets 93.62.
    bank = capital.add_account("Bank")
    cb = capital.add_account("CB", "cb")
    capital.transfer(cb, bank, 100)
    ents = capital.list_entries()
    assert sorted(e["amount"] for e in ents if e["kind"] == "transfer") == [-100.0, 100.0]
    comm = [e for e in ents if e["kind"] == "commission"]
    assert len(comm) == 1 and comm[0]["amount"] == pytest.approx(-6.38, abs=0.01)
    assert sum(e["amount"] for e in ents if e["account_id"] == bank) == pytest.approx(93.62, abs=0.01)
    assert sum(e["amount"] for e in ents) == pytest.approx(-6.38, abs=0.01)
    assert capital.capital_summary()["totals"]["commission_paid"] == pytest.approx(6.38, abs=0.01)


def test_transfer_bank_to_book_is_free(clean_db):
    # Deposit (bank → book) and book↔book carry no commission — conserved.
    bank = capital.add_account("Bank")
    cb = capital.add_account("CB", "cb")
    capital.transfer(bank, cb, 100)
    assert sum(e["amount"] for e in capital.list_entries()) == 0.0


def test_pushed_excluded_from_yield(clean_db):
    cb = capital.add_account("CB", "cb")
    capital.add_entry(cb, "opening", 1000)
    w = _bet(account_id=cb, stake=100, odds_taken=2.0); bets.settle_bet(w, "won")   # +100
    p = _bet(account_id=cb, stake=100); bets.settle_bet(p, "pushed")                # no action
    t = capital.capital_summary()["totals"]
    # yield counts only the won bet's turnover (100), not the push → 100/100
    assert t["yield_pct"] == pytest.approx(100.0, abs=0.01)
    assert t["settled_pnl"] == 100.0   # push stays pnl-neutral


def test_set_book_tag_enables_withdrawal_fee(clean_db):
    # An account created WITHOUT a tag (e.g. a 'Liderbet' that defaulted to null)
    # is treated as the bank → no fee. Tagging it makes it a book.
    bank = capital.add_account("Bank")
    lid = capital.add_account("Liderbet")        # no tag
    capital.add_entry(lid, "opening", 100)
    a = next(x for x in capital.capital_summary()["accounts"] if x["id"] == lid)
    assert a["book_tag"] is None and a["withdrawal_cost"] == 0.0   # fee-free while untagged
    assert capital.set_book_tag(lid, "liderbet")
    a = next(x for x in capital.capital_summary()["accounts"] if x["id"] == lid)
    assert a["book_tag"] == "liderbet"
    assert a["withdrawal_cost"] == pytest.approx(6.38, abs=0.01)   # now a book
    # and a transfer to the bank now actually deducts the commission
    capital.transfer(lid, bank, 50)
    bank_net = sum(e["amount"] for e in capital.list_entries() if e["account_id"] == bank)
    assert bank_net == pytest.approx(50 - 50 * 6 / 94, abs=0.01)


def test_ensure_dividend_account_idempotent(clean_db):
    a = capital.ensure_dividend_account()
    b = capital.ensure_dividend_account()
    assert a == b
    divs = [x for x in capital.list_accounts() if x["is_dividend"]]
    assert len(divs) == 1 and divs[0]["name"] == "Dividend"


def test_dividend_excluded_from_total(clean_db):
    bank = capital.add_account("Bank")
    capital.add_entry(bank, "opening", 1000)
    div = capital.ensure_dividend_account()
    t0 = capital.capital_summary()["totals"]
    assert t0["equity"] == 1000.0 and t0["dividend_total"] == 0.0
    capital.transfer(bank, div, 300)              # pull 300 out as a dividend
    t = capital.capital_summary()["totals"]
    assert t["dividend_total"] == 300.0
    assert t["equity"] == 700.0                   # Total reduced by the dividend
    assert t["total_gross"] == 700.0
    assert t["commission_paid"] == 0.0            # bank→dividend is fee-free
    # the dividend account is still listed (a transfer target), just flagged
    div_row = next(a for a in capital.capital_summary()["accounts"] if a["id"] == div)
    assert div_row["is_dividend"] == 1 and div_row["balance"] == 300.0


def test_commission_paid_deducted_from_net_pnl(clean_db):
    bank = capital.add_account("Bank")
    cb = capital.add_account("CB", "cb")
    capital.add_entry(cb, "opening", 1000)
    w = _bet(account_id=cb, stake=100, odds_taken=2.0); bets.settle_bet(w, "won")  # +100
    capital.transfer(cb, bank, 200)            # commission = 200 × 6/94 = 12.77
    t = capital.capital_summary()["totals"]
    assert t["settled_pnl"] == 100.0           # betting PnL unchanged (yield uses this)
    assert t["commission_paid"] == pytest.approx(12.77, abs=0.01)   # what was actually paid
    assert t["net_pnl"] == pytest.approx(100.0 - 12.77, abs=0.01)   # PnL after the fee


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


def test_parlay_counts_as_one_position(clean_db):
    # A parlay is ONE bets row, so rolling a bet into a parlay must NOT double-
    # count: its single stake is exposure once, and settled pnl = payout − stake.
    cb = capital.add_account("CB", "cb")
    capital.add_entry(cb, "opening", 500)
    bid = _bet(account_id=cb, stake=100, odds_taken=2.0)
    bets.add_leg(bid, match_label="PSG vs Villa", side="home", odds=1.5)  # combined 3.0
    acct = next(a for a in capital.capital_summary()["accounts"] if a["id"] == cb)
    assert acct["open_stake"] == 100.0          # the ONE stake, not 100+150
    assert acct["total_stake"] == 100.0
    assert acct["balance"] == 500 - 100
    # settle both legs won → payout = 100 × 2.0 × 1.5 = 300, pnl = 200
    bets.settle_leg(bid, 1, "won")
    bets.settle_leg(bid, 2, "won")
    acct2 = next(a for a in capital.capital_summary()["accounts"] if a["id"] == cb)
    assert acct2["bet_pnl"] == 200.0
    assert acct2["open_stake"] == 0.0
    assert acct2["balance"] == 500 + 200


def test_open_and_total_bet_counts(clean_db):
    cb = capital.add_account("CB", "cb")
    w = _bet(account_id=cb); bets.settle_bet(w, "won")
    _bet(account_id=cb)   # open
    _bet(account_id=cb)   # open
    s = capital.capital_summary()
    acct = next(a for a in s["accounts"] if a["id"] == cb)
    assert acct["n_bets"] == 3 and acct["n_open"] == 2
    assert s["totals"]["n_bets"] == 3 and s["totals"]["n_open"] == 2


def test_total_stake_and_open_stake_money(clean_db):
    cb = capital.add_account("CB", "cb")
    w = _bet(account_id=cb, stake=40); bets.settle_bet(w, "won")
    _bet(account_id=cb, stake=60)   # open
    s = capital.capital_summary()
    acct = next(a for a in s["accounts"] if a["id"] == cb)
    assert acct["total_stake"] == 100.0   # 40 settled + 60 open
    assert acct["open_stake"] == 60.0
    assert s["totals"]["total_stake"] == 100.0


def test_current_capital_applies_book_fee(clean_db):
    # Bank (no tag) is fee-free; books lose the GROSS withdrawal commission on
    # their balance (6% gross → 6.38 on 100, not 6).
    bank = capital.add_account("Bank")          # no book_tag
    cb = capital.add_account("CB", "cb")         # book → fee
    capital.add_entry(bank, "opening", 2000)
    capital.add_entry(cb, "opening", 1000)
    s = capital.capital_summary()
    accts = {a["name"]: a for a in s["accounts"]}
    assert accts["Bank"]["current_value"] == 2000.0          # fee-free
    assert accts["Bank"]["withdrawal_cost"] == 0.0
    assert accts["CB"]["current_value"] == pytest.approx(936.17, abs=0.01)  # 1000 − 63.83
    assert accts["CB"]["withdrawal_cost"] == pytest.approx(63.83, abs=0.01)  # 1000 × 6/94
    assert s["totals"]["equity"] == 3000.0
    assert s["totals"]["current_capital"] == pytest.approx(2936.17, abs=0.01)
    assert s["totals"]["withdrawal_cost"] == pytest.approx(63.83, abs=0.01)
    assert s["totals"]["withdrawal_fee_pct"] == 6.0


def test_total_gross_is_starting_plus_pnl(clean_db):
    # Total (gross) = starting + PnL, valuing open bets at stake = equity + open.
    cb = capital.add_account("CB", "cb")
    capital.add_entry(cb, "opening", 1000)
    w = _bet(account_id=cb, stake=100, odds_taken=2.0); bets.settle_bet(w, "won")  # +100
    _bet(account_id=cb, stake=200)               # open
    t = capital.capital_summary()["totals"]
    # starting 1000 + settled pnl 100 = 1100 (open stake is part of that)
    assert t["total_gross"] == 1100.0
    assert t["total_gross"] == round(t["equity"] + t["open_exposure"], 2)
    # net applies the gross-up commission to the whole book total (1100):
    # 1100 − 1100×6/94 = 1100 − 70.21 = 1029.79
    assert t["current_capital"] == pytest.approx(1029.79, abs=0.01)


def test_current_capital_includes_open_bet_stakes(clean_db):
    # current capital = (book balance + open bets) − commission — the open stake
    # is still book money the fee hits on withdrawal.
    cb = capital.add_account("CB", "cb")
    capital.add_entry(cb, "opening", 1000)
    _bet(account_id=cb, stake=200)               # open → balance 800, open 200
    s = capital.capital_summary()
    acct = next(a for a in s["accounts"] if a["id"] == cb)
    assert acct["balance"] == 800.0
    assert acct["current_value"] == pytest.approx(936.17, abs=0.01)   # 1000 − 1000×6/94
    assert s["totals"]["current_capital"] == pytest.approx(936.17, abs=0.01)


def test_current_capital_no_fee_on_negative_total(clean_db):
    cb = capital.add_account("CB", "cb")
    capital.add_entry(cb, "adjustment", -50)     # negative, nothing to withdraw
    s = capital.capital_summary()
    acct = next(a for a in s["accounts"] if a["id"] == cb)
    assert acct["current_value"] == -50.0


def test_roi_vs_yield_distinct(clean_db):
    cb = capital.add_account("CB", "cb")
    capital.add_entry(cb, "opening", 1000)
    w = _bet(account_id=cb, stake=100, odds_taken=2.0); bets.settle_bet(w, "won")  # +100
    s = capital.capital_summary()
    # yield = 100 / 100 turnover = 100%; roi = 100 / 1000 capital = 10%
    assert s["totals"]["yield_pct"] == pytest.approx(100.0, abs=0.01)
    assert s["totals"]["roi_pct"] == pytest.approx(10.0, abs=0.01)


def test_time_window_filters_performance_not_balances(clean_db):
    cb = capital.add_account("CB", "cb")
    capital.add_entry(cb, "opening", 500)
    old = _bet(account_id=cb, stake=100, odds_taken=2.0)
    bets.settle_bet(old, "won")  # +100
    bets.update_bet(old, settled_at="2020-01-01T00:00:00+00:00")  # ancient
    recent = _bet(account_id=cb, stake=50, odds_taken=2.0)
    bets.settle_bet(recent, "won")  # +50, settled now
    full = capital.capital_summary()
    windowed = capital.capital_summary(since="2026-01-01T00:00:00+00:00")
    # all-time: both count; windowed: only the recent one
    assert full["totals"]["settled_pnl"] == 150.0
    assert windowed["totals"]["settled_pnl"] == 50.0
    # balances stay all-time in BOTH (money is where it is now)
    assert full["totals"]["equity"] == windowed["totals"]["equity"] == 650.0
    # windowed curve has just the recent point
    assert len(windowed["pnl_curve"]) == 1


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
    assert len(capital.list_accounts()) == len(capital._DEFAULT_ACCOUNTS)
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


def test_set_balance_books_difference_as_pnl(clean_db):
    acc = capital.add_account("Betlive", "betlive")
    capital.add_entry(acc, "opening", 1000)
    delta = capital.set_balance(acc, 1250)          # I actually have 1250 now
    assert delta == 250.0
    t = capital.capital_summary()["totals"]
    assert t["manual_pnl"] == 250.0 and t["settled_pnl"] == 250.0 and t["net_pnl"] == 250.0
    row = next(a for a in capital.capital_summary()["accounts"] if a["id"] == acc)
    assert row["balance"] == 1250.0                 # balance reconciled
    assert capital.set_balance(acc, 1100) == -150.0  # a later drop books a loss
    assert capital.capital_summary()["totals"]["settled_pnl"] == 100.0
    assert capital.set_balance(acc, 1100) == 0.0     # idempotent when unchanged


def test_manual_pnl_adds_to_settled_but_not_yield(clean_db):
    acc = capital.add_account("CB", "cb")
    capital.add_entry(acc, "opening", 1000)
    w = _bet(account_id=acc, stake=100, odds_taken=2.0); bets.settle_bet(w, "won")  # +100
    capital.set_balance(acc, 1300)                  # balance was 1100 → +200 manual
    t = capital.capital_summary()["totals"]
    assert t["manual_pnl"] == 200.0
    assert t["settled_pnl"] == 300.0                # 100 logged + 200 manual
    assert t["yield_pct"] == pytest.approx(100.0)   # yield = logged bet only (100/100)
    assert t["roi_pct"] == pytest.approx(30.0)      # 300 / 1000


def test_set_open_exposure_moves_from_balance_not_pnl(clean_db):
    acc = capital.add_account("Betlive", "betlive")
    capital.add_entry(acc, "opening", 1000)
    assert capital.set_open_exposure(acc, 200)
    row = next(a for a in capital.capital_summary()["accounts"] if a["id"] == acc)
    t = capital.capital_summary()["totals"]
    assert row["open_stake"] == 200.0 and row["balance"] == 800.0   # 200 tied up
    assert t["open_exposure"] == 200.0 and t["total_gross"] == 1000.0  # reclassified
    assert t["settled_pnl"] == 0.0                                  # exposure isn't PnL
    # bets settle +50: clear exposure, set the new free balance → books the PnL
    capital.set_open_exposure(acc, 0)
    capital.set_balance(acc, 1050)
    t2 = capital.capital_summary()["totals"]
    assert t2["settled_pnl"] == 50.0 and t2["open_exposure"] == 0.0


def test_reconcile_account_sets_balance_and_exposure_in_one_step(clean_db):
    acc = capital.add_account("Betlive", "betlive")
    capital.add_entry(acc, "opening", 1000)
    # Read off the book: free balance 850, 200 tied up in open bets → it computes PnL.
    capital.reconcile_account(acc, balance=850, exposure=200)
    row = next(a for a in capital.capital_summary()["accounts"] if a["id"] == acc)
    t = capital.capital_summary()["totals"]
    assert row["balance"] == 850.0 and row["open_stake"] == 200.0
    assert t["total_gross"] == 1050.0 and t["settled_pnl"] == 50.0   # 1000→1050 = +50
    # same numbers again = no-op (no phantom PnL)
    capital.reconcile_account(acc, balance=850, exposure=200)
    assert capital.capital_summary()["totals"]["settled_pnl"] == 50.0
