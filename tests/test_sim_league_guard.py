"""Simulated / e-football league guard — real competitions must survive,
simulated ones (which reuse real team/national names) must be dropped.

2026-07-14: Lider hides a simulated "World Cup" (England v Argentina) under the
parent category "Simulated Reality League"; the REAL "World Cup 2026" is a
separate category. Blocking the tournament leaf name would kill the real WC —
the guard must key on markers that only the simulated shelves carry.
"""
from src.normalize import is_simulated_league


def test_blocks_simulated_shelves():
    for name in (
        "Simulated Reality League",
        "EA Sports FC. UEL",
        "Cyberfootball. Stream 1 Tournament 4. Matches 8 minutes.",
        "Summer SRL Friendlies",
        "K liga 1 SRL",
        "Soccer - e-Sports Battle - Serie A",
        "eFootball Battle",
    ):
        assert is_simulated_league(name), name


def test_keeps_real_competitions():
    for name in (
        "World Cup 2026",
        "World Cup",                 # a real WC tournament leaf (parent decides)
        "FIFA World Cup",
        "Champions League",
        "Europa League",
        "Premier League (whoscored.com)",
        "Matches (www.whoscored.com)",
        "Friendlies",
        "England", "Argentina",
        "NBA Summer League",
        None, "",
    ):
        assert not is_simulated_league(name), name
