"""
Tests for src.normalize.normalize_tennis_name.

Anchors the Phase 3.2 behavior: CB "LastName Initial[s]" and Pin
"FirstName [Middle] LastName" reduce to overlapping token sets so that
fuzz.token_set_ratio scores them at 100 in the matcher.
"""
from __future__ import annotations

from rapidfuzz import fuzz

from src.normalize import normalize_tennis_name


# ── Single-name (singles) cases ──────────────────────────────────────────────

def test_cb_trailing_single_initial():
    assert normalize_tennis_name("Cobolli F") == "cobolli"


def test_cb_trailing_initial_with_period():
    assert normalize_tennis_name("Wu Y.") == "wu"


def test_cb_multi_initial_dotted():
    assert normalize_tennis_name("Bulgaru M.B.") == "bulgaru"


def test_cb_multi_initial_no_periods_is_left_intact():
    # CB always uses periods between multi-initials ("M.B."), which the
    # regex splits into single-char tokens. A bare "MB" (no period) is
    # ambiguous — could be a 2-letter initialism that's part of the name —
    # so we deliberately do NOT strip it. If real data ever shows this
    # form we'll revisit.
    assert normalize_tennis_name("Bulgaru MB") == "bulgaru mb"


def test_cb_multi_word_lastname_with_initial():
    # "Teixido Garcia M.A." → both lastname tokens kept, initials stripped.
    assert normalize_tennis_name("Teixido Garcia M.A.") == "teixido garcia"


def test_pin_firstname_lastname_kept_whole():
    # Pin form has no single-char tokens, so nothing is stripped.
    # The downstream fuzz.token_set_ratio against "cobolli" returns 100
    # because the set is a strict subset.
    assert normalize_tennis_name("Flavio Cobolli") == "flavio cobolli"


def test_pin_three_part_name():
    assert normalize_tennis_name("Meritxell Teixido Garcia") == "meritxell teixido garcia"


def test_leading_initial_inversion():
    # Rare "F. Cobolli" form — leading single-char token should also be stripped.
    assert normalize_tennis_name("F. Cobolli") == "cobolli"


def test_accent_strip():
    # NFD decomposition + Mn drop — "Müller" → "muller", "Łukasz" handled too.
    assert normalize_tennis_name("Müller F") == "muller"


def test_empty_string():
    assert normalize_tennis_name("") == ""


def test_single_token_kept():
    # "Cobolli" alone → keep as-is, no over-eager initial stripping.
    assert normalize_tennis_name("Cobolli") == "cobolli"


# ── Doubles (slash-separated) cases ──────────────────────────────────────────

def test_doubles_sorted_order_invariant():
    # Partner order shouldn't change the result.
    a = normalize_tennis_name("Patten H / Nicholls O")
    b = normalize_tennis_name("Nicholls O / Patten H")
    assert a == b


def test_doubles_normalizes_each_side():
    # Both partners get the initial-stripping treatment.
    assert normalize_tennis_name("Patten H / Nicholls O") == "nicholls / patten"


def test_doubles_pin_form():
    # Pinnacle doubles use "FirstName LastName / FirstName LastName".
    out = normalize_tennis_name("Hugo Nys / Edouard Roger-Vasselin")
    # No single-char tokens to strip; lowercased and slash-joined sorted.
    # "roger vasselin" comes after "hugo nys" alphabetically.
    assert out == "edouard roger vasselin / hugo nys"


# ── End-to-end: CB form vs Pin form must score 100 via token_set_ratio ──────

def test_cb_vs_pin_token_set_ratio_100():
    """The core guarantee: after normalization, the CB and Pin forms are
    set-subset matches, so token_set_ratio = 100. This is what makes the
    fix work — we don't need to strip Pin's firstname token, the metric
    handles it."""
    cb = normalize_tennis_name("Cobolli F")
    pin = normalize_tennis_name("Flavio Cobolli")
    assert fuzz.token_set_ratio(cb, pin) == 100


def test_cb_vs_pin_multi_word_lastname():
    cb = normalize_tennis_name("Teixido Garcia M.A.")
    pin = normalize_tennis_name("Meritxell Teixido Garcia")
    assert fuzz.token_set_ratio(cb, pin) == 100


def test_cb_vs_pin_multi_initial():
    cb = normalize_tennis_name("Bulgaru M.B.")
    pin = normalize_tennis_name("Miriam Bulgaru")
    assert fuzz.token_set_ratio(cb, pin) == 100


def test_unrelated_surnames_still_score_low():
    """Sanity check that the normalizer hasn't created spurious matches —
    two different surnames should still score poorly."""
    cb = normalize_tennis_name("Cobolli F")
    pin = normalize_tennis_name("Novak Djokovic")
    assert fuzz.token_set_ratio(cb, pin) < 50
