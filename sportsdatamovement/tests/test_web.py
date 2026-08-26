"""The dashboard's API, against a store built here.

The queries are the fiddly part — reading a change log backwards to recover
what each price moved FROM — so they are tested on a board whose movements are
known by construction.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sportsdatamovement import web
from sportsdatamovement.store import Store


@pytest.fixture()
def client(tmp_path, monkeypatch):
    store = Store(tmp_path / "w.db")
    monkeypatch.setattr(web, "_store", store)

    def board(rows, *, book="crystalbet", sport="soccer"):
        with store.snapshot(book, sport) as w:
            for ev, mkt, side, odds in rows:
                w.add_event(ev, league="England, Premier League",
                            home="Liverpool", away="Nottingham",
                            start_time="2026-08-29T11:30:00+00:00")
                w.mark_read(ev)
                w.add(ev, mkt, mkt, side, odds)
            w.commit()

    # Three passes. "Main result / 1" drifts, then jumps; "HT/FT / 1/1" never
    # moves at all — the shape the whole project is looking for.
    board([("e1", "Main result", "1", 2.00), ("e1", "HT/FT", "1/1", 4.00)])
    board([("e1", "Main result", "1", 2.20), ("e1", "HT/FT", "1/1", 4.00)])
    board([("e1", "Main result", "1", 1.10), ("e1", "HT/FT", "1/1", 4.00)])
    return TestClient(web.app), store


# ── meta ──────────────────────────────────────────────────────────────────────

def test_meta_lists_the_scopes_and_passes(client):
    c, _ = client
    d = c.get("/api/meta").json()
    assert d["scopes"][0]["book"] == "crystalbet"
    assert len(d["passes"]) == 3
    assert d["db"]["positions"] == 2


# ── movers ────────────────────────────────────────────────────────────────────

def test_a_mover_carries_the_price_it_moved_from(client):
    """`odds` stores only the new price; the old one is the previous row for
    that position, which is the whole reason this query is a lookup."""
    c, _ = client
    rows = c.get("/api/movers", params={"book": "crystalbet", "sport": "soccer",
                                        "since_passes": 1}).json()["rows"]
    assert len(rows) == 1
    r = rows[0]
    assert (r["was"], r["odds"]) == (2.20, 1.10)
    assert r["pct"] == pytest.approx(-50.0)
    assert r["market"] == "Main result"


def test_a_market_that_never_moved_never_appears(client):
    c, _ = client
    rows = c.get("/api/movers", params={"since_passes": 5}).json()["rows"]
    assert all(r["market"] != "HT/FT" for r in rows)


def test_movers_sorts_by_the_size_of_the_move(client):
    c, _ = client
    rows = c.get("/api/movers", params={"since_passes": 5}).json()["rows"]
    sizes = [abs(r["pct"]) for r in rows]
    assert sizes == sorted(sizes, reverse=True)
    assert sizes[0] == pytest.approx(50.0)      # the 2.20 -> 1.10 jump


def test_the_first_price_of_a_position_is_not_a_move(client):
    """It has nothing to have moved from. Counting it would make every first
    pass look like the busiest hour of the week."""
    c, _ = client
    rows = c.get("/api/movers", params={"since_passes": 99}).json()["rows"]
    assert len(rows) == 2               # 2.00->2.20 and 2.20->1.10, not three


def test_a_magnitude_floor_filters_small_drift(client):
    c, _ = client
    rows = c.get("/api/movers", params={"since_passes": 99,
                                        "min_pct": 20}).json()["rows"]
    assert [round(r["pct"]) for r in rows] == [-50]


def test_search_matches_the_event_and_the_market(client):
    c, _ = client
    assert c.get("/api/movers", params={"since_passes": 99,
                                        "search": "Liverpool"}).json()["rows"]
    assert not c.get("/api/movers", params={"since_passes": 99,
                                            "search": "Arsenal"}).json()["rows"]


def test_the_pass_window_is_scoped_to_this_board(client):
    """"the last pass" has to mean one of THIS board's passes — the snapshots
    table interleaves 57 sports, so a global row count would be meaningless."""
    c, store = client
    with store.snapshot("liderbet", "tennis") as w:
        w.add_event("t1", home="A", away="B")
        w.mark_read("t1")
        w.add("t1", "Winner", "Winner", "1", 1.5)
        w.commit()
    d = c.get("/api/movers", params={"book": "crystalbet", "sport": "soccer",
                                     "since_passes": 1}).json()
    assert len(d["snapshot_ids"]) == 1
    assert d["rows"][0]["pct"] == pytest.approx(-50.0)


# ── the event grid ────────────────────────────────────────────────────────────

def test_the_grid_has_a_column_per_pass_and_a_row_per_position(client):
    c, store = client
    eid = c.get("/api/events").json()["rows"][0]["event_id"]
    d = c.get(f"/api/event/{eid}").json()
    assert len(d["columns"]) == 3
    assert {r["market"] for r in d["rows"]} == {"Main result", "HT/FT"}


def test_a_stale_market_has_one_filled_cell_and_the_rest_empty(client):
    """The gap IS the finding: no cell means the change log asserts the price
    was unchanged, not that nothing was recorded."""
    c, _ = client
    eid = c.get("/api/events").json()["rows"][0]["event_id"]
    d = c.get(f"/api/event/{eid}").json()
    htft = next(r for r in d["rows"] if r["market"] == "HT/FT")
    main = next(r for r in d["rows"] if r["market"] == "Main result")
    assert len(htft["cells"]) == 1 and htft["n_moves"] == 1
    assert len(main["cells"]) == 3 and main["n_moves"] == 3


def test_the_columns_run_oldest_first(client):
    c, _ = client
    eid = c.get("/api/events").json()["rows"][0]["event_id"]
    cols = c.get(f"/api/event/{eid}").json()["columns"]
    assert [x["id"] for x in cols] == sorted(x["id"] for x in cols)


def test_an_unknown_event_is_a_404(client):
    c, _ = client
    assert c.get("/api/event/999999").status_code == 404


# ── history ───────────────────────────────────────────────────────────────────

def test_history_returns_every_recorded_price(client):
    c, _ = client
    eid = c.get("/api/events").json()["rows"][0]["event_id"]
    row = next(r for r in c.get(f"/api/event/{eid}").json()["rows"]
               if r["market"] == "Main result")
    d = c.get(f"/api/history/{row['position_id']}").json()
    assert [p["odds"] for p in d["points"]] == [2.00, 2.20, 1.10]
    assert d["position"]["home"] == "Liverpool"


# ── markets ───────────────────────────────────────────────────────────────────

def test_the_market_leaderboard_separates_movers_from_the_still(client):
    c, _ = client
    d = c.get("/api/markets", params={"book": "crystalbet", "sport": "soccer",
                                      "since_passes": 3}).json()
    by = {r["market"]: r for r in d["rows"]}
    assert by["Main result"]["moves"] == 3
    assert by["HT/FT"]["moves"] == 1
    assert by["Main result"]["rate"] > by["HT/FT"]["rate"]


def test_markets_can_be_sorted_to_surface_the_stillest(client):
    c, _ = client
    rows = c.get("/api/markets", params={"since_passes": 3,
                                         "sort": "rate_asc"}).json()["rows"]
    assert rows[0]["market"] == "HT/FT"


# ── events ────────────────────────────────────────────────────────────────────

def test_events_are_ranked_by_how_much_of_the_board_moved(client):
    c, _ = client
    rows = c.get("/api/events", params={"since_passes": 3}).json()["rows"]
    assert rows[0]["positions"] == 2 and rows[0]["moves"] == 4


def test_the_page_is_served(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200 and "movement" in r.text


# ── ranking by probability, not by percentage ─────────────────────────────────

@pytest.fixture()
def longshot(tmp_path, monkeypatch):
    """A longshot drifting and a favourite crashing, in the same pass."""
    store = Store(tmp_path / "l.db")
    monkeypatch.setattr(web, "_store", store)
    for prices in ((40.0, 2.00), (60.0, 1.10)):
        with store.snapshot("crystalbet", "soccer") as w:
            w.add_event("e1", home="A", away="B")
            w.mark_read("e1")
            w.add("e1", "Correct score", "Correct score", "0:3", prices[0])
            w.add("e1", "Main result", "Main result", "1", prices[1])
            w.commit()
    return TestClient(web.app)


def test_the_default_ranking_is_the_probability_shift(longshot):
    """40.0 -> 60.0 is +50 % but 0.8pp; 2.00 -> 1.10 is -45 % but 41pp. Ranked
    by percentage the whole first page is longshot noise."""
    rows = longshot.get("/api/movers", params={"since_passes": 1}).json()["rows"]
    assert rows[0]["market"] == "Main result"
    assert rows[0]["pp"] == pytest.approx(40.9, abs=0.2)
    assert rows[1]["pp"] == pytest.approx(-0.83, abs=0.05)


def test_ranking_by_percentage_puts_the_longshot_first(longshot):
    rows = longshot.get("/api/movers", params={"since_passes": 1,
                                               "sort": "abs_pct"}).json()["rows"]
    assert rows[0]["market"] == "Correct score"


def test_a_max_odds_cap_removes_longshots_outright(longshot):
    rows = longshot.get("/api/movers", params={"since_passes": 1,
                                               "max_odds": 10}).json()["rows"]
    assert [r["market"] for r in rows] == ["Main result"]


def test_a_shortening_price_is_a_rising_probability(longshot):
    rows = longshot.get("/api/movers", params={"since_passes": 1}).json()["rows"]
    main = next(r for r in rows if r["market"] == "Main result")
    assert main["pct"] < 0 and main["pp"] > 0
