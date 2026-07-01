"""
Prematch +EV/ARB scanner entry point.

Default — launch the dashboard (venv assumed active):

    cd /Users/malish/Documents/cb/prematch
    python main.py
    # → http://localhost:8000

Common flavors (per-sport mode via SPORTS env var — see below):

    SPORTS=basketball:full,soccer:list python main.py    # recommended laptop config
    SPORTS=basketball:full python main.py                 # basketball only
    SPORTS=basketball:list,soccer:list python main.py     # lightest, both sports
    python main.py 2>&1 | tee dashboard.log               # tee logs for debugging

    # Anomalies tab — separate hourly full-detail basketball scan, on top of
    # whatever (light) modes you run. Works even with everything in list mode.
    # The scan is browser-free by default now (CB_ANOMALY_TRANSPORT=http), so it
    # needs no Chromium even when CB_TRANSPORT=playwright. Add BETLIVE_ANOMALY=1
    # for the betlive favourite-flip watch (incl-OT vs regulation 1X2):
    ANOMALY_SCAN=1 BETLIVE_ANOMALY=1 BETLIVE=1 \
      SPORTS=basketball:list,soccer:list,tennis:list python main.py 2>&1 | tee dashboard.log

Dev mode — one cycle, print table, exit:

    python main.py --once                # live both books
    python main.py --once --cb-saved     # reuse saved CB HTML
    python main.py --once --min-edge 2.0

Environment variables (dashboard mode):
    PINNACLE_POLL_SEC=60      poll cadence for Pinnacle (all enabled sports)
    CRYSTALBET_POLL_SEC=180   poll cadence for CrystalBet (all enabled sports)
    CB_HEADLESS=1             1=headless Playwright, 0=headed
    CB_USE_SAVED=0            1=parse saved CB HTML samples (dev mode)

    Extra soft books (opt-in; default OFF = unchanged CB-vs-Pin pipeline):
    LIDERBET=1                add Lider-Bet (browser-free JSON; docs/liderbet.md)
    BETLIVE=1                 add Betlive    (browser-free JSON; docs/betlive.md)
                              Each is matched vs Pinnacle exactly like CB (filter
                              by book on the Arbs tab) AND against each other for
                              cross-book arbs joined on the SportRadar match id
                              (/api/cross_arbs). Example (fully browser-free —
                              all books over HTTP/JSON, anomaly scan incl.):
                                CB_TRANSPORT=http ANOMALY_SCAN=1 BETLIVE_ANOMALY=1 \
                                  SPORTS=basketball:list,soccer:list,tennis:list \
                                  LIDERBET=1 BETLIVE=1 python main.py 2>&1 | tee dashboard.log
    EXTRA_BOOK_POLL_SEC=120   poll cadence for Lider-Bet / Betlive (JSON, cheap)

    BETLIVE_ANOMALY=1         betlive favourite-flip watch → Anomalies tab. Slow
                              full-ladder discover (BETLIVE_DISCOVER_SEC=600) +
                              cheap refreshOdds watch (BETLIVE_WATCH_SEC=8,
                              ~50 KB/tick). BETLIVE_MIN_GAP_PP=2.0,
                              BETLIVE_ANOMALY_SPORT_IDS=6. Independent of BETLIVE=1.
    CB_ANOMALY_TRANSPORT=http anomaly scan transport (default http = browser-free,
                              even when CB_TRANSPORT=playwright). =playwright to force.

    SPORTS=                   per-sport mode, one knob to rule them all.
                              Comma-separated 'sport:mode' or 'sport_mode'.
                              Modes:
                                full = CB+Pin+detail expansion (heaviest)
                                list = CB list-view only, no detail
                                       (~30s/cycle, no alt-lines)
                                off  = sport disabled
                              Sports not listed = off.
                              Default (unset) = all known sports in full mode.
                              Examples:
                                SPORTS=basketball:full,soccer:list   (recommended)
                                SPORTS=basketball_full,soccer_list   (underscore syntax)
                                SPORTS=basketball                    (basketball only, full)
                                SPORTS=basketball:full,soccer:off    (soccer disabled)

    ANOMALY_SCAN=0            1=enable the Anomalies tab. Runs a SEPARATE
                              full-detail basketball scrape (force_detail) on
                              its own cadence, independent of SPORTS modes, so
                              the CB ladder anomaly detector has the alt-lines
                              even when basketball runs in list mode for the
                              other tabs. (Or add `anomalies:on` to SPORTS.)
    ANOMALY_SCAN_AT_MINUTE=15,45  clock-align the scan to each listed minute of
                              every hour. Default "15,45" = every 30 min (fresh
                              a couple minutes before :00 and :30). Comma-list.
                              Set to off/-1 to use a fixed interval instead.
    ANOMALY_SCAN_SEC=1800     fixed-interval fallback when alignment is off.

    Legacy (back-compat, prefer SPORTS instead):
    ENABLED_SPORTS=           comma-separated sport names; default = all.
    CB_SKIP_DETAIL_SPORTS=    comma-separated; sports in list-view-only mode.

    HOST=127.0.0.1            uvicorn bind host
    PORT=8000                 uvicorn bind port
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure `src` is importable when run as `python main.py` from prematch/
sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger(__name__)
OUTPUT_DIR = Path(__file__).parent / "output"


# ── Dashboard mode ────────────────────────────────────────────────────────────
def _serve() -> None:
    """Launch uvicorn against src.app:app. Imports deferred so --once doesn't pay the cost."""
    import uvicorn
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    log.info("dashboard starting on http://%s:%d", host, port)
    uvicorn.run("src.app:app", host=host, port=port, log_level="info")


