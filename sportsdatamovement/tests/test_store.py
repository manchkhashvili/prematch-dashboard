"""The change-only store must be lossless: everything the naive snapshot
schema would have held has to be reconstructable from the change log plus the
snapshot heartbeat."""
from __future__ import annotations

import pytest

from sportsdatamovement.store import Store, decode_price, encode_price


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "t.db")


def _pass(store, rows, *, book="liderbet", sport="soccer", ok=True,
          unread=()):
    """`unread` names events that were LISTED but whose markets we failed to
    read — the CrystalBet failed-expand case."""
    with store.snapshot(book, sport) as w:
        for ev, mkt, side, odds in rows:
            w.add_event(ev, league="L", home="A", away="B")
            if ev not in unread:
                w.mark_read(ev)
                w.add(ev, mkt, mkt, side, odds)
        for ev in unread:
            w.add_event(ev, league="L", home="A", away="B")
        return w.commit(ok=ok)


# ── encoding ──────────────────────────────────────────────────────────────────

def test_price_roundtrips_as_milli_odds():
    assert encode_price(1.85) == 1850
    assert decode_price(1850) == 1.85
    assert encode_price(24.0) == 24000


@pytest.mark.parametrize("bad", [None, 1.0, 0.0, -3.0, "x"])
def test_unbettable_prices_encode_as_none(bad):
    """1.01 is CrystalBet's locked-cell rendering and 1.0 is not a price. The
    collectors are expected to pass None; this is the backstop."""
    assert encode_price(bad) is None or bad == 1.01


# ── change-only semantics ─────────────────────────────────────────────────────

def test_second_pass_with_identical_odds_writes_nothing(store):
    rows = [("e1", "Main result", "1", 1.85), ("e1", "Main result", "2", 2.05)]
    first = _pass(store, rows)
    assert first["n_new"] == 2
    second = _pass(store, rows)
    assert (second["n_new"], second["n_moved"], second["n_gone"]) == (0, 0, 0)
    assert store.stats()["odds_rows"] == 2


def test_only_the_moved_position_gets_a_row(store):
    _pass(store, [("e1", "M", "1", 1.85), ("e1", "M", "2", 2.05)])
    res = _pass(store, [("e1", "M", "1", 1.90), ("e1", "M", "2", 2.05)])
    assert res["n_moved"] == 1
    assert store.stats()["odds_rows"] == 3


def test_a_suspended_position_is_null_not_its_displayed_price(store):
    """CrystalBet shows 1.01 on a locked cell. Storing that would log every
    suspension as a crash to 1.01 and every release as a recovery."""
    _pass(store, [("e1", "M", "1", 1.85)])
    res = _pass(store, [("e1", "M", "1", None)])
    assert res["n_gone"] == 1 and res["n_moved"] == 0
    board = store.snapshot_at("liderbet", "soccer", "9999")
    assert board == []                      # unpriced positions are not a board


def test_a_position_that_leaves_the_board_is_nulled(store):
    _pass(store, [("e1", "M", "1", 1.85), ("e1", "M", "2", 2.05)])
    res = _pass(store, [("e1", "M", "1", 1.85)])
    assert res["n_gone"] == 1
    board = store.snapshot_at("liderbet", "soccer", "9999")
    assert {r["side"] for r in board} == {"1"}


def test_a_duplicate_key_in_one_pass_is_not_a_price_move(store):
    """Two rows claiming one position is a collector bug. Booking the second
    as movement is how a first-ever Lider pass reported 1,856 moves."""
    with store.snapshot("liderbet", "soccer") as w:
        w.add_event("e1", home="A", away="B")
        w.mark_read("e1")
        w.add("e1", "m", "M", "1", 1.85)
        w.add("e1", "m", "M", "1", 9.99)      # same key, different price
        res = w.commit()
    assert (res["n_dupes"], res["n_moved"], res["n_new"]) == (1, 0, 1)
    assert store.snapshot_at("liderbet", "soccer")[0]["odds"] == 1.85


def test_an_event_that_leaves_the_board_is_nulled(store):
    _pass(store, [("e1", "M", "1", 1.85), ("e2", "M", "1", 2.05)])
    res = _pass(store, [("e1", "M", "1", 1.85)])
    assert res["n_gone"] == 1
    assert len(store.snapshot_at("liderbet", "soccer")) == 1


def test_a_failed_list_read_never_nulls_a_missing_event(store):
    """A partial read is the one thing that must not be mistaken for the book
    pulling its markets."""
    _pass(store, [("e1", "M", "1", 1.85), ("e2", "M", "1", 2.05)])
    res = _pass(store, [("e1", "M", "1", 1.85)], ok=False)
    assert res["n_gone"] == 0
    assert len(store.snapshot_at("liderbet", "soccer")) == 2


