"""
CB transport parity check — Playwright vs browser-free HTTP, live, side by side.

Proves the CB_TRANSPORT=http path yields the SAME data as the Playwright path
before flipping production over. Both transports feed the same untouched
parsers, so any divergence must come from the bytes themselves.

Per round:
  Phase 1 (list view):
    - Playwright: ensure page, SelectAllChampionats, page.content()
    - HTTP:       warmed session, SelectAllChampionats, UpdatePanelGames panel
      (captured immediately after, ~1-3 s apart)
    - compare _extract_games_from_list_html output: event-id sets, metadata
      (home/away/league/start_time — must be EXACTLY equal), raw loadinfo
      strings (equal ⇒ identical list-Odds by construction), and parsed
      list-Odds for games whose loadinfo differs (price drift vs structure).
  Phase 2 (detail, A/B/A):
    - for N common games: expand Playwright → HTTP → Playwright AGAIN,
      run the production detail parse on all three, compare canonical Odds
      keyed by (market_type, period, line, submarket, team_side, section).
      The pw1/pw2 pair measures the game's own movement over the same window
      that contains the HTTP capture, so a moving ladder (odds racing near
      kickoff) can't masquerade as a transport bug.
    - candidates prefer games >2 h from kickoff (stable odds); event ids
      ascending picked the soonest games and maximized drift noise.

Mismatch classes per market key:
  STRUCTURAL — http disagrees with BOTH pw captures (key present/absent in
               http but the opposite in pw1 AND pw2; or price differing from
               both). Systematic structural diffs = transport bug.
  DRIFT      — http agrees with at least one pw capture, or the key/price
               changed between pw1 and pw2 anyway (the game was moving).

Usage:
  .venv/bin/python scripts/cb_parity_check.py --sport basketball --details 6
  .venv/bin/python scripts/cb_parity_check.py --sport soccer --details 8 --rounds 2

Run more rounds if any structural diffs appear: real transport bugs repeat,
suspension flicker doesn't.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.models import Odds
from src.scrapers import cb_http
from src.scrapers.crystalbet import (
    _SPORT_MODULES,
    _ensure_page_for_sport,
    _expand_and_parse_one,
    _expand_and_parse_one_http,
    _extract_games_from_list_html,
    _load_all_leagues_for_sport,
    close_crystalbet,
)

OUT_DIR = Path(__file__).resolve().parents[1] / "output"


def odds_key(o: Odds) -> tuple:
    return (o.market_type, o.period, o.line, o.submarket, o.team_side, o.section)


def odds_canon(o: Odds) -> dict:
    return {k: round(v, 4) for k, v in o.selections.items()}


def compare_odds_lists(pw: list[Odds], http: list[Odds]) -> dict:
    """Compare two Odds lists for ONE event. Returns counters + diff lines."""
    pw_map = {odds_key(o): o for o in pw}
    ht_map = {odds_key(o): o for o in http}
    only_pw = sorted(set(pw_map) - set(ht_map), key=str)
    only_ht = sorted(set(ht_map) - set(pw_map), key=str)
    exact = drift = 0
    lines: list[str] = []
    for k in sorted(set(pw_map) & set(ht_map), key=str):
        a, b = odds_canon(pw_map[k]), odds_canon(ht_map[k])
        if a == b:
            exact += 1
        else:
            drift += 1
            lines.append(f"      DRIFT {k}: pw={a} http={b}")
    for k in only_pw:
        lines.append(f"      ONLY-PW   {k}: {odds_canon(pw_map[k])}")
    for k in only_ht:
        lines.append(f"      ONLY-HTTP {k}: {odds_canon(ht_map[k])}")
    return {"exact": exact, "drift": drift, "only_pw": len(only_pw),
            "only_http": len(only_ht), "lines": lines}


def compare_aba(pw1: list[Odds], http: list[Odds], pw2: list[Odds]) -> dict:
    """A/B/A compare: http vs the pw1+pw2 envelope.

    A market key counts as STRUCTURAL only when http disagrees with BOTH
    playwright captures; agreement with either one means the difference is
    the game moving (drift), not the transport.
    """
    m1 = {odds_key(o): odds_canon(o) for o in pw1}
    mh = {odds_key(o): odds_canon(o) for o in http}
    m2 = {odds_key(o): odds_canon(o) for o in pw2}
    self_moved = sum(1 for k in set(m1) | set(m2) if m1.get(k) != m2.get(k))

    exact = drift = structural = 0
    lines: list[str] = []
    for k in sorted(set(m1) | set(mh) | set(m2), key=str):
        in1, inh, in2 = k in m1, k in mh, k in m2
        if not inh:
            if in1 and in2:
                structural += 1
                lines.append(f"      STRUCT MISSING-IN-HTTP {k}: "
                             f"pw1={m1[k]} pw2={m2[k]}")
            else:
                drift += 1  # key flickered between pw captures too
            continue
        if not in1 and not in2:
            structural += 1
            lines.append(f"      STRUCT ONLY-HTTP {k}: {mh[k]}")
            continue
        if mh[k] == m1.get(k) or mh[k] == m2.get(k):
            exact += 1
        elif m1.get(k) != m2.get(k):
            drift += 1  # pw itself moved across the window; http mid-flight
        else:
            structural += 1
            lines.append(f"      STRUCT PRICE {k}: pw1=pw2={m1.get(k)} "
                         f"http={mh[k]}")
    return {"exact": exact, "drift": drift, "structural": structural,
            "self_moved": self_moved, "lines": lines}


async def run_round(sport_name: str, n_details: int, rep: list[str]) -> dict:
    sport = _SPORT_MODULES[sport_name]
    sport_id = sport.SPORT_ID
    fetched_at = datetime.now(tz=timezone.utc)
    totals = {"exact": 0, "drift": 0, "structural_detail": 0,
              "meta_mismatch": 0, "structural_list": 0}

    def emit(s: str) -> None:
        print(s)
        rep.append(s)

    # ── Phase 1: list view, both transports, back-to-back ──
    emit(f"\n── Phase 1: list view ({sport_name}) ──")
    t0 = asyncio.get_event_loop().time()
    page = await _ensure_page_for_sport(sport_id, headed=False)
    await _load_all_leagues_for_sport(page, sport_id)
    pw_html = await page.content()
    t_pw = asyncio.get_event_loop().time() - t0
    t0 = asyncio.get_event_loop().time()
    http_html = await cb_http.fetch_list_html(sport_id)
    t_ht = asyncio.get_event_loop().time() - t0
    emit(f"  playwright list: {len(pw_html):,}B in {t_pw:.1f}s | "
         f"http list: {len(http_html):,}B in {t_ht:.1f}s")

    pw_games = _extract_games_from_list_html(pw_html, fetched_at, sport=sport)
    ht_games = _extract_games_from_list_html(http_html, fetched_at, sport=sport)
    pw_by_id = {g.event_id: g for g in pw_games}
    ht_by_id = {g.event_id: g for g in ht_games}
    common = sorted(set(pw_by_id) & set(ht_by_id))
    only_pw_ids = sorted(set(pw_by_id) - set(ht_by_id))
    only_ht_ids = sorted(set(ht_by_id) - set(pw_by_id))
    emit(f"  games: playwright={len(pw_games)} http={len(ht_games)} "
         f"common={len(common)} only-pw={len(only_pw_ids)} only-http={len(only_ht_ids)}")
    for eid in only_pw_ids[:10]:
        g = pw_by_id[eid]
        emit(f"    ONLY-PW   {eid}: {g.home} vs {g.away} [{g.league}]")
    for eid in only_ht_ids[:10]:
        g = ht_by_id[eid]
        emit(f"    ONLY-HTTP {eid}: {g.home} vs {g.away} [{g.league}]")

    meta_bad = 0
    li_same = li_diff = 0
    list_odds_stats = {"exact": 0, "drift": 0, "only_pw": 0, "only_http": 0}
    for eid in common:
        a, b = pw_by_id[eid], ht_by_id[eid]
        if (a.home, a.away, a.league, a.start_time) != (b.home, b.away, b.league, b.start_time):
            meta_bad += 1
            emit(f"    META MISMATCH {eid}:")
            emit(f"      pw  : {a.home!r} vs {a.away!r} | {a.league!r} | {a.start_time}")
            emit(f"      http: {b.home!r} vs {b.away!r} | {b.league!r} | {b.start_time}")
        if a.loadinfo == b.loadinfo:
            li_same += 1
        else:
            # loadinfo bytes differ ⇒ CB changed this game's data between the
            # two captures — any parsed difference here is movement, not
            # transport. Informational only; doesn't count toward the verdict.
            li_diff += 1
            r = compare_odds_lists(a.list_odds, b.list_odds)
            for k in ("exact", "drift", "only_pw", "only_http"):
                list_odds_stats[k] += r[k]
            if r["only_pw"] or r["only_http"]:
                emit(f"    LIST-ODDS MOVED {eid} ({a.home} vs {a.away}):")
                for ln in r["lines"]:
                    emit(ln)
    emit(f"  metadata: {len(common) - meta_bad}/{len(common)} exact, {meta_bad} mismatched")
    emit(f"  loadinfo: {li_same} byte-identical, {li_diff} differ "
         f"(parsed compare on those: {list_odds_stats})")
    totals["meta_mismatch"] += meta_bad
    totals["structural_list"] += len(only_pw_ids) + len(only_ht_ids)

    # ── Phase 2: detail expansion, A/B/A (pw → http → pw) per game ──
    def has_detail(eid: str) -> bool:
        li = pw_by_id[eid].loadinfo
        return '"HasAdditionalOdds":"True"' in li or not li

    now = datetime.now(tz=timezone.utc)
    stable, soon = [], []
    for eid in common:
        if not has_detail(eid):
            continue
        st = pw_by_id[eid].start_time
        (stable if st and (st - now).total_seconds() > 2 * 3600 else soon).append(eid)
    # mostly stable games (odds shouldn't move at all) + a couple of
    # near-kickoff ones as a stress sample
    cands = stable[:max(1, n_details - 2)] + soon[:2]
    cands = cands[:n_details] or common[:n_details]

    emit(f"\n── Phase 2: detail expansion A/B/A ({len(cands)} games; "
         f"{len(stable)} stable / {len(soon)} near-kickoff available) ──")
    for eid in cands:
        g_pw, g_ht = pw_by_id[eid], ht_by_id[eid]
        label = f"{eid} {g_pw.home} vs {g_pw.away} [start {g_pw.start_time}]"
        try:
            pw1 = await _expand_and_parse_one(g_pw, fetched_at, sport, page)
            odds_ht = await _expand_and_parse_one_http(g_ht, fetched_at, sport)
            pw2 = await _expand_and_parse_one(g_pw, fetched_at, sport, page)
        except Exception as e:
            emit(f"  {label}: EXPAND FAILED ({type(e).__name__}: {e})")
            continue
        r = compare_aba(pw1, odds_ht, pw2)
        totals["exact"] += r["exact"]
        totals["drift"] += r["drift"]
        totals["structural_detail"] += r["structural"]
        flag = "  <-- STRUCTURAL" if r["structural"] else ""
        emit(f"  {label}: pw1={len(pw1)} http={len(odds_ht)} pw2={len(pw2)} | "
             f"agree={r['exact']} drift={r['drift']} structural={r['structural']} "
             f"(pw self-moved {r['self_moved']} keys){flag}")
        for ln in r["lines"]:
            emit(ln)
    return totals


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="basketball", choices=sorted(_SPORT_MODULES))
    ap.add_argument("--details", type=int, default=6)
    ap.add_argument("--rounds", type=int, default=1)
    args = ap.parse_args()

    rep: list[str] = []
    grand = {"exact": 0, "drift": 0, "structural_detail": 0,
             "meta_mismatch": 0, "structural_list": 0}
    try:
        for rnd in range(1, args.rounds + 1):
            line = f"\n===== ROUND {rnd}/{args.rounds} — {args.sport} ====="
            print(line)
            rep.append(line)
            t = await run_round(args.sport, args.details, rep)
            for k in grand:
                grand[k] += t[k]
    finally:
        await close_crystalbet()

    verdict_bad = grand["meta_mismatch"] or grand["structural_list"] \
        or grand["structural_detail"]
    summary = [
        "\n===== PARITY SUMMARY =====",
        f"detail markets: agree={grand['exact']} drift={grand['drift']} "
        f"structural={grand['structural_detail']}",
        f"list: structural={grand['structural_list']} "
        f"metadata-mismatch={grand['meta_mismatch']}",
        "VERDICT: " + ("STRUCTURAL DIFFS — inspect above before flipping transport"
                       if verdict_bad else
                       "PARITY OK — only price drift (expected); safe to flip "
                       "CB_TRANSPORT=http"),
    ]
    for s in summary:
        print(s)
        rep.append(s)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"parity_{args.sport}_{datetime.now():%Y%m%d_%H%M%S}.txt"
    out.write_text("\n".join(rep), encoding="utf-8")
    print(f"\nreport saved: {out}")
    return 1 if verdict_bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
