"""
Betlive favourite-flip live watch — browser-free, two-speed.

Detecting the incl-OT vs regulation moneyline inconsistency (see
src/betlive_anomalies.py) needs the FULL ladder per event, which the cheap
getLeagueEvents list feed does not carry (it prices only the incl-OT 2-way; the
3-way is a null placeholder). Pulling every full ladder on every poll would be
heavy (~26 MB / board). So this splits the work:

  discover()  — slow (~10 min). getPrematchEvent every real H2H event, run the
                detector, AND record the two moneyline markets' outcomeIds.
  refresh()   — fast (~seconds). ONE /api/outcome/refreshOdds POST re-prices
                just those moneyline outcomeIds for the whole board (~50 KB),
                and the OT-fold check is recomputed from the fresh prices.

That keeps the per-poll cost tiny while still catching a flip within seconds of
it appearing — which matters because these errors are short-lived (the VBA game
self-corrected inside ~15-30 min). Both produce the same consistency-flag dicts
the dashboard's Anomalies tab already renders.

All network is curl_cffi Chrome impersonation (no browser, no session/token);
run from a Georgian IP. Sync functions; the app wraps them in asyncio.to_thread.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from src.betlive_anomalies import anomalies_for_event, pick_ml_markets

log = logging.getLogger(__name__)

BASE = "https://sportnew.betlive.com"
COUNTRIES_URL = f"{BASE}/api/category/getCountryCategories"
LEAGUE_EVENTS_URL = f"{BASE}/api/event/getLeagueEvents"
PREMATCH_EVENT_URL = f"{BASE}/api/event/getPrematchEvent"
REFRESH_ODDS_URL = f"{BASE}/api/outcome/refreshOdds"
IMPERSONATE = "chrome124"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*", "Accept-Language": "en",
    "Referer": "https://sport.betlive.com/", "Origin": "https://sport.betlive.com",
}
_TIMEOUT = 30.0
_LEAGUES_PER_CALL = 25
_EVENTS_PER_LEAGUE = 80

# Sports that play overtime off a drawable regulation result (the only ones the
# favourite-flip check fires on). 6=basketball, 18=ice hockey on betlive.
DEFAULT_SPORT_IDS = (6,)
SPORT_NAME = {6: "basketball", 18: "ice hockey", 133: "e-basketball", 135: "e-hockey"}


def session():
    from curl_cffi.requests import Session
    return Session(impersonate=IMPERSONATE)


def _leaf_leagues(nodes: list) -> list:
    out = []
    for n in nodes:
        kids = n.get("children") or []
        if kids:
            out += _leaf_leagues(kids)
        elif n.get("eventCount", 0) > 0:
            out.append(n)
    return out


def _has_2way(ev: dict) -> bool:
    """List-feed proxy for 'real H2H game worth extending' — has a priced 2-way
    (1/2) market. Skips outright/winner shelves (their markets are multi-runner),
    so the heavy getPrematchEvent sweep only touches games we could flag."""
    for mk in ev.get("markets") or []:
        labels = {(o.get("name") or "").strip() for o in (mk.get("outcomes") or [])
                  if o.get("odd")}
        if labels == {"1", "2"}:
            return True
    return False


def _ml_outcome_ids(raw_market: dict) -> dict[str, int]:
    """label -> outcomeId for a market's bettable outcomes."""
    out: dict[str, int] = {}
    for o in raw_market.get("outcomes") or []:
        if o.get("id") is not None:
            out[(o.get("name") or "").strip()] = o["id"]
    return out


def discover(sport_ids=DEFAULT_SPORT_IDS, *, max_events: int | None = None):
    """Full sweep. Returns (watch, anomalies, energy):

      watch     : {event_id: {meta, reg, ot}} — reg/ot are the raw moneyline
                  market dicts (with outcome ids), ready for refresh().
      anomalies : list[BetliveAnomaly] detected from the full ladders.
      energy    : byte/time/call tallies for the run.
    """
    s = session()
    energy = {"list_bytes": 0, "list_calls": 0, "ext_bytes": 0, "ext_calls": 0,
              "ext_time": 0.0}

    candidates: list[int] = []
    for sid in sport_ids:
        r = s.get(f"{COUNTRIES_URL}?sportId={sid}", headers=HEADERS, timeout=_TIMEOUT)
        energy["list_calls"] += 1
        energy["list_bytes"] += len(r.content)
        ids = [str(l["id"]) for l in _leaf_leagues(r.json())]
        for i in range(0, len(ids), _LEAGUES_PER_CALL):
            chunk = ",".join(ids[i:i + _LEAGUES_PER_CALL])
            lr = s.get(f"{LEAGUE_EVENTS_URL}?leagueIds={chunk}&page=0&take={_EVENTS_PER_LEAGUE}",
                       headers=HEADERS, timeout=_TIMEOUT)
            energy["list_calls"] += 1
            energy["list_bytes"] += len(lr.content)
            for w in lr.json() or []:
                for ev in w.get("events") or []:
                    if ev.get("id") and _has_2way(ev):
                        candidates.append(ev["id"])
    if max_events:
        candidates = candidates[:max_events]

    watch: dict[int, dict] = {}
    anomalies = []
    from src.betlive_anomalies import find_betlive_anomalies  # local import: detector
    full_events = []
    for eid in candidates:
        t = time.time()
        try:
            r = s.get(f"{PREMATCH_EVENT_URL}?id={eid}", headers=HEADERS, timeout=_TIMEOUT)
        except Exception as exc:
            log.debug("betlive discover: getPrematchEvent %s failed: %s", eid, exc)
            continue
        energy["ext_calls"] += 1
        energy["ext_bytes"] += len(r.content)
        energy["ext_time"] += time.time() - t
        body = r.json()
        fe = body[0] if isinstance(body, list) and body else body
        if not isinstance(fe, dict):
            continue
        full_events.append(fe)
        reg, ot = pick_ml_markets(fe)
        if reg and ot:
            watch[eid] = {
                "meta": {k: fe.get(k) for k in
                         ("id", "homeTeamName", "awayTeamName", "leagueName",
                          "sportName", "startDate")},
                "reg": reg, "ot": ot,
            }
    anomalies = find_betlive_anomalies(full_events)
    log.info("betlive discover: %d candidates, %d watchable, %d anomalies "
             "(%.1f MB / %.1fs extended)", len(candidates), len(watch),
             len(anomalies), energy["ext_bytes"] / 1e6, energy["ext_time"])
    return watch, anomalies, energy


