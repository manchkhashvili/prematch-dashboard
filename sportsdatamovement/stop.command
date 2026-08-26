#!/bin/bash
# Stop the collector and the dashboard, wherever they were started from.
#
# run.command cleans up after itself on Ctrl-C. This is for when it could not:
# a closed terminal, a crash, or a run started with nohup. It is safe to run
# when nothing is up.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# `pgrep -f` matches the full command line. Note the interpreter renders as
# `/…/Python -m sportsdatamovement` with a CAPITAL P on macOS framework builds,
# which is why the pattern matches the module name and not "python".
found="$(pgrep -f "sportsdatamovement (loop|serve|snapshot)" 2>/dev/null \
         || pgrep -f "sportsdatamovement" 2>/dev/null)"

if [ -z "$found" ]; then
  echo "nothing running."
else
  echo "stopping:"
  for p in $found; do
    ps -o pid=,command= -p "$p" 2>/dev/null | cut -c1-100 | sed 's/^/  /'
  done
  echo "$found" | xargs kill 2>/dev/null
  # A pass mid-commit deserves a moment; nothing is written until the end, so
  # the worst case is losing that one pass, not a half-written one.
  n=0
  while [ "$n" -lt 15 ]; do
    pgrep -f "sportsdatamovement" >/dev/null 2>&1 || break
    n=$((n + 1)); sleep 1
  done
  if pgrep -f "sportsdatamovement" >/dev/null 2>&1; then
    echo "  did not exit in ${n}s — forcing"
    pgrep -f "sportsdatamovement" | xargs kill -9 2>/dev/null
    sleep 1
  fi
fi

if pgrep -f "sportsdatamovement" >/dev/null 2>&1; then
  echo "STILL RUNNING — check manually: pgrep -fl sportsdatamovement"
  exit 1
fi

echo "stopped."
echo "  database  $HERE/data/snapshots.db"
[ -f "$HERE/data/snapshots.db" ] && \
  ls -l "$HERE/data/snapshots.db" | awk '{printf "  size      %.1f MB\n", $5/1e6}'
