"""Handball on CrystalBet — an LSport-only, soccer-shaped sport. See soccerlike.py.

LSport supplies ~10 % of CB's handball board (Brazil women's league,
Luxembourg); the European men's leagues come from the other feed. The LSport
detail page carries the pick'em trio — a 3-way result, Draw No Bet and an
Asian handicap with a 0.0 rung — plus half results and an HT/FT grid, i.e.
exactly what `pickem_*` and `htft_combo` read. CB sport_id: 20. No Pinnacle
mapping (not fetched).
"""
from __future__ import annotations

from src.scrapers.sports import soccerlike

SPORT_ID = 20
SPORT_NAME = "handball"

parse_loadinfo, parse_div_odds = soccerlike.make_parsers(SPORT_NAME)
classify_market_title = soccerlike.classify
classify_market_title_permissive = soccerlike.classify
