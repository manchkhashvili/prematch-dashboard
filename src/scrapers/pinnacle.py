"""
Pinnacle prematch scraper — multi-sport (basketball + soccer in Phase 2).

Hits the guest Arcadia API documented in the project brief:

  GET /sports/{sport_id}/leagues?all=false           → list of leagues
  (brandId=0 used to be passed here but tennis returns 403 with it; safer
   to omit entirely — see Phase 3.1.1 notes in build_log.)
  GET /sports/{sport_id}/matchups                     → bulk: all matchups
  GET /leagues/{id}/markets/straight                  → all market entries

Each market entry has shape:

  {
    "matchupId": int,
    "period": 0 | 1,                # 0 = FT, 1 = H1 (no Q1-Q4 prematch)
    "type": "moneyline" | "spread" | "total" | "team_total",
    "key": "s;0;s;2.5",             # used by Pinnacle internally
    "side": "home" | "away",        # PRESENT for team_total only
    "isAlternate": bool,            # alt-line marker (we keep all lines)
    "cutoffAt": "2026-05-25T19:00:00Z",
    "prices": [
      {"designation": "home"|"away"|"draw"|"over"|"under",
       "points": float|None,        # line value for spread/total/team_total
       "price": int},               # American odds
      ...
    ]
  }

We emit one `Odds` per (matchupId, period, type, line, submarket, team_side),
both sides joined into `selections`. All alt-lines included — no main-line
filter. Decimal odds ≤ 1.0 (suspended) → that whole market line is dropped.

Soccer-specific (Phase 2, sport_id=29) wire shapes (verified 2026-05-26):

  - moneyline 3-way: one entry with 3 prices [home, draw, away]. Detected
    by the presence of any "draw" designation.
  - team_total: TWO entries per matchupId, distinguished by top-level
    `"side": "home"|"away"`. Each entry has over/under prices.
  - Corners child matchups: separate matchup with `parentId` set and
    `"units": "Corners"`. The child's markets live in the PARENT league's
    /markets/straight (the corners child league itself 403s). We fold
    child Odds onto the parent matchupId tagged with submarket="corners",
    so the matcher's CB↔Pin join stays 1:1 — see Option A architecture
    in build_log.
  - period=39 ("To Advance" on knockout ties): already filtered by
    PERIOD_MAP — period_int not in {0, 1} → continue. Zero new code.
  - type=special (~2700 entries on soccer, unstructured props): filtered
    at the matchup index step.
  - Bookings child matchups (`units="Bookings"`): deferred to Phase 2.5
    per user — only 2 Pinnacle leagues ship them currently.

Run from prematch/:
    .venv/bin/python -m src.scrapers.pinnacle           # basketball smoke
    .venv/bin/python -m src.scrapers.pinnacle --soccer  # soccer smoke
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

# Run-as-script safety net: when invoked via `python src/scrapers/pinnacle.py`,
# the parent `prematch/` dir isn't on sys.path so `from src.models` would fail.
# `python -m src.scrapers.pinnacle` works without this. Both are supported.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.models import Odds  # noqa: E402
from src.vig import american_to_decimal  # noqa: E402

log = logging.getLogger(__name__)

PINNACLE_BASE = "https://guest.api.arcadia.pinnacle.com/0.1"
SPORT_ID_BASKETBALL = 4
SPORT_ID_SOCCER = 29
SPORT_ID_TENNIS = 33  # verify on first live run — adjust if Pinnacle ships tennis on a different id
SPORT_ID_MMA = 22     # Phase 4.3 — verified via CC discovery 2026-05-27 ("Mixed Martial Arts": UFC + LFA + Road to UFC)

# Per-sport Referer mostly cosmetic — Pinnacle's WAF doesn't enforce it
# strictly — but matches what a real user would send. Other headers shared.
def _make_headers(sport_name: str) -> dict[str, str]:
    return {
        "x-api-key": "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R",  # from /config/app.json; rotate if 403
        "Origin": "https://www.pinnacle.com",
        "Referer": f"https://www.pinnacle.com/en/{sport_name}/",
    }


# Back-compat constant: basketball headers. Tests don't import this, but the
# original module exposed HEADERS at module level.
HEADERS = _make_headers("basketball")

# Skip league names containing any of these (case-insensitive). Includes
# soccer-specific "corners"/"bookings" — those leagues exist in the
# /sports/29/leagues response but their /markets/straight always 403s
# (the markets are accessible only via the PARENT league's response,
# folded via parent.id). Adding to skip prevents wasted requests +
# unnecessary failure-tracker churn.
LEAGUE_SKIP = (
    "cyber", "esport", "ebasket", "specials", "outright",
    "corners", "bookings",
)

# Pinnacle period int → our Period literal. Q1-Q4 not offered prematch.
# Period 39 (knockout "To Advance") gets dropped here naturally — it's
# not in PERIOD_MAP so the filter `period_int not in PERIOD_MAP` skips it.
PERIOD_MAP: dict[int, str] = {0: "FT", 1: "H1"}

# Allowed market types — Phase 2 added "team_total" for soccer Home/Away
# Team Total. Kept as a UNION across sports for back-compat with any
# external code importing it. Per-sport gating happens via
# ALLOWED_MARKET_TYPES_BY_SPORT below so basketball stays at the Phase 1
# scope (no team_total) even though Pinnacle does ship 10-ish basketball
# team_total Odds per cycle. CB-side basketball classifier has no
# team_total rules so those Pin Odds would be unpaired phantom rows; the
# v1 fix is to not fetch them at all. To enable basketball team_total
# end-to-end is a Phase 1.5 follow-up (CB classifier + frontend).
ALLOWED_MARKET_TYPES = {"moneyline", "spread", "total", "team_total"}

ALLOWED_MARKET_TYPES_BY_SPORT: dict[str, set[str]] = {
    "basketball": {"moneyline", "spread", "total"},                  # Phase 1 set
    "soccer":     {"moneyline", "spread", "total", "team_total"},    # Phase 2 set
    "tennis":     {"moneyline", "spread", "total"},                  # Phase 3.1 — same as basketball
    "mma":        {"moneyline", "total"},                            # Phase 4.3 — no spread (can't handicap a fight)
}


RETRY_STATUSES = {403, 429, 500, 502, 503, 504}
RETRY_BACKOFF_SEC = 1.5  # second attempt fires after this delay

# Persistent-failure tracker for /markets/straight per league.
# Pinnacle's guest API has two kinds of 403:
#   (1) Cloudflare/WAF flicker — different leagues fail each cycle. Retry
#       and one-cycle backoff handles these.
#   (2) Commercial-data-feed restrictions — same leagues fail every cycle.
#       Retry never helps. Better to skip and try again in 30 min in case
#       Pinnacle's access changes.
# After MAX_CONSECUTIVE_FAILURES misses on a league, suppress requests for
# that league until SKIP_DURATION_SEC has elapsed. A success at any point
# resets the counter.
#
# Keys are (sport_id, league_id) tuples so basketball league 559 and any
# hypothetical soccer league 559 don't collide.
MAX_CONSECUTIVE_FAILURES = 3
SKIP_DURATION_SEC = 1800   # 30 min

_consecutive_failures: dict[tuple[int, int], int] = {}
_skip_until: dict[tuple[int, int], datetime] = {}


async def _get(client: httpx.AsyncClient, path: str,
               params: dict[str, Any] | None = None) -> Any:
    """
    GET with one retry on 403/429/5xx. Pinnacle's guest edge flakes
    intermittently — same headers/params can 403 once and 200 a second
    later. One retry recovers most of those without paying the cost on
    truly forbidden endpoints (those still fail twice and we give up).
    """
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            r = await client.get(f"{PINNACLE_BASE}{path}", params=params, timeout=15.0)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            last_exc = e
            if e.response.status_code in RETRY_STATUSES and attempt == 0:
                log.debug("retrying %s after %d (sleep %.1fs)",
                          path, e.response.status_code, RETRY_BACKOFF_SEC)
                await asyncio.sleep(RETRY_BACKOFF_SEC)
                continue
            raise
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ReadError) as e:
            last_exc = e
            if attempt == 0:
                log.debug("retrying %s after transient %s", path, type(e).__name__)
                await asyncio.sleep(RETRY_BACKOFF_SEC)
                continue
            raise
    # Should be unreachable — both branches above either return or raise.
    raise last_exc if last_exc else RuntimeError(f"_get fell through for {path}")


# ── Generic per-sport fetch ───────────────────────────────────────────────────

async def _fetch_pinnacle_for_sport(
    sport_id: int, sport_name: str, *, concurrency: int = 10,
) -> list[Odds]:
    """
    Return all prematch Odds for the given sport across FT + H1, including
    all alt-lines for spread/total/team_total, plus 3-way moneyline (soccer)
    and corners child-matchup markets folded onto parent matchupIds with
    `submarket="corners"`.

    Request shape per cycle:
      - 1 call /sports/{sport_id}/leagues       (discover leagues)
      - 1 call /sports/{sport_id}/matchups      (bulk: parents + children)
      - N calls /leagues/{id}/markets/straight  (per non-skipped, non-cooldown league)
    """
    now = datetime.now(timezone.utc)
    out: list[Odds] = []
    headers = _make_headers(sport_name)

    async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
        # ── League discovery ──────────────────────────────────────────────
        # NOTE: dropped brandId=0 (Phase 3.1.1, 2026-05-26). Tennis (sport_id=33)
        # returns 403 BAD_APIKEY with brandId=0 but 200 without it. Basketball
        # and soccer accept either form — without is safe across all sports.
        # Same gotcha hit /leagues/{id}/matchups in the May 24 fix; this
        # generalizes the rule: brandId=0 is unreliable on Pinnacle's guest
        # endpoints, drop it everywhere.
        try:
            leagues = await _get(
                client,
                f"/sports/{sport_id}/leagues",
                params={"all": "false"},
            )
        except Exception as e:
            log.error("Pinnacle %s league discovery failed: %s", sport_name, e)
            return []

        leagues = [
            L for L in leagues
            if isinstance(L, dict)
            and not any(s in (L.get("name") or "").lower() for s in LEAGUE_SKIP)
        ]
        log.info("Pinnacle %s: %d leagues after filter", sport_name, len(leagues))

        # ── Bulk matchups: parents + child folding map ─────────────────────
        try:
            all_matchups = await _get(
                client, f"/sports/{sport_id}/matchups"
            )
        except Exception as e:
            log.error("Pinnacle %s bulk matchups failed: %s", sport_name, e)
            return []

        matchup_by_id = _index_matchups(all_matchups, now)
        child_to_parent = _index_child_matchups(all_matchups)
        log.info(
            "Pinnacle %s: bulk matchups indexed %d parents, %d corners children",
            sport_name, len(matchup_by_id), len(child_to_parent),
        )

        # ── Per-league markets (parallel, with skip + failure tracking) ────
        sem = asyncio.Semaphore(concurrency)
        n_skipped = 0

        async def _one_league(L: dict) -> list[Odds]:
            nonlocal n_skipped
            lid = L.get("id")
            lname = L.get("name", "")
            key = (sport_id, lid) if lid is not None else None
            if key is not None and _league_in_cooldown(key):
                n_skipped += 1
                return []
            async with sem:
                try:
                    markets = await _get(
                        client, f"/leagues/{lid}/markets/straight"
                    )
                except httpx.HTTPStatusError as e:
                    # 404 = no markets booked, not a real failure
                    if e.response.status_code == 404:
                        return []
                    if key is not None:
                        _record_failure(key, lname, str(e.response.status_code))
                    return []
                except Exception as e:
                    if key is not None:
                        _record_failure(key, lname, type(e).__name__)
                    return []
            if key is not None:
                _record_success(key)

            # Phase 3.8 (2026-05-27): Pinnacle's bulk /sports/{id}/matchups
            # endpoint sometimes omits matchups that the per-league
            # /leagues/{id}/matchups endpoint includes (verified via the
            # Resende vs America RJ / Brazil Carioca A2 debug session — the
            # match was visible on pinnacle.com and present in per-league
            # markets, but missing from bulk). When that happens, _build_odds_
            # for_league silently drops every market row referencing an
            # unknown matchup_id (line ~405 in this file). Recovery: detect
            # missing parents, fall back to the per-league matchups endpoint
            # for THIS league only, merge into a local view, retry.
            league_matchup_by_id = matchup_by_id
            league_child_to_parent = child_to_parent
            referenced_mids = {
                mkt.get("matchupId") for mkt in markets if isinstance(mkt, dict)
            } - {None}
            # Resolve child→parent before checking missing.
            needed_parent_mids = {
                child_to_parent.get(mid, (mid, None))[0]
                for mid in referenced_mids
            }
            missing_parents = needed_parent_mids - matchup_by_id.keys()
            if missing_parents:
                async with sem:
                    try:
                        league_matchups_raw = await _get(
                            client, f"/leagues/{lid}/matchups"
                        )
                    except Exception as e:
                        log.warning(
                            "Pinnacle %s league '%s' (id=%s): per-league matchups "
                            "fallback failed (%s); %d matchup(s) still missing → rows dropped",
                            sport_name, lname, lid, e, len(missing_parents),
                        )
                        league_matchups_raw = None
                if league_matchups_raw is not None:
                    extra_index = _index_matchups(league_matchups_raw, now)
                    extra_children = _index_child_matchups(league_matchups_raw)
                    # Merge — local view supersedes bulk where keys overlap.
                    league_matchup_by_id = {**matchup_by_id, **extra_index}
                    league_child_to_parent = {**child_to_parent, **extra_children}
                    recovered = missing_parents & extra_index.keys()
                    still_missing = missing_parents - league_matchup_by_id.keys()
                    if recovered:
                        log.warning(
                            "Pinnacle %s league '%s' (id=%s): bulk missed %d matchup(s), "
                            "recovered %d via per-league fallback%s",
                            sport_name, lname, lid, len(missing_parents), len(recovered),
                            f" ({len(still_missing)} still missing)" if still_missing else "",
                        )

            rows = _build_odds_for_league(
                markets, league_matchup_by_id, lname,
                child_to_parent=league_child_to_parent, sport_name=sport_name,
            )
            if rows:
                log.info("%s league %s: %d Odds rows", sport_name, lname, len(rows))
            return rows

        results = await asyncio.gather(*[_one_league(L) for L in leagues])
        for r in results:
            out.extend(r)
        if n_skipped:
            log.info(
                "Pinnacle %s: %d league(s) skipped this cycle (in failure cooldown)",
                sport_name, n_skipped,
            )

    return out


async def fetch_pinnacle_basketball(*, concurrency: int = 10) -> list[Odds]:
    """Pre-Phase-2 public API — thin wrapper around _fetch_pinnacle_for_sport."""
    return await _fetch_pinnacle_for_sport(
        SPORT_ID_BASKETBALL, "basketball", concurrency=concurrency,
    )


async def fetch_pinnacle_soccer(*, concurrency: int = 10) -> list[Odds]:
    """Fetch all prematch soccer Odds (parent matchups + corners children folded in)."""
    return await _fetch_pinnacle_for_sport(
        SPORT_ID_SOCCER, "soccer", concurrency=concurrency,
    )


async def fetch_pinnacle_tennis(*, concurrency: int = 10) -> list[Odds]:
    """Fetch all prematch tennis Odds. 2-way ML + spread + total, no child matchups."""
    return await _fetch_pinnacle_for_sport(
        SPORT_ID_TENNIS, "tennis", concurrency=concurrency,
    )


async def fetch_pinnacle_mma(*, concurrency: int = 10) -> list[Odds]:
    """Fetch all prematch MMA Odds. 2-way ML + total rounds. Small inventory
    (typically <10 leagues — UFC, LFA, Road to UFC, occasionally ONE/PFL)."""
    return await _fetch_pinnacle_for_sport(
        SPORT_ID_MMA, "mma", concurrency=concurrency,
    )


# ── Per-league failure tracker (keys are (sport_id, league_id) tuples) ────────
def _league_in_cooldown(key: tuple[int, int]) -> bool:
    until = _skip_until.get(key)
    if until is None:
        return False
    if datetime.now(timezone.utc) >= until:
        # Cooldown expired — clear state and let it retry.
        _skip_until.pop(key, None)
        _consecutive_failures.pop(key, None)
        return False
    return True


def _record_failure(key: tuple[int, int], league_name: str, reason: str) -> None:
    count = _consecutive_failures.get(key, 0) + 1
    _consecutive_failures[key] = count
    if count >= MAX_CONSECUTIVE_FAILURES and key not in _skip_until:
        until = datetime.now(timezone.utc) + timedelta(seconds=SKIP_DURATION_SEC)
        _skip_until[key] = until
        log.warning(
            "league %s (sport %d) (%s): %d consecutive failures (last=%s); "
            "cooldown until %s",
            key[1], key[0], league_name, count, reason,
            until.isoformat(timespec="seconds"),
        )
    else:
        log.warning(
            "league %s (sport %d) (%s): %s (consecutive failures: %d/%d)",
            key[1], key[0], league_name, reason, count, MAX_CONSECUTIVE_FAILURES,
        )


def _record_success(key: tuple[int, int]) -> None:
    _consecutive_failures.pop(key, None)
    _skip_until.pop(key, None)


# ── Market parsing ────────────────────────────────────────────────────────────

def _build_odds_for_league(
    markets: list[dict],
    matchup_by_id: dict[int, dict],
    league_name: str,
    *,
    child_to_parent: Optional[dict[int, tuple[int, str]]] = None,
    sport_name: str = "basketball",
) -> list[Odds]:
    """
    Emit Odds for one league's markets, using the pre-built matchup_by_id map
    for team names + start times.

    Phase 2 additions (kw-only, defaults preserve pre-Phase-2 basketball behavior):
      - `child_to_parent`: {child_matchupId: (parent_matchupId, submarket)}. When
        a market's matchupId is in this map, attribute the Odds to the PARENT
        matchupId and tag with submarket. Empty dict → behaves like basketball
        (no child folding).
      - `sport_name`: stamped onto emitted Odds.sport. Default "basketball" for
        back-compat with the existing test suite.

    3-way moneyline (soccer 1X2): detected by presence of any `"draw"`
    designation in the prices array. Emits selections={home, draw, away}.

    team_total: distinguished from total by `type="team_total"`. Reads the
    top-level `"side"` field for team_side. Same over/under prices as total.
    """
    out: list[Odds] = []
    fetched_at = datetime.now(timezone.utc)
    if child_to_parent is None:
        child_to_parent = {}

    for mkt in markets:
        mid = mkt.get("matchupId")
        # Child-matchup fold: if this market belongs to a corners/games child,
        # rewrite mid to the PARENT matchupId and remember the submarket tag.
        # `from_child` flag is critical for the units=sets skip below: child
        # markets are games-based by construction (units=Games on the child),
        # so the parent's units=sets shouldn't filter them out.
        submarket: Optional[str] = None
        from_child = False
        if mid in child_to_parent:
            parent_id, submarket = child_to_parent[mid]
            mid = parent_id
            from_child = True
        info = matchup_by_id.get(mid)
        if info is None:
            continue
        period_int = mkt.get("period")
        if period_int not in PERIOD_MAP:
            continue
        mtype = (mkt.get("type") or "").lower()

        # Phase 3.1.4: tennis Sets/Games split. Pinnacle parent matchup has
        # units="Sets" — its spread + total are SET-based (handicap ±1.5 sets,
        # total 2.5 sets). CB tennis primary handicap is games-based, so
        # set-based variants would mis-pair. Skip the parent's spread/total
        # when units=sets — but ONLY when the market came from the parent
        # itself, not from a Games child folded onto this parent.
        # Games-based spread/total come from the child (units=Games), folded
        # via child_to_parent above with submarket=None — they bypass this
        # skip via `from_child=True` and appear as the primary spread/total.
        # ML is kept regardless of units (ML is the same prediction either way).
        if (not from_child
                and info.get("units") == "sets"
                and mtype in ("spread", "total", "team_total")):
            continue
        # Per-sport gating: basketball was Phase 1 (no team_total) and
        # adding it would surface phantom unpaired Pin rows (CB-side has
        # no team_total classifier). Unknown sports fall back to the full
        # ALLOWED_MARKET_TYPES set.
        allowed_for_sport = ALLOWED_MARKET_TYPES_BY_SPORT.get(
            sport_name, ALLOWED_MARKET_TYPES,
        )
        if mtype not in allowed_for_sport:
            continue

        prices = mkt.get("prices") or []
        if not prices:
            continue

        # 3-way ML detection: any "draw" designation in this entry's prices
        # means it's soccer 1X2. Basketball ML entries never have "draw"
        # (only "home"/"away"), so this is safe for both sports.
        is_3way_ml = (mtype == "moneyline" and any(
            (p.get("designation") or "").lower() == "draw" for p in prices
        ))

        # team_total side detection: required for team_total, ignored otherwise.
        team_side: Optional[str] = None
        if mtype == "team_total":
            side = (mkt.get("side") or "").lower()
            if side not in ("home", "away"):
                # Malformed team_total entry (no side field) — skip.
                continue
            team_side = side

        # Allowed designations expand for 3-way ML (adds "draw").
        allowed_designations = {"home", "away", "over", "under"}
        if is_3way_ml:
            allowed_designations = allowed_designations | {"draw"}

        selections: dict[str, float] = {}
        points: dict[str, float] = {}
        all_sides_ok = True
        for p in prices:
            designation = (p.get("designation") or "").lower()
            if designation not in allowed_designations:
                continue
            price = p.get("price")
            if price is None:
                all_sides_ok = False
                continue
            try:
                dec = american_to_decimal(price)
            except (ValueError, ZeroDivisionError):
                all_sides_ok = False
                continue
            if dec <= 1.0:
                all_sides_ok = False
                continue
            selections[designation] = dec
            pts = p.get("points")
            if pts is not None:
                points[designation] = float(pts)

        # Line convention: ALWAYS the home side's points for spreads. Taking
        # "first price with points" flipped the sign whenever Pinnacle listed
        # the away price first (v2 finding #2, 2026-06-12) — a +1.5 became
        # -1.5, silently mispairing against CB lines. Totals/team totals carry
        # the same points on both sides, so over/under is sign-safe.
        line: Optional[float] = points.get(
            "home", points.get("over", points.get("under"))
        )

        if not all_sides_ok:
            continue

        # Per-type validation: must have all required sides + a line (where applicable).
        if mtype == "moneyline":
            if is_3way_ml:
                if not {"home", "draw", "away"} <= selections.keys():
                    continue
            else:
                if not {"home", "away"} <= selections.keys():
                    continue
            line = None
        elif mtype == "spread":
            if not {"home", "away"} <= selections.keys() or line is None:
                continue
        elif mtype == "total":
            if not {"over", "under"} <= selections.keys() or line is None:
                continue
        elif mtype == "team_total":
            if not {"over", "under"} <= selections.keys() or line is None:
                continue
            # team_side guaranteed non-None by the early continue above.

        # Pinnacle's own risk limit for this market (v2 finding: genuinely
        # useful for "is this edge bet-sized worth it"; rides into the arbs
        # and moves columns).
        limits = mkt.get("limits") or []
        max_stake = next(
            (l.get("amount") for l in limits
             if isinstance(l, dict) and l.get("type") == "maxRiskStake"),
            None,
        )

        try:
            out.append(Odds(
                source="pinnacle",
                sport=sport_name,
                home=info["home"],
                away=info["away"],
                market_type=mtype,
                period=PERIOD_MAP[period_int],
                selections=selections,
                fetched_at=fetched_at,
                line=line,
                start_time=info["start_time"],
                league=league_name,
                raw_event_id=str(mid),
                submarket=submarket,  # type: ignore[arg-type]
                team_side=team_side,  # type: ignore[arg-type]
                max_stake=max_stake,
            ))
        except ValueError as e:
            # Odds.__post_init__ rejected — should be unreachable given our
            # filtering above, but keep the diagnostic.
            log.debug("Odds construction rejected for matchup %s: %s", mid, e)

    return out


def _index_matchups(matchups: list[dict], now: datetime) -> dict[int, dict]:
    """
    Build matchupId → {home, away, start_time, units} for TOP-LEVEL prematch games.

    Phase 3.1.4: now also stores `units` field. Tennis parent matchups have
    units="Sets" — used downstream to skip set-handicap and set-total markets
    in favor of the games-based equivalents from the child matchup. Soccer
    and basketball parents typically have units="Regular" — no special
    behavior applied.

    Filters applied:
      - Skip sub-matchups (parent != None) — those are corners/bookings
        children OR props/derivatives. Children get folded via
        _index_child_matchups separately.
      - Skip specials (type=="special") — Pinnacle's unstructured prop entries.
      - Skip live matchups (startTime <= now).
      - Skip if we can't extract both home + away from `participants`.

    Returns only parent matchups.
    """
    out: dict[int, dict] = {}
    for m in matchups:
        if not isinstance(m, dict):
            continue
        # Phase 2: filter type=special (Pinnacle's unstructured props/futures —
        # ~2700 entries on soccer, no machine-priceable structure).
        if m.get("type") == "special":
            continue
        if m.get("parent") is not None:
            continue
        start = _parse_iso(m.get("startTime"))
        if start is None or start <= now:
            continue
        home = away = None
        for p in (m.get("participants") or []):
            align = (p.get("alignment") or "").lower()
            name = p.get("name")
            if align == "home":
                home = name
            elif align == "away":
                away = name
        if not home or not away:
            continue
        out[m["id"]] = {
            "home": home,
            "away": away,
            "start_time": start,
            "units": (m.get("units") or "").lower(),  # Phase 3.1.4
        }
    return out


def _index_child_matchups(matchups: list[dict]) -> dict[int, tuple[int, Optional[str]]]:
    """
    Build child_matchupId → (parent_matchupId, submarket) for child matchups
    we want to fold onto the parent.

    Distinguishes via top-level `parentId` field (NOT nested `parent` dict)
    + `units` field:

      units == "Corners"  → submarket = "corners"  (soccer corners markets)
      units == "Games"    → submarket = None       (tennis games markets are
                                                    the primary spread/total
                                                    we want; folded as-if-primary)
      units == "Bookings" → DEFERRED to Phase 2.5 (skipped)
      anything else       → not a known child type → skipped

    Phase 3.1.4 added the "Games" handling for tennis. Pinnacle splits tennis
    into a parent matchup (units="Sets", ML + set handicap + set total) and a
    child matchup (units="Games", games handicap + games total + first-set
    markets). CB ships games-handicap as primary, so we want Pin's games
    markets to be the primary spread/total — hence submarket=None on fold.
    The parent's set-handicap and set-total are skipped via the "units=sets"
    check in `_build_odds_for_league`.

    Returns empty dict for basketball + soccer (which lack the children of
    these kinds in their leagues' market response).
    """
    out: dict[int, tuple[int, Optional[str]]] = {}
    for m in matchups:
        if not isinstance(m, dict):
            continue
        if m.get("type") == "special":
            continue
        parent_id = m.get("parentId")
        if parent_id is None:
            continue
        units = (m.get("units") or "").lower()
        if units == "corners":
            out[m["id"]] = (parent_id, "corners")
        elif units == "games":
            # Phase 3.1.4: tennis games-units child → fold as primary
            # (no submarket tag). Parent's set-based spread/total are
            # filtered separately by the "units=sets" check in
            # _build_odds_for_league.
            out[m["id"]] = (parent_id, None)
        # "bookings" intentionally skipped for v1
    return out


def _parse_iso(s: str | None) -> datetime | None:
    """Pinnacle uses ISO 8601 with trailing Z. Returns UTC-aware datetime."""
    if not s:
        return None
    try:
        # Python 3.11+ accepts 'Z' directly, but the replace keeps us safe on 3.10.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


# ── Smoke test ─────────────────────────────────────────────────────────────────
def _smoke(sport: str = "basketball") -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if sport == "soccer":
        odds = asyncio.run(fetch_pinnacle_soccer())
    else:
        odds = asyncio.run(fetch_pinnacle_basketball())

    print("\n" + "=" * 60)
    print(f"  Pinnacle {sport} smoke test — {len(odds)} Odds rows total")
    print("=" * 60)

    by_type: dict[str, int] = {}
    by_period: dict[str, int] = {}
    by_submarket: dict[str, int] = {}
    matchups: set[tuple[str, str, str]] = set()
    for o in odds:
        by_type[o.market_type] = by_type.get(o.market_type, 0) + 1
        by_period[o.period] = by_period.get(o.period, 0) + 1
        sm = o.submarket or "(parent)"
        by_submarket[sm] = by_submarket.get(sm, 0) + 1
        matchups.add((o.raw_event_id or "", o.home, o.away))

    print(f"\n  unique matchups: {len(matchups)}")
    print(f"  by market_type:  {by_type}")
    print(f"  by period:       {by_period}")
    print(f"  by submarket:    {by_submarket}")

    sample_types = ["moneyline", "spread", "total"]
    if sport == "soccer":
        sample_types.append("team_total")
    for mt in sample_types:
        sample = next((o for o in odds if o.market_type == mt), None)
        if sample is None:
            print(f"\n  --- no {mt} samples found ---")
            continue
        print(f"\n  --- sample {mt} ---")
        print(f"    league:     {sample.league}")
        print(f"    matchup:    {sample.home} vs {sample.away}")
        print(f"    start_time: {sample.start_time.isoformat()}")
        print(f"    period:     {sample.period}")
        print(f"    line:       {sample.line}")
        print(f"    selections: {sample.selections}")
        print(f"    submarket:  {sample.submarket}")
        print(f"    team_side:  {sample.team_side}")
        print(f"    raw_id:     {sample.raw_event_id}")

    if sport == "soccer":
        # Find a corners sample
        corners = next((o for o in odds if o.submarket == "corners"), None)
        if corners:
            print(f"\n  --- sample corners ---")
            print(f"    league:     {corners.league}")
            print(f"    matchup:    {corners.home} vs {corners.away}")
            print(f"    market:     {corners.market_type} {corners.period} line={corners.line}")
            print(f"    submarket:  {corners.submarket}")
            print(f"    selections: {corners.selections}")
            print(f"    raw_id:     {corners.raw_event_id}")
        else:
            print(f"\n  --- no corners samples this cycle ---")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Pinnacle prematch smoke test")
    ap.add_argument("--soccer", action="store_true", help="smoke soccer instead of basketball")
    args = ap.parse_args()
    _smoke("soccer" if args.soccer else "basketball")
