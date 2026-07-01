"""
HT/FT favourite anomaly — a soft-book screen for heavy favourites (book-agnostic).

For a heavy FT favourite the HT/FT combo for that side (1/1 home, 2/2 away)
should be only marginally longer than the first-half result for the SAME side:
given a heavy favourite leads at the break they almost always win, so
HTFT ≈ H1 (ratio ~1.0, measured 0.97–1.03 across a live soccer board). A combo
priced >= RATIO× its first-half leg is a soft/generous line worth flagging.

Owner-calibrated 2026-07-02: RATIO = 1.2 — real cases seen at H1 1.1 → HT/FT 1.4
(1.27×). Applies to BOTH sides (1/1 and 2/2). Top leagues (World Cup / UEFA /
big-5) are skipped — sharp, no soft errors, and they dominate the <1.3 set.

"Favourite" = a side priced under 1.30 OR a side with NO price while the other
is quoted — a book omitting the favourite's price means it's an extreme
favourite, exactly the case the owner didn't want to miss.

Feed it odds from any book (CrystalBet, Betlive, …); the caller extracts them.
"""
from __future__ import annotations

from typing import Optional

FAV_MAX = 1.30      # a side under this is a heavy favourite
RATIO = 1.2         # flag when HTFT combo >= RATIO × its first-half leg

TOP_LEAGUE_TOKENS = (
    "world cup", "champions league", "europa", "conference", "uefa",
    "premier league", "la liga", "serie a", "bundesliga", "ligue 1",
    "nations league", "copa america", "euro 20",
)


def is_top_league(name: Optional[str]) -> bool:
    n = (name or "").lower()
    return any(tok in n for tok in TOP_LEAGUE_TOKENS)


def favourite_side(ml_home: Optional[float], ml_away: Optional[float]) -> Optional[str]:
    """Which side is the heavy favourite from the FT moneyline, or None. A side
    under FAV_MAX, or a side with NO odds while the other IS priced (an omitted
    favourite price = an extreme favourite). Both-missing → None."""
    if ml_home is not None and ml_home < FAV_MAX:
        return "home"
    if ml_away is not None and ml_away < FAV_MAX:
        return "away"
    if ml_home is None and ml_away is not None:
        return "home"
    if ml_away is None and ml_home is not None:
        return "away"
    return None


def htft_flag(
    fav: str,
    h1_home: Optional[float], h1_away: Optional[float],
    htft_11: Optional[float], htft_22: Optional[float],
    *, ratio: float = RATIO,
) -> Optional[tuple[str, float, float, float]]:
    """For the favourite side, return (side, h1_odds, htft_odds, ratio) if the
    HT/FT combo is >= `ratio` × its first-half leg, else None."""
    if fav == "home" and h1_home and htft_11 and htft_11 >= ratio * h1_home:
        return ("home", h1_home, htft_11, round(htft_11 / h1_home, 2))
    if fav == "away" and h1_away and htft_22 and htft_22 >= ratio * h1_away:
        return ("away", h1_away, htft_22, round(htft_22 / h1_away, 2))
    return None


def should_open(ml_home: Optional[float], ml_away: Optional[float],
                league: Optional[str]) -> bool:
    """Cheap list-view gate: open a match's detail only if it has a heavy
    favourite AND isn't a top league. Keeps the scan lightweight."""
    return favourite_side(ml_home, ml_away) is not None and not is_top_league(league)
