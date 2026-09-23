"""Futsal on CrystalBet — an LSport-only, soccer-shaped sport. See soccerlike.py.

100 % of CB's futsal board was LSport on the day it was measured (8 of 8).
CB sport_id: 26. No Pinnacle mapping (not fetched).
"""
from __future__ import annotations

from src.scrapers.sports import soccerlike

SPORT_ID = 26
SPORT_NAME = "futsal"

parse_loadinfo, parse_div_odds = soccerlike.make_parsers(SPORT_NAME)
classify_market_title = soccerlike.classify
classify_market_title_permissive = soccerlike.classify
