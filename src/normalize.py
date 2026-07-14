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
    # u18/u20/u23 used to be dropped here — removed 2026-06-12 (v2 finding #3):
    # dropping them let "NWS Spirit U20" fuzzy-match the senior "NWS Spirit"
    # at score 100 (token_set_ratio scores subsets at 100). The tokens now
    # stay in the normalized form AND the matcher applies the hard
    # has_youth_tag guard below.
})

# Youth/age-group markers (U16..U23, with or without a dash/space). Checked on
# the RAW name — a youth side must never match a senior side, whatever the
# fuzzy score says.
_YOUTH_TAG = re.compile(r"(?i)\bu[- ]?(1[6-9]|2[0-3])\b")


def has_youth_tag(name: str) -> bool:
    """True if the team name carries a youth/age-group marker (U16–U23)."""
    return bool(_YOUTH_TAG.search(name or ""))


# Women's-competition markers. Unlike youth tags, gender is carried in the
# LEAGUE, not the team names: an Australian NBL1 double-header lists the men's and
# women's games with IDENTICAL club names ("Southern Tigers v North Adelaide
# Rockets") distinguished only by "Women" in the league ("NBL1" vs "NBL1 Women").
# Checked deaccented so "Féminine" → "feminine"; men's leagues are simply
# unmarked, so the flag is binary (women vs not) exactly like the youth guard.
_WOMEN_TAG = re.compile(
    r"(?i)(?:\b(?:women|womens|woman|ladies|female|feminine|feminin|femminile|"
    r"frauen|damen|dames|femenino|femenina|mujeres|senhoras)\b|\(w\))"
)


def has_women_tag(*texts: str) -> bool:
    """True if any string (league and/or team names) marks a WOMEN's competition.

    Used to (1) keep a men's and women's game with identical team names from
    collapsing into one event, and (2) stop a women's event matching a men's one
    — the gender analogue of has_youth_tag. The marker normally lives in the
    league; team names are checked too for books that suffix "(W)"."""
    for t in texts:
        if not t:
            continue
        flat = "".join(c for c in unicodedata.normalize("NFD", t)
                       if unicodedata.category(c) != "Mn")
        if _WOMEN_TAG.search(flat):
            return True
    return False

_WHITESPACE = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]")

# Cyrillic → Latin transliteration (2026-06-15, for Lider-Bet).
# Lider-Bet sometimes returns competitor names in Russian even at lang=en
# (e.g. the away side "Нуэва Чикаго" for Pinnacle's "Nueva Chicago"). Without
# this, the NFD→[a-z0-9] pipeline below strips every Cyrillic char and the name
# normalizes to "" — unmatchable. Transliterating first turns it into a Latin
# approximation ("nueva chikago") that rapidfuzz can still pair with Pinnacle's
# spelling ("nueva chicago") at a high token_set_ratio. The exact cross-book
# (Lider↔Betlive) join uses the SportRadar id instead and bypasses this; this
# map only rescues the name-only fallback (any local book ↔ Pinnacle, since
# Pinnacle exposes no SportRadar id). Standard BGN/PCGN-ish scheme; covers
# Russian/Ukrainian/Serbian Cyrillic. Multi-char outputs (zh, kh, ch, …) are
# fine — downstream tokenization is whitespace-only.
_CYRILLIC_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "ґ": "g", "д": "d", "е": "e",
    "ё": "e", "є": "ie", "ж": "zh", "з": "z", "и": "i", "і": "i", "ї": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "yu", "я": "ya", "ђ": "dj", "ј": "j", "љ": "lj", "њ": "nj",
    "ћ": "c", "џ": "dz",
}

