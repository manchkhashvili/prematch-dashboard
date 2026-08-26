# sportsdatamovement

Hourly whole-board snapshots of **Lider-Bet** and **CrystalBet** — every sport,
every league, every position — stored so that "which markets moved, and which
stayed stale while their neighbours moved" is a query rather than a guess.

Step 1 is collection. Nothing here detects anything; it records what both books
were showing, hour after hour, in a form you can read by hand.

```bash
cd prematch
python -m sportsdatamovement sports                 # what's on the boards now
python -m sportsdatamovement snapshot --max-events 2   # smoke run
python -m sportsdatamovement loop                   # the real thing
```

---

## What gets collected

Everything. No allowlist, no classifier, no normalisation — the books' own
market names and their own selection labels, stored verbatim.

That is a deliberate departure from the dashboard's scrapers next door, which
read 8 of Lider's ~385 markets per match and a dozen of CrystalBet's ~525. They
have to: they feed a matcher that prices one book against another, and a market
read as the wrong kind invents a line nobody quoted. Here nothing is
interpreted, so nothing can be misinterpreted — and the whole point is to
compare a market against its neighbours, which a classifier would have thrown
away before we ever saw them.

Measured over full passes, 2026-08-26, with player markets excluded:

| | sports | events | positions | pass time | transport |
|---|---|---|---|---|---|
| Lider-Bet | 24 | 2,516 | 1.34 M | ~2 min | 0.5 GB |
| CrystalBet | 33 | 3,557 | 1.23 M | ~11 min | 1.4 GB |
| **both** | **57** | **6,073** | **2.57 M** | **~11 min** | **1.9 GB** |

Soccer dominates both: 1,805 CrystalBet games (1.06 M positions, 640 s, 919 MB)
and 1,444 Lider matches (1.26 M positions, **78 s**, 357 MB). The books run
concurrently, so the pass costs what CrystalBet costs — Lider is essentially
free, because its whole board arrives in 143 JSON calls while CrystalBet needs
two HTML round trips per game.

A full pass leaves the database at **235 MB**, or ~93 bytes per stored change.

---

## Player markets are dropped

Anytime goalscorer, shots, assists, cards and the combinations built on them are
**not collected**. `--keep-player-markets` brings them back.

They were half the board — 52 % of CrystalBet's soccer positions and 33 % of
Lider's — and they were also the entire cause of the dimension tables being
unusable. Purging them from a full pass:

| | before | after |
|---|---|---|
| positions | 4,368,032 | **2,574,225** |
| markets | 122,170 | **10,194** |
| sides | 113,793 | **4,621** |

`markets` going from 122 k rows to 10 k is what makes "how does *Halftime/
Fulltime* behave across every game" a question the schema can answer at all.

Lider says so structurally — the specifier carries a `player` key or an
`sr:player:NNNN` value — and that is used where available. CrystalBet says
nothing structurally, so a player is recognised by the "Last, First" convention
both books write people in. The pattern has to survive what real boards contain:

    Welbeck, D                     initial-only first name       -> player
    Bughail-mellor, D Mani         hyphen, two given names       -> player
    Assists Chust, Víctor (Elche)  accented, mid-title           -> player
    0:1, 0:2 or 0:3                a Multiscores selection       -> KEEP
    Team 1 win or 0-0, 0-1         a scoreline list              -> KEEP

The discriminator is what sits immediately before the comma — a letter for a
person, a digit for a scoreline. Matching on `", "` alone would take every
correct-score market out of the study.

Already-collected player history can be removed in place, without losing the
passes around it:

```bash
python -m sportsdatamovement purge-players    # then VACUUM to reclaim the file
```

## The dashboard

```bash
python -m sportsdatamovement serve      # http://127.0.0.1:8100
```

Four views, all filtered by book/sport and all sortable by clicking a column:

- **movers** — every price change with what it moved *from*, ranked by how far.
  `odds` stores only the new price, so the old one is recovered by looking back
  for that position's previous row. Click a row for its full price history.
