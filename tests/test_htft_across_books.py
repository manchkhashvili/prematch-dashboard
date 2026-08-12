"""The HT/FT grid on Crocobet and Setanta (2026-08-13).

Companion to tests/test_liderbet_detail.py. Owner asked why every consistency
flag said "cb"; for these two books the answer was that their per-event ladders
were ALREADY being fetched and parsed by the main scraper, and we mapped 3 of
99 gameTypes (Crocobet soccer) and 4 of 34 marketTypes (Setanta soccer). Adding
the rest costs no extra requests at all.

Both books were mapped by market SHAPE — outcome count and labels — never by
name. Crocobet's names are Georgian and, critically, the ①/②/③ suffix is the
STATISTIC rather than decoration: `8 golebis r-ba ①` is a goals total and
`23 kutkhurebis r-ba ②` a CORNERS total with an identical outcome shape. Shape
alone is not sufficient; every code here was confirmed ① and then price-checked.

Verification, live 2026-08-13:
  * vs Pinnacle — Crocobet H1 moneyline 0.56pp median (n=372), H1 total 1.20pp
    (n=1033); Setanta H1 moneyline 0.32pp (n=39), H1 total 0.59pp (n=142).
  * the grids by INTERNAL identity, which needs no reference book: rows must
    sum to the H1 1X2 and columns to the FT 1X2. Medians — Lider 0.60/0.65pp,
    Crocobet 0.69/0.79pp, Setanta 0.32/0.99pp. A mis-ordered cell map breaks
    both sums immediately, so this is the check that pins the ordering.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.scrapers import crocobet as C
from src.scrapers import setanta as ST

NOW = datetime.now(tz=timezone.utc)
CELLS = ("1/1", "1/X", "1/2", "X/1", "X/X", "X/2", "2/1", "2/X", "2/2")


# ── Crocobet ─────────────────────────────────────────────────────────────────

def _croco_event():
    return {"eventId": 1, "remoteId": 999, "eventStart": None,
            "category3Name": "League",
            "participants": [{"number": 1, "name": "Home"},
                             {"number": 2, "name": "Away"}]}


def _croco_game(gt, outs, arg=None):
    return {"gameType": gt, "argument": arg,
            "outcomes": [{"outcomeName": n, "outcomeOdds": v} for n, v in outs]}


def test_crocobet_htft_grid_is_parsed():
    """gameType 5, 'taimboli ①' — the nine cells arrive already labelled."""
    odds = [2.95, 17.0, 70.0, 4.6, 4.0, 9.6, 40.0, 19.0, 9.0]
    rows = C._parse_event(_croco_event(),
                          [_croco_game(5, list(zip(CELLS, odds)))], "soccer", NOW)
    assert len(rows) == 1
    o = rows[0]
    assert (o.market_type, o.period, o.line) == ("htft", "FT", None)
    assert sorted(o.selections) == sorted(CELLS)
    assert o.sr_match_id == "999"


def test_crocobet_partial_htft_grid_is_rejected():
    """All nine or nothing — the consistency check reasons about the grid as a
    distribution, so a half-read grid would look like a half-priced book."""
    odds = [2.95, 17.0, 70.0, 4.6, 4.0, 9.6, 40.0, 19.0]
    rows = C._parse_event(_croco_event(),
                          [_croco_game(5, list(zip(CELLS[:8], odds)))], "soccer", NOW)
    assert rows == []


def test_crocobet_half_results_and_totals_carry_their_period():
    games = [
        _croco_game(3, [("1", 2.45), ("X", 1.85), ("2", 5.4)]),
        _croco_game(111, [("1", 2.6), ("X", 2.0), ("2", 4.0)]),
        _croco_game(-284, [("ნაკლები", 1.6), ("მეტი", 2.3)], arg=1.5),
        _croco_game(-30335, [("ნაკლები", 1.7), ("მეტი", 2.1)], arg=1.5),
    ]
    rows = C._parse_event(_croco_event(), games, "soccer", NOW)
    got = {(o.market_type, o.period, o.line) for o in rows}
    assert got == {("moneyline", "H1", None), ("moneyline", "H2", None),
                   ("total", "H1", 1.5), ("total", "H2", 1.5)}
    for o in rows:
        if o.market_type == "moneyline":
            assert set(o.selections) == {"home", "draw", "away"}


def test_crocobet_corner_and_card_markets_are_not_mapped():
    """The trap this book sets: `23 kutkhurebis r-ba ②` is a CORNERS total with
    the same outcome shape as a goals total. Mapping on shape alone would price
    corners as goals."""
    for gt in (23, 13, -255, 105, -2975, -168, 160, 171):
        assert gt not in C._GAMETYPE["soccer"], f"gameType {gt} is not a goals market"


def test_crocobet_three_way_european_handicap_stays_unmapped():
    assert -6048 not in C._GAMETYPE["soccer"]


def test_crocobet_double_chance_is_not_a_moneyline():
    """gameType 4 is 1X / 12 / X2 — three outcomes like a 1X2, but a different
    market. Shape is necessary, not sufficient."""
    assert 4 not in C._GAMETYPE["soccer"]


# ── Setanta ──────────────────────────────────────────────────────────────────

def test_setanta_htft_cell_map_is_complete_and_ordered():
    """outcomeTypes 16..24 in feed order. The book's own dictionary renders
    19-21 as 'Х / 1', 'Х / Х', 'Х / 2', which anchors the middle row; the odds
    anchor the rest (the reversal cells price like reversal cells)."""
    assert ST._OUT_HTFT == {16: "1/1", 17: "1/X", 18: "1/2",
                            19: "X/1", 20: "X/X", 21: "X/2",
                            22: "2/1", 23: "2/X", 24: "2/2"}
    assert sorted(ST._OUT_HTFT.values()) == sorted(CELLS)
    assert list(ST._OUT_HTFT) == sorted(ST._OUT_HTFT), "feed order must be kept"


def test_setanta_htft_is_mapped_for_soccer_only():
    assert ST._MARKET["F"][10] == ("htft", 9)
    for code in ("B", "T", "AF"):
        assert 10 not in ST._MARKET[code], (
            f"marketType 10 is unmapped for {code} — a sport needs its own "
            "census before its codes can be trusted")


def test_setanta_selections_require_the_whole_grid():
    def outs(pairs):
        return [{"key": {"type": t}, "odd": v} for t, v in pairs]
    full = outs([(16, 1645), (17, 2610), (18, 2874), (19, 1592), (20, 822),
                 (21, 481), (22, 4988), (23, 1619), (24, 203)])
    sel = ST._selections("htft", 9, full)
    assert sel is not None and sorted(sel) == sorted(CELLS)
    assert sel["1/1"] == pytest.approx(16.45)
    assert ST._selections("htft", 9, full[:-1]) is None


def test_setanta_htft_survives_the_line_guard():
    """Every non-moneyline market used to be dropped when it had no line. htft
    has none by nature, so the guard had to learn about it — otherwise the grid
    would parse correctly and then be silently discarded."""
    import inspect
    src = inspect.getsource(ST._parse_markets)
    assert 'market_type not in ("moneyline", "htft")' in src


# ── what both books now feed ─────────────────────────────────────────────────

@pytest.mark.parametrize("book", ["liderbet", "crocobet", "setanta"])
def test_book_is_checked_by_the_consistency_engine(book):
    from src import app
    assert book in app._LADDER_BOOKS, (
        f"{book} parses the markets but nothing would check them")


def test_the_grid_and_both_legs_reach_htft_combo():
    """End to end on Crocobet: grid + H1 1X2 + FT 1X2 is exactly what the check
    consumes, and having all three is why this work was done."""
    from collections import defaultdict
    from src.consistency import _first_htft, _first_1x2
    odds = [2.95, 17.0, 70.0, 4.6, 4.0, 9.6, 40.0, 19.0, 9.0]
    games = [
        _croco_game(5, list(zip(CELLS, odds))),
        _croco_game(3, [("1", 2.45), ("X", 1.85), ("2", 5.4)]),
        _croco_game(1, [("1", 1.8), ("X", 2.85), ("2", 4.5)]),
    ]
    rows = C._parse_event(_croco_event(), games, "soccer", NOW)
    per = defaultdict(lambda: defaultdict(list))
    for o in rows:
        per[o.period][o.market_type].append(o)
    assert _first_htft(per)
    assert _first_1x2(per["H1"])
    assert _first_1x2(per["FT"])