# Georgian → Latin (national romanization). Lider-Bet sometimes returns a few
# in-house tournament / category names only in Georgian even at lang=en — these
# have no English in the feed (verified 2026-06-15: the name is identical across
# lang=en/ka/ru), so transliteration is the only way to keep Georgian script off
# the dashboard. Modern Georgian has no case, so the map is single-case.
_GEORGIAN_MAP = {
    "ა": "a", "ბ": "b", "გ": "g", "დ": "d", "ე": "e", "ვ": "v", "ზ": "z",
    "თ": "t", "ი": "i", "კ": "k", "ლ": "l", "მ": "m", "ნ": "n", "ო": "o",
    "პ": "p", "ჟ": "zh", "რ": "r", "ს": "s", "ტ": "t", "უ": "u", "ფ": "p",
    "ქ": "k", "ღ": "gh", "ყ": "q", "შ": "sh", "ჩ": "ch", "ც": "ts", "ძ": "dz",
    "წ": "ts", "ჭ": "ch", "ხ": "kh", "ჯ": "j", "ჰ": "h",
}
_TRANSLIT_MAP = {**_CYRILLIC_MAP, **_GEORGIAN_MAP}


def _has_nonlatin(name: str) -> bool:
    # Cyrillic (incl. Serbian) U+0400–U+04FF / U+0490 range, Georgian U+10A0–U+10FF.
    return any(("Ѐ" <= c <= "ӿ") or ("Ⴀ" <= c <= "ჿ") for c in name)


def transliterate(name: str) -> str:
    """Romanize any Cyrillic OR Georgian in `name`; pass other chars through.

    No-op (returns the input unchanged) when there's no Cyrillic/Georgian, so
    Latin names — every CrystalBet/Pinnacle/Betlive name — are untouched.

    >>> transliterate("Нуэва Чикаго")
    'nueva chikago'
    >>> transliterate("პორტუგალია. Hard")
    'portugalia. Hard'
    >>> transliterate("Nueva Chicago")
    'Nueva Chicago'
    """
    if not name or not _has_nonlatin(name):
        return name
    return "".join(_TRANSLIT_MAP.get(c, _TRANSLIT_MAP.get(c.lower(), c)) for c in name)


# Back-compat alias (Cyrillic-only callers / tests predate the Georgian add).
transliterate_cyrillic = transliterate

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
    name = transliterate_cyrillic(name)
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
    aliased = transliterate_cyrillic(aliased)
    nfd = unicodedata.normalize("NFD", aliased)
    ascii_approx = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    lower = ascii_approx.lower()
    alnum_only = _NON_ALNUM.sub(" ", lower)
    tokens = [t for t in alnum_only.split() if t not in _DROP_TOKENS]
    return _WHITESPACE.sub(" ", " ".join(tokens)).strip()


# ── Simulated / esports league guard (2026-07-11) ─────────────────────────────
# Every Georgian book lists SIMULATED competitions inside the real-sport
# sections: Betlive "e-Sports Battle" + "Simulated Reality League (SRL)",
# Crocobet "K liga 1 SRL", Lider "Cyberfootball" + "K-League 1 SRL". Their
# fixtures reuse REAL team names ("K-League 1 SRL" vs the real K-League), so
# they fuzzy-match genuine fixtures on the reference books and fire phantom
# edges/arbs/anomalies. One shared token list so every scraper drops them
# identically. Tokens are matched as substrings of the lowercased (and, for
# Georgian, transliterated) league/tournament name.
SIM_LEAGUE_TOKENS = (
    "srl", "simulated", "cyber", "esoccer", "e-soccer", "esports", "e-sports",
    "ebasket", "e-basket", "volta", "setka", "liga pro", "battle",
    "8 minutes", "2x6", "2х6",   # e-Sports Battle formats (latin + cyrillic х)
    # e-football platforms whose leagues reuse real club/national names
    # (caught on the PARENT category node, e.g. Lider c:25629 / c:17065).
    # Kept UNAMBIGUOUS — no "fifa" (real FIFA World Cup) or "esport" bare.
    "ea sports", "reality league", "efootball", "e-football",
)


def is_simulated_league(name: str | None) -> bool:
    """True when a league/tournament name marks a simulated/esports shelf."""
    low = transliterate(name or "").lower()
    return any(tok in low for tok in SIM_LEAGUE_TOKENS)