# ── One-shot mode (preserved for dev/iteration) ───────────────────────────────
async def _run_once(args: argparse.Namespace) -> None:
    from src.edge import compute_opportunities
    from src.matcher import match_events
    from src.models import Opportunity
    from src.scrapers.crystalbet import (
        SAMPLE_OUT as CB_SAMPLE_PATH,
        dry_run_parse_saved,
        fetch_crystalbet_basketball_prematch,
    )
    from src.scrapers.pinnacle import fetch_pinnacle_basketball

    log.info("fetching Pinnacle prematch basketball…")
    pin_odds = await fetch_pinnacle_basketball()
    log.info("Pinnacle: %d Odds rows", len(pin_odds))

    if args.cb_saved:
        log.info("parsing saved CB HTML from %s", CB_SAMPLE_PATH)
        cb_odds = dry_run_parse_saved(CB_SAMPLE_PATH)
    else:
        log.info("scraping CrystalBet (Playwright)…")
        cb_odds = await fetch_crystalbet_basketball_prematch(headed=not args.headless)
    log.info("CrystalBet: %d Odds rows", len(cb_odds))

    matched = match_events(cb_odds, pin_odds)
    opps = compute_opportunities(matched, min_edge_pct=args.min_edge)

    _print_table(opps)

    if not args.no_csv and opps:
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M")
        csv_path = OUTPUT_DIR / f"edges_{ts}.csv"
        _save_csv(opps, csv_path)
        print(f"  CSV saved: {csv_path}\n")


# ── Rich / plain table ────────────────────────────────────────────────────────
def _print_table(opps) -> None:
    try:
        from rich.console import Console
        from rich.table import Table
        _rich_table(opps, Console())
    except ImportError:
        _plain_table(opps)


def _rich_table(opps, console) -> None:
    from rich.table import Table

    if not opps:
        console.print("\n[yellow]No +EV/ARB opportunities found.[/yellow]\n")
        return

    t = Table(title=f"CB vs Pinnacle — Opportunities ({len(opps)} rows)")
    t.add_column("Match", style="cyan", no_wrap=True)
    t.add_column("Market", style="white")
    t.add_column("Side", style="white")
    t.add_column("CB odds", justify="right", style="green")
    t.add_column("Pin ref", justify="right", style="blue")
    t.add_column("Edge %", justify="right", style="bold green")
    t.add_column("Kind", style="magenta")
    t.add_column("Kelly", justify="right", style="yellow")
    t.add_column("Kickoff (UTC)", style="dim")

    for o in opps:
        kickoff = o.start_time.strftime("%m-%d %H:%M") if o.start_time else "?"
        t.add_row(
            o.match_label,
            o.market,
            o.side,
            f"{o.cb_odds:.3f}",
            f"{o.pin_no_vig:.3f}",
            f"{o.edge_pct:+.1f}%",
            o.kind,
            f"{o.kelly_stake:.0f}",
            kickoff,
        )

    console.print()
    console.print(t)
    console.print()


def _plain_table(opps) -> None:
    if not opps:
        print("\nNo +EV/ARB opportunities found.\n")
        return
    hdr = (f"{'Match':<40} {'Market':<22} {'Side':<6} {'CB':>6} "
           f"{'Ref':>6} {'Edge':>7} {'Kind':<4} {'Kelly':>6} Kickoff")
    print("\n" + hdr)
    print("-" * len(hdr))
    for o in opps:
        kickoff = o.start_time.strftime("%m-%d %H:%M") if o.start_time else "?"
        print(
            f"{o.match_label:<40} {o.market:<22} {o.side:<6} "
            f"{o.cb_odds:>6.3f} {o.pin_no_vig:>6.3f} "
            f"{o.edge_pct:>+6.1f}% {o.kind:<4} {o.kelly_stake:>6.0f} {kickoff}"
        )
    print()


# ── CSV ───────────────────────────────────────────────────────────────────────
def _save_csv(opps, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "match_label", "market", "side", "cb_odds", "pin_no_vig",
        "edge_pct", "kind", "kelly_stake", "start_time",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for o in opps:
            w.writerow({
                "match_label": o.match_label,
                "market": o.market,
                "side": o.side,
                "cb_odds": round(o.cb_odds, 4),
                "pin_no_vig": round(o.pin_no_vig, 4),
                "edge_pct": round(o.edge_pct, 2),
                "kind": o.kind,
                "kelly_stake": round(o.kelly_stake, 2),
                "start_time": o.start_time.isoformat() if o.start_time else "",
            })
    log.info("saved %d rows → %s", len(opps), path)


# ── Argparse ──────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(
        description="CrystalBet vs Pinnacle prematch +EV/ARB scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Default: launches the dashboard at http://localhost:8000.\n"
            "Use --once for the one-shot CLI used during development."
        ),
    )
    ap.add_argument("--once", action="store_true",
                    help="run one cycle, print table, exit (dev mode)")
    ap.add_argument("--cb-saved", action="store_true",
                    help="(--once) parse saved CB HTML instead of live scraping")
    ap.add_argument("--headless", action="store_true",
                    help="(--once) run CB Playwright headless")
    ap.add_argument("--min-edge", type=float, default=1.0, metavar="PCT",
                    help="(--once) minimum edge%% to include in output (default: 1.0)")
    ap.add_argument("--no-csv", action="store_true",
                    help="(--once) skip writing CSV output")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="DEBUG logging")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.once:
        asyncio.run(_run_once(args))
    else:
        _serve()


if __name__ == "__main__":
    main()
