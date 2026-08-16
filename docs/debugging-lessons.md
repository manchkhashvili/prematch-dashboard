# Debugging lessons — what a full day of chasing the wrong thing taught us

*Written 2026-08-15, after a session that produced nine correct commits, none of
which were the bug. Kept project-agnostic on purpose: the specifics are in
`docs/performance.md` and `notes/build_log.md`, this file is the method.*

---

## 1. Establish the baseline before you change anything

The symptom was "soccer HT/FT anomalies stopped appearing". Six restarts and
nine commits later, the actual test took ten minutes:

```bash
git worktree add /tmp/before <commit-before-the-changes>
# run the same operation in both trees, against the same live data
```

| build | time | events | flags |
|---|---|---|---|
| BEFORE | 155.9s | 151 | **18** |
| HEAD | 160.2s | 151 | **18** |

Identical — which proved instantly that the detector was fine and the problem
was *scheduling*, not logic. Every hour spent before that was spent fixing
forward on a hypothesis.

**Rule:** the moment someone says "it used to work", reproduce the old build
against today's data. It is nearly always cheaper than reasoning about what
changed, and it partitions the search space in one shot.

---

## 2. Ask what is *calling* the system, not just what it is doing

Every measurement taken during the long middle of that day was real, correct,
and about the app's internals — poll cycles, lock waits, parse costs, cache hit
rates. None of them could see a browser tab.

The cause was the project's own dashboard: a page polling an expensive endpoint
every 30 s, from every open tab, where one call cost ~25 s of CPU. Closing the
tabs moved a trivial endpoint from **8–17 s to 0.004 s** and CPU from **97% to
0.6%**.

**Rule:** when a single-threaded service is "slow", enumerate its clients before
profiling its internals.

```bash
lsof -nP -iTCP:<port> | grep -v LISTEN      # who is connected
grep -rn "setInterval\|poll" static/         # how often does your own UI ask
```

**Corollary — your monitoring is load.** A watcher polling the expensive
endpoint every 2 minutes was a meaningful part of the very problem being
measured. Probe cheap endpoints, at a cadence you've costed.

---

## 3. "It used to work" usually means a threshold was crossed

Nothing had broken. The matcher grew as the board grew, and at some point one
call cost more than the interval at which it was being polled. Before that, the
same code with the same config was fine.

**Rule:** look for a quantity that grew — rows, events, markets, clients —
rather than a change that landed. Ask "what is the cost of one call, and how
often is it called?" If those two numbers have crossed, that's the bug.

---

## 4. `to_thread` fixes I/O, never CPU

Python's GIL means `asyncio.to_thread` moves work off the loop's *stack* but not
off its *thread of execution*.

| workload | threads help? | measured |
|---|---|---|
| network-bound (HTTP, JSON over the wire) | **yes** | a 54s fetch let the loop tick 1050 times, 55 ms worst stall |
| CPU-bound pure Python (html5lib) | **no** | 4 pages on 4 threads was 0.88× — *slower* than one |

For the CPU-bound case only processes help, and the win is mostly that the loop
stops freezing (9,477 ms → 21 ms max stall), not raw throughput (2.5×).

**Rule:** measure which kind you have with a heartbeat coroutine — `await
asyncio.sleep(0.05)` in a loop, recording lateness — rather than assuming.

---

## 5. Test against real inputs, not saved fixtures

A parser swap looked perfect on every saved sample: identical output, 3–5×
faster. On live data it matched **0 of 25** cases.

The fixtures were captures of *already-repaired* HTML; the live path carries raw
malformed markup. The fixture could not express the thing that mattered.

**Rule:** before trusting a fixture, ask what transformation it has already been
through. A fixture proves a parser handles the fixture.

---

## 6. A filter that removes data is opt-in, per case, with a measurement

A market-count ceiling was measured on one sport and shipped as a global
default. It immediately skipped 23 of another sport's 45 games, because that
board's distribution was different, and silently removed its flags.

**Rules:**
- default to no filtering; a caller that says nothing gets everything;
- opt in per case, and only where the distribution has been measured;
- **ordering is not filtering** — rank by expected yield if you like, but let a
  cursor guarantee everything is eventually reached;
- check your filter still points at the data you care about. Ordering
  "cheapest-first" maximised games examined per pass while systematically
  expanding the games that carried nothing worth checking.

---

## 7. A truncated pass must advance, not repeat

Adding a wall-clock budget to a long scan converted "slow but complete" into
"prompt but 2.4% covered" — because the work was sorted the same way every pass,
so the same prefix ran forever and the rest of the board was never seen.

**Rule:** any budget-truncated loop needs a cursor. Record what was covered,
sort unseen work first, reset when the set is exhausted. Then partial passes
accumulate instead of restarting.

---

## 8. Report the failure mode, not just the happy path

Two fields made the difference between a five-minute diagnosis and an hour:

- `"truncated_at": stopped_at or None` mapped a stop at the *first* item to
  `None` — i.e. "ran to completion". "Expanded 0 of 500" read as healthy.
- A loop that had never completed a pass reported `at: null`, which is
  indistinguishable from "never started".

**Rules:** stamp "last ran" *before* the work and publish it unconditionally; a
sentinel for "nothing happened" must be distinct from the sentinel for "finished
normally"; split a duration into its phases (`wait` / `setup` / `work`) because
they have completely different fixes.

---

## 9. Caches and pools belong off under test

Both a process pool and a TTL cache broke the test suite in the same way: the
pool spawned interpreters and turned a 4 s run into a timeout; the cache made
"mutate state, re-query, assert" non-deterministic — and several tests did
exactly that, correctly.

**Rule:** default such machinery off when `pytest` is importable, let an explicit
env var win, and have the feature's *own* tests turn it on deliberately.

---

## 10. Revert on measurement, not on feeling

Two changes were written, benchmarked, and thrown away the same day: the parser
swap (failed live parity) and moving I/O-bound fetches into a process pool (25%
*slower*, and the premise was wrong). Both are recorded as negative results so
nobody re-derives them from the same plausible reasoning.

**Rule:** a change that a measurement does not support comes out, immediately,
even when it is finished and tested. And write down why — a negative result you
can't find again gets rebuilt.

---

## The cheap instruments that did the work

```python
# is the event loop actually running? lateness IS your contention metric
async def heartbeat(stop, out, period=0.05):
    while not stop.is_set():
        t0 = time.perf_counter()
        await asyncio.sleep(period)
        out.append((time.perf_counter() - t0 - period) * 1000)
```

```bash
# a trivial endpoint's latency is the loop's health
curl -o /dev/null -w "%{time_total}\n" localhost:8000/api/config

lsof -nP -iTCP:8000 | grep -v LISTEN      # who is talking to it
ps -M <pid>                                # per-thread CPU: one hot thread = GIL-bound
ps -o %cpu,rss -p <pid>
git worktree add /tmp/before <commit>      # the A/B that should come first
```

None of these needed root, a profiler, or a code change. The one tool that would
have helped (`py-spy dump`) needs root on macOS — worth arranging *before* an
incident rather than during one.
