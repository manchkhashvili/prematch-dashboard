"""
Soft-book prematch anomaly sweep — soccer HT/FT + basketball favourite
disagreement, across CrystalBet, Betlive and Lider-Bet. Slow, filtered, and
independent of the CB basketball ladder scan (which stays untouched).

Per book: cheap list → keep only heavy-favourite / non-top games (soft_scan
gate) → open the FULL ladder for just those → extract the moneyline / HT-FT /
1st-half markets by fuzzy name → run the detectors:
  soccer     — htft_favourite: HT/FT combo >= 1.2× its first-half leg.
  basketball — basketball_fav:  favourite flips across 2-way / 3-way / HT ML.

Each scanner returns plain flag dicts shaped like the Anomalies-tab consistency
rows, tagged with `book`. Fuzzy matchers keep one code path working across the
three books' different market names; the companion probe confirms hit-rates.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from src import htft_favourite as hf
from src import basketball_fav
from src.basketball_fav import fav_disagreement

log = logging.getLogger(__name__)


# ── fuzzy market-name matchers (shared across books) ──────────────────────────
def _norm(s) -> str:
    return " ".join((s or "").lower().split())


_COMBO = ("exact", "correct", " or ", "both", "goals", "score", "/1st",
          "- 1st", "handicap", "total", "team")


def is_htft_market(name: str) -> bool:
    n = _norm(name)
    if any(t in n for t in _COMBO):
        return False
    return (("half" in n and "time" in n and "full" in n)
            or "halftime/fulltime" in n or n.startswith("ht/ft"))


def is_h1_result_market(name: str) -> bool:
    n = _norm(name)
    if any(t in n for t in _COMBO) or "full" in n or "/" in n:
        return False
    is_h1 = ("1st half" in n or "first half" in n or "half time" in n
             or "halftime" in n or n.endswith("halftime") or "1st period" in n)
    return is_h1 and ("result" in n or "1x2" in n or "winner" in n or n in
                      ("12 halftime", "1x2 halftime"))


# ── flag shaping (Anomalies-tab consistency row) ──────────────────────────────
def _flag(book, sport, kind, home, away, league, event_id, detail, severity,
          outcome=None, start_time=None):
    return {
        "book": book, "sport": sport, "kind": kind,
        "match_label": f"{home} — {away}", "home": home, "away": away,
        "league": league, "cb_event_id": None, "book_event_id": str(event_id),
        "start_time": start_time.isoformat() if start_time else None,
        "periods": "HT/FT" if sport == "soccer" else "2W/3W/HT",
        "detail": f"[{book}] {detail}", "severity": round(severity, 1),
        "outcome": outcome,
    }


def _soccer_htft_flag(book, home, away, league, eid, ml, h1, htft, start=None):
    """ml=(h,a) FT moneyline; h1=(h,a) 1st-half result (may be None,None); htft
    combo dict. Try HT/FT vs the standalone first-half leg; else vs the FT ML."""
    fav = hf.favourite_side(ml[0], ml[1])
    if fav is None or not htft:
        return None
    c11, c22 = htft.get("1/1"), htft.get("2/2")
    vs = "1st-half"
    res = hf.htft_flag(fav, h1[0], h1[1], c11, c22) if (h1 and h1[0]) else None
    if not res:                                   # no first-half (or clean) → vs the ML
        res = hf.htft_vs_ml_flag(fav, ml[0], ml[1], c11, c22)
        vs = "ML"
    if not res:
        return None
    side, leg, combo, ratio = res
    return _flag(book, "soccer", "soccer_htft", home, away, league, eid,
                 f"{side} HT/FT {combo:g} vs {vs} {leg:g} = {ratio:g}× "
                 f"(combo priced generously for a heavy favourite)",
                 (ratio - 1) * 100, outcome=("1/1" if side == "home" else "2/2"),
                 start_time=start)


def _basketball_fav_flag(book, home, away, league, eid, ml2, ml3, ht, htft=None, start=None):
    res = fav_disagreement(ml2=ml2, ml3=ml3, ht=ht,
                           htft=basketball_fav.htft_winner(htft))
    if not res:
        return None
    probs = "  ".join(f"{k}={v*100:.0f}%" for k, v in res["probs"].items())
    what = "favourite FLIPS" if res["flip"] else f"win-prob spread {res['gap_pp']:.0f}pp"
    return _flag(book, "basketball", "basketball_fav", home, away, league, eid,
                 f"{what} across P(home): {probs}", res["gap_pp"],
                 start_time=start)


# ── CrystalBet ────────────────────────────────────────────────────────────────
def _cb_extract(odds):
    """From permissive CB detail Odds → the markets each check needs."""
    ft2 = ft3 = h1_3 = h1_2 = htft = None
    for o in odds:
        s = o.selections
        if o.market_type == "htft":
            htft = s
        elif o.market_type == "moneyline" and o.period == "FT":
            if "draw" in s:
                ft3 = s
            else:
                ft2 = s
        elif o.market_type == "moneyline" and o.period == "H1":
            if "draw" in s:
                h1_3 = s
            else:
                h1_2 = s
    return ft2, ft3, h1_3, h1_2, htft


def scan_cb(sport_name: str, min_gap_pp: float = hf.RATIO) -> list[dict]:
    import asyncio
    from src.scrapers import crystalbet as cb, cb_http, cb_detail
    from src.scrapers.sports import soccer as sc, basketball as bb
    sport = sc if sport_name == "soccer" else bb
    classify = sport.classify_market_title_permissive
    fetched = datetime.now(tz=timezone.utc)

    async def run():
        html = await cb_http.fetch_list_html(sport.SPORT_ID)
        games = cb._extract_games_from_list_html(html, fetched, sport=sport)
        flags = []
        for g in games:
            ml = _list_ml(g.list_odds)
            if not hf.should_open(ml[0], ml[1], g.league):
                continue
            try:
                blob = await cb_http.expand_detail_html(sport.SPORT_ID, g.event_id)
            except Exception:
                continue
            odds = cb_detail.parse_detail_page(
                blob, event_id=g.event_id, home=g.home, away=g.away, league=g.league,
                start_time=g.start_time, fetched_at=fetched, sport_name=sport_name,
                classify=classify, scope_to_event=True, per_section=True)
            ft2, ft3, h1_3, h1_2, htft = _cb_extract(odds)
            fl = _one(sport_name, "cb", g.home, g.away, g.league, g.event_id,
                      ml, ft3, ft2, h1_3 or h1_2, htft, g.start_time)
            if fl:
                flags.append(fl)
        return flags
    return asyncio.run(run())


def _list_ml(list_odds):
    for o in list_odds:
        if o.market_type == "moneyline" and o.period == "FT":
            s = o.selections
            return s.get("home"), s.get("away")
    return None, None


# ── unified per-event dispatch ────────────────────────────────────────────────
def _one(sport, book, home, away, league, eid, ft_ml, ft3, ft2, h1, htft, start):
    """ft_ml=(h,a) list moneyline; ft3/ft2 = 3-way/2-way selections dicts;
    h1 = 1st-half selections; htft = combo selections."""
    if sport == "soccer":
        if not htft:
            return None
        h1_pair = (h1.get("home"), h1.get("away")) if h1 else (None, None)
        return _soccer_htft_flag(book, home, away, league, eid, ft_ml, h1_pair, htft, start)
    ml2 = (ft2.get("home"), ft2.get("away")) if ft2 else None
    ml3 = (ft3.get("home"), ft3.get("away")) if ft3 else None
    htm = (h1.get("home"), h1.get("away")) if h1 else None
    return _basketball_fav_flag(book, home, away, league, eid, ml2, ml3, htm, htft, start)


# ── Betlive (getLeagueEvents list → getPrematchEvent detail) ──────────────────
def _bl_markets(fe):
    """(ft_ml, ft3, ft2, h1, htft) from a betlive getPrematchEvent."""
    ft_ml = (None, None)
    ft3 = ft2 = h1 = htft = None
    for mk in fe.get("markets") or []:
        ocs = mk.get("outcomes") or []
        if not ocs:
            continue
        name = ocs[0].get("marketName", "")
        # a lined market (spread/total) also carries "1"/"2" labels — exclude it
        # so a "Points Spread (OT)" never masquerades as the moneyline.
        lined = any(o.get("argumentX") not in (None, 0, 0.0)
                    or (o.get("argumentString") or "").strip() for o in ocs)
        # labels vary in spacing across books ("1 / 1" vs "1/1") — strip spaces.
        od = {(o.get("name") or "").replace(" ", ""): _f(o.get("odd")) for o in ocs}
        labs = {k for k, v in od.items() if v}
        low = name.lower()
        if is_htft_market(name) and "1/1" in labs:
            htft = {k: v for k, v in od.items() if "/" in k}     # full 9-way combo
        elif lined:
            continue
        elif is_h1_result_market(name) and {"1", "2"} <= labs:
            h1 = {"home": od.get("1"), "away": od.get("2")}
        elif ({"1", "X", "2"} <= labs                                  # FT 3-way result
              and not any(t in low for t in ("half", "quarter", "period", "2nd", "3rd", "4th"))):
            ft3 = {"home": od.get("1"), "draw": od.get("X"), "away": od.get("2")}
            if ft_ml == (None, None):
                ft_ml = (od.get("1"), od.get("2"))
        elif "(ot)" in low and labs == {"1", "2"}:                     # bball 2-way winner
            ft2 = {"home": od.get("1"), "away": od.get("2")}
            if ft_ml == (None, None):
                ft_ml = (od.get("1"), od.get("2"))
    return ft_ml, ft3, ft2, h1, htft


def _bl_list_ml(ev):
    for mk in ev.get("markets") or []:
        ocs = mk.get("outcomes") or []
        if not ocs:
            continue
        od = {(o.get("name") or "").strip(): _f(o.get("odd")) for o in ocs}
        if ocs[0].get("marketId") == 1 and "1" in od:                # soccer 1X2
            return od.get("1"), od.get("2")
        if {"1", "2"} == {k for k, v in od.items() if v}:            # bball 2-way
            return od.get("1"), od.get("2")
    return None, None


def scan_betlive(sport_name: str) -> list[dict]:
    from src import betlive_watch as bw
    from src.scrapers.betlive import SPORT_ID
    sid = SPORT_ID.get(sport_name)
    if sid is None:
        return []
    s = bw.session()
    cc = s.get(f"{bw.COUNTRIES_URL}?sportId={sid}", headers=bw.HEADERS, timeout=30).json()
    ids = [str(l["id"]) for l in bw._leaf_leagues(cc)]
    events = []
    for i in range(0, len(ids), 25):
        for w in (s.get(f"{bw.LEAGUE_EVENTS_URL}?leagueIds={','.join(ids[i:i+25])}"
                        f"&page=0&take=80", headers=bw.HEADERS, timeout=30).json() or []):
            events += w.get("events") or []
    flags = []
    for ev in events:
        h, a = _bl_list_ml(ev)
        if not hf.should_open(h, a, ev.get("leagueName")):
            continue
        try:
            r = s.get(f"{bw.PREMATCH_EVENT_URL}?id={ev['id']}", headers=bw.HEADERS, timeout=30).json()
        except Exception:
            continue
        fe = r[0] if isinstance(r, list) and r else r
        if not isinstance(fe, dict):
            continue
        ft_ml, ft3, ft2, h1, htft = _bl_markets(fe)
        fl = _one(sport_name, "betlive", ev.get("homeTeamName"), ev.get("awayTeamName"),
                  ev.get("leagueName"), ev["id"], (h, a), ft3, ft2, h1, htft, None)
        if fl:
            flags.append(fl)
    return flags


# ── Lider-Bet (matchData list → matchData/details full ladder, batched) ───────
def _lb_labels(mk, mts):
    tp = mts.get(mk.get("typeId"), {})
    lbl = {ot["id"]: (ot.get("name") or "").replace(" ", "") for ot in tp.get("outcomeTypes", [])}
    return tp.get("name", ""), {lbl.get(k, ""): o.get("value")
                                for k, o in (mk.get("outcomes") or {}).items()}


def _lb_ft_ml(m, mts, L):
    for mk in (m.get("markets") or {}).values():
        name, labels = _lb_labels(mk, mts)
        cls = L._classify_market(name)
        if cls and cls[0] == "moneyline":
            return L._odds(labels.get("1")), L._odds(labels.get("2"))
    return None, None


def _lb_extract(m, mts, L):
    ft3 = ft2 = h1 = htft = None
    for mk in (m.get("markets") or {}).values():
        name, labels = _lb_labels(mk, mts)
        od = {k: L._odds(v) for k, v in labels.items()}
        if is_htft_market(name) and od.get("1/1"):
            htft = {k: v for k, v in od.items() if "/" in k}     # full 9-way combo
        elif is_h1_result_market(name) and od.get("1") and od.get("2"):
            h1 = {"home": od.get("1"), "away": od.get("2")}
        cls = L._classify_market(name)
        if cls and cls[0] == "moneyline":
            if od.get("X"):
                ft3 = {"home": od.get("1"), "draw": od.get("X"), "away": od.get("2")}
            else:
                ft2 = {"home": od.get("1"), "away": od.get("2")}
    return ft3, ft2, h1, htft


def scan_liderbet(sport_name: str) -> list[dict]:
    from curl_cffi.requests import Session
    from src.scrapers import liderbet as L
    from src.normalize import transliterate
    sec = L.SECTION.get(sport_name)
    if sec is None:
        return []
    s = Session(impersonate="chrome124")
    menu = s.get(f"{L.MENU_URL}?lang=en&marketFilter=true", headers=L.HEADERS, timeout=30).json()["menu"]
    tours = [n["id"] for n in L._menu_nodes(menu)
             if n.get("id", "").startswith("t:") and n.get("sectionId") == sec and n.get("cnt", 0) > 0]
    cand = []
    for i in range(0, len(tours), 25):
        data = s.get(f"{L.MATCHDATA_URL}?tourIds={','.join(tours[i:i+25])}&lang=en&marketFilter=true",
                     headers=L.HEADERS, timeout=30).json()["data"]
        anc, mts = data.get("ancestors", {}), data.get("marketTypes", {})
        for m in (data.get("matches") or {}).values():
            h, a = _lb_ft_ml(m, mts, L)
            lg = transliterate((anc.get(m.get("tourId"), {}) or {}).get("name") or "") or None
            if hf.should_open(h, a, lg):
                cand.append((m.get("id"),
                             transliterate((anc.get(m.get("homeId"), {}) or {}).get("name") or ""),
                             transliterate((anc.get(m.get("awayId"), {}) or {}).get("name") or ""),
                             lg, (h, a)))
    flags = []
    for i in range(0, len(cand), 20):
        batch = cand[i:i + 20]
        ids = ",".join(x[0] if str(x[0]).startswith("pr:m:") else f"pr:m:{x[0]}" for x in batch)
        try:
            data = s.get(f"{L.MATCHDATA_URL}/details?matchIds={ids}&lang=en",
                         headers=L.HEADERS, timeout=45).json()["data"]
        except Exception:
            continue
        mts, matches = data.get("marketTypes", {}), (data.get("matches") or {})
        for mid, home, away, lg, ml in batch:
            m = matches.get(mid) or matches.get(str(mid))
            if not m:
                continue
            ft3, ft2, h1, htft = _lb_extract(m, mts, L)
            fl = _one(sport_name, "liderbet", home, away, lg, mid, ml, ft3, ft2, h1, htft, None)
            if fl:
                flags.append(fl)
    return flags


def _f(v):
    try:
        v = float(v)
        return v if v > 1.0 else None
    except (TypeError, ValueError):
        return None


def scan_all() -> list[dict]:
    """Every book × the sport its check applies to. Resilient: a book failing
    doesn't sink the rest."""
    out: list[dict] = []
    for name, fn, arg in (
        ("cb soccer", scan_cb, "soccer"), ("cb basketball", scan_cb, "basketball"),
        ("betlive soccer", scan_betlive, "soccer"), ("betlive basketball", scan_betlive, "basketball"),
        ("liderbet soccer", scan_liderbet, "soccer"), ("liderbet basketball", scan_liderbet, "basketball"),
    ):
        try:
            out += fn(arg)
        except Exception as e:
            log.warning("soft_scan %s failed: %s", name, e)
    return out
