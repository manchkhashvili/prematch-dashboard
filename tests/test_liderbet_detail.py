"""Lider-Bet detail tier — half markets and the HT/FT grid (2026-08-12).

Owner asked why the consistency flags are all CrystalBet. The answer for
Lider-Bet turned out not to be "the book doesn't post it" but "we discarded
it": `_classify_market` dropped anything whose NAME contained "half", so the
H1/H2 1X2 and half totals that `matchData` was ALREADY returning were thrown
away. Reading them costs nothing extra — 11 728 -> 23 655 rows on the same
call. Only the 9-way HT/FT grid needed the `matchData/details` endpoint, which
soft_scan has been calling for a filtered 14 % of the board all along.

The tests below exist because of the specific way this can go wrong. A details
payload carries ~270 market names, two of which are BOTH called "Handicap":

    mt:16:501   'Handicap ①'    outcomes 1 / 2       2-way Asian
    mt:16:1079  'Handicap  ①'   outcomes 1 / X / 2   3-way European

They differ by one space, which whitespace-collapsing erases. Name-classifying
that payload produced 155 bogus 2-way spreads against 149 real ones, and the
result disagreed with CrystalBet by up to 17.8pp where every correct mapping
agreed within 1.6pp. Hence: the detail path is a typeId ALLOWLIST and the name
classifier is not allowed near it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.scrapers import liderbet as L

NOW = datetime.now(tz=timezone.utc)


def _mk_match(markets, start=None):
    return {
        "id": "pr:m:1", "homeId": "h", "awayId": "a", "tourId": "t",
        "startTime": (start or NOW + timedelta(hours=2)).replace(tzinfo=None).isoformat(),
        "markets": markets,
    }


ANC = {"h": {"name": "Home FC"}, "a": {"name": "Away FC"}, "t": {"name": "League"}}


def _mt(name, outcomes):
    return {"name": name,
            "outcomeTypes": [{"id": f"ot:{i}", "name": n} for i, n in enumerate(outcomes)]}


def _outcomes(vals, spec=None):
    return {f"ot:{i}": {"value": v, **({"specifier": spec} if spec else {})}
            for i, v in enumerate(vals)}


# ── the allowlist is the contract ────────────────────────────────────────────

def test_the_three_way_european_handicap_is_never_read_as_a_spread():
    """The bug this file exists for. mt:16:1079 is 1/X/2 and shares its name
    with the 2-way line; on the detail path it must produce nothing at all."""
    mts = {"mt:16:1079": _mt("Handicap  ①", ["1", "X", "2"])}
    m = _mk_match({"a": {"typeId": "mt:16:1079",
                         "specifier": {"hcp": "-1.0"},
                         "outcomes": _outcomes([1.9, 3.4, 4.1])}})
    assert L._parse_match(m, ANC, mts, "soccer", NOW, detail=True) == []
    assert "mt:16:1079" not in L._DETAIL_TYPES


def test_the_detail_path_ignores_the_name_classifier_entirely():
    """Anything not on the allowlist must be dropped even when its name would
    classify — that is the whole point of the allowlist."""
    mts = {"mt:16:9999": _mt("Total", ["Under", "Over"])}
    m = _mk_match({"a": {"typeId": "mt:16:9999", "specifier": {"total": "2.5"},
                         "outcomes": _outcomes([1.9, 1.9])}})
    assert L._classify_market("Total") is not None      # the name WOULD classify
    assert L._parse_match(m, ANC, mts, "soccer", NOW, detail=True) == []


def test_the_list_path_still_uses_the_name_classifier():
    """The list tier has been correct for months and must not regress."""
    mts = {"mt:16:9999": _mt("Total", ["Under", "Over"])}
    m = _mk_match({"a": {"typeId": "mt:16:9999", "specifier": {"total": "2.5"},
                         "outcomes": _outcomes([1.9, 1.9])}})
    rows = L._parse_match(m, ANC, mts, "soccer", NOW)
    assert [(o.market_type, o.period, o.line) for o in rows] == [("total", "FT", 2.5)]


# ── the markets we now read ──────────────────────────────────────────────────

def test_half_result_is_read_as_a_three_way_moneyline():
    mts = {"mt:16:602": _mt("1st Half-3way ①", ["1", "X", "2"]),
           "mt:16:619": _mt("2nd Half-3way ①", ["1", "X", "2"])}
    m = _mk_match({"a": {"typeId": "mt:16:602", "outcomes": _outcomes([2.45, 1.85, 5.4])},
                   "b": {"typeId": "mt:16:619", "outcomes": _outcomes([2.6, 2.0, 4.0])}})
    rows = L._parse_match(m, ANC, mts, "soccer", NOW, detail=True)
    got = {(o.period, tuple(sorted(o.selections))) for o in rows}
    assert got == {("H1", ("away", "draw", "home")), ("H2", ("away", "draw", "home"))}


def test_half_totals_carry_their_line():
    mts = {"mt:16:600": _mt("1st Half Total ①", ["Under", "Over"])}
    m = _mk_match({"a": {"typeId": "mt:16:600", "specifier": {"total": "1.5"},
                         "outcomes": _outcomes([1.6, 2.3])}})
    rows = L._parse_match(m, ANC, mts, "soccer", NOW, detail=True)
    assert len(rows) == 1
    assert (rows[0].market_type, rows[0].period, rows[0].line) == ("total", "H1", 1.5)


def test_the_htft_grid_is_all_nine_cells_or_nothing():
    """The consistency check reasons about the grid as a distribution. A partial
    grid would look like a book that priced only some outcomes, rather than one
    we half-read — so a missing cell drops the whole market."""
    cells = list(L._HTFT_CELLS)
    mts = {"mt:16:573": _mt("Half Time/Full Time ①", cells)}
    full = _mk_match({"a": {"typeId": "mt:16:573",
                            "outcomes": _outcomes([2.95, 17.0, 70.0, 4.6, 4.0,
                                                   9.6, 40.0, 19.0, 9.0])}})
    rows = L._parse_match(full, ANC, mts, "soccer", NOW, detail=True)
    assert len(rows) == 1
    assert rows[0].market_type == "htft" and rows[0].period == "FT"
    assert sorted(rows[0].selections) == sorted(cells)

    partial = _mk_match({"a": {"typeId": "mt:16:573",
                               "outcomes": _outcomes([2.95, 17.0, 70.0, 4.6, 4.0,
                                                      9.6, 40.0, 19.0, None])}})
    assert L._parse_match(partial, ANC, mts, "soccer", NOW, detail=True) == []


def test_the_htft_grid_feeds_the_consistency_engine():
    """End to end: the grid plus both regulation 1X2 legs is exactly what
    htft_combo consumes, and it is why this work was done."""
    from src.consistency import _first_htft, _first_1x2
    from collections import defaultdict
    cells = list(L._HTFT_CELLS)
    mts = {"mt:16:573": _mt("Half Time/Full Time ①", cells),
           "mt:16:602": _mt("1st Half-3way ①", ["1", "X", "2"]),
           "mt:16:500": _mt("Full Time Result", ["1", "X", "2"])}
    m = _mk_match({
        "a": {"typeId": "mt:16:573",
              "outcomes": _outcomes([2.95, 17.0, 70.0, 4.6, 4.0, 9.6, 40.0, 19.0, 9.0])},
        "b": {"typeId": "mt:16:602", "outcomes": _outcomes([2.45, 1.85, 5.4])},
        "c": {"typeId": "mt:16:500", "outcomes": _outcomes([1.8, 2.85, 4.5])},
    })
    rows = L._parse_match(m, ANC, mts, "soccer", NOW, detail=True)
    per = defaultdict(lambda: defaultdict(list))
    for o in rows:
        per[o.period][o.market_type].append(o)
    assert _first_htft(per), "the grid must be visible to htft_combo"
    assert _first_1x2(per["H1"]), "the H1 regulation leg must be visible"
    assert _first_1x2(per["FT"]), "the FT regulation leg must be visible"


# ── what was deliberately NOT shipped ────────────────────────────────────────

@pytest.mark.parametrize("type_id,why", [
    ("mt:16:598", "half handicap — 6.16pp median vs CB, a shifted distribution"),
    ("mt:16:621", "half handicap — same"),
    ("mt:16:501", "FT 2-way handicap — 1.17pp median but p90 18.98 / max 50.38"),
    ("mt:16:504", "team total — max 51.20pp"),
    ("mt:16:505", "team total — max 51.20pp"),
    ("mt:16:1079", "3-way European handicap masquerading as 'Handicap'"),
    ("mt:16:618", "2nd-half DOUBLE CHANCE, not the 3-way result"),
])
def test_unverified_markets_stay_out(type_id, why):
    """Only price-verified mappings ship. A market that disagrees with the
    reference on a bettable line is a phantom-arb generator, which is strictly
    worse than not having it."""
    assert type_id not in L._DETAIL_TYPES, why


# ── cost control ─────────────────────────────────────────────────────────────

def test_the_detail_pass_is_horizon_gated():
    """Measured on the soccer board (1291 matches): one 20-match batch is
    0.48 s / 3.2 MB, so the whole board would be ~26 s and ~172 MB per cycle
    against Lider's ~10 s total today. 24 h covers 162 matches for ~4 s."""
    assert L.DETAIL_HOURS > 0
    assert L.DETAIL_HOURS <= 48, "a wide horizon makes Lider the heaviest book"


def test_detail_is_only_attempted_for_sports_with_a_mapped_table():
    """typeIds are section-scoped (mt:16:* is soccer). Pulling a detail payload
    we cannot read would be pure cost."""
    assert L._DETAIL_TYPES_FOR("soccer") is True
    for other in ("basketball", "tennis", "americanfootball"):
        assert L._DETAIL_TYPES_FOR(other) is False
    assert all(k.startswith("mt:16:") for k in L._DETAIL_TYPES)


def test_liderbet_is_in_the_ladder_book_set():
    """Without this the new markets would be parsed and then never checked."""
    from src import app
    assert "liderbet" in app._LADDER_BOOKS
