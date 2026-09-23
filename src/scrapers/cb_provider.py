"""Which odds feed a CrystalBet game comes from — read off the list view.

CrystalBet prices its board from two providers, and the owner knows from bet
tickets that one of them — **LSport** — is the feed whose prices carry the
mistakes: essentially every anomaly and consistency opportunity that has been
placed came from an LSport match, and sports LSport does not supply (table
tennis, for one) are all noise. CB exposes no provider field. What it does
expose, on every game row of the list panel, is the provider's OWN fixture id:

    <div class="game-row x_loop_game_title_block" id="G3235754054"
         data-game-code="20264337">                       <- LSport
    <div class="game-row x_loop_game_title_block" id="G3181702400"
         data-game-code="68932806" fs-gameid="hAfMDh4C">  <- the other feed

Measured 2026-09-21 on 400+ games across 16 sports, each one's detail page
expanded and its market vocabulary scored independently of the code: every
game whose markets are LSport's catalogue (`1st Period Winner Home/Away`,
`Under/Over Including Overtime`, `Asian Handicap Halftime`, `Race To 20
Points`, labels `1(-6.5)` / `Under 53.5` / `Odd`) carried a code in
19.5–20.3 M; every game on the other template (`Full Time Result(1X2)*`,
`Halftime/Fulltime`, `Which player will win the match`, labels `1 (-2.50)` /
`Und 5.5` / `odd`) carried one in 63–75 M. Zero overlap. The other feed's rows
also carry a Flashscore `fs-gameid`; LSport rows never do.

Share of the kept (non-outright) board that is LSport, same day: soccer 9 %
(reserves, U19/U20, Northern League D1, Finland Nelonen, Estonia Liiga II),
basketball 8 % (Brazil Paulista U20, Korea Student League), tennis 46 % (ITF),
volleyball 75 %, futsal 100 %, handball 11 %; American football, ice hockey,
rugby, baseball, cricket, table tennis, MMA, boxing and eSoccer 0 %. LSport
also supplies every outright market, which is why a raw count of the board
reads much higher — those rows are skipped by `_skip_league` before anything
here runs. Against that base rate the dashboard's own flags on the same night
were soccer 13/19 LSport, basketball 2/3, tennis 2/2.

The threshold is a range boundary, not a magic number: 40 M sits in the empty
band between the two id spaces and either would have to grow 2x to cross it.
`LSPORT_CODE_MAX` is the single place to move if CB ever changes feeds.
"""
from __future__ import annotations

import re
from typing import Any, Optional

LSPORT = "lsport"
OTHER = "other"

# Upper bound (exclusive) of the LSport fixture-id space. See module docstring.
LSPORT_CODE_MAX = 40_000_000

_RE_INT = re.compile(r"^\d+$")


def game_code_of(container: Any) -> Optional[int]:
    """The provider fixture id for one list-view game container, or None.

    Looks for the first descendant carrying `data-game-code` — CB renders it
    on the game-row title block. A game with no code is possible (measured:
    33 of 1292 raw soccer rows, all outrights) and is reported as unknown
    rather than guessed.
    """
    el = container.find(attrs={"data-game-code": True})
    if el is None:
        return None
    raw = (el.get("data-game-code") or "").strip()
    return int(raw) if _RE_INT.match(raw) else None


def provider_of(game_code: Optional[int]) -> Optional[str]:
    """`"lsport"` / `"other"` from a fixture id; None when there is no id."""
    if game_code is None or game_code <= 0:
        return None
    return LSPORT if game_code < LSPORT_CODE_MAX else OTHER
