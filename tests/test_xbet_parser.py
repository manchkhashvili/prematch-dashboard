"""1xbet GameZip parser tests — fixtures captured live 2026-07-10
(research/samples/), the same payloads the mappings were price-verified on.
"""
import glob
import json
import pathlib
from datetime import datetime, timezone

import pytest

from src.scrapers.xbet import _parse_zip, _spread_rows

SAMPLES = pathlib.Path(__file__).resolve().parent / "data" / "xbet"
BBALL = sorted(glob.glob(str(SAMPLES / "gamezip_bball_matched_*.json")))
FOOT = sorted(glob.glob(str(SAMPLES / "gamezip_football_*.json")))
FETCHED = datetime(2026, 7, 10, tzinfo=timezone.utc)


def _load(path):
    val = json.loads(pathlib.Path(path).read_text())["Value"]
    game = {"I": val.get("I"), "O1": val.get("O1"), "O2": val.get("O2"),
            "S": val.get("S"), "L": val.get("L"), "SG": val.get("SG")}
    return val, game


@pytest.mark.parametrize("path", BBALL)
def test_basketball_ft_families(path):
    val, game = _load(path)
    rows = _parse_zip(val, game, "basketball", "FT", FETCHED)
    by = {}
    for r in rows:
        by.setdefault(r.market_type, []).append(r)
    assert len(by.get("moneyline", [])) == 1
    ml = by["moneyline"][0]
    assert set(ml.selections) == {"home", "away"} and ml.line is None
    assert by.get("spread"), "G=2 side-line pairing must yield spread rows"
    for r in by["spread"]:
        assert set(r.selections) == {"home", "away"} and r.line is not None
    assert by.get("total") and by.get("team_total")
    for r in by["team_total"]:
        assert r.team_side in ("home", "away")
        assert set(r.selections) == {"over", "under"}
    assert all(r.source == "xbet" for r in rows)
    assert all(r.start_time is not None and r.start_time.tzinfo for r in rows)


@pytest.mark.parametrize("path", FOOT)
def test_soccer_three_way_ml(path):
    val, game = _load(path)
    rows = _parse_zip(val, game, "soccer", "FT", FETCHED)
    ml = [r for r in rows if r.market_type == "moneyline"]
    assert len(ml) == 1
    assert set(ml[0].selections) == {"home", "draw", "away"}
    assert [r for r in rows if r.market_type == "total"]
    # soccer team totals are deliberately NOT emitted (T codes unverified)
    assert not [r for r in rows if r.market_type == "team_total"]


def test_spread_pairing_prefers_side_signed_lines():
    """Basketball G=2: T7 lives at P=+L, its partner T8 at P=−L. Same-P
    pairing (the naive read) must lose to negated pairing here."""
    entries = [{"T": 7, "P": 4.5, "C": 2.19}, {"T": 8, "P": -4.5, "C": 1.7},
               {"T": 7, "P": 7.5, "C": 1.885}, {"T": 8, "P": -7.5, "C": 1.912}]
    rows = _spread_rows(entries)
    assert set(rows) == {4.5, 7.5}
    assert rows[7.5] == (1.885, 1.912)


def test_spread_pairing_same_line_mode():
    """If a sport encodes both sides at one P, the self-check keeps that."""
    entries = [{"T": 7, "P": -1.5, "C": 1.9}, {"T": 8, "P": -1.5, "C": 1.9},
               {"T": 7, "P": -2.5, "C": 2.2}, {"T": 8, "P": -2.5, "C": 1.65}]
    rows = _spread_rows(entries)
    assert set(rows) == {-1.5, -2.5}
    assert rows[-2.5] == (2.2, 1.65)


def test_suspended_prices_dropped():
    entries = [{"T": 7, "P": 4.5, "C": 1.0}, {"T": 8, "P": -4.5, "C": 1.7}]
    assert not _spread_rows(entries)