- **events** — events ranked by how much of their board moved. Click one for its
  **move grid**: a row per position, a column per hourly pass, a filled cell
  where the price changed — green up, red down, blank unchanged, grey not
  offered. This is the view the project exists for: the main result moving while
  HT/FT 1/1 stays blank for six hours is visible without trusting a statistic.
- **markets** — moves per position per pass, by market type. Sort *ascending* to
  find the markets that never move.
- **passes** — the collector's own log, including `dupes`, which should be zero.

Charts are inline SVG with no libraries, and the price chart draws a **step**
line rather than a curve: between two ticks the price is *known* to have held,
which is the premise the whole store rests on.

## Why the store is a change log

One snapshot of both boards is **2.57 million positions** (4.43 M before player
markets were dropped). At 24 snapshots a day that is 62 million rows/day, and
there is no row encoding that survives it — the last project that tried
produced a 32 GB database in three days.

So a row is written **only when a price differs from the last price recorded for
that position**. The `snapshots` table is the heartbeat that makes this
lossless: it records that a board was read in full at time T, which is what
licenses the inference "no tick means unchanged". Together they reconstruct any
snapshot exactly:

```bash
python -m sportsdatamovement board --book crystalbet --sport soccer --out board.csv
```

That is also, conveniently, the product. A change-only table **is** the movement
log — `WHERE snapshot_id = ?` is "what moved this hour", and its complement
within an event is "what didn't".

### What actually moves

Measured across a 27-minute gap between two passes, player markets excluded:

| board | positions | moved | rate |
|---|---|---|---|
| crystalbet / tennis | 14,907 | 1,595 | **10.7 %** |
| liderbet / tennis | 6,977 | 627 | 9.0 % |
| crystalbet / basketball | 49,744 | 4,100 | 8.2 % |
| liderbet / soccer | 1,255,186 | 33,368 | 2.7 % |
| crystalbet / soccer | 1,058,269 | 19,741 | 1.9 % |
| crystalbet / icehockey | 21,764 | 185 | **0.9 %** |

Tennis and basketball reprice five times as often as soccer, and ice hockey
barely at all. Soccer is 87 % of the positions and among the *least* volatile,
which is why a change log costs so little: a pass writes ~66 k rows against
2.57 M positions read.

### Schema

```
events      book, sport, event_key, league, home, away, start_time, first/last_seen
markets     book, market_key, name              -- interned
sides       label                               -- interned
positions   event_id, market_id, side_id, line, ref
snapshots   book, sport, started_at, ok, n_events/n_positions/n_new/n_moved/
            n_gone/n_dupes, bytes, dur_ms, error
odds        position_id, snapshot_id, price     -- WITHOUT ROWID, milli-odds
latest      position_id, price, snapshot_id
```

`movements` is a view giving the flat shape the study asked for — sportsbook,
sport, league, position, side, record_time — over the rows that actually moved.

Prices are `INTEGER` milli-odds (1.85 → 1850): SQLite varints make that two
bytes where a `REAL` is eight. `NULL` means **not bettable at this instant**,
covering both suspension and leaving the board.

---

## Five traps, all of them measured

Each of these was found by running the collector and disbelieving the output.
Every one is pinned by a test.

**1. A first-ever pass reported 1,856 price moves out of 5,571 positions.**
A first pass cannot contain a move. Lider's `European Handicap` carries
`{"special": "(0:1)", "hcp": "0:1"}` — `hcp` is a *string*, so pulling a number
out of it fails and all four rungs collapsed onto one key, with the last one
written winning at a price belonging to a different handicap. The fix keeps the
**whole** specifier as the identity, so an unrecognised key widens the position
rather than being dropped. It was also destroying data: fixing it took the same
board from 5,571 positions to **9,009**.

**2. CrystalBet renders 20 cells per soccer game with duplicate labels.**
The cells of one market sit in visual groups separated by `<div class='clear'>`,
and the group is sometimes the only thing telling two bets apart:

