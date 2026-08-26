"""Every string below was taken off a live board on 2026-08-26."""
from __future__ import annotations

import pytest

from sportsdatamovement.players import is_player_market, looks_like_person


@pytest.mark.parametrize("text", [
    "Eze, Eberechi",
    "Haaland, Erling Braut",
    "Wirtz, Florian Richard",
    "Welbeck, D",                       # CrystalBet abbreviates to an initial
    "Bughail-mellor, D Mani",           # hyphen, then two given names
    "Assists Chust, Víctor (Elche CF)",  # accented, mid-title
    "Goals Garcia Raja, Víctor (Levante UD)",
    "Anytime goalscorer & correct score Soula, Mazire (PFC Levski Sofia)",
])
def test_a_person_is_recognised(text):
    assert looks_like_person(text)


@pytest.mark.parametrize("text", [
    "0:1, 0:2 or 0:3",                  # a Multiscores selection
    "1:2, 1:3, 1:4",                    # a correct-score grouping
    "Team 1 win or 0-0, 0-1",
    "Main result",
    "Halftime/Fulltime and Total",
    "Total goals",
    "",
])
def test_a_scoreline_is_not_a_person(text):
    """What sits before the comma is the whole discriminator: a letter for a
    person, a digit for a scoreline. Matching on ", " alone would take every
    correct-score market out of the study."""
    assert not looks_like_person(text)


# ── the market-level decision ─────────────────────────────────────────────────

def test_a_lider_specifier_settles_it_without_reading_names():
    assert is_player_market(
        market="1X2 / Anytime goalscorer Donovan, Colby",
        specifier={"special": "sr:player:2735979", "player": "Donovan, Colby"})


def test_a_roster_key_counts_as_the_structural_tell():
    """Lider's player props ship the squad as `sr:player:NNN` KEYS."""
    assert is_player_market(market="Player 4+ shots",
                            specifier={"sr:player:918650": "Trusty, Auston",
                                       "count": "4"})


def test_a_title_naming_no_one_but_pricing_individuals_is_caught():
    assert is_player_market(market="1st Team - 1st player to score", side="Yes")
    assert is_player_market(market="Player 3+ shots on target", side="1+")


def test_the_player_can_be_in_the_side_rather_than_the_title():
    assert is_player_market(market="1st Team - 1st player to score",
                            side="Koita, Abo")


@pytest.mark.parametrize("market,side", [
    ("Main result", "1"),
    ("Halftime/Fulltime", "1/1"),
    ("Total goals", "over 2.5"),
    ("Multiscores***", "0:1, 0:2 or 0:3"),
    ("Both teams to score", "Yes"),        # "score" is not "scorer"
    ("Correct score", "2:1"),
    ("Exact number of goals", "3"),
    ("Team 1 Total Shots 13+", "Yes"),     # a TEAM total, not a player's
])
def test_match_markets_are_kept(market, side):
    assert not is_player_market(market=market, side=side)


def test_a_market_with_no_specifier_still_decides_on_the_names():
    assert not is_player_market(market="Draw no bet", side="(0)1", specifier=None)


def test_a_specifier_that_is_not_a_dict_is_ignored():
    assert not is_player_market(market="Total", side="Over", specifier="nonsense")


# ── "player 1" is a competitor, not a prop ────────────────────────────────────
# In tennis, table tennis, darts and chess the two sides ARE player 1 and
# player 2. A bare \bplayer\b token threw away 36 of 190 outcomes on a live
# Lider tennis match — 19 %, in the sport with the highest movement rate.

@pytest.mark.parametrize("market", [
    "Total games won by player 1",
    "Total games won by player 2",
    "Player 1 to win exactly 1 set",
    "Player 2 win at least one set",
    "Player 1 to win the match",
])
def test_a_competitor_index_is_not_a_player_prop(market):
    assert not is_player_market(market=market, side="Yes")


@pytest.mark.parametrize("market", [
    "Player 1+ goals",           # a threshold, not a competitor
    "Player 3+ shots on target",
    "Player {count}+ assists (SuperSub)",
    "1st Team - 1st player to score",
    "Player Points",
])
def test_a_threshold_or_a_bare_player_market_is_still_dropped(market):
    assert is_player_market(market=market, side="Yes")


@pytest.mark.parametrize("market", [
    "Team 1 Total Assists",
    "Total Assists",
    "Team 2 assists",
])
def test_team_assist_markets_survive(market):
    """`assists` used to be a token and took these with it. Every genuine
    assists prop says "player" or names someone."""
    assert not is_player_market(market=market, side="over 22.5")


def test_a_named_assists_prop_is_still_caught():
    assert is_player_market(market="Assists Chust, Víctor (Elche CF)", side="1+")