def _refresh_payload(watch: dict) -> list[dict]:
    body = []
    for eid, w in watch.items():
        for mk in (w["reg"], w["ot"]):
            for oid in _ml_outcome_ids(mk).values():
                body.append({"outcomeId": oid, "eventId": eid})
    return body


def _reprice_market(raw_mk: dict, fresh: dict) -> dict:
    ocs = []
    for o in raw_mk.get("outcomes") or []:
        no = dict(o)
        f = fresh.get(o.get("id"))
        if f is not None:
            no["odd"] = f["odd"]
            no["statusId"] = 2 if f.get("suspend") else 1
            no["marketStatusId"] = 1
        ocs.append(no)
    return {"outcomes": ocs}


def refresh(s, watch: dict):
    """Cheap re-price of every watched event's moneylines in one refreshOdds
    POST, then recompute the OT-fold check. Returns (anomalies, bytes)."""
    body = _refresh_payload(watch)
    if not body:
        return [], 0
    r = s.post(REFRESH_ODDS_URL, headers={**HEADERS, "Content-Type": "application/json"},
               json=body, timeout=_TIMEOUT)
    nbytes = len(r.content)
    fresh = {row["outcomeId"]: row for row in (r.json() or [])
             if row.get("outcomeId") is not None}

    anomalies = []
    for w in watch.values():
        ev = {**w["meta"], "markets": [_reprice_market(w["reg"], fresh),
                                       _reprice_market(w["ot"], fresh)]}
        anomalies.extend(anomalies_for_event(ev))
    anomalies.sort(key=lambda a: a.gap_pp, reverse=True)
    return anomalies, nbytes


def anomaly_to_flag(a) -> dict:
    """One BetliveAnomaly -> the consistency-flag dict the Anomalies tab renders."""
    reg_p = a.fair_reg[0 if a.side == "home" else 2] * 100
    ot_p = a.fair_ot[0 if a.side == "home" else 1] * 100
    detail = (f"{a.side} {reg_p:.0f}%→{ot_p:.0f}% incl-OT (−{a.gap_pp:.1f}pp): "
              f"{a.ot_market} 1@{a.ot_odds[0]:g} 2@{a.ot_odds[1]:g} vs "
              f"{a.reg_market} 1@{a.reg_odds[0]:g} X@{a.reg_odds[1]:g} 2@{a.reg_odds[2]:g}")
    return {
        "sport": (a.sport or "").lower() or "basketball",
        "league": a.league, "match_label": a.match_label,
        "home": a.home, "away": a.away,
        "cb_event_id": None, "betlive_event_id": a.event_id, "book": "betlive",
        "start_time": a.start_time.isoformat() if a.start_time else None,
        "kind": "betlive_flip" if a.favourite_flip else "betlive_ot_fold",
        "periods": "incl-OT vs FT", "detail": detail,
        "severity": round(a.gap_pp, 1), "outcome": a.side,
    }


def flags_with_first_seen(anomalies, prev_flags: list[dict],
                          now_iso: str | None = None) -> list[dict]:
    """Build flag dicts, carrying first_seen across polls (identity = book +
    event + side) so the tab shows how long a flip has been live."""
    now_iso = now_iso or datetime.now(tz=timezone.utc).isoformat()
    prev = {(f.get("betlive_event_id"), f.get("outcome")): f.get("first_seen")
            for f in prev_flags}
    out = []
    for a in anomalies:
        f = anomaly_to_flag(a)
        f["first_seen"] = prev.get((f["betlive_event_id"], f["outcome"])) or now_iso
        out.append(f)
    return out