- *Exact number of goals* is three ladders of different depth (`0..5+`,
  `0..6+`, `0..9`) under one title. The overlapping rungs carry identical
  prices, so this is the same bet quoted three times — which makes it a free
  internal control: if CrystalBet ever moves one ladder and not the others,
  that is staleness inside a single book.
- *Halftime/Fulltime and Total* is four groups, one per total line, and the
  book's own labels are unreliable — `1/1 & Over  1.5` (double space),
  `X/1 & under 1.5` (lowercase), `2/1& Over1.5` (no spaces) and, four times
  over, `2/X & Over` with **no line at all**. For those the group ordinal is
  the only discriminator that exists.

Groups after the first get a `#2`, `#3` suffix on the market name. CrystalBet
emits no header for them, so what `#2` *means* is not in the markup.

**3. A DOM-shaped walk of CrystalBet's detail table finds half the board.**
The `<tr>` tags are never closed, so pairing `td1/td2` then `td3/td4` silently
drops the right-hand column: 263 markets found of 526 present. Splitting on
`<tr id=` is both correct and 15× faster than BeautifulSoup (17 ms vs 200–280 ms
on a 1.7 MB expand).

Its `<tr id>` is not a market key either — it is allocated per game, so keying
on it turned `markets` into one row per (game, market): 56,460 rows after a
single pass, growing without bound as fixtures churn, and no way to ask how one
market behaves across games. Keying on the name instead: **4,707 rows**.

**4. A locked CrystalBet cell displays 1.01.** Stored literally, every
suspension becomes a crash to 1.01 and every release a recovery. The CSS class
says it is locked; the price does not.

**5. A failed expand would have nulled that game's entire board.** Several
percent of expands fail when CrystalBet bounces a session mid-pass, and nulling
~4,900 positions per failure means thousands of phantom departures followed by
an equal number of returns — in the exact table the study reads. Disappearance
is now decided at two granularities: a position missing from an event we *read
in full* really was pulled; an event missing from a *complete list read* has
left the board; anything else is left untouched. **Not read is not gone.**

**6. Names arrive as templates, and unfilled they are all the same string.**
Lider ships `1X2 / Anytime goalscorer {lb_br_player}` with the value in the
specifier next to the line (`"lb_br_player": "Donovan, Colby"`). Left alone,
every goalscorer market on an event displays identically and eight players'
prices cannot be told apart by eye — which defeats the point of storing the
book's own names. Placeholders are filled in, except the one that became
`positions.line`: `Total {total}` stays a template so `markets` holds one row
per market rather than one per rung.

**7. Two collectors against one database destroy the data silently.**
Each pass reads a `latest` the other has just updated, so both attribute the
other's movement to themselves and neither change count means anything. This
happened here: a `pkill -f "python -m sportsdatamovement"` missed a process
whose argv reads `/…/Python -m sportsdatamovement` — capital P — and three
hours of change data had to be thrown away. `snapshot` and `loop` now take an
flock beside the database and refuse to start if another collector holds it.

### And one that looks like a trap but is the opposite

CrystalBet publishes a stable internal selection id (`<div id='S45217357861'>`),
and it is genuinely stable — 4,615 of 4,615 survived a 20-minute gap. Keying on
it would still be wrong: 30 of them had a **different line** hanging off them
afterwards ("21.5 Und" became "22.5 Und"), so a line slide would have been
booked as a price move on a line the book no longer offers. The label is the
key; the id is kept in `positions.ref` as a breadcrumb.

---

## Cadence

`loop` schedules from the **start** of the previous pass — sleep until
`started + interval` — so quick passes stay on an hourly grid. If a pass
overruns the interval, the next one starts after `--min-gap` (default 5 min)
instead of immediately: that is the anti-runaway, and `dur_ms` on every snapshot
row makes the overrun visible rather than silent.

```bash
python -m sportsdatamovement loop --interval 3600 --min-gap 300 --prune-days 14
```

Both books run concurrently (different hosts, no contention). Within
CrystalBet, `--cb-concurrency` sports run at once — CrystalBet serialises
concurrent postbacks inside one ASP.NET session, so the only real parallelism is
across sessions.

