"""Global prematch data horizon — one cap, every book.

Owner call 2026-08-12: *"I don't need any data later than 7 days to be pulled,
from any book."*

Before this, each book had its own idea of how far ahead to look and none of
them had a ceiling:

  * 1xbet had `HORIZON_HOURS` (36 h) — an energy budget, not a policy, and it
    needed a per-sport override the moment American football arrived because
    AF plays weekly and 36 h priced nothing;
  * Crocobet and Setanta had `DETAIL_HOURS`, which only chooses between a full
    ladder and the cheap tier — both still pull the whole board;
  * CrystalBet, Pinnacle, Lider-Bet and Betlive had no horizon at all and
    pulled everything the book published. On the AF board that is 177 CB games
    of which 165 start more than a week out, and Pinnacle publishes Super Bowl
    futures.

This module is the single ceiling on top of all of that. It does not replace
the per-book budgets — those still cut work inside the window — it bounds them:

    effective_horizon = min(book_horizon, MAX_START_DAYS)

Applied at the point in each scraper where it saves the most work, which is
never the same place twice:

    crystalbet  after the list parse, so far games are neither expanded (the
                expensive part) nor emitted
    pinnacle    in _index_matchups, so a dropped matchup takes its whole
                market block with it
    xbet        min() against HORIZON_HOURS — skips the per-event GetGameZip
    liderbet    per match, before parsing its markets
    betlive     per event, before parsing its markets
    crocobet    on the board, which also drops that event's ladder call
    setanta     on the event map, before the GetMarketsByEventIds sweep

**Unknown start times are KEPT.** A missing kickoff is not evidence that the
event is far away, and silently dropping those rows would lose real fixtures —
the matcher takes the same view (an unknown time falls back to name confidence
rather than being rejected).

Live-adjustable from the Config tab as `limits.max_start_days`; seeded from
`MAX_START_DAYS`. 0 disables the cap entirely (restores the old behaviour).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional, TypeVar

log = logging.getLogger(__name__)

# The default the owner asked for. Anything starting more than this many days
# from now is not fetched, not parsed and not emitted.
MAX_START_DAYS_DEFAULT = 7.0

T = TypeVar("T")


def max_start_days() -> float:
    """Current cap in days. 0 (or negative) means "no cap".

    Reads the live runtime config so the Config tab takes effect on the next
    poll without a restart; falls back to the env seed if the config store is
    unavailable for any reason (a scraper must never fail to fetch because a
    settings file is unreadable).
    """
    try:
        from src import runtime_config
        return float(runtime_config.num("limits", "max_start_days",
                                        MAX_START_DAYS_DEFAULT))
    except Exception:
        try:
            return float(os.environ.get("MAX_START_DAYS", MAX_START_DAYS_DEFAULT))
        except (TypeError, ValueError):
            return MAX_START_DAYS_DEFAULT


def max_start_hours() -> Optional[float]:
    """The cap in hours, or None when disabled. For books whose own budget is
    already expressed in hours — see `capped_hours`."""
    days = max_start_days()
    return days * 24.0 if days > 0 else None


def capped_hours(book_hours: float) -> float:
    """Combine a book's own horizon with the global cap: the tighter wins.

    Keeps the two concerns separate. A book's number says "this is all the work
    I can afford"; the cap says "this is all the data anyone wants". Writing
    min() here rather than lowering the book constant means raising the cap
    later restores the book's real intent instead of leaving it pinned at
    whatever the cap happened to be.
    """
    cap = max_start_hours()
    if cap is None:
        return book_hours
    if book_hours <= 0:            # 0 = "unlimited" in several book configs
        return cap
    return min(book_hours, cap)


def cutoff() -> Optional[datetime]:
    """UTC instant past which nothing is wanted, or None when disabled."""
    hours = max_start_hours()
    if hours is None:
        return None
    return datetime.now(tz=timezone.utc) + timedelta(hours=hours)


def cutoff_ts() -> Optional[float]:
    """`cutoff()` as epoch seconds — for feeds that carry raw timestamps."""
    c = cutoff()
    return c.timestamp() if c is not None else None


def keeps(start_time: Any, *, cut: Optional[datetime] = None) -> bool:
    """Is this event inside the horizon?

    Accepts a datetime (naive treated as UTC) or None. None → True: see the
    module docstring on unknown start times. Pass `cut` when filtering a whole
    board so every row is judged against one instant rather than a clock that
    moves under the loop.
    """
    if start_time is None:
        return True
    if cut is None:
        cut = cutoff()
        if cut is None:
            return True
    if not isinstance(start_time, datetime):
        return True
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=timezone.utc)
    return start_time <= cut


def filter_items(
    items: Iterable[T],
    start_of: Callable[[T], Any],
    *,
    label: str = "",
) -> list[T]:
    """Drop items starting beyond the horizon; log once per call, not per item.

    `start_of` extracts the kickoff from an item and may return None (kept).
    """
    cut = cutoff()
    if cut is None:
        return list(items)
    kept, dropped = [], 0
    for it in items:
        try:
            ok = keeps(start_of(it), cut=cut)
        except Exception:
            ok = True              # never let an extractor bug lose data
        if ok:
            kept.append(it)
        else:
            dropped += 1
    if dropped and label:
        log.debug("%s: %d events beyond the %.1f-day horizon skipped",
                  label, dropped, max_start_days())
    return kept
