"""Lider-Bet's details payload, read whole."""
from __future__ import annotations

import pytest

from sportsdatamovement.liderbet import (
    fill_name, parse_match, read_menu, split_specifier,
)
from sportsdatamovement.runner import next_sleep

ANCESTORS = {
    "c:1": {"name": "Home FC"},
    "c:2": {"name": "Away FC"},
    "t:9": {"name": "Premier League"},
}

MARKET_TYPES = {
    # The two markets whose NAMES differ only by a double space. The dashboard
    # scraper needs an allowlist to keep them apart; here the typeId does it.
    "mt:16:501": {"name": "Handicap ①",
                  "outcomeTypes": [{"id": "o1", "name": "1"},
                                   {"id": "o2", "name": "2"}]},
    "mt:16:1079": {"name": "Handicap  ①",
                   "outcomeTypes": [{"id": "o1", "name": "1"},
                                    {"id": "ox", "name": "X"},
                                    {"id": "o2", "name": "2"}]},
    "mt:16:502": {"name": "Total {total}",
                  "outcomeTypes": [{"id": "ov", "name": "Over"},
                                   {"id": "un", "name": "Under"}]},
    "mt:16:573": {"name": "HT/FT",
                  "outcomeTypes": [{"id": f"c{i}", "name": c} for i, c in
                                   enumerate(("1/1", "1/X", "1/2", "X/1", "X/X",
                                              "X/2", "2/1", "2/X", "2/2"))]},
}

MATCH = {
    "id": "pr:m:555",
    "homeId": "c:1", "awayId": "c:2", "tourId": "t:9",
    "startTime": "2026-08-27T18:00:00",
    "markets": {
        "a": {"typeId": "mt:16:501",
              "outcomes": {"o1": {"value": 1.85, "specifier": {"hcp": -0.5}},
                           "o2": {"value": 2.05, "specifier": {"hcp": 0.5}}}},
        "b": {"typeId": "mt:16:1079",
              "outcomes": {"o1": {"value": 2.4}, "ox": {"value": 3.3},
                           "o2": {"value": 3.1}}},
        "c": {"typeId": "mt:16:502", "specifier": {"total": 2.5},
              "outcomes": {"ov": {"value": 1.90}, "un": {"value": 1.95}}},
        "d": {"typeId": "mt:16:573",
              "outcomes": {f"c{i}": {"value": 3.0 + i} for i in range(9)}},
        "e": {"typeId": "mt:16:999",          # not in marketTypes at all
              "outcomes": {"z": {"value": 4.4}}},
        "f": {"typeId": "mt:16:502", "specifier": {"total": 3.5},
              "outcomes": {"ov": {"value": 0.0}, "un": {"value": 1.02}}},
        # Four rungs of one European handicap. `hcp` is the STRING "0:1" —
        # float() fails, so a line-only key collapses all four onto None and
        # books three unrelated prices as movement.
        "g": {"typeId": "mt:16:1102", "specifier": {"special": "(0:1)", "hcp": "0:1"},
              "outcomes": {"o1": {"value": 5.4}, "ox": {"value": 3.6},
                           "o2": {"value": 1.5}}},
        "h": {"typeId": "mt:16:1102", "specifier": {"special": "(2:0)", "hcp": "2:0"},
              "outcomes": {"o1": {"value": 1.05}, "ox": {"value": 7.4},
                           "o2": {"value": 22.0}}},
        # A discriminator that is not a line at all.
        "i": {"typeId": "mt:16:2839", "specifier": {"variant": "sr:point_range:6+"},
              "outcomes": {"o1": {"value": 3.1}}},
        "j": {"typeId": "mt:16:2839",
              "outcomes": {"o1": {"value": 1.4}}},
    },
}

MARKET_TYPES["mt:16:1102"] = {
    "name": "2nd- European Handicap ①",
    "outcomeTypes": [{"id": "o1", "name": "1"}, {"id": "ox", "name": "X"},
                     {"id": "o2", "name": "2"}]}