Simulated / "Simulated Reality League" shelves are **skipped by default**: they
reprice every few minutes by construction and would swamp the log with the one
kind of movement that carries no information. `--include-simulated` keeps them.

---

## Reading it back

```bash
python -m sportsdatamovement passes                     # per-pass diff counts
python -m sportsdatamovement movements --sport soccer    # what moved, newest first
python -m sportsdatamovement event "AEK Athens"          # one event, every position,
                                                         # sorted by when it last moved
python -m sportsdatamovement board --book liderbet --sport soccer --out board.csv
python -m sportsdatamovement export --out movements.csv
python -m sportsdatamovement stats
```

`event` is the hand-inspection view the study is for: sort by last-move and the
markets that followed a price change sit apart from the ones that did not.

Watch `n_dupes` in `passes`. It counts two rows of one pass claiming the same
position — always a collector key bug, never movement — and it should be zero.

---

## Where this goes next

Step 1 collects; nothing here decides anything. In rough order of value:

**1. The follow-rate matrix.** For each sport, for each pair of market names,
`P(B moved this pass | A moved this pass)` over every event. That is one scan of
`odds` grouped by snapshot and event, and it directly answers the question the
project was built for: which markets track their primitives and which sit still.
An earlier hand-run of this shape on Lider found 6-way grids repricing with their
legs 98.5–100 % of the time while 2-way Yes/No families followed no more often
than their own baseline (~13 %) — every arb found back then was in the second
group. This makes that measurement systematic and cross-book.

**2. Staleness age, normalised.** Per position, passes since it last moved,
against how often its own event's main markets moved in the same window. A
market that has not moved in twelve passes while its 1X2 moved six times is the
target; one that has not moved because nothing on the event moved is not.

**3. Anchor to price, not just to movement.** "Moved" is binary and a market can
move the wrong way. Devig the main result and the main total per event per pass,
take the implied-probability delta, and ask whether each derived market repriced
consistently with it. This is where an edge actually lives, and `src/soccer_model.py`
already fits a Poisson score matrix to exactly those inputs.

**4. A fast lane near kickoff.** An hour is the right cadence for "does this ever
move" and far too coarse for "who moved first". Events inside a few hours of
kickoff are a small slice of the board and could be swept every 10–15 minutes
without materially changing the cost.

**5. Cross-book lead/lag.** Same event on both books: when Lider moves and
CrystalBet does not, who is late? Lider carries SportRadar match ids and
CrystalBet does not, so this needs the name matcher in `src/matcher.py`.

Two costs worth knowing before they bite. CrystalBet spends **two** round trips
per game (`ExpandDetail`, then `CollapseDetail`), each returning the whole
re-rendered panel — 1.4 GB a pass. `docs/performance.md` measured the fix: keep
each session's view to 1–2 championships and an expand drops from ~1.5 MB to
~350 KB. And parallelism, if the board grows: CrystalBet serialises postbacks
inside one ASP.NET session, so soccer is only as fast as its single session
(~2.2 games/s measured). More sessions for one sport is the lever, and the
transport already supports it — it is simply not needed at 14 minutes a pass.

One open wart: CrystalBet's `#2`/`#3` market suffixes. They separate the cell
groups correctly, but the book emits no header saying what a group *is*, so
"Exact number of goals #2" cannot be labelled from the markup alone.

## Housekeeping

Events churn: ~2,000 fixtures arrive daily and as many kick off and vanish.
`prune` drops events not seen for N days and everything hanging off them;
`loop --prune-days 14` does it every pass.

The database lives at `sportsdatamovement/data/snapshots.db` (gitignored).
Override with `--db` or `SDM_DB_PATH`.

The honest cost is transport, not disk: a CrystalBet expand re-renders the whole
games panel, so each of 4,242 expands drags back ~1.5 MB of markup for one
game's markets. `docs/performance.md` describes the fix — keep each session's
view to 1–2 championships and each expand drops to ~350 KB — and it is the first
optimisation to reach for if the bandwidth bites.