def test_an_event_listed_but_unread_is_left_alone(store):
    """A CrystalBet expand that fails leaves the game listed and its ~4,900
    positions unseen. Nulling them would book thousands of phantom departures
    and an equal number of returns on the next pass."""
    _pass(store, [("e1", "M", "1", 1.85), ("e2", "M", "1", 2.05)])
    res = _pass(store, [("e1", "M", "1", 1.85), ("e2", "M", "1", 2.05)],
                unread=("e2",))
    assert res["n_gone"] == 0
    assert len(store.snapshot_at("liderbet", "soccer")) == 2


def test_a_market_pulled_from_a_read_event_is_still_caught(store):
    """The precision that makes the above safe: within an event we DID read,
    a missing position really was pulled."""
    _pass(store, [("e1", "M", "1", 1.85), ("e1", "M", "2", 2.05)])
    res = _pass(store, [("e1", "M", "1", 1.85)])
    assert res["n_gone"] == 1


def test_a_position_first_seen_suspended_writes_no_row(store):
    res = _pass(store, [("e1", "M", "1", None)])
    assert res["n_new"] == 0
    assert store.stats()["odds_rows"] == 0


# ── reconstruction ────────────────────────────────────────────────────────────

def test_board_reconstructs_the_last_value_of_every_position(store):
    _pass(store, [("e1", "M", "1", 1.85), ("e1", "M", "2", 2.05)])
    _pass(store, [("e1", "M", "1", 1.90), ("e1", "M", "2", 2.05)])
    board = {r["side"]: r["odds"] for r in
             store.snapshot_at("liderbet", "soccer", "9999")}
    # "2" never ticked in pass 2 and must still be on the board at its old price
    assert board == {"1": 1.90, "2": 2.05}


def test_board_at_an_earlier_pass_ignores_later_moves(store):
    first = _pass(store, [("e1", "M", "1", 1.85)])
    _pass(store, [("e1", "M", "1", 5.00)])
    board = store.snapshot_at("liderbet", "soccer",
                              snapshot_id=first["snapshot_id"])
    assert board[0]["odds"] == 1.85
    assert store.snapshot_at("liderbet", "soccer")[0]["odds"] == 5.00


def test_a_restart_does_not_re_report_the_whole_board_as_new(store, tmp_path):
    """The board diff is rebuilt from the database, not carried in memory."""
    _pass(store, [("e1", "M", "1", 1.85)])
    reopened = Store(tmp_path / "t.db")
    res = _pass(reopened, [("e1", "M", "1", 1.85)])
    assert (res["n_new"], res["n_moved"]) == (0, 0)


# ── identity ──────────────────────────────────────────────────────────────────

def test_two_lines_of_one_market_are_two_positions(store):
    with store.snapshot("liderbet", "soccer") as w:
        w.add_event("e1", home="A", away="B")
        w.add("e1", "mt:16:502", "Total {total}", "Over", 1.85, line=2.5)
        w.add("e1", "mt:16:502", "Total {total}", "Over", 2.60, line=3.5)
        res = w.commit()
    assert res["n_new"] == 2
    assert store.stats()["positions"] == 2


def test_a_line_moving_under_one_label_is_a_new_position_not_a_price_move(store):
    """CrystalBet re-hangs a line on a stable selection id ("21.5 Und" became
    "22.5 Und" on 30 of 4,615 ids in 20 minutes). Booking that as a price move
    would quote a line the book no longer offers."""
    _pass(store, [("e1", "Fouls", "21.5 Und", 1.94)])
    res = _pass(store, [("e1", "Fouls", "22.5 Und", 2.06)])
    assert res["n_new"] == 1        # the new rung
    assert res["n_gone"] == 1       # the old one left the board
    assert res["n_moved"] == 0


def test_the_same_position_on_two_books_stays_separate(store):
    _pass(store, [("e1", "M", "1", 1.85)], book="liderbet")
    _pass(store, [("e1", "M", "1", 2.50)], book="crystalbet")
    assert store.stats()["positions"] == 2
    assert store.snapshot_at("liderbet", "soccer", "9999")[0]["odds"] == 1.85
    assert store.snapshot_at("crystalbet", "soccer", "9999")[0]["odds"] == 2.50


def test_no_line_and_line_zero_are_different_positions(store):
    """A pick'em (line 0.0) is a real market. The NULL sentinel must not
    collide with it."""
    with store.snapshot("liderbet", "soccer") as w:
        w.add_event("e1", home="A", away="B")
        w.add("e1", "m", "Handicap", "1", 1.85, line=0.0)
        w.add("e1", "m", "Handicap", "1", 1.90, line=None)
        res = w.commit()
    assert res["n_new"] == 2


# ── bookkeeping ───────────────────────────────────────────────────────────────

def test_a_snapshot_is_marked_incomplete_until_it_commits(store):
    with store.snapshot("liderbet", "soccer") as w:
        rows = store.recent_snapshots(1)
        assert rows[0]["ok"] == 0 and rows[0]["finished_at"] is None
        w.add_event("e1", home="A", away="B")
        w.add("e1", "m", "M", "1", 1.85)
        w.commit()
    assert store.recent_snapshots(1)[0]["ok"] == 1