MARKET_TYPES["mt:16:2839"] = {
    "name": "Total Goals (aggregated) ①",
    "outcomeTypes": [{"id": "o1", "name": "2-3"}]}


def _rows():
    return parse_match(MATCH, ANCESTORS, MARKET_TYPES)["rows"]


def _find(market_key, side, line=None):
    for r in _rows():
        if r[0] == market_key and r[2] == side and (line is None or r[3] == line):
            return r
    return None


def test_event_metadata_comes_off_the_ancestor_map():
    ev = parse_match(MATCH, ANCESTORS, MARKET_TYPES)["event"]
    assert (ev["home"], ev["away"], ev["league"]) == (
        "Home FC", "Away FC", "Premier League")
    assert ev["event_key"] == "pr:m:555"
    assert ev["start_time"].isoformat() == "2026-08-27T18:00:00+00:00"


def test_nothing_is_filtered_out():
    """No allowlist: every market and every outcome the feed carries."""
    rows = _rows()
    assert len(rows) == 2 + 3 + 2 + 9 + 1 + 2 + 3 + 3 + 1 + 1
    assert {r[0] for r in rows} >= {"mt:16:501", "mt:16:1079", "mt:16:573",
                                    "mt:16:999"}


def test_every_position_key_in_one_match_is_unique():
    """The invariant the whole store rests on. Two rows of one pass claiming
    one position means the second is booked as a price move, and on a first-
    ever pass that produced 1,856 phantom moves out of 5,571 positions."""
    rows = _rows()
    keys = [(r[0], r[2], r[3]) for r in rows]
    assert len(set(keys)) == len(keys)


# ── specifiers ────────────────────────────────────────────────────────────────

def test_a_non_numeric_handicap_keeps_its_rungs_apart():
    """`{"hcp": "0:1"}` is a correct-score handicap, not a number. Silently
    dropping it puts four different bets on one key."""
    rungs = {r[0] for r in _rows() if r[0].startswith("mt:16:1102")}
    assert rungs == {"mt:16:1102|hcp=0:1;special=(0:1)",
                     "mt:16:1102|hcp=2:0;special=(2:0)"}


def test_a_variant_specifier_separates_it_from_the_plain_market():
    keys = {r[0] for r in _rows() if r[0].startswith("mt:16:2839")}
    assert keys == {"mt:16:2839", "mt:16:2839|variant=sr:point_range:6+"}


@pytest.mark.parametrize("spec,expected", [
    (None, (None, "")),
    ({}, (None, "")),
    ({"total": 2.5}, (2.5, "")),
    ({"hcp": -0.5}, (-0.5, "")),
    ({"hcp": "0:1", "special": "(0:1)"}, (None, "hcp=0:1;special=(0:1)")),
    ({"variant": "sr:point_range:6+"}, (None, "variant=sr:point_range:6+")),
    # A line AND a discriminator: the number becomes the line, the rest keeps
    # two players' totals from merging.
    ({"total": 1.5, "player": "sr:player:1"}, (1.5, "player=sr:player:1")),
])
def test_split_specifier(spec, expected):
    assert split_specifier(spec) == expected


def test_an_unknown_specifier_key_fails_towards_keeping_positions_apart():
    """A key nobody has seen before must widen the identity, not be ignored."""
    line, rest = split_specifier({"something_new": "x"})
    assert (line, rest) == (None, "something_new=x")


def test_the_two_handicaps_stay_apart_without_an_allowlist():
    """Their names differ by a double space, which whitespace collapsing
    erases. Reading the 3-way as a 2-way invents a spread nobody quoted."""
    two_way = [r for r in _rows() if r[0] == "mt:16:501"]
    three_way = [r for r in _rows() if r[0] == "mt:16:1079"]
    assert len(two_way) == 2 and len(three_way) == 3
    assert {r[2] for r in three_way} == {"1", "X", "2"}


