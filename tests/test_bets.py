"""
Tests for src.bets — SQLite-backed bet tracker.

Each test uses a fresh temp DB via the `clean_db` fixture so we don't pollute
data/bets.db. The fixture also resets the connection cache between tests.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src import bets


@pytest.fixture
def clean_db(tmp_path):
    """Open a fresh SQLite DB at a temp path; tear down between tests."""
    bets._reset_for_tests()
    db_path = tmp_path / "test_bets.db"
    bets.init_db(db_path)
    yield db_path
    bets._reset_for_tests()


# ── Schema + init ────────────────────────────────────────────────────────────

def test_init_db_creates_tables(clean_db):
    # Re-init at same path is a no-op (covers the early-return branch).
    bets.init_db(clean_db)
    assert bets.list_bets() == []


def test_init_db_is_idempotent(clean_db):
    bets.init_db(clean_db)
    bets.init_db(clean_db)
    bets.init_db(clean_db)
    # Should still be able to insert + read.
    bid = _make_bet()
    assert bets.get_bet(bid) is not None


# ── create_bet ───────────────────────────────────────────────────────────────

def _make_bet(**overrides) -> int:
    fields = {
        "sport": "basketball",
        "match_label": "Lakers vs Warriors",
        "period": "FT",
        "market_type": "moneyline",
        "side": "home",
        "book": "cb",
        "odds_taken": 2.05,
        "stake": 20.0,
        "bankroll_at_time": 500.0,
    }
    fields.update(overrides)
    return bets.create_bet(**fields)


def test_create_bet_basic(clean_db):
    bid = _make_bet()
    assert isinstance(bid, int) and bid > 0
    bet = bets.get_bet(bid)
    assert bet["status"] == "open"
    assert bet["odds_taken"] == 2.05
    assert bet["placed_at"] is not None


def test_create_bet_rejects_missing_required(clean_db):
    with pytest.raises(ValueError, match="missing required"):
        bets.create_bet(sport="basketball")


def test_create_bet_rejects_odds_le_1(clean_db):
    with pytest.raises(ValueError, match="odds_taken"):
        _make_bet(odds_taken=1.0)


def test_create_bet_rejects_zero_stake(clean_db):
    with pytest.raises(ValueError, match="stake"):
        _make_bet(stake=0)


def test_create_bet_accepts_any_book(clean_db):
    # book is free-form (it's the account's tag) — any non-empty name is first-class
    bid = _make_bet(book="fanduel")
    assert bets.get_bet(bid)["book"] == "fanduel"
    # blank/non-string still rejected (schema is NOT NULL)
    with pytest.raises(ValueError, match="book"):
        _make_bet(book="   ")


def test_create_bet_accepts_optional_fields(clean_db):
    bid = _make_bet(
        cb_event_id="evt-123",
        line=-3.5,
        submarket=None,
        team_side=None,
        pin_fair_at_placement=2.00,
        cb_fair_at_placement=2.05,
        edge_at_placement_pct=2.5,
        note="testing",
        start_time="2026-05-28T15:00:00+00:00",
    )
    bet = bets.get_bet(bid)
    assert bet["cb_event_id"] == "evt-123"
    assert bet["line"] == -3.5
    assert bet["note"] == "testing"
    assert bet["pin_fair_at_placement"] == 2.00


# ── list_bets ────────────────────────────────────────────────────────────────

def test_list_bets_empty(clean_db):
    assert bets.list_bets() == []


def test_list_bets_status_filters(clean_db):
    b1 = _make_bet()
    b2 = _make_bet(match_label="A vs B")
    b3 = _make_bet(match_label="C vs D")
    bets.settle_bet(b1, "won")
    bets.settle_bet(b2, "lost")
    assert len(bets.list_bets()) == 3
    assert len(bets.list_bets("open")) == 1
    assert len(bets.list_bets("settled")) == 2
    assert len(bets.list_bets("won")) == 1
    assert len(bets.list_bets("lost")) == 1


def test_list_bets_unknown_filter(clean_db):
    with pytest.raises(ValueError, match="unknown status filter"):
        bets.list_bets("garbage")


# ── settle_bet ───────────────────────────────────────────────────────────────

def test_settle_bet_won_default_payout(clean_db):
    bid = _make_bet(odds_taken=2.10, stake=10.0)
    assert bets.settle_bet(bid, "won")
    bet = bets.get_bet(bid)
    assert bet["status"] == "won"
    assert bet["payout"] == 21.0   # stake * odds


def test_settle_bet_lost(clean_db):
    bid = _make_bet()
    bets.settle_bet(bid, "lost")
    assert bets.get_bet(bid)["payout"] == 0.0


def test_settle_bet_pushed_returns_stake(clean_db):
    bid = _make_bet(stake=30.0)
    bets.settle_bet(bid, "pushed")
    assert bets.get_bet(bid)["payout"] == 30.0


def test_settle_bet_void_returns_stake(clean_db):
    bid = _make_bet(stake=15.0)
    bets.settle_bet(bid, "void")
    assert bets.get_bet(bid)["payout"] == 15.0


def test_settle_bet_custom_payout_overrides_default(clean_db):
    bid = _make_bet(odds_taken=2.50, stake=10.0)
    bets.settle_bet(bid, "won", payout=15.0)  # half-won partial cashout scenario
    assert bets.get_bet(bid)["payout"] == 15.0


def test_settle_bet_rejects_unknown_outcome(clean_db):
    bid = _make_bet()
    with pytest.raises(ValueError, match="settle outcome"):
        bets.settle_bet(bid, "garbage")


def test_settle_bet_can_resettle_to_fix_outcome(clean_db):
    # 2026-06-13: re-settling an already-settled bet is allowed (mis-click fix).
    bid = _make_bet(stake=100, odds_taken=2.0)
    bets.settle_bet(bid, "won")
    assert bets.get_bet(bid)["payout"] == 200.0
    bets.settle_bet(bid, "lost")        # change the result
    b = bets.get_bet(bid)
    assert b["status"] == "lost" and b["payout"] == 0.0


def test_settle_bet_reopen_clears_settlement(clean_db):
    bid = _make_bet()
    bets.settle_bet(bid, "won")
    assert bets.settle_bet(bid, "open")
    b = bets.get_bet(bid)
    assert b["status"] == "open"
    assert b["payout"] is None and b["settled_at"] is None


def test_settle_bet_returns_false_for_missing(clean_db):
    assert not bets.settle_bet(99999, "won")


# ── delete_bet ───────────────────────────────────────────────────────────────

def test_delete_bet(clean_db):
    bid = _make_bet()
    assert bets.delete_bet(bid)
    assert bets.get_bet(bid) is None
    assert not bets.delete_bet(bid)  # second delete = no-op


def test_delete_bet_cascades_history(clean_db):
    bid = _make_bet()
    bets.record_history_snapshot(bid, 2.05, 2.00)
    bets.record_history_snapshot(bid, 2.06, 2.01,
                                  recorded_at="2026-05-27T16:00:01+00:00")
    assert len(bets.get_history(bid)) == 2
    bets.delete_bet(bid)
    assert bets.get_history(bid) == []


# ── update_bet ───────────────────────────────────────────────────────────────

def test_update_bet_note(clean_db):
    bid = _make_bet()
    assert bets.update_bet(bid, note="updated")
    assert bets.get_bet(bid)["note"] == "updated"


def test_update_bet_rejects_disallowed_field(clean_db):
    # odds_taken/stake/market spec became editable 2026-06-12 (in-place bet
    # editing); identity + placement-snapshot fields stay immutable.
    bid = _make_bet()
    with pytest.raises(ValueError, match="cannot modify"):
        bets.update_bet(bid, placed_at="2020-01-01T00:00:00+00:00")
    with pytest.raises(ValueError, match="cannot modify"):
        bets.update_bet(bid, bankroll_at_time=1.0)


def test_update_bet_pin_fair_closing(clean_db):
    bid = _make_bet()
    bets.update_bet(bid, pin_fair_closing=1.85)
    assert bets.get_bet(bid)["pin_fair_closing"] == 1.85


# ── history ──────────────────────────────────────────────────────────────────

def test_record_history_inserts_row(clean_db):
    bid = _make_bet()
    bets.record_history_snapshot(bid, 2.05, 2.00,
                                  recorded_at="2026-05-27T16:00:00+00:00")
    hist = bets.get_history(bid)
    assert len(hist) == 1
    assert hist[0]["cb_decimal"] == 2.05
    assert hist[0]["pin_fair_decimal"] == 2.00


def test_record_history_dedupes_same_timestamp(clean_db):
    bid = _make_bet()
    ts = "2026-05-27T16:00:00+00:00"
    bets.record_history_snapshot(bid, 2.05, 2.00, recorded_at=ts)
    bets.record_history_snapshot(bid, 2.10, 2.05, recorded_at=ts)  # same ts → ignored
    hist = bets.get_history(bid)
    assert len(hist) == 1
    assert hist[0]["cb_decimal"] == 2.05  # first write wins


def test_record_history_noop_when_both_none(clean_db):
    bid = _make_bet()
    bets.record_history_snapshot(bid, None, None)
    assert bets.get_history(bid) == []


def test_record_history_one_side_present(clean_db):
    bid = _make_bet()
    bets.record_history_snapshot(bid, None, 2.00)
    bets.record_history_snapshot(bid, 2.05, None,
                                  recorded_at="2026-05-27T16:00:01+00:00")
    hist = bets.get_history(bid)
    assert len(hist) == 2


def test_get_history_ordered_ascending(clean_db):
    bid = _make_bet()
    bets.record_history_snapshot(bid, 2.05, 2.00, recorded_at="2026-05-27T16:00:02+00:00")
    bets.record_history_snapshot(bid, 2.06, 2.01, recorded_at="2026-05-27T16:00:00+00:00")
    bets.record_history_snapshot(bid, 2.07, 2.02, recorded_at="2026-05-27T16:00:01+00:00")
    hist = bets.get_history(bid)
    assert [h["recorded_at"] for h in hist] == [
        "2026-05-27T16:00:00+00:00",
        "2026-05-27T16:00:01+00:00",
        "2026-05-27T16:00:02+00:00",
    ]


# ── open_bet_ids ─────────────────────────────────────────────────────────────

def test_open_bet_ids(clean_db):
    b1 = _make_bet()
    b2 = _make_bet(match_label="A vs B")
    b3 = _make_bet(match_label="C vs D")
    bets.settle_bet(b2, "won")
    ids = bets.open_bet_ids()
    assert set(ids) == {b1, b3}


# ── Cash-out + in-place editing (2026-06-12) ──────────────────────────────────

class TestCashout:
    def test_cashout_requires_payout(self, clean_db):
        bid = _make_bet()
        with pytest.raises(ValueError, match="cashout needs"):
            bets.settle_bet(bid, "cashout")

    def test_cashout_settles_with_given_payout(self, clean_db):
        bid = _make_bet()
        assert bets.settle_bet(bid, "cashout", payout=72.50)
        b = bets.get_bet(bid)
        assert b["status"] == "cashout"
        assert b["payout"] == 72.50
        assert b["settled_at"] is not None

    def test_cashout_listed_as_settled(self, clean_db):
        bid = _make_bet()
        bets.settle_bet(bid, "cashout", payout=10)
        assert [b["id"] for b in bets.list_bets(status="settled")] == [bid]
        assert bets.list_bets(status="open") == []


class TestEditBet:
    def test_stake_and_odds_editable(self, clean_db):
        bid = _make_bet()
        assert bets.update_bet(bid, stake=100.0, odds_taken=2.05)
        b = bets.get_bet(bid)
        assert b["stake"] == 100.0 and b["odds_taken"] == 2.05

    def test_rounding_a_stake_after_settlement(self, clean_db):
        # The motivating case: stake logged as 100.01 (old step bug), bet
        # already settled — round both stake and payout in place.
        bid = _make_bet(stake=100.01)
        bets.settle_bet(bid, "won")           # payout 100.01 * odds
        assert bets.update_bet(bid, stake=100.0, payout=190.0)
        b = bets.get_bet(bid)
        assert b["stake"] == 100.0 and b["payout"] == 190.0

    def test_edit_validation(self, clean_db):
        bid = _make_bet()
        with pytest.raises(ValueError):
            bets.update_bet(bid, stake=0)
        with pytest.raises(ValueError):
            bets.update_bet(bid, odds_taken=1.0)
        with pytest.raises(ValueError):
            bets.update_bet(bid, book="")          # blank book rejected
        bets.update_bet(bid, book="1xbet")          # any non-empty book accepted
        assert bets.get_bet(bid)["book"] == "1xbet"

    def test_account_and_market_editable(self, clean_db):
        bid = _make_bet()
        assert bets.update_bet(bid, account_id=7, market_type="spread",
                               line=-3.5, side="away", match_label="X vs Y")
        b = bets.get_bet(bid)
        assert (b["account_id"], b["market_type"], b["line"], b["side"]) \
            == (7, "spread", -3.5, "away")


# ── Parlays (bet_legs) ────────────────────────────────────────────────────────

def _leg(**o):
    leg = {"sport": "soccer", "match_label": "PSG vs Villa", "period": "FT",
           "market_type": "moneyline", "side": "home", "odds": 1.5}
    leg.update(o)
    return leg


def test_add_leg_converts_single_to_parlay(clean_db):
    bid = _make_bet(odds_taken=2.0, stake=20.0)     # leg 1 @ 2.0
    bets.add_leg(bid, **_leg(odds=1.5))             # leg 2 @ 1.5
    b = bets.get_bet(bid)
    legs = bets.list_legs(bid)
    assert b["is_parlay"] == 1
    assert len(legs) == 2
    assert [l["leg_index"] for l in legs] == [1, 2]
    assert legs[0]["odds"] == 2.0 and legs[1]["odds"] == 1.5   # leg 1 migrated
    assert b["odds_taken"] == pytest.approx(3.0)               # combined 2.0×1.5
    assert b["stake"] == 20.0 and b["status"] == "open"        # stake untouched


def test_add_third_leg_multiplies(clean_db):
    bid = _make_bet(odds_taken=2.0)
    bets.add_leg(bid, **_leg(odds=1.5))
    bets.add_leg(bid, **_leg(odds=2.0, match_label="A vs B"))
    assert len(bets.list_legs(bid)) == 3
    assert bets.get_bet(bid)["odds_taken"] == pytest.approx(6.0)  # 2×1.5×2


def test_add_leg_rejects_settled_bet(clean_db):
    bid = _make_bet()
    bets.settle_bet(bid, "won")
    with pytest.raises(ValueError, match="open"):
        bets.add_leg(bid, **_leg())


def test_add_leg_rejects_bad_odds(clean_db):
    bid = _make_bet()
    with pytest.raises(ValueError, match="odds"):
        bets.add_leg(bid, **_leg(odds=1.0))


def test_parlay_all_won_pays_product(clean_db):
    bid = _make_bet(odds_taken=2.0, stake=10.0)
    bets.add_leg(bid, **_leg(odds=1.5))             # combined 3.0
    bets.settle_leg(bid, 1, "won")
    assert bets.get_bet(bid)["status"] == "open"    # still one leg open
    bets.settle_leg(bid, 2, "won")
    b = bets.get_bet(bid)
    assert b["status"] == "won"
    assert b["payout"] == pytest.approx(30.0)       # 10 × 2.0 × 1.5


def test_parlay_any_leg_lost_loses_whole(clean_db):
    bid = _make_bet(odds_taken=2.0, stake=10.0)
    bets.add_leg(bid, **_leg(odds=1.5))
    bets.settle_leg(bid, 1, "won")
    bets.settle_leg(bid, 2, "lost")
    b = bets.get_bet(bid)
    assert b["status"] == "lost" and b["payout"] == 0.0


def test_parlay_pushed_leg_drops_and_recomputes(clean_db):
    bid = _make_bet(odds_taken=2.0, stake=10.0)
    bets.add_leg(bid, **_leg(odds=1.5))
    bets.settle_leg(bid, 1, "pushed")               # leg 1 drops (factor 1.0)
    bets.settle_leg(bid, 2, "won")
    b = bets.get_bet(bid)
    assert b["status"] == "won"
    assert b["odds_taken"] == pytest.approx(1.5)    # only the surviving leg
    assert b["payout"] == pytest.approx(15.0)       # 10 × 1.5


def test_parlay_all_pushed_refunds_stake(clean_db):
    bid = _make_bet(odds_taken=2.0, stake=10.0)
    bets.add_leg(bid, **_leg(odds=1.5))
    bets.settle_leg(bid, 1, "pushed")
    bets.settle_leg(bid, 2, "void")
    b = bets.get_bet(bid)
    assert b["status"] == "pushed" and b["payout"] == pytest.approx(10.0)


def test_parlay_reopen_leg_returns_to_open(clean_db):
    bid = _make_bet(odds_taken=2.0, stake=10.0)
    bets.add_leg(bid, **_leg(odds=1.5))
    bets.settle_leg(bid, 1, "won")
    bets.settle_leg(bid, 2, "won")
    assert bets.get_bet(bid)["status"] == "won"
    bets.settle_leg(bid, 2, "open")                 # un-settle one leg
    b = bets.get_bet(bid)
    assert b["status"] == "open" and b["payout"] is None and b["settled_at"] is None


def test_remove_leg_folds_back_to_single(clean_db):
    bid = _make_bet(odds_taken=2.0, stake=10.0, match_label="Lakers vs Warriors")
    bets.add_leg(bid, **_leg(odds=1.5, match_label="PSG vs Villa"))
    bets.remove_leg(bid, 2)                          # drop the added leg
    b = bets.get_bet(bid)
    assert b["is_parlay"] == 0 and bets.list_legs(bid) == []
    assert b["odds_taken"] == 2.0 and b["match_label"] == "Lakers vs Warriors"
