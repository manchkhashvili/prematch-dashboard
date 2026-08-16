"""Run snippets of static/alerts.js under node, with a localStorage stub.

Shared by the two JS-logic suites (anomaly alerts, arb alert layers). One copy
on purpose: alerts.js touches a handful of browser globals at load, and when
that set changes both suites need the same stub — two hand-maintained copies
would drift and the second one to break would look like a real failure.

Skipped, not failed, where node is absent: node is a convenience for testing the
frontend logic, not a dependency of the dashboard.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

ALERTS = Path(__file__).resolve().parent.parent / "static" / "alerts.js"
NODE = shutil.which("node")

# alerts.js is an IIFE that schedules timers and registers DOM listeners at
# load. Stub exactly what it touches; anything else should surface loudly rather
# than being silently absorbed.
_HARNESS = """
    const store = %s;
    globalThis.localStorage = {
      getItem: k => (k in store ? String(store[k]) : null),
      setItem: (k, v) => { store[k] = String(v); },
    };
    globalThis.document = { addEventListener() {} };
    globalThis.setTimeout = () => 0;
    globalThis.setInterval = () => 0;
    globalThis.fetch = () => Promise.reject(new Error("no network in tests"));
    const A = require(%s);
    let out;
    %s
    console.log("\\u0001" + JSON.stringify(out === undefined ? null : out));
"""


def run_js(body: str, store: dict | None = None):
    """Execute `body` with alerts.js loaded as `A`. Assign the result to `out`.

    Returns the JSON-decoded `out`. Anything the snippet logs itself is ignored,
    so a console.warn inside alerts.js cannot corrupt the result.
    """
    if NODE is None:                      # pragma: no cover - guarded by skipif
        raise RuntimeError("node not installed")
    src = textwrap.dedent(_HARNESS) % (
        json.dumps(store or {}), json.dumps(str(ALERTS)), body)
    r = subprocess.run([NODE, "-e", src], capture_output=True, text=True)
    assert r.returncode == 0, f"node failed:\n{r.stderr}"
    for line in r.stdout.splitlines():
        if line.startswith(""):
            return json.loads(line[1:])
    raise AssertionError(f"snippet produced no result:\n{r.stdout}\n{r.stderr}")