def test_an_outcome_specifier_beats_the_markets_one():
    """The two sides of one Asian handicap are +0.5 and -0.5, not one line."""
    assert _find("mt:16:501", "1")[3] == -0.5
    assert _find("mt:16:501", "2")[3] == 0.5


def test_the_market_specifier_is_used_when_the_outcome_has_none():
    assert _find("mt:16:502", "Over", 2.5)[3] == 2.5


def test_the_line_is_a_column_not_part_of_the_name():
    """Keeping the book's template in the name holds the intern table to a few
    hundred rows instead of one per rung."""
    assert _find("mt:16:502", "Over", 2.5)[1] == "Total {total}"


# ── readable names ────────────────────────────────────────────────────────────

def test_a_player_placeholder_is_filled_in_from_the_specifier():
    """Otherwise every goalscorer market on an event displays as the same
    string and eight players' prices cannot be told apart by eye."""
    spec = {"special": "sr:player:2735979", "lb_br_player": "Donovan, Colby",
            "goalnr": "1", "player": "Donovan, Colby"}
    assert fill_name("1X2 / Anytime goalscorer {lb_br_player}", spec) == \
        "1X2 / Anytime goalscorer Donovan, Colby"


def test_the_line_placeholder_is_left_alone():
    """Substituting it would put one `markets` row per rung; the line has its
    own column."""
    assert fill_name("Total {total}", {"total": 2.5}, skip="total") == \
        "Total {total}"


def test_a_placeholder_with_no_value_survives_verbatim():
    assert fill_name("{!quarternr} quarter - handicap", {"hcp": -1.5},
                     skip="hcp") == "{!quarternr} quarter - handicap"


@pytest.mark.parametrize("template", ["{!quarternr} quarter", "{#quarternr} quarter",
                                      "{quarternr} quarter"])
def test_every_placeholder_form_is_substituted(template):
    """Lider uses all three; `{#setnr} set - Winner` went out unsubstituted
    until the `#` form was handled."""
    assert fill_name(template, {"quarternr": 3}) == "3 quarter"


def test_a_name_with_no_slot_gets_the_discriminator_appended():
    """"1st goalscorer" is the whole title — the player lives only in the
    specifier, so substitution alone leaves every player's market identical."""
    spec = {"special": "sr:player:2735979", "player": "Donovan, Colby",
            "goalnr": "1"}
    assert fill_name("1st goalscorer", spec) == "1st goalscorer [Donovan, Colby]"


def test_opaque_ids_and_bare_ordinals_are_not_appended():
    """"sr:player:2735979" and "goalnr: 1" identify the market to Lider and say
    nothing to a reader."""
    out = fill_name("1st goalscorer", {"special": "sr:player:1", "goalnr": "1"})
    assert out == "1st goalscorer"


def test_a_correct_score_handicap_shows_its_scoreline():
    """Twenty rungs of `2nd- European Handicap` were all one string before."""
    assert fill_name("2nd- European Handicap",
                     {"special": "(0:1)", "hcp": "0:1"}) == \
        "2nd- European Handicap [(0:1)]"


def test_the_line_is_never_appended_either():
    assert fill_name("Total {total}", {"total": 2.5}, skip="total") == \
        "Total {total}"


def test_filling_a_name_cannot_create_a_new_market_row():
    """Every key that gets substituted is already part of the market key, so
    the name follows the key rather than multiplying it."""
    rows = _rows()
    by_key = {}
    for key, name, *_ in rows:
        by_key.setdefault(key, set()).add(name)
    assert all(len(names) == 1 for names in by_key.values())


def test_decoration_glyphs_are_stripped_from_names():
    assert _find("mt:16:501", "1")[1] == "Handicap"


def test_a_market_type_missing_from_the_dictionary_still_collects():
    """An unknown typeId is a gap in the feed's dictionary, not a reason to
    drop prices — the id and the outcome key are enough to track it."""
    row = _find("mt:16:999", "z")
    assert row is not None and row[1] == "mt:16:999" and row[4] == 4.4