def test_an_exception_inside_a_snapshot_marks_it_failed(store):
    with pytest.raises(ValueError):
        with store.snapshot("liderbet", "soccer") as w:
            w.add_event("e1", home="A", away="B")
            raise ValueError("boom")
    row = store.recent_snapshots(1)[0]
    assert row["ok"] == 0 and "boom" in row["error"]


def test_prune_drops_stale_events_and_their_history(store):
    _pass(store, [("e1", "M", "1", 1.85)])
    assert store.prune(keep_days=-1)["events"] == 1   # everything is "old"
    s = store.stats()
    assert (s["events"], s["positions"], s["odds_rows"]) == (0, 0, 0)


def test_prune_collects_orphaned_markets_and_sides(store):
    """Both books encode players into the market identity, so `markets` runs to
    tens of thousands of fixture-specific rows. Left behind they outlive their
    events forever."""
    _pass(store, [("e1", "Anytime scorer Soula, Mazire", "Yes", 4.2)])
    assert store.stats()["markets"] == 1
    dropped = store.prune(keep_days=-1)
    assert dropped["markets"] == 1 and dropped["sides"] == 1
    s = store.stats()
    assert (s["markets"], s["sides"]) == (0, 0)


def test_prune_keeps_dimensions_still_in_use(store):
    _pass(store, [("e1", "M", "1", 1.85)])
    assert store.prune(keep_days=7)["markets"] == 0
    assert store.stats()["markets"] == 1


def test_prune_keeps_events_still_on_the_board(store):
    _pass(store, [("e1", "M", "1", 1.85)])
    assert store.prune(keep_days=7)["events"] == 0
    assert store.stats()["positions"] == 1


def test_a_writer_can_be_filled_from_another_thread(store):
    """Lider's whole pass runs inside one `asyncio.to_thread`, so the writer is
    opened on the event loop and filled from a worker."""
    import threading
    with store.snapshot("liderbet", "soccer") as w:
        def fill():
            w.add_event("e1", home="A", away="B")
            w.mark_read("e1")
            w.add("e1", "m", "M", "1", 1.85)
        t = threading.Thread(target=fill)
        t.start()
        t.join()
        res = w.commit()
    assert res["n_new"] == 1


# ── the collector lock ────────────────────────────────────────────────────────

def test_a_second_collector_is_refused(store):
    """Two loops against one database interleave silently: each reads a
    `latest` the other just updated, so both attribute the other's movement to
    themselves. Three hours of data were lost to this on 2026-08-26."""
    import pytest as _pytest
    from sportsdatamovement import runner
    with runner.exclusive(store):
        with _pytest.raises(SystemExit) as exc:
            with runner.exclusive(store):
                pass
    assert "another collector is already running" in str(exc.value)


def test_the_lock_is_released_when_the_collector_finishes(store):
    from sportsdatamovement import runner
    with runner.exclusive(store):
        pass
    with runner.exclusive(store):          # must not raise
        pass


# ── undoing a bad pass ────────────────────────────────────────────────────────

def test_dropping_a_pass_removes_its_ticks_and_restores_the_baseline(store):
    """A bad pass moves the baseline: `latest` is what the NEXT pass diffs
    against, so deleting its ticks without rebuilding `latest` leaves the
    following pass wrong too."""
    _pass(store, [("e1", "M", "1", 2.00)])
    _pass(store, [("e1", "M", "1", 2.20)])
    bad = _pass(store, [("e1", "M", "1", 9.99)])
    assert store.snapshot_at("liderbet", "soccer")[0]["odds"] == 9.99

    out = store.drop_snapshots([bad["snapshot_id"]])
    assert out["odds"] == 1
    assert store.snapshot_at("liderbet", "soccer")[0]["odds"] == 2.20


def test_a_pass_after_an_undo_diffs_against_the_restored_baseline(store):
    _pass(store, [("e1", "M", "1", 2.00)])
    bad = _pass(store, [("e1", "M", "1", 9.99)])
    store.drop_snapshots([bad["snapshot_id"]])
    res = _pass(store, [("e1", "M", "1", 2.00)])
    assert (res["n_moved"], res["n_new"]) == (0, 0)   # nothing actually changed


def test_undoing_the_only_pass_leaves_a_position_unpriced(store):
    first = _pass(store, [("e1", "M", "1", 2.00)])
    store.drop_snapshots([first["snapshot_id"]])
    assert store.snapshot_at("liderbet", "soccer") == []
    assert store.stats()["odds_rows"] == 0


def test_dropping_nothing_is_a_no_op(store):
    _pass(store, [("e1", "M", "1", 2.00)])
    assert store.drop_snapshots([])["snapshots"] == 0
    assert store.stats()["odds_rows"] == 1
