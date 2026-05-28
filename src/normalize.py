"""
Team name normalization for CB ↔ Pinnacle fuzzy matching.

CB and Pinnacle use different transliterations and abbreviations for the same
teams. This module reduces both to a comparable canonical form before scoring.

Two public normalizers:
  - normalize_team():        team-sport names (basketball, soccer)
  - normalize_tennis_name(): tennis player + doubles pair names

normalize_team pipeline:
  1. Optional manual alias from team_aliases.yaml (case-insensitive lookup
     on the *original* name). Reloaded automatically when the YAML's
     mtime changes — no server restart needed.
  2. Unicode → ASCII (NFD decompose, drop combining marks).
  3. Lowercase + strip non-alphanumeric.
  4. Drop noise tokens (FC, BC, U18, etc.) that don't distinguish teams.

normalize_tennis_name pipeline (Phase 3.2, 2026-05-27):
  CB writes "Cobolli F" / "Cobolli F." / "Teixido Garcia M.A.", Pinnacle writes
  "Flavio Cobolli" / "Meritxell Teixido Garcia". Stripping the single-char
  initial tokens from the CB side leaves the surname tokens, and
  fuzz.token_set_ratio handles Pinnacle's extra firstname token via set overlap
  (a strict subset scores 100). Doubles ("A / B vs C / D") recurse per side
  and join sorted so partner order is irrelevant.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

_DROP_TOKENS = frozenset({
    "fc", "bc", "bk", "sk", "sc", "ac", "kk", "nk",
    "club", "clubs", "basketball", "basket", "basquet",
    "baloncesto", "pallacanestro", "csk",
    "u18", "u20", "u23",
})

_WHITESPACE = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]")

# Alias file lives next to this module.
ALIASES_PATH = Path(__file__).resolve().parent / "team_aliases.yaml"

# mtime-keyed cache: reload only when the file on disk changes.
_alias_cache: dict = {"mtime": -1.0, "map": {}}


def _load_aliases() -> dict[str, str]:
    """Return {lowercased_source_name: canonical_name}. Reloads on file change."""
    if not ALIASES_PATH.exists():
        return {}
    try:
        mtime = ALIASES_PATH.stat().st_mtime
    except OSError:
        return _alias_cache["map"]
    if mtime == _alias_cache["mtime"]:
        return _alias_cache["map"]
    try:
        with ALIASES_PATH.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        # Corrupt YAML — keep the previous good map, log once.
        log.warning("team_aliases.yaml unreadable: %s", e)
        _alias_cache["mtime"] = mtime  # don't retry until next edit
        return _alias_cache["map"]
    raw = data.get("aliases", {}) if isinstance(data, dict) else {}
    if not isinstance(raw, dict):
        log.warning("team_aliases.yaml: 'aliases' must be a mapping; got %s", type(raw).__name__)
        raw = {}
    new_map = {
        str(k).strip().lower(): str(v) for k, v in raw.items()
        if k and v is not None
    }
    if new_map != _alias_cache["map"]:
        log.info("team_aliases: loaded %d alias(es) from %s", len(new_map), ALIASES_PATH.name)
    _alias_cache["map"] = new_map
    _alias_cache["mtime"] = mtime
    return new_map


def normalize_tennis_name(name: str) -> str:
    """
    Reduce a tennis player (or doubles pair) name to a canonical, matchable form.

    CB format:  "LastName F[.X.]" — surname first, trailing initials.
                Multi-word surnames keep all words: "Teixido Garcia M.A.".
                Doubles use slash: "Patten H / Nicholls O".
    Pin format: "FirstName [Middle] LastName" — given name first.
                Doubles use slash: "Hugo Nys / Edouard Roger-Vasselin".

    Strategy: strip ASCII + lowercase + drop trailing/leading single-char
    tokens (the CB initials). Keep all remaining tokens unsorted. Pinnacle's
    extra firstname token is handled downstream by fuzz.token_set_ratio,
    which scores a strict subset at 100.

    For doubles, recurse on each side then join the normalized halves sorted
    so partner order doesn't matter:
        "B Smith / A Jones" → "jones / smith"

    Examples
    --------
    >>> normalize_tennis_name("Cobolli F")
    'cobolli'
    >>> normalize_tennis_name("Flavio Cobolli")
    'flavio cobolli'
    >>> normalize_tennis_name("Bulgaru M.B.")
    'bulgaru'
    >>> normalize_tennis_name("Teixido Garcia M.A.")
    'teixido garcia'
    >>> normalize_tennis_name("Patten H / Nicholls O")
    'nicholls / patten'
    """
    if not name:
        return ""
    nfd = unicodedata.normalize("NFD", name)
    ascii_approx = "".join(c for c in nfd if unicodedata.category(c) != "Mn")

    # Doubles → recurse per partner, join sorted (partner order is meaningless).
    if "/" in ascii_approx:
        parts = [normalize_tennis_name(p) for p in ascii_approx.split("/")]
        parts = [p for p in parts if p]
        return " / ".join(sorted(parts))

    lower = ascii_approx.lower()
    alnum = _NON_ALNUM.sub(" ", lower)
    tokens = alnum.split()
    if not tokens:
        return ""
    # Strip trailing single-char tokens — these are CB's initial(s) like
    # "F" / "M.B." → after punctuation cleanup, "F." → ["f"], "M.B." → ["m","b"].
    while len(tokens) > 1 and len(tokens[-1]) == 1:
        tokens.pop()
    # Strip leading single-char tokens — covers rare "F. Cobolli" inversion.
    while len(tokens) > 1 and len(tokens[0]) == 1:
        tokens.pop(0)
    return " ".join(tokens)


def normalize_team(name: str) -> str:
    """
    Reduce a team name to a canonical, matchable form.

    Examples
    --------
    >>> normalize_team("Connecticut")  # with alias "Connecticut": "Connecticut Sun"
    'connecticut sun'
    >>> normalize_team("FC Barcelona")
    'barcelona'
    >>> normalize_team("Crvena Zvezda")
    'crvena zvezda'
    """
    if not name:
        return ""
    aliases = _load_aliases()
    key = name.strip().lower()
    aliased = aliases[key] if key in aliases else name
    nfd = unicodedata.normalize("NFD", aliased)
    ascii_approx = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    lower = ascii_approx.lower()
    alnum_only = _NON_ALNUM.sub(" ", lower)
    tokens = [t for t in alnum_only.split() if t not in _DROP_TOKENS]
    return _WHITESPACE.sub(" ", " ".join(tokens)).strip()