def test_a_zeroed_outcome_is_unpriced():
    """Lider zeroes an outcome it is not taking rather than dropping it. Unlike
    CrystalBet, there is no locked-cell marker to read — the value is all there
    is, so anything at or below evens is treated as unpriced."""
    assert _find("mt:16:502", "Over", 3.5)[4] is None


def test_a_genuinely_short_price_is_kept():
    """1.02 is a real Lider price on a runaway favourite. Only CrystalBet has a
    1.01 that means "locked", and there it is the CSS class that says so."""
    assert _find("mt:16:502", "Under", 3.5)[4] == 1.02


def test_the_outcome_id_is_kept_as_a_breadcrumb():
    assert _find("mt:16:573", "1/1")[5] == "c0"


# ── menu ──────────────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class _FakeSession:
    def __init__(self, payload):
        self._p = payload

    def get(self, *a, **kw):
        return _FakeResponse(self._p)


MENU = {"menu": {
    "root": [{"id": "s:16", "name": "Soccer"},
             {"id": "s:28", "name": "Table Tennis"},
             {"id": "c:17065", "name": "Simulated Reality League",
              "sectionId": "s:16"}],
    "s:16": [{"id": "t:1", "name": "Premier League", "sectionId": "s:16", "cnt": 10}],
    "c:17065": [{"id": "t:99", "name": "World Cup", "sectionId": "s:16", "cnt": 40}],
    "s:28": [{"id": "t:5", "name": "TT Cup", "sectionId": "s:28", "cnt": 400}],
}}


def test_menu_enumerates_every_section_not_a_hardcoded_four():
    found = read_menu(_FakeSession(MENU))
    assert {v["slug"] for v in found.values()} == {"soccer", "table_tennis"}


def test_a_simulated_shelf_is_dropped_by_its_parent_category():
    """The simulated "World Cup" sits under "Simulated Reality League" and its
    own name is innocent — the parent is the tell."""
    found = read_menu(_FakeSession(MENU))
    assert found["s:16"]["tours"] == ["t:1"]
    assert found["s:16"]["matches"] == 10


def test_simulated_shelves_can_be_kept_on_request():
    found = read_menu(_FakeSession(MENU), skip_simulated=False)
    assert set(found["s:16"]["tours"]) == {"t:1", "t:99"}


# ── cadence ───────────────────────────────────────────────────────────────────

def test_a_quick_pass_waits_out_the_rest_of_the_hour():
    assert next_sleep(elapsed=600, interval=3600, min_gap=300) == 3000


def test_an_overrunning_pass_falls_back_to_the_minimum_gap():
    """The anti-runaway: a pass slower than the interval must not queue up
    back-to-back passes with no breathing room."""
    assert next_sleep(elapsed=5400, interval=3600, min_gap=300) == 300


# ── squad rosters in the specifier ────────────────────────────────────────────

ROSTER = {
    "sr:player:918650": "Trusty, Auston",
    "sr:player:97046": "McGregor, Callum",
    "sr:player:91744": "Forrest, James",
    "count": "4",
    "special": "sr:player:918650",
}


def test_a_squad_roster_is_not_part_of_the_market_key():
    """Lider's player props ship all 46 squad members in the specifier. Putting
    them in the key means ~1.5 KB per market and — worse — one transfer or
    substitution re-keys every position on the event."""
    line, rest = split_specifier(ROSTER)
    assert line is None
    assert rest == "count=4;special=sr:player:918650"


def test_the_roster_is_used_to_name_the_player():
    """That is what it is there for: `special` holds the id, the roster holds
    the name."""
    assert fill_name("Player {count}+ shots (SuperSub)", ROSTER) == \
        "Player 4+ shots (SuperSub) [Trusty, Auston]"


def test_a_roster_entry_is_never_appended_as_a_discriminator():
    out = fill_name("Player {count}+ shots", ROSTER)
    assert "McGregor" not in out and "Forrest" not in out
