"""
Tests for the Cyrillic→Latin transliteration added 2026-06-15 for Lider-Bet.

Lider-Bet sometimes returns competitor names in Russian even at lang=en. Before
transliteration the NFD→[a-z0-9] pipeline stripped every Cyrillic char and the
name normalized to "" (unmatchable against Pinnacle, which exposes no SportRadar
id to join on). These anchor that Cyrillic names now romanize to a Latin
approximation that fuzzy-matches the Latin spelling, while Latin names are
left byte-for-byte unchanged.
"""
from __future__ import annotations

from rapidfuzz import fuzz

from src.normalize import normalize_team, normalize_tennis_name, transliterate_cyrillic


def test_latin_names_untouched():
    # No Cyrillic → identity, so CB/Pinnacle/Betlive names are unaffected.
    assert transliterate_cyrillic("Nueva Chicago") == "Nueva Chicago"
    assert transliterate_cyrillic("FC Barcelona") == "FC Barcelona"
    assert transliterate_cyrillic("") == ""


def test_russian_team_romanizes():
    assert transliterate_cyrillic("Нуэва Чикаго") == "nueva chikago"
    assert normalize_team("Нуэва Чикаго") == "nueva chikago"


def test_cyrillic_team_no_longer_empties_out():
    # The regression we are fixing: a Cyrillic name must not normalize to "".
    assert normalize_team("Зенит") != ""
    assert normalize_team("ЦСКА Москва") != ""


def test_transliterated_name_fuzzy_matches_pinnacle_spelling():
    # The real fixture from the 2026-06-15 probe: Lider "Нуэва Чикаго" vs
    # Pinnacle "Nueva Chicago" must clear the matcher's SCORE_LOOSE=80 threshold.
    score = fuzz.token_set_ratio(
        normalize_team("Нуэва Чикаго"), normalize_team("Nueva Chicago")
    )
    assert score >= 80


def test_tennis_cyrillic_surname():
    # Lider tennis "Медведев Д" → drop the initial, romanize the surname.
    assert normalize_tennis_name("Медведев Д") == "medvedev"
    assert fuzz.token_set_ratio(
        normalize_tennis_name("Медведев Д"), normalize_tennis_name("Daniil Medvedev")
    ) == 100


def test_georgian_romanizes():
    # Lider ships a few in-house tournament names only in Georgian even at
    # lang=en (no English in the feed). They must not reach the dashboard as
    # Georgian script — transliterate() romanizes them; Latin tails are kept.
    from src.normalize import transliterate
    assert transliterate("პორტუგალია. Hard ") == "portugalia. Hard "
    assert normalize_team("დინამო თბილისი") == "dinamo tbilisi"
    assert normalize_team("დინამო თბილისი") != ""
