"""
Lider-Bet prematch scraper — browser-free JSON, multi-sport.

Transport + parse for the third book on the dashboard (alongside CrystalBet and
Pinnacle). Full protocol writeup: ../../docs/liderbet.md. In short, two GETs:

  GET /services/pre/m1/api/sport/menu?lang=en      → sport→country→tournament tree
  GET /services/pre/m4/api/sport/matchData?tourIds=…&lang=en
                                                   → matches + odds, inline

No session, no ViewState, no auth — plain idempotent GETs over curl_cffi.
curl_cffi's Session is sync; callers get async wrappers (asyncio.to_thread),
mirroring cb_http.py.

Scope (list view, FT only): moneyline (1X2 for soccer, 2-way for basketball /
tennis), total (O/U, every line the list ships), spread (Asian handicap). That
is exactly the market set the matcher joins against Pinnacle. Sub-period markets
(1st half / quarter / set) are deliberately skipped here — they can follow the
same pattern later.

Every Odds row carries `sr_match_id` (the bare SportRadar id from
meta.matchProvider.matchId, e.g. "71792526") when present, so the matcher can
join Lider↔Betlive EXACTLY for cross-book arbs. Names are emitted raw; the
matcher's normalizer handles Lider's occasional Cyrillic competitor names via
transliteration (Pinnacle exposes no SportRadar id, so that leg is name-only).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from src import horizon
from src.models import Odds
from src.normalize import is_simulated_league, transliterate

log = logging.getLogger(__name__)

BASE = "https://sports.lider-bet.com/services"
MENU_URL = f"{BASE}/pre/m1/api/sport/menu"
MATCHDATA_URL = f"{BASE}/pre/m4/api/sport/matchData"
IMPERSONATE = "chrome124"

# Lider sport "section" id per dashboard sport name (verified 2026-06-15).
# s:3 = Ice Hockey, read off the book's own menu (85 games, 17 tournaments,
# 2026-08-27) — fourth-largest section after soccer, table tennis and tennis.
SECTION = {"soccer": "s:16", "basketball": "s:2", "tennis": "s:13",
           "icehockey": "s:3",
           # American football (2026-08-12): 85 matches over CFL / Regular
           # Season / Pre-Season. Its three market names — 'Winner (OT)',
           # 'Handicap (OT)', 'Total (OT)' — are already in _classify_market's
           # incl-overtime aliases, so no classifier change was needed.
           "americanfootball": "s:34"}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en",
    "Referer": "https://sports.lider-bet.com/",
    "Origin": "https://sports.lider-bet.com",
}

_TOURS_PER_CALL = 40          # batch tournaments into one matchData GET
_MATCHES_PER_DETAIL = 20      # batch matches into one matchData/details GET
_HTTP_TIMEOUT = 30.0

# ── Detail tier (2026-08-12) ─────────────────────────────────────────────────
# matchData carries the FT headline markets only. `matchData/details` carries
# ~270 market names per match, including every input the consistency engine's
# most productive check needs — and soft_scan has been calling that endpoint
# for a filtered 14 % of the board all along, so the transport is proven.
#
# It is NOT free, which is why it is horizon-gated like Crocobet's and
# Setanta's DETAIL_HOURS. Measured 2026-08-12 on the soccer board (1291
# matches): one 20-match batch is 0.48 s / 3.2 MB, so
#     within 12 h :  92 matches ->  5 calls, ~2.4 s, ~16 MB
#     within 24 h : 162 matches ->  9 calls, ~4.3 s, ~28 MB
#     whole board : 1086        -> 55 calls, ~26 s, ~172 MB   <- not worth it
# 24 h keeps Lider among the cheapest books (its whole cycle is ~10 s today)
# while covering everything close enough to bet.
DETAIL_HOURS = float(os.environ.get("LIDERBET_DETAIL_HOURS", "24"))

# typeId -> (market_type, period, n_way, team_side). This is an ALLOWLIST and
# it is the ONLY thing used to read a details payload.
#
# The name-based `_classify_market` below still handles the list tier, where it
# has been correct for months against a handful of market names. It must not be
# let anywhere near the detail tier, and the reason is concrete: that payload
# carries ~270 market names per match, and TWO of them are called "Handicap" —
#     mt:16:501   'Handicap ①'    outcomes 1 / 2       the 2-way Asian line
#     mt:16:1079  'Handicap  ①'   outcomes 1 / X / 2   a 3-way European one
# differing only by a double space, which whitespace-collapsing erases. Reading
# the 3-way as a 2-way silently drops the draw and invents a spread that was
# never priced. Measured before this allowlist existed: 155 bogus rows against
# 149 real ones, and the resulting FT spread disagreed with CrystalBet by up to
# 17.8pp where every correctly-mapped market agreed within 1.6pp.
# `mt:16:618` is the same trap on the halves ("2nd Half-3way", but the
# double-chance variant 1X / X2 / 12).
#
# Every entry below is PRICE-VERIFIED against CrystalBet on live matched
# fixtures (2026-08-12), median absolute devigged gap in brackets. Sport-scoped:
# the middle number is Lider's section id, so these are soccer (mt:16:*) only;
# basketball / tennis / am. football need their own census first.
_DETAIL_TYPES: dict[str, tuple] = {
    # ── full time (also present in the list tier; repeated here because a
    #    details payload REPLACES a match's list rows, so the allowlist has to
    #    cover everything we still want after the swap)
    "mt:16:500":  ("moneyline", "FT", 3, None),      # [1.22pp]
    "mt:16:502":  ("total", "FT", 2, None),          # [0.39pp]
    # ── the 9-way HT/FT grid: the input htft_combo needs, and the only market
    #    here that no other soft book supplies
    "mt:16:573":  ("htft", "FT", 9, None),
    # ── regulation 1X2 legs for the halves (htft_combo settles on regulation)
    "mt:16:602":  ("moneyline", "H1", 3, None),      # [0.73pp]
    "mt:16:619":  ("moneyline", "H2", 3, None),      # mirror of 602; no
                                                     # reference book prices
                                                     # soccer H2, so unchecked
    # ── half totals. Verified twice over: 0.36pp against CB at H1, and the
    #    engine's own total_additivity puts H1+H2 within 0.24 POINTS of FT
    #    across 876 events — which a mislabelled period could not do.
    "mt:16:600":  ("total", "H1", 2, None),          # [0.36pp]
    "mt:16:622":  ("total", "H2", 2, None),
    # ── DELIBERATELY ABSENT ─────────────────────────────────────────────────
    # mt:16:598 / mt:16:621  half handicaps — 6.16pp MEDIAN against CrystalBet
    #   (max 17.2) where every correct mapping landed under 1.6pp. A shifted
    #   median is the signature of a wrong market, not of noise.
    # mt:16:501  FT 2-way handicap — 1.17pp median against Pinnacle over 413
    #   rungs, but p90 18.98 and max 50.38. The line convention is NOT the
    #   problem: pairing as-is gives a 2.59pp median while negating the line
    #   gives 32.49 and swapping the sides 28.30, so as-is is right and a
    #   SUBSET of rungs is wrong. Until that subset is identified this would be
    #   a phantom-arb generator on a bettable market, which is worse than not
    #   having it.
    # mt:16:504 / mt:16:505  team totals — same shape, max 51.20pp.
    # None of the three is needed for the consistency checks; they are an arbs
    # -grid enrichment and can wait for a proper census.

    # ══ ICE HOCKEY (mt:3:*) — census 2026-08-27, 65 matches / 20 market types ══
    # These are NOT optional decoration on top of the name classifier: without
    # them hockey is actively wrong. Lider names its 3-way regulation result
    # "Result", and `_classify_market` maps the bare word "result" to a 2-WAY
    # moneyline. That row would carry the regulation home/away prices with the
    # tie silently dropped — two prices summing to ~80 % — and the
    # selection-shape guard in edge.py cannot catch it, because the shape is
    # right and only the meaning is wrong. It would then pair against
    # Pinnacle's period-0 incl-OT moneyline and show the tie probability as
    # edge on every event on the board.
    #
    # Hence `_names_classified()` below returns False for hockey: this table is
    # the whole mapping, and an unlisted mt:3:* market is skipped rather than
    # guessed at by name.
    #
    # Everything here is REGULATION. Lider prices no incl-OT hockey market at
    # all in the list tier — no "Winner (OT)", no "Total (OT)" — which is why
    # the book contributes nothing to the FT (incl-OT) comparison and shows up
    # only against Pinnacle's period 6.
    "mt:3:500":  ("moneyline", "REG", 3, None),   # 'Result' — 3 outcomes
    "mt:3:502":  ("total", "REG", 2, None),       # 'Total'
    "mt:3:607":  ("spread", "REG", 2, None),      # 'Handicap'
    "mt:3:697":  ("total", "P1", 2, None),        # '1st Period - Total'
    "mt:3:741":  ("total", "P2", 2, None),        # '2nd Period - Total'
    "mt:3:792":  ("total", "P3", 2, None),        # '3rd Period - Total'
    "mt:3:694":  ("spread", "P1", 2, None),       # '1st Period - Hadicap' (sic)
    "mt:3:734":  ("spread", "P2", 2, None),       # '2nd Period - Hadicap' (sic)
    "mt:3:789":  ("spread", "P3", 2, None),       # '3rd Period - Handicap'
    # ── deliberately absent ─────────────────────────────────────────────────
    # mt:3:503 Double Chance, mt:3:538 Both Teams To Score, mt:3:662/663/664
    #   per-period BTTS — no representation in Odds.
    # mt:3:699/700, 743/744, 793/794 — SIX ids sharing three names ("1st
    #   Period", "2nd Period", "3rd Period"), two per period, both 3-way. One
    #   of each pair is presumably the period result and the other a double
    #   chance, but the feed does not say which and the prices alone did not
    #   separate them. Pinnacle's period moneyline is 2-way regardless, so
    #   there is nothing to pair a 3-way against; left out until named.
}

_HTFT_CELLS = ("1/1", "1/X", "1/2", "X/1", "X/X", "X/2", "2/1", "2/X", "2/2")

# Strip the circled/superscript variant glyphs Lider appends to market names
# ("Handicap ①", "1st Half Total ②") before classifying.
_DECOR = re.compile(r"[①-⓿⁰-₟⅐-↏]")
_SUBPERIOD = ("half", "quarter", " set", "{")  # → not a full-match market


# ── corners: a second board, on the permissive/anomaly path ────────────────
# Lider ships a fuller corner board than CrystalBet does — Corner Matchbet on
# 319 events, Corners Handicap on 322, Total corners on 344, plus first-half
# variants on 335-344. Same shape as the goals board: a 3-way result and a
# handicap ladder.
#
# These MUST carry submarket="corners". The consistency engine groups on
# (event, submarket), so an unlabelled corners row would land in the goals
# bucket and every check would compare a goals price against a corner count.
#
# Kept out of _classify_market deliberately: that function feeds the Pinnacle
# matching path too, and Pinnacle prices none of this.
_CORNER_MARKETS: dict[str, tuple[str, str, int]] = {
    "corner matchbet":            ("moneyline", "FT", 3),
    "corners handicap":           ("spread",    "FT", 2),
    "total corners":              ("total",     "FT", 2),
    "1st half - corner matchbet": ("moneyline", "H1", 3),
    "1st half - corner handicap": ("spread",    "H1", 2),
    "1st half - total corners":   ("total",     "H1", 2),
}


def _classify_corner_market(raw_name: str) -> tuple[str, str, int] | None:
    """Corner markets → (market_type, period, n_way). None if not one."""
    name = _DECOR.sub("", raw_name or "").strip().lower()
    name = re.sub(r"\s+", " ", name)
    return _CORNER_MARKETS.get(name)


def _classify_market(raw_name: str) -> tuple[str, int] | None:
    """Map a Lider marketType name → (market_type, n_way) for FT markets only.

    Returns None for sub-period markets (1st half / quarter / set) and anything
    that isn't a plain FT moneyline / total / handicap.
    """
    name = _DECOR.sub("", raw_name or "").strip().lower()
    name = re.sub(r"\s+", " ", name)
    if any(tok in name for tok in _SUBPERIOD):
        return None
    if name == "full time result":
        return ("moneyline", 3)
    if name in ("winner", "winner (ot)", "match winner", "money line", "result"):
        return ("moneyline", 2)
    if name in ("total", "total (ot)", "total games"):
        return ("total", 2)
    if name in ("handicap", "handicap (ot)", "asian handicap"):
        return ("spread", 2)
    return None


def _menu_nodes(menu: dict) -> list[dict]:
    return [it for v in menu.values() if isinstance(v, list)
            for it in v if isinstance(it, dict)]


def _to_float(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _spec_line(specifier: dict | None) -> float | None:
    """Pull the numeric line out of a market/outcome specifier."""
    if not isinstance(specifier, dict):
        return None
    for key in ("total", "handicap", "hcp", "special"):
        if key in specifier:
            v = _to_float(specifier[key])
            if v is not None:
                return v
    return None


def _odds(value) -> float | None:
    """Decimal odds, or None if missing/suspended (<= 1.0)."""
    v = _to_float(value)
    return v if v is not None and v > 1.0 else None


def _names_classified(sport_name: str) -> bool:
    """May this sport fall back to the NAME classifier for unlisted markets?

    No for ice hockey. `_classify_market` reads a bare "Result" as a 2-way
    moneyline, which is exactly what Lider calls hockey's 3-way regulation
    result — a mapping that is wrong in the dangerous direction (see the
    mt:3:* block in _DETAIL_TYPES). With names off, the typeId allowlist is
    the entire hockey mapping and an unlisted market is skipped.
    """
    return sport_name != "icehockey"


def _DETAIL_TYPES_FOR(sport_name: str) -> bool:
    """Is the detail tier mapped for this sport?

    Only soccer today. The typeIds are section-scoped (mt:16:* is soccer), so
    the other sports need their own census before their detail payloads can be
    trusted — and pulling detail we cannot read would be pure cost.
    """
    return sport_name == "soccer"


def _start_time(m: dict):
    st = m.get("startTime")
    if not st:
        return None
    try:
        return datetime.fromisoformat(st).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _parse_match(
    m: dict, ancestors: dict, market_types: dict, sport_name: str, fetched_at: datetime,
    *, detail: bool = False,
) -> list[Odds]:
    """Parse one match.

    `detail=True` switches to the typeId ALLOWLIST and disables the name-based
    classifier entirely — see the note on _DETAIL_TYPES for why a details
    payload must never be name-classified.
    """
    home = (ancestors.get(m.get("homeId"), {}) or {}).get("name") or ""
    away = (ancestors.get(m.get("awayId"), {}) or {}).get("name") or ""
    # Romanize for display — Lider ships some names in Russian/Georgian even at
    # lang=en (no English exists in the feed). transliterate() is a no-op on the
    # Latin majority. The matcher re-normalizes anyway, so this is display-safe.
    home, away = transliterate(home.strip()), transliterate(away.strip())
    if not home or not away:
        return []
    league = transliterate((ancestors.get(m.get("tourId"), {}) or {}).get("name") or "") or None

    start_time = None
    st = m.get("startTime")
    if st:
        try:
            start_time = datetime.fromisoformat(st).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    # Global data horizon (src/horizon.py). matchData returns a whole batch of
    # tournaments in one GET, so there is no request to save — but a far match
    # can carry hundreds of ladder rungs, and skipping it here keeps them out
    # of the parse, the matcher and the tick writer.
    if not horizon.keeps(start_time):
        return []

    sr = (m.get("meta", {}) or {}).get("matchProvider", {}) or {}
    sr_id = sr.get("matchId") or ""
    sr_match_id = sr_id[len("sr:match:"):] if sr_id.startswith("sr:match:") else None

    event_id = m.get("id")
    out: list[Odds] = []

    for mk in (m.get("markets") or {}).values():
        type_id = mk.get("typeId")
        tp = market_types.get(type_id, {})
        # Reset per market. Only the corner branch sets it, and without this a
        # corners row would leave "corners" behind for the next market in the
        # loop — mislabelling a goals market as a corner one, which the
        # consistency engine would then check against the real corner board.
        submarket: str | None = None
        # Detail-tier markets are identified by their STABLE typeId; the FT
        # headline set still goes through the name classifier. typeId wins when
        # both would match, because two markets can share a name ("Handicap" is
        # both the 2-way Asian line and a 3-way European one) and only the id
        # tells them apart.
        hit = _DETAIL_TYPES.get(type_id)
        if hit is not None:
            market_type, period, n_way, team_side = hit
        elif detail or not _names_classified(sport_name):
            continue            # allowlist-only (detail payload, and hockey)
        elif _classify_corner_market(tp.get("name", "")) is not None:
            market_type, period, n_way = _classify_corner_market(tp.get("name", ""))
            team_side, submarket = None, "corners"
        else:
            cls = _classify_market(tp.get("name", ""))
            if cls is None:
                continue
            market_type, n_way = cls
            period, team_side = "FT", None

        # outcomeType id → label ("1"/"X"/"2"/"Over"/"Under"/"1/1"…). The feed
        # ships its own dictionary per market type, so the labels are
        # authoritative even where the market NAME is ambiguous.
        label = {ot["id"]: (ot.get("name") or "").strip()
                 for ot in tp.get("outcomeTypes", [])}
        priced = {label.get(otid, ""): o for otid, o in (mk.get("outcomes") or {}).items()}

        if market_type == "moneyline":
            sel = {"home": _odds((priced.get("1") or {}).get("value")),
                   "away": _odds((priced.get("2") or {}).get("value"))}
            if n_way == 3:
                sel["draw"] = _odds((priced.get("X") or {}).get("value"))
            row = _build(sport_name, home, away, "moneyline", period, sel,
                         None, league, start_time, event_id, sr_match_id, fetched_at, submarket=submarket)
            if row:
                out.append(row)

        elif market_type in ("total", "team_total"):
            line = _spec_line(mk.get("specifier"))
            sel = {"over": _odds((priced.get("Over") or {}).get("value")),
                   "under": _odds((priced.get("Under") or {}).get("value"))}
            row = _build(sport_name, home, away, market_type, period, sel,
                         line, league, start_time, event_id, sr_match_id, fetched_at,
                         team_side=team_side, submarket=submarket)
            if row:
                out.append(row)

        elif market_type == "spread":
            home_oc = priced.get("1") or {}
            line = _spec_line(home_oc.get("specifier")) or _spec_line(mk.get("specifier"))
            sel = {"home": _odds(home_oc.get("value")),
                   "away": _odds((priced.get("2") or {}).get("value"))}
            row = _build(sport_name, home, away, "spread", period, sel,
                         line, league, start_time, event_id, sr_match_id, fetched_at, submarket=submarket)
            if row:
                out.append(row)

        elif market_type == "htft":
            # All NINE cells or nothing: the consistency check reasons about the
            # grid as a distribution, and a partial grid would look like a book
            # that had priced only some outcomes rather than one we half-read.
            sel = {c: _odds((priced.get(c) or {}).get("value")) for c in _HTFT_CELLS}
            if any(v is None for v in sel.values()):
                continue
            row = _build(sport_name, home, away, "htft", "FT", sel,
                         None, league, start_time, event_id, sr_match_id, fetched_at, submarket=submarket)
            if row:
                out.append(row)

    return out


def _build(sport_name, home, away, market_type, period, selections, line,
           league, start_time, event_id, sr_match_id, fetched_at,
           team_side=None, submarket=None) -> Odds | None:
    if any(v is None for v in selections.values()):
        return None
    if market_type in ("total", "spread", "team_total") and line is None:
        return None
    try:
        return Odds(
            source="liderbet", sport=sport_name, home=home, away=away,
            market_type=market_type, period=period, selections=selections,
            fetched_at=fetched_at, line=line, start_time=start_time,
            league=league, raw_event_id=str(event_id) if event_id else None,
            sr_match_id=sr_match_id, team_side=team_side,
            submarket=submarket,
        )
    except ValueError as exc:               # odds <= 1.0 slipped through
        log.debug("liderbet Odds rejected: %s", exc)
        return None


def _fetch_sport_sync(sport_name: str) -> list[Odds]:
    from curl_cffi.requests import Session

    section = SECTION.get(sport_name)
    if section is None:
        log.warning("liderbet: unknown sport %r", sport_name)
        return []

    fetched_at = datetime.now(tz=timezone.utc)
    s = Session(impersonate=IMPERSONATE)

    menu = s.get(f"{MENU_URL}?lang=en&marketFilter=true",
                 headers=HEADERS, timeout=_HTTP_TIMEOUT).json()["menu"]
    # The menu is a flat adjacency map {parentNodeId: [childNodes]}. Simulated
    # football hides under real-looking TOURNAMENT names ("World Cup",
    # "Champions League") whose PARENT category is the tell — e.g. the sim
    # "World Cup" (t:69953) sits under c:17065 "Simulated Reality League",
    # while the REAL c:22319 "World Cup 2026" is separate. So we drop a
    # tournament when its own name OR its parent category name looks simulated
    # (fixes the England-v-Argentina phantom edges, 2026-07-14).
    name_by_id = {n["id"]: (n.get("name") or "")
                  for v in menu.values() if isinstance(v, list)
                  for n in v if isinstance(n, dict) and n.get("id")}
    parent_of: dict[str, str] = {}
    for parent_id, children in menu.items():
        if not isinstance(children, list):
            continue
        for n in children:
            if isinstance(n, dict) and n.get("id"):
                parent_of[n["id"]] = parent_id
    tour_ids = [
        n["id"] for n in _menu_nodes(menu)
        if n.get("id", "").startswith("t:")
        and n.get("sectionId") == section and n.get("cnt", 0) > 0
        and not is_simulated_league(n.get("name"))
        and not is_simulated_league(name_by_id.get(parent_of.get(n["id"], "")))
    ]
    if not tour_ids:
        log.info("liderbet %s: no tournaments with games", sport_name)
        return []

    rows: list[Odds] = []
    ancestors: dict = {}
    near: list[str] = []            # matches inside the detail horizon
    detail_cut = fetched_at + timedelta(hours=DETAIL_HOURS) if DETAIL_HOURS > 0 else None
    for i in range(0, len(tour_ids), _TOURS_PER_CALL):
        chunk = ",".join(tour_ids[i:i + _TOURS_PER_CALL])
        try:
            data = s.get(f"{MATCHDATA_URL}?tourIds={chunk}&lang=en&marketFilter=true",
                         headers=HEADERS, timeout=_HTTP_TIMEOUT).json()["data"]
        except Exception as exc:
            log.warning("liderbet %s: matchData chunk failed: %s", sport_name, exc)
            continue
        anc, mts = data.get("ancestors", {}), data.get("marketTypes", {})
        ancestors.update(anc)
        for m in (data.get("matches") or {}).values():
            rows.extend(_parse_match(m, anc, mts, sport_name, fetched_at))
            if detail_cut is not None and _DETAIL_TYPES_FOR(sport_name):
                st = _start_time(m)
                if st is not None and st <= detail_cut and m.get("id"):
                    near.append(m["id"])

    # ── detail tier ──────────────────────────────────────────────────────────
    # matchData gives the FT headline markets. `matchData/details` gives the
    # half markets and the 9-way HT/FT grid — the inputs the consistency engine
    # needs, and which no other soft book supplies. Horizon-gated: see
    # DETAIL_HOURS for the measured cost.
    n_detail = 0
    for i in range(0, len(near), _MATCHES_PER_DETAIL):
        batch = near[i:i + _MATCHES_PER_DETAIL]
        ids = ",".join(b if str(b).startswith("pr:m:") else f"pr:m:{b}" for b in batch)
        try:
            data = s.get(f"{MATCHDATA_URL}/details?matchIds={ids}&lang=en",
                         headers=HEADERS, timeout=_HTTP_TIMEOUT * 2).json()["data"]
        except Exception as exc:
            log.warning("liderbet %s: details batch failed: %s", sport_name, exc)
            continue
        anc = {**ancestors, **(data.get("ancestors") or {})}
        mts = data.get("marketTypes", {})
        matches = data.get("matches") or {}
        for mid in batch:
            m = matches.get(mid) or matches.get(str(mid))
            if not m:
                continue
            # The details payload REPEATS the FT markets, so parse it whole and
            # drop this match's list-tier rows rather than merging: a market the
            # book has since pulled must not survive as a leftover, and the two
            # tiers were fetched seconds apart so the detail one is the truth.
            fresh = _parse_match(m, anc, mts, sport_name, fetched_at, detail=True)
            if not fresh:
                continue
            rows = [o for o in rows if o.raw_event_id != str(mid)]
            rows.extend(fresh)
            n_detail += 1

    log.info("liderbet %s: %d Odds rows from %d tournaments "
             "(%d matches with full detail, %.0fh horizon)",
             sport_name, len(rows), len(tour_ids), n_detail, DETAIL_HOURS)
    return rows


async def fetch_liderbet(sport_name: str) -> list[Odds]:
    """Async wrapper — runs the sync curl_cffi fetch in a worker thread."""
    return await asyncio.to_thread(_fetch_sport_sync, sport_name)


async def fetch_liderbet_soccer() -> list[Odds]:
    return await fetch_liderbet("soccer")


async def fetch_liderbet_basketball() -> list[Odds]:
    return await fetch_liderbet("basketball")


async def fetch_liderbet_tennis() -> list[Odds]:
    return await fetch_liderbet("tennis")


async def fetch_liderbet_americanfootball() -> list[Odds]:
    return await fetch_liderbet("americanfootball")


async def fetch_liderbet_icehockey() -> list[Odds]:
    return await fetch_liderbet("icehockey")


if __name__ == "__main__":   # smoke: python -m src.scrapers.liderbet [sport]
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sport = sys.argv[1] if len(sys.argv) > 1 else "soccer"
    odds = asyncio.run(fetch_liderbet(sport))
    print(f"\n{sport}: {len(odds)} Odds rows")
    by_mt: dict[str, int] = {}
    with_sr = 0
    for o in odds:
        by_mt[o.market_type] = by_mt.get(o.market_type, 0) + 1
        with_sr += o.sr_match_id is not None
    print("by market_type:", by_mt)
    print(f"with sr_match_id: {with_sr}/{len(odds)}")
    for o in odds[:6]:
        print(f"  {o.home} vs {o.away} | {o.market_type} {o.period} "
              f"line={o.line} {o.selections} sr={o.sr_match_id} @ {o.start_time}")
