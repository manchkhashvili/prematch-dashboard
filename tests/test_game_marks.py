"""Game marks — "I have money on this GAME" (2026-07-28).

A mark is deliberately NOT a bet. A bet is one position in one market with
odds and a settlement; a mark says "I'm on this fixture, here's my total
across however many positions and books". It must never touch PnL, capital or
CLV — several tests below exist purely to pin that boundary down.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import bets

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
KEY = "soccer|lahti|sjk"


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("BETS_DB_PATH", str(tmp_path / "bets.db"))
    bets._reset_for_tests()
    bets.init_db()
    yield
    bets._reset_for_tests()


def _mark(game_key=KEY, **kw):
    base = dict(match_label="Lahti vs SJK", sport="soccer",
                start_time=NOW.isoformat(), amount=120.0)
    base.update(kw)
    return bets.upsert_mark(game_key, **base)


# ── create / read ────────────────────────────────────────────────────────────

def test_mark_round_trips():
    m = _mark()
    assert m["game_key"] == KEY
    assert m["amount"] == 120.0
    assert m["match_label"] == "Lahti vs SJK"
    assert [x["game_key"] for x in bets.list_marks()] == [KEY]


def test_amount_is_optional():
    """Marking without an amount is the common case — just highlight it."""
    m = bets.upsert_mark(KEY, match_label="Lahti vs SJK", amount=None)
    assert m["amount"] is None
    assert len(bets.list_marks()) == 1


def test_marking_the_same_game_updates_rather_than_stacking():
    """The amount is the TOTAL on the fixture, so a second mark replaces it."""
    _mark(amount=100.0)
    _mark(amount=250.0)
    rows = bets.list_marks()
    assert len(rows) == 1
    assert rows[0]["amount"] == 250.0


def test_update_preserves_start_time_when_the_caller_omits_it():
    """Pages that lack start_time (some move rows) must not blank it."""
    _mark(start_time=NOW.isoformat())
    bets.upsert_mark(KEY, match_label="Lahti vs SJK", start_time=None, amount=5.0)
    assert bets.list_marks()[0]["start_time"] == NOW.isoformat()


def test_distinct_games_are_distinct_marks():
    _mark()
    bets.upsert_mark("soccer|a|b", match_label="A vs B", amount=10.0)
    assert len(bets.list_marks()) == 2


# ── validation ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["", "   ", None])
def test_blank_game_key_rejected(key):
    with pytest.raises(ValueError):
        bets.upsert_mark(key, match_label="A vs B")


def test_blank_match_label_rejected():
    with pytest.raises(ValueError):
        bets.upsert_mark(KEY, match_label="  ")


def test_negative_amount_rejected():
    with pytest.raises(ValueError):
        bets.upsert_mark(KEY, match_label="A vs B", amount=-1.0)


def test_zero_amount_is_allowed():
    """0 means "marked, nothing staked yet" — the UI uses it to unmark, but
    the store must not editorialise."""
    assert bets.upsert_mark(KEY, match_label="A vs B", amount=0.0)["amount"] == 0.0


# ── delete / prune ───────────────────────────────────────────────────────────

def test_delete_removes_the_mark():
    _mark()
    assert bets.delete_mark(KEY) is True
    assert bets.list_marks() == []


def test_delete_missing_key_reports_false():
    assert bets.delete_mark("nope") is False


def test_prune_drops_games_that_already_kicked_off():
    old = (datetime.now(tz=timezone.utc) - timedelta(days=5)).isoformat()
    new = (datetime.now(tz=timezone.utc) + timedelta(days=1)).isoformat()
    bets.upsert_mark("old", match_label="Old", start_time=old)
    bets.upsert_mark("new", match_label="New", start_time=new)
    assert bets.prune_marks(days=2) == 1
    assert [m["game_key"] for m in bets.list_marks()] == ["new"]


@pytest.mark.parametrize("st", [None, ""])
def test_prune_keeps_marks_with_no_start_time(st):
    """We cannot tell whether they are stale, and deleting the user's own
    annotation on a guess is worse than leaving it."""
    bets.upsert_mark("nokick", match_label="No kickoff", start_time=st)
    assert bets.prune_marks(days=0) == 0
    assert len(bets.list_marks()) == 1


# ── the boundary: a mark is not a bet ────────────────────────────────────────

def test_marks_do_not_appear_as_bets():
    _mark(amount=999.0)
    assert bets.list_bets() == []


def test_marks_do_not_move_capital():
    from src import capital
    before = capital.capital_summary()["totals"]
    _mark(amount=999.0)
    after = capital.capital_summary()["totals"]
    assert before == after


# ── API layer ────────────────────────────────────────────────────────────────
# Endpoints are called directly (not via TestClient) — the lifespan handler
# would spawn live poll tasks. Same convention as tests/test_app_soccer.py.

import asyncio  # noqa: E402

from fastapi import HTTPException  # noqa: E402

from src import app as app_mod  # noqa: E402


def test_api_upsert_and_list():
    m = asyncio.run(app_mod.api_upsert_mark({
        "game_key": KEY, "match_label": "Lahti vs SJK",
        "sport": "soccer", "start_time": NOW.isoformat(), "amount": 75,
    }))
    assert m["amount"] == 75.0
    assert [x["game_key"] for x in asyncio.run(app_mod.api_list_marks())] == [KEY]


def test_api_accepts_a_numeric_string_amount():
    """The browser sends whatever prompt() returned."""
    m = asyncio.run(app_mod.api_upsert_mark(
        {"game_key": KEY, "match_label": "A vs B", "amount": "42.5"}))
    assert m["amount"] == 42.5


def test_api_rejects_a_non_numeric_amount():
    with pytest.raises(HTTPException) as e:
        asyncio.run(app_mod.api_upsert_mark(
            {"game_key": KEY, "match_label": "A vs B", "amount": "lots"}))
    assert e.value.status_code == 422


def test_api_rejects_a_negative_amount():
    with pytest.raises(HTTPException) as e:
        asyncio.run(app_mod.api_upsert_mark(
            {"game_key": KEY, "match_label": "A vs B", "amount": -5}))
    assert e.value.status_code == 422


def test_api_rejects_a_missing_game_key():
    with pytest.raises(HTTPException) as e:
        asyncio.run(app_mod.api_upsert_mark({"match_label": "A vs B"}))
    assert e.value.status_code == 422


def test_api_delete_handles_a_pipe_separated_key():
    """game_key contains '|' and team names — the route uses :path for this."""
    asyncio.run(app_mod.api_upsert_mark({"game_key": KEY, "match_label": "A vs B"}))
    assert asyncio.run(app_mod.api_delete_mark(KEY)) == {"deleted": True}
    assert asyncio.run(app_mod.api_list_marks()) == []


def test_api_list_prunes_kicked_off_games():
    old = (datetime.now(tz=timezone.utc) - timedelta(days=9)).isoformat()
    asyncio.run(app_mod.api_upsert_mark(
        {"game_key": "old", "match_label": "Old", "start_time": old}))
    assert asyncio.run(app_mod.api_list_marks()) == []


# ── hybrid keys: one mark per GAME, whichever page set it ────────────────────
# Live finding (2026-07-28): the same fixture appeared on the Arbs page three
# times — "Mjallby AIF" (setanta), "Mjallby" (liderbet), "Mjallby Aif"
# (betlive) — all sharing pin_event_id 1632802084. A name-only key gave one
# game two keys; consistency flags meanwhile carry no pin_event_id at all.

PIN = "pin:1632802084"
NAME_A = "soccer|lincolnredimps|mjallbyaif"
NAME_B = "soccer|lincolnredimps|mjallby"


def test_marking_by_pin_then_by_name_updates_one_row():
    """Arbs marks under the pin key; Anomalies later offers only a name key."""
    bets.upsert_mark(PIN, alt_key=NAME_A, match_label="Lincoln vs Mjallby", amount=100)
    bets.upsert_mark(NAME_A, alt_key=NAME_A, match_label="Lincoln vs Mjallby", amount=250)
    rows = bets.list_marks()
    assert len(rows) == 1
    assert rows[0]["game_key"] == PIN        # original row updated, not replaced
    assert rows[0]["amount"] == 250.0


def test_marking_by_name_then_by_pin_updates_one_row():
    bets.upsert_mark(NAME_A, alt_key=NAME_A, match_label="Lincoln vs Mjallby", amount=40)
    bets.upsert_mark(PIN, alt_key=NAME_A, match_label="Lincoln vs Mjallby", amount=60)
    rows = bets.list_marks()
    assert len(rows) == 1
    assert rows[0]["amount"] == 60.0


def test_two_books_spelling_a_game_differently_share_one_mark():
    """Different alt_key, same pin key — must not create a second mark."""
    bets.upsert_mark(PIN, alt_key=NAME_A, match_label="Lincoln vs Mjallby AIF", amount=10)
    bets.upsert_mark(PIN, alt_key=NAME_B, match_label="Lincoln vs Mjallby", amount=30)
    rows = bets.list_marks()
    assert len(rows) == 1
    assert rows[0]["amount"] == 30.0


def test_genuinely_different_games_still_get_their_own_marks():
    bets.upsert_mark(PIN, alt_key=NAME_A, match_label="Lincoln vs Mjallby")
    bets.upsert_mark("pin:999", alt_key="soccer|a|b", match_label="A vs B")
    assert len(bets.list_marks()) == 2


def test_delete_by_the_name_key_removes_a_pin_keyed_mark():
    bets.upsert_mark(PIN, alt_key=NAME_A, match_label="Lincoln vs Mjallby")
    assert bets.delete_mark(NAME_A) is True
    assert bets.list_marks() == []


def test_alt_key_column_is_migrated_onto_an_existing_table(tmp_path, monkeypatch):
    """game_marks shipped without alt_key for a few hours. CREATE TABLE IF NOT
    EXISTS does not backfill a column, so a DB created in that window must be
    ALTERed or every mark write fails."""
    import sqlite3
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE game_marks (
        game_key TEXT PRIMARY KEY, sport TEXT, match_label TEXT NOT NULL,
        start_time TEXT, amount REAL, note TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    con.execute(
        "INSERT INTO game_marks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("old", "soccer", "Old vs Game", None, 5.0, None,
         "2026-07-28T00:00:00+00:00", "2026-07-28T00:00:00+00:00"))
    con.commit(); con.close()

    monkeypatch.setenv("BETS_DB_PATH", str(db))
    bets._reset_for_tests()
    bets.init_db()
    try:
        assert "alt_key" in {r[1] for r in bets._require_conn().execute(
            "PRAGMA table_info(game_marks)")}
        assert bets.list_marks()[0]["amount"] == 5.0        # pre-existing row kept
        bets.upsert_mark("new", alt_key="soccer|a|b", match_label="A vs B", amount=1)
        assert len(bets.list_marks()) == 2                  # writes work after migration
    finally:
        bets._reset_for_tests()
