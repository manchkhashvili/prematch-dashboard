"""
Recognising player markets, so they can be left out of the study.

WHY DROP THEM
-------------
They are half the board. Measured 2026-08-26: **52 % of CrystalBet's soccer
positions and 33 % of Lider's** are scoped to an individual — anytime
goalscorer, shots, assists, cards, and the combination markets built on top of
them. Dropping them takes a pass from 4.43 M positions to roughly 2.7 M, which
is most of the storage and a good share of the parse.

They are also the markets a soft book watches hardest. Player props carry low
limits and get their prices from a different feed than the match markets, so
their movement says little about whether the *match* markets are keeping up —
which is the question here.

HOW
---
Lider says so structurally, and that is used where available: the market or
outcome specifier carries a `player` / `lb_br_player` key, or an
`sr:player:NNNN` value. No guessing.

CrystalBet says nothing structurally — the player's name is simply inside the
market title ("Anytime goalscorer & correct score Soula, Mazire (PFC Levski
Sofia)") or the selection label. So it is recognised by the "Last, First"
convention both books write people in, and the pattern has to survive what real
boards contain:

    Welbeck, D                     initial-only first name
    Bughail-mellor, D Mani         hyphen, then two given names
    Assists Chust, Víctor (Elche)  accented, name in the middle of a title
    0:1, 0:2 or 0:3                a MULTISCORE selection, not a person
    Team 1 win or 0-0, 0-1         a scoreline list, not a person

The discriminator is what sits immediately before the comma: a letter for a
person, a digit for a scoreline. Requiring that — rather than just looking for
", " — is what keeps correct-score markets in the study.
"""
from __future__ import annotations

import re

# A letter (any script, not a digit or underscore), then name characters, then
# ", " and an upper-case letter. The trailing letter may stand alone, because
# CrystalBet abbreviates given names to an initial.
PERSON = re.compile(r"[^\W\d_][\w.'’\-]*,\s+[A-ZÀ-ÖØ-Þ]")

# Titles that name no one but exist only to price individuals.
TOKENS = re.compile(r"\bgoalscorer\b|\bplayer\b|\bscorer\b|\bassists\b", re.I)

# Lider's structural tells.
_PLAYER_KEYS = ("player", "lb_br_player")
_PLAYER_ID = re.compile(r"^sr:player:", re.I)


def looks_like_person(text: str) -> bool:
    """Does this string name an individual?"""
    return bool(text) and PERSON.search(text) is not None


def is_player_market(market: str = "", side: str = "", specifier=None) -> bool:
    """Is this position scoped to an individual?

    `specifier` is Lider's, when there is one — it settles the question without
    reading any names. Everything else falls back to the naming convention.
    """
    if isinstance(specifier, dict):
        for key, value in specifier.items():
            if key in _PLAYER_KEYS or _PLAYER_ID.match(str(key)):
                return True
            if _PLAYER_ID.match(str(value)):
                return True
    if market and TOKENS.search(market):
        return True
    return looks_like_person(market) or looks_like_person(side)
