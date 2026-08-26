#!/bin/bash
# Start the whole thing: hourly collector + dashboard. Ctrl-C stops both.
#
#   ./run.command                 collector + dashboard
#   ./run.command collector       collector only
#   ./run.command dashboard       dashboard only
#   ./run.command once            a single pass, then exit
#   ./run.command smoke           ~90s across all boards, to check it works
#
# Named .command so double-clicking it in Finder opens a Terminal and runs it.
# Finder starts with a bare PATH and an arbitrary working directory, which is
# why this resolves both the project root and the interpreter itself rather
# than trusting the environment.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"            # .../prematch — `src.scrapers` resolves from here
cd "$ROOT" || exit 1

# The venv first: a double-clicked script gets /usr/bin/python3, which has none
# of the dependencies.
if   [ -x "$ROOT/../.venv/bin/python" ]; then PY="$ROOT/../.venv/bin/python"
elif [ -x "$ROOT/.venv/bin/python" ];    then PY="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null;      then PY="$(command -v python3)"
else PY="$(command -v python)"; fi

if [ -z "${PY:-}" ] || ! "$PY" -c "import fastapi, curl_cffi" 2>/dev/null; then
  echo "No usable Python found (need fastapi + curl_cffi)."
  echo "Tried: $PY"
  echo "Fix:   cd $ROOT && pip install -r requirements.txt"
  read -r -p "press return to close " _ 2>/dev/null || true
  exit 1
fi

MODE="${1:-all}"
LOGS="$HERE/logs"; mkdir -p "$LOGS"
COLLECTOR_LOG="$LOGS/collector.log"
DASHBOARD_LOG="$LOGS/dashboard.log"
PORT="${SDM_PORT:-8100}"

INTERVAL="${SDM_INTERVAL:-3600}"     # seconds between the START of each pass
MIN_GAP="${SDM_MIN_GAP:-300}"        # never closer than this, if a pass overruns
PRUNE_DAYS="${SDM_PRUNE_DAYS:-14}"
CB_CONCURRENCY="${SDM_CB_CONCURRENCY:-3}"

# Plain variables rather than an array: macOS ships bash 3.2, where
# ${arr[-1]} is a syntax error ("bad array subscript"). Under `set -u` that
# killed this script mid-start and left the dashboard orphaned.
DASH_PID=""
LOOP_PID=""
TAIL_PID=""

stop() {
  echo ""
  echo "stopping…"
  for p in "$TAIL_PID" "$LOOP_PID" "$DASH_PID"; do
    [ -n "$p" ] && kill "$p" 2>/dev/null
  done
  # Give a pass that is mid-commit a moment to finish rather than tearing it
  # in half. A killed pass loses only its own staging — nothing is committed
  # until the end — but a clean exit keeps the snapshot row honest.
  n=0
  while [ "$n" -lt 12 ]; do
    still=0
    for p in "$LOOP_PID" "$DASH_PID"; do
      [ -n "$p" ] && kill -0 "$p" 2>/dev/null && still=1
    done
    [ "$still" -eq 0 ] && break
    n=$((n + 1)); sleep 1
  done
  for p in "$TAIL_PID" "$LOOP_PID" "$DASH_PID"; do
    [ -n "$p" ] && kill -0 "$p" 2>/dev/null && kill -9 "$p" 2>/dev/null
  done
  echo "stopped. data is in $HERE/data/snapshots.db"
  exit 0
}
trap stop INT TERM

banner() {
  echo "── sportsdatamovement ──────────────────────────────────"
  echo "  python     $PY"
  echo "  database   $HERE/data/snapshots.db"
  echo "  logs       $LOGS"
}

case "$MODE" in
  smoke)
    # Deliberately a THROWAWAY database. A capped pass reads two events per
    # board, and the move grid reads a blank cell as "the price was unchanged"
    # — which is true for an event that was read and false for one that was
    # not. Letting a smoke run into the real store would put a column of
    # silent lies through every event's grid.
    banner
    echo "  mode       smoke — 2 events per board, into a throwaway db"
    echo ""
    rm -f "$HERE/data/smoke.db" "$HERE/data/smoke.db-wal" "$HERE/data/smoke.db-shm"
    exec "$PY" -m sportsdatamovement --db "$HERE/data/smoke.db" \
         snapshot --max-events 2
    ;;
  once)
    banner
    echo "  mode       one full pass (~11 min), then exit"
    echo ""
    exec "$PY" -m sportsdatamovement snapshot --cb-concurrency "$CB_CONCURRENCY"
    ;;
  dashboard)
    banner
    echo "  mode       dashboard only"
    echo ""
    echo "  → http://127.0.0.1:$PORT"
    exec "$PY" -m sportsdatamovement serve --port "$PORT"
    ;;
esac

banner
echo "  cadence    a pass every ${INTERVAL}s, min gap ${MIN_GAP}s"
echo ""

if [ "$MODE" != "collector" ]; then
  "$PY" -m sportsdatamovement serve --port "$PORT" > "$DASHBOARD_LOG" 2>&1 &
  DASH_PID=$!
  sleep 2
  if kill -0 "$DASH_PID" 2>/dev/null; then
    echo "  dashboard  → http://127.0.0.1:$PORT"
  else
    DASH_PID=""
    echo "  dashboard  FAILED to start — see $DASHBOARD_LOG"
    tail -3 "$DASHBOARD_LOG"
  fi
fi

# The collector refuses to start if another one holds the lock, so a stale
# instance shows up here as a clear message rather than as silently interleaved
# passes that make every change count meaningless.
"$PY" -m sportsdatamovement loop \
      --interval "$INTERVAL" --min-gap "$MIN_GAP" \
      --prune-days "$PRUNE_DAYS" --cb-concurrency "$CB_CONCURRENCY" \
      > "$COLLECTOR_LOG" 2>&1 &
LOOP_PID=$!
sleep 3
if ! kill -0 "$LOOP_PID" 2>/dev/null; then
  echo "  collector  FAILED to start:"
  sed 's/^/             /' "$COLLECTOR_LOG" | tail -6
  LOOP_PID=""
  stop
fi

echo "  collector  running (pid $LOOP_PID)"
echo ""
echo "Ctrl-C stops both. Following the collector log:"
echo "────────────────────────────────────────────────────────"

# Follow the log, but keep this shell in the foreground so the trap can fire.
tail -f "$COLLECTOR_LOG" &
TAIL_PID=$!
wait "$LOOP_PID"      # if the collector dies, tear the rest down too
stop
