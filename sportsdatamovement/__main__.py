"""
CLI: python -m sportsdatamovement <command>

    sports                 what both books publish right now
    snapshot               one pass over everything
    loop                   snapshot, wait ~an hour, repeat
    stats                  database size and row counts
    passes                 the last N snapshot rows, with their diff counts
    movements              what moved, newest first
    event                  one event's whole board, each position's last move
    board                  reconstruct a full board as it stood at an instant
    export                 the movement log as CSV
    prune                  drop events not seen for N days

Run from the prematch root so `src.scrapers` resolves:
    python -m sportsdatamovement snapshot --max-events 5
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sportsdatamovement import crystalbet, liderbet, runner   # noqa: E402
from sportsdatamovement.store import Store, decode_price      # noqa: E402


def _log(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S")
    logging.getLogger("curl_cffi").setLevel(logging.WARNING)


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_sports(args) -> None:
    from curl_cffi.requests import Session
    print("CrystalBet")
    for sid, name, cnt in crystalbet.discover_sports():
        print(f"  {sid:5d}  {name:22s} {cnt:6d} games")
    print("\nLider-Bet")
    s = Session(impersonate=liderbet.IMPERSONATE)
    try:
        found = liderbet.read_menu(s, skip_simulated=not args.include_simulated)
    finally:
        s.close()
    for sec_id, sec in sorted(found.items(), key=lambda kv: -kv[1]["matches"]):
        print(f"  {sec_id:8s}  {sec['slug']:22s} {sec['matches']:6d} matches "
              f"({len(sec['tours'])} tournaments)")


def cmd_snapshot(args) -> None:
    store = Store(args.db) if args.db else runner.open_store()
    with runner.exclusive(store):
        res = asyncio.run(runner.run_pass(
            store, books=tuple(args.books), cb_concurrency=args.cb_concurrency,
            max_start_days=args.max_start_days,
            skip_simulated=not args.include_simulated,
            max_events=args.max_events))
    print(json.dumps(res, indent=2, default=str))
    print("\ndb:", json.dumps(store.stats(), indent=2))


def cmd_loop(args) -> None:
    store = Store(args.db) if args.db else runner.open_store()
    try:
        with runner.exclusive(store):
            asyncio.run(runner.run_loop(
                store, interval_sec=args.interval, min_gap_sec=args.min_gap,
                passes=args.passes, prune_days=args.prune_days,
                books=tuple(args.books), cb_concurrency=args.cb_concurrency,
                max_start_days=args.max_start_days,
                skip_simulated=not args.include_simulated,
                max_events=args.max_events))
    except KeyboardInterrupt:
        print("\nstopped")


def cmd_stats(args) -> None:
    store = Store(args.db) if args.db else runner.open_store()
    s = store.stats()
    print(f"db file        {_human(s['db_bytes'])}")
    print(f"events         {s['events']:>12,}")
    print(f"positions      {s['positions']:>12,}")
    print(f"odds rows      {s['odds_rows']:>12,}")
    print(f"markets/sides  {s['markets']:>12,} / {s['sides']:,}")
    print(f"snapshots      {s['snapshots']:>12,}  ({s['snapshots_ok']:,} ok)")
    if s["odds_rows"]:
        print(f"bytes per stored change  ~{s['db_bytes'] / s['odds_rows']:.0f}")


def cmd_passes(args) -> None:
    store = Store(args.db) if args.db else runner.open_store()
    rows = store.recent_snapshots(args.limit)
    print(f"{'id':>6} {'book':<11} {'sport':<18} {'started':<20} {'ok':>2} "
          f"{'events':>7} {'positions':>10} {'new':>8} {'moved':>8} {'gone':>7} "
          f"{'dupe':>6} {'MB':>7} {'sec':>6}")
    for r in rows:
        print(f"{r['id']:>6} {r['book']:<11} {r['sport']:<18} "
              f"{(r['started_at'] or '')[:19]:<20} {r['ok']:>2} "
              f"{r['n_events']:>7,} {r['n_positions']:>10,} {r['n_new']:>8,} "
              f"{r['n_moved']:>8,} {r['n_gone']:>7,} {r['n_dupes']:>6,} "
              f"{r['bytes']/1e6:>7.0f} {r['dur_ms']/1000:>6.0f}"
              + (f"  ERR {r['error'][:60]}" if r.get("error") else ""))


def _query(store: Store, sql: str, params: tuple = ()) -> list[tuple]:
    con = store._connect()
    try:
        return list(con.execute(sql, params))
    finally:
        con.close()


def cmd_movements(args) -> None:
    store = Store(args.db) if args.db else runner.open_store()
    where, params = ["o.price IS NOT NULL"], []
    if args.book:
        where.append("e.book = ?"); params.append(args.book)
    if args.sport:
        where.append("e.sport = ?"); params.append(args.sport)
    if args.snapshot:
        where.append("o.snapshot_id = ?"); params.append(args.snapshot)
    sql = f"""
    SELECT sn.started_at, e.book, e.sport, e.league, e.home, e.away,
           m.name, s.label, p.line, o.price
      FROM odds o
      JOIN positions p ON p.id = o.position_id
      JOIN markets   m ON m.id = p.market_id
      JOIN sides     s ON s.id = p.side_id
      JOIN events    e ON e.id = p.event_id
      JOIN snapshots sn ON sn.id = o.snapshot_id
     WHERE {' AND '.join(where)}
     ORDER BY o.snapshot_id DESC, e.id, m.id
     LIMIT ?"""
    params.append(args.limit)
    for ts, book, sport, league, home, away, mkt, side, line, price in _query(
            store, sql, tuple(params)):
        ln = "" if line is None else f" @{line:g}"
        print(f"{ts[:19]}  {book:<11} {sport:<14} {home} - {away}"
              f"\n     {mkt}{ln}  [{side}]  {decode_price(price)}")


def cmd_event(args) -> None:
    """Every position on one event, with the snapshot it last moved in.

    This is the hand-inspection view the study needs: sort by `last_move` and
    the markets that followed a price change sit apart from the ones that did
    not.
    """
    store = Store(args.db) if args.db else runner.open_store()
    ev = _query(store, """
        SELECT id, book, sport, league, home, away, start_time FROM events
         WHERE (home || ' - ' || away) LIKE ? OR event_key = ?
         ORDER BY start_time LIMIT 1""",
        (f"%{args.match}%", args.match))
    if not ev:
        print(f"no event matching {args.match!r}")
        return
    eid, book, sport, league, home, away, start = ev[0]
    print(f"{book} / {sport} / {league}\n{home} - {away}   kickoff {start}\n")
    rows = _query(store, """
        SELECT m.name, s.label, p.line, l.price, l.snapshot_id, sn.started_at,
               (SELECT COUNT(*) FROM odds o WHERE o.position_id = p.id)
          FROM positions p
          JOIN markets m ON m.id = p.market_id
          JOIN sides   s ON s.id = p.side_id
          LEFT JOIN latest l ON l.position_id = p.id
          LEFT JOIN snapshots sn ON sn.id = l.snapshot_id
         WHERE p.event_id = ?
         ORDER BY sn.started_at DESC, m.name, s.label""", (eid,))
    print(f"{'last move':<20} {'n':>3} {'odds':>7}  market / side")
    for mkt, side, line, price, _sid, ts, n in rows[:args.limit]:
        ln = "" if line is None else f" @{line:g}"
        o = decode_price(price)
        print(f"{(ts or '-')[:19]:<20} {n:>3} {('' if o is None else f'{o:.2f}'):>7}"
              f"  {mkt}{ln}  [{side}]")
    print(f"\n{len(rows)} positions")


def cmd_board(args) -> None:
    store = Store(args.db) if args.db else runner.open_store()
    when = args.at or "9999"
    rows = store.snapshot_at(args.book, args.sport, when)
    print(f"{len(rows):,} priced positions at {when}")
    if args.out:
        with open(args.out, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=[
                "book", "sport", "league", "home", "away", "start_time",
                "position", "side", "line", "odds", "record_time"])
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {args.out}")
    else:
        for r in rows[:args.limit]:
            print(r)


def cmd_export(args) -> None:
    store = Store(args.db) if args.db else runner.open_store()
    con = store._connect()
    try:
        cur = con.execute(
            "SELECT record_time, sportsbook, sport, league, event, kickoff,"
            " position, side, line, odds FROM movements"
            " ORDER BY snapshot_id DESC LIMIT ?", (args.limit,))
        cols = [d[0] for d in cur.description]
        with open(args.out, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            n = 0
            for row in cur:
                w.writerow(row)
                n += 1
        print(f"wrote {n:,} rows to {args.out}")
    finally:
        con.close()


def cmd_prune(args) -> None:
    store = Store(args.db) if args.db else runner.open_store()
    print(store.prune(args.days))


# ── wiring ────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sportsdatamovement", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", help="database path (default sportsdatamovement/data/snapshots.db)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def collect_args(sp):
        sp.add_argument("--books", nargs="+", default=["liderbet", "crystalbet"],
                        choices=["liderbet", "crystalbet"])
        sp.add_argument("--cb-concurrency", type=int, default=3,
                        help="CrystalBet sports collected at once (default 3)")
        sp.add_argument("--max-start-days", type=float, default=0.0,
                        help="skip events starting later than this (0 = no cap)")
        sp.add_argument("--max-events", type=int, default=0,
                        help="cap events per sport — for smoke runs")
        sp.add_argument("--include-simulated", action="store_true",
                        help="keep simulated/virtual leagues (off by default: "
                             "they reprice constantly by construction)")

    sp = sub.add_parser("sports"); sp.set_defaults(func=cmd_sports)
    sp.add_argument("--include-simulated", action="store_true")

    sp = sub.add_parser("snapshot"); collect_args(sp); sp.set_defaults(func=cmd_snapshot)

    sp = sub.add_parser("loop"); collect_args(sp); sp.set_defaults(func=cmd_loop)
    sp.add_argument("--interval", type=float, default=runner.DEFAULT_INTERVAL_SEC)
    sp.add_argument("--min-gap", type=float, default=runner.DEFAULT_MIN_GAP_SEC)
    sp.add_argument("--passes", type=int, default=0, help="0 = forever")
    sp.add_argument("--prune-days", type=float, default=0.0)

    sp = sub.add_parser("stats"); sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("passes"); sp.set_defaults(func=cmd_passes)
    sp.add_argument("--limit", type=int, default=30)

    sp = sub.add_parser("movements"); sp.set_defaults(func=cmd_movements)
    sp.add_argument("--book"); sp.add_argument("--sport")
    sp.add_argument("--snapshot", type=int)
    sp.add_argument("--limit", type=int, default=40)

    sp = sub.add_parser("event"); sp.set_defaults(func=cmd_event)
    sp.add_argument("match", help="team-name substring or the book's event id")
    sp.add_argument("--limit", type=int, default=80)

    sp = sub.add_parser("board"); sp.set_defaults(func=cmd_board)
    sp.add_argument("--book", required=True); sp.add_argument("--sport", required=True)
    sp.add_argument("--at", help="ISO timestamp (default: now)")
    sp.add_argument("--out"); sp.add_argument("--limit", type=int, default=20)

    sp = sub.add_parser("export"); sp.set_defaults(func=cmd_export)
    sp.add_argument("--out", default="movements.csv")
    sp.add_argument("--limit", type=int, default=1_000_000)

    sp = sub.add_parser("prune"); sp.set_defaults(func=cmd_prune)
    sp.add_argument("--days", type=float, default=14.0)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _log(args.verbose)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
