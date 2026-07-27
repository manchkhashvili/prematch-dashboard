"""CB parses each panel ONCE — html5lib soup handed straight to the parsers.

The browser-free transport must normalise with html5lib (html.parser mis-nests
CB's unclosed <td>s; lxml's recovery diverges from browsers). It used to return
`str(soup)`, which every downstream parser re-parsed with html.parser. On saved
soccer panels that serialize + re-parse round trip measured **33 %** of CB's
HTML processing — for a byte-identical set of Odds.

These tests lock in the two properties that make dropping it safe:
  1. a soup and its serialized form produce the SAME Odds, per sport;
  2. every entry point still accepts a plain string (Playwright transport,
     saved-HTML path, fixtures), so nothing else had to change.
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timezone

import pytest
from bs4 import BeautifulSoup

from src.scrapers import cb_http, crystalbet as CB

RAW = pathlib.Path(__file__).parent.parent / "data" / "raw"
NOW = datetime(2026, 7, 26, tzinfo=timezone.utc)

CASES = [
    ("cb_prematch_sample_soccer.html", "soccer"),
    ("cb_prematch_sample.html", "basketball"),
    ("cb_prematch_sample_tennis.html", "tennis"),
]


def _key(o):
    return (o.source, o.sport, o.home, o.away, o.market_type, o.period, o.line,
            o.team_side, o.submarket, o.section, tuple(sorted(o.selections.items())))


@pytest.mark.parametrize("fname,sport_name", CASES)
def test_soup_and_string_paths_give_identical_odds(fname, sport_name):
    path = RAW / fname
    if not path.exists():
        pytest.skip(f"sample {fname} not present")
    sport = getattr(CB, sport_name)
    raw = path.read_text(encoding="utf-8")

    # old path: html5lib -> str -> html.parser (inside the parser)
    old = CB._parse_html_for_sport(str(BeautifulSoup(raw, "html5lib")), NOW, sport)
    # new path: html5lib soup consumed directly
    new = CB._parse_html_for_sport(BeautifulSoup(raw, "html5lib"), NOW, sport)

    assert old, "fixture produced no Odds — sample may be stale"
    assert sorted(map(_key, old)) == sorted(map(_key, new))


def test_as_soup_accepts_both_forms():
    html = "<div class='GContainerList' data-id='1'></div>"
    from_str = cb_http.as_soup(html)
    assert from_str.select_one("div.GContainerList") is not None
    soup = BeautifulSoup(html, "html5lib")
    assert cb_http.as_soup(soup) is soup      # passthrough, no re-parse


def test_normalize_html_still_returns_text():
    """Kept for callers that genuinely want a string: it is exactly the
    serialization of what normalize_soup builds.

    (Note the repair being exercised: a bare `<td>` outside a table is invalid,
    so html5lib drops the cell and keeps the text — the same thing a browser
    does, which is the whole reason CB needs html5lib rather than html.parser.)
    """
    frag = "<table><tr><td>x</table>"
    out = cb_http.normalize_html(frag)
    assert isinstance(out, str)
    assert out == str(cb_http.normalize_soup(frag))
    assert "<td>x</td>" in out          # unclosed cell repaired


def test_parsers_still_accept_plain_strings():
    """The Playwright transport and the saved-HTML path pass strings."""
    path = RAW / "cb_prematch_sample.html"
    if not path.exists():
        pytest.skip("sample not present")
    raw = path.read_text(encoding="utf-8")
    games = CB._extract_games_from_list_html(raw, NOW, sport=CB.basketball)
    assert isinstance(games, list)
