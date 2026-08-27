# Ice hockey — the sport where regulation is not full time

Phase 3.3, added **2026-08-27/28**. Fifth sport on the dashboard, after
basketball, soccer, tennis and American football.

Everything below was measured against the whole board — 349 CrystalBet events
with full detail pages from the collected history, every Pinnacle hockey
league, and a live market census on each of the other five books. No sampling.

---

## 1. Why ice hockey, and not table tennis

The brief was "whichever sport is best covered on CrystalBet and Pinnacle".
Measured 2026-08-27:

| candidate sport | CrystalBet | Pinnacle | verdict |
|---|---|---|---|
| **ice hockey** | **345 events** | **47 matchups (id 19)** | **first** |
| table tennis | 893 events | **0** | CB's second-biggest board, and Pinnacle prices none of it |
| baseball | 102 | 27 (id 3) | next |
| volleyball | 34 | 12 (id 34) | after that |
| esoccer | 26 | — | esports, excluded by `LEAGUE_SKIP` |

Table tennis is the trap in that table. It is the largest uncovered CB board by
a factor of two and it is worthless as a Pinnacle-referenced sport, because
Pinnacle ships **zero** prematch table-tennis matchups. It remains a candidate
for a book-vs-book comparison (Lider-Bet carries 385 games), which is a
different pipeline from the one this pass extends.

---

## 2. The one thing that decides the whole sport

Hockey is the first sport here where a book prices the same game twice — once
for the 60 minutes of **regulation**, once for the game as it settles
**including overtime and the shootout** — and where both are high volume on
both books.

```
CrystalBet   "Main result"                    3-way   regulation
             "Winner (incl. overtime and penalties)"  2-way   incl-OT
             "Total Goals*"                           regulation
             "Total Goals(incl. overtime and penalties)"  incl-OT

Pinnacle     period 6   3-way ML, spread, total, team_total   REGULATION
             period 0   2-way ML only                         incl-OT
             period 1   1st period (no period 2 or 3 exists prematch)
```

Getting this backwards drops nothing and raises nothing. It silently scores a
60-minute total against a full-game total on **every event on the board**, and
reports the overtime goals as edge. It is the most dangerous kind of mapping
error, so it was verified three independent ways before any code was written.

### 2.1 The overtime box

You cannot win in overtime without first drawing in regulation. So

```
P(win reg)  ≤  P(win incl OT)  ≤  P(win reg) + P(tie)
```

and the implied conditional `P(win in OT | tied after 60)` must land near a
coin flip. Run on every book that prices both moneylines:

| book | (event, side) pairs | impossible | implied P(win OT \| tie) |
|---|---|---|---|
| Pinnacle | 64 | **0** | p10 0.433 · **median 0.502** · p90 0.567 |
| CrystalBet | 170 | **0** | p10 0.477 · **median 0.500** · p90 0.524 |
| Setanta | 86 | **0** | p10 0.484 · **median 0.500** · p90 0.516 |
| 1xbet | 54 | **0** | p10 0.443 · **median 0.500** · p90 0.557 |

A median of 0.500 for "who wins the overtime" is what the conditional has to
be. No wrong period assignment produces one by accident.

### 2.2 The two goal ladders, compared at identical lines

The ladder steps in half-goals, which cannot resolve the ~0.2 goals overtime
adds — 30 of 49 events tied on a naive pivot comparison. Comparing the two
ladders **at the same line** removes the granularity entirely:

| | n | median | positive |
|---|---|---|---|
| totals: `P_inclOT(over) − P_reg(over)` | 366 rungs | **+0.024** | 60 %, negative on none |
| the same, mid-ladder (4.5–6.5) | — | **+0.03 … +0.06** | — |
| team totals | 888 rungs | **+0.017** | **868 (98 %)** |

### 2.3 The puck line is exempt, and by arithmetic

CrystalBet serves `Handicap` and `Handicap (incl. overtime and penalties)*`
with byte-identical prices. That is not laziness — overtime is sudden death and
is only ever reached from a **tie**, so the overtime goal always produces a
one-goal win. Winning by two or more including overtime **is** winning by two
or more in regulation.

Measured: identical on **300 of 300 rungs**, at every |line| CB offers (1.5,
2.5, 3.5, 4.5, 5.5, 6.5, 7.5). CB ships no ±0.5 hockey handicap, which is the
only line where the two would diverge. Both ladders are therefore emitted at
`REG` and either may be matched against Pinnacle's period-6 spread.

### 2.4 What it cost the code

`Period` gained **`REG`** (the 60 minutes) and **`P1`–`P3`**. `FT` keeps its
meaning everywhere — the game as it settles.

Pinnacle's period ints stopped being sport-agnostic, so `PERIOD_MAP` gained a
per-sport override. The shared map would have read hockey's period 1 as a
*first half* — a thing hockey does not have — and dropped period 6 for want of
an entry, **discarding 63 % of Pinnacle's hockey board** (129 spreads, 115
totals, 39 three-way moneylines, 26 team totals against period 0's 32
moneylines).

---

## 3. CrystalBet

CB sport_id **18**. 322 games on the live list, 349 in the collected history.

### 3.1 List view — a 12-column layout that is neither basketball's nor soccer's

All 322 containers shipped Format B and **zero** shipped `data-loadinfo`.

```
col0  col1  col2    1X2 regulation      2.30 / 3.85 / 2.35
col3  col4  col5    Double Chance       1.45 / 1.20 / 1.45   SKIP
col6  col7  col8    handicap home / line "-0.5/+0.5" / away
col9  col10 col11   total under / line "6.0" / over
```

Basketball has 8 cols and no draw; soccer has 13 and no handicap. Hockey has
both, so neither parser could be delegated to. `parse_loadinfo` is a documented
no-op rather than a parser written against a format never observed.

### 3.2 Detail page — whole-board title census (349 events)

| ev | title | → |
|---|---|---|
| 129 | `Main result` (3-way) | moneyline **REG** |
| 129 | `Draw No Bet*`, `Double chance` | permissive / skip |
| 89 | `Total Goals*` (11 rungs) | total **REG** |
| 89 | `Handicap` (7 rungs) | spread **REG** |
| 89 | `Handicap(1X2)*` (6 rungs) | permissive only — 3-way |
| 89 | `Home Team Total` / `Away Team total*` (7 rungs) | team_total **REG** |
| 89 | `Correct Score`, `Winning margin`, `Odd/Even*`, `Both Teams to Score*`, `Matchbet and Total goals*` | skip |
| 85 | `Winner (incl. overtime and penalties)` | moneyline **FT** |
| 53 | `N Period - Draw No Bet*` | moneyline **P1–P3** (2-way) |
| 53 | `Nth Period - 1X2*` | permissive only — 3-way |
| 53 | `Nth Period - Handicap*` | spread **P1–P3** |
| 51 | `Nth Period - Total Goals*` | total **P1–P3** |
| 51 | `N period - Home/Away Team total` | team_total **P1–P3** |
| 49 | `Total Goals(incl. overtime and penalties)` | total **FT** |
| 49 | `Handicap (incl. overtime and penalties)*` | spread **REG** (§2.3) |
| 49 | `Home/Away Team total (incl. …)` | team_total **FT** |
| 32 | `Winner` / `Runner UP` / `Miss Playoffs` / team names | skip — NHL season outrights |

### 3.3 Three traps, all live on one page

**(a) The period prefix is spelled three ways.** A single NHL detail page
carries `1st Period - 1X2*`, `1 Period - Draw No Bet*` and `1 period - Home
Team total` — ordinal-with-suffix, bare-digit capitalised, bare-digit
lowercase. Anchoring on `1st period` alone drops two thirds of the period
markets, and they then fall through to a period-less default where their
~1.5-goal ladders interleave with the ~5.5-goal full-game one.

**(b) CrystalBet's own typo is load-bearing.** The third-period scoring markets
ship as `2rd Period - Last Team To Score*` — named "2rd", sitting inside the
3rd-period block. Both are skip-markets, so nothing downstream cares today, but
a deriver that trusts the digit files third-period prices under P2. `_split()`
requires the digit and the ordinal suffix to **agree** before trusting either.

**(c) `Draw No Bet` is the 2-way period moneyline.** Pinnacle's period-1 hockey
moneyline is 2-way (33 rows, not one with a draw designation) while CB's
`Nth Period - 1X2*` is 3-way. Emitting the 3-way as the matched moneyline would
score a three-outcome price against a two-outcome one — so DNB is the strict
2-way and the 1X2 goes out permissively, the same resolution basketball uses
for H1. Verified: CB `moneyline P1` against Pinnacle's, **0.84pp median**.

CB's `Draw No Bet*` at full time is regulation-parented, not the incl-OT
moneyline said twice — it tracks `P_reg(home)/(P_reg(home)+P_reg(away))` to a
median **0.77pp** (n=125) against **1.87pp** (n=85) for the incl-OT price.

---

## 4. The other six books

Every one was censused live. Not one is a clean copy of another sport's table.

| book | id | what was different |
|---|---|---|
| Pinnacle | 19 (`Hockey`) | period **6** is regulation; the shared period map drops it |
| 1xbet | **2** | ships **both** moneylines: `G=1 T1/2/3` 3-way regulation, `G=101 T401/402` 2-way incl-OT. Sub-games are `1st/2nd/3rd period`, not halves |
| Lider-Bet | `s:3` | see below — the dangerous one |
| Betlive | **2** | its market is *named* `Fulltime Result` and ships **three** outcomes, so it is regulation. Name says full time, market is the 60 minutes |
| Crocobet | **4** | two codes are **soccer's** (`1` 3-way result, `8` total) because hockey's regulation board has soccer's shape. No `(OT)` suffix anywhere — nothing incl-OT is priced |
| Setanta | **`H`** | files both moneylines under period **0** and only the market type separates them: `mt 2` 3-way regulation, `mt 1` 2-way incl-OT |

### 4.1 Lider-Bet is the one that would have shipped wrong

Lider names hockey's 3-way regulation result **`Result`**, and the shared
name classifier maps the bare word "result" to a **2-way moneyline**. That row
would carry the regulation home/away prices with the tie silently dropped — two
prices summing to about 80 % — and then pair against Pinnacle's period-0
incl-OT moneyline, showing the tie probability as edge on every event.

The selection-shape guard added to `edge.py` **cannot** catch this one: the
shape is right and only the meaning is wrong. So hockey is mapped by stable
`typeId` (`mt:3:*`) and `_names_classified("icehockey")` returns `False` — the
allowlist is the entire mapping and an unlisted market is skipped, not guessed.

Six ids are deliberately left out: `mt:3:699/700`, `743/744`, `793/794` are
**two per period sharing one name** ("1st Period" twice), both 3-way. One of
each pair is presumably the result and the other a double chance; the feed does
not say which and price alone did not separate them. Pinnacle's period
moneyline is 2-way regardless, so there is nothing to pair a 3-way against.

### 4.2 One thing found and deliberately not fixed

Crocobet's `SPORT_ID["tennis"] = 3` points at **baseball** (`ბეისბოლი`);
tennis is really 5 (`ჩოგბურთი`). It has never mattered because
`_GAMETYPE["tennis"]` is empty and the guard in `_fetch_sport_sync` returns
before any request. Left as found and noted in the code — fixing it is a tennis
change, not a hockey one.

---

## 5. Price verification

The house rule: a mapping is accepted on **devigged price agreement against
Pinnacle on live matched fixtures**, never because a label read right. Median
absolute gap in percentage points, 2026-08-27/28.

| book | events | moneyline REG | moneyline FT | spread REG | total REG | P1 markets |
|---|---|---|---|---|---|---|
| CrystalBet | 12 | 1.08 | 0.86 | 1.08 | 0.73 | ML 0.84 · sp 0.67 · tot 1.44 |
| Setanta | 13 | 0.47 | 0.83 | 0.96 | 0.44 | tot 1.52 |
| Crocobet | 8 | 1.24 | — | 0.86 | 0.59 | sp 0.49 · tot 1.12 |
| 1xbet | 7 | 1.01 | 1.25 | 0.63 | 0.27 | tot 0.59 |
| Lider-Bet | 10 | 1.79 | — | 1.13 | 1.13 | sp 2.73 · tot 1.02 |
| Betlive | 9 | 1.18 | — | — | — | — |

Everything lands **0.27–1.79pp**, against 0.6–1.3 for American football and
0.4–2.8 for basketball. The totals are the load-bearing rows: had any book's
full-game total actually been incl-OT while mapped to `REG`, it would sit ~0.2
goals off Pinnacle's regulation line and show a **shifted** median of several
points, which is the signature Lider's rejected half-handicaps showed at 6.16pp.

*(Setanta `spread P1` came in at 4.38pp on n=2 — too few observations to mean
anything either way.)*

---

## 6. What ice hockey unlocks in the anomaly engine — and what it does not

**This is the honest part.** Hockey is the cleanest board on the dashboard, and
that is a finding rather than a gap. Every CB-internal check, run over the whole
collected history:

| check | surface | violations |
|---|---|---|
| ladder monotonicity | **4 866 rungs / 86 events** | **0** |
| overtime box (floor and ceiling) | 85 events pricing both moneylines | **0** |
| incl-OT ladder dominance | **924 shared rungs / 49 events** | **0** |
| coin-flip point estimate | 85 events | max **1.81pp** against an 8.0pp threshold |
| P1+P2+P3 vs REG total | 349 events | **never ran** — see below |

The reason is in that fourth row. **CrystalBet does not price its incl-OT
hockey moneyline independently at all.** It derives it from the regulation 1X2
with a flat even overtime: implied `P(win in OT | tie)` came out p10 0.477,
median 0.500, p90 0.524. Two markets computed from one number cannot contradict
each other, so no CB-internal check can find anything in them.

Two checks were still added — `ot_vs_regulation` extended to hockey's
cross-period shape (`REG` vs `FT`, where AF's version compares within one
period), and a new **`ot_monotone`** (the incl-OT goal ladder must dominate the
regulation one at every line, since overtime only adds goals). Both are exact
identities costing a comparison each, both have unit tests proving they *fire*
on a constructed violation, and the same class of check does fire elsewhere —
AF's `ot_vs_regulation` flagged 3 of 50 pairs on its first live run. Hockey's
board is also 30-of-47 preseason friendlies today and multiplies when the NHL
season opens in late September.

`P1+P2+P3 vs REG` cannot run at all right now: CB publishes exactly **one rung**
on each per-period goal ladder, and a ladder centre needs two rungs bracketing
50 %. 0 of 349 events priced all four ladders.

### 6.1 A threshold bug this pass exposed

`TOTAL_ADD_PTS = 5.0` is a **basketball** number — five points on a ~220-point
total. Applied to hockey's ~5.5-goal total, the three periods would have to sum
to roughly double the regulation line before anything fired. Hockey now has its
own entry in `TOTAL_ADD_PTS_BY_SPORT` (1.0 goal), labelled in the code as
**uncalibrated** — it is scaled off the board's own line spacing, not tuned on
observed violations, because the check has never had a chance to run.

**Soccer sits in the same trap and was deliberately left alone.** Its FT total
is ~2.5 goals, so `H1+H2 vs FT` has been effectively muted by the basketball
constant since it shipped. Fixing that changes what an existing sport puts on
the board and belongs in its own pass.

### 6.2 Where hockey actually pays

Not in CB-internal contradictions — in the cross-book grid. Seven books now
price its regulation market, verified to 0.27–1.79pp.

And there is a structural asymmetry worth knowing: **Pinnacle adjusts the
overtime conditional for team strength and CrystalBet does not.** Pinnacle's
implied `P(win OT | tie)` ranges p10 0.433 to p90 0.567; CB's is pinned at
0.477–0.524. So CB's incl-OT moneyline is systematically wrong on mismatched
teams, by roughly `(conditional − 0.5) × P(tie)` — up to ±1.6pp with a ~23 %
tie probability. That needs no new detector: the ordinary +EV path picks it up
now that the sport is wired.

A second consequence worth remembering when staking: CB's two full-game
moneylines are the same bet priced twice. An edge on one is the same edge on
the other, not a second position.

---

## 7. What the live board looks like today

End-to-end through the real pipeline, 2026-08-28:

```
Pinnacle    193 rows / 13 events        CrystalBet  1 553 rows / 23 events
matched     11 events  ->  417 priced CB legs
            (moneyline REG/FT/P1, spread REG/P1, total REG/P1)
edge        p10 −14.66 %   median −10.41 %   p90 −8.36 %   max −3.63 %
```

Every leg is negative, and that is the expected shape rather than a failure:
CB's hockey vig runs ~11–12 % (a 1X2 of 2.30/3.85/2.35 sums to 1.12), so
against Pinnacle's devigged fair price the median leg is −10 %. **Nothing on
the CB-vs-Pinnacle pair is +EV at this moment** — the board is late-August
preseason, 30 of Pinnacle's 47 matchups are club friendlies, and the 7-day
horizon cuts the NHL slate entirely. The sport is wired, verified and feeding
the grid; whether it pays depends on the board.

---

## 8. Enabling it

```
SPORTS=basketball:full,soccer:list,tennis:list,americanfootball:full,icehockey:full
```

Full mode is required, not optional: the list view carries only the regulation
1X2, handicap and total. The incl-OT moneyline that Pinnacle's period-0 board
matches against — and all three period ladders — exist **only** on the detail
page. List-only mode would leave the sport's defining market unreachable.

`ANOMALY_EXTRA_SPORTS` now defaults to
`soccer,tennis,americanfootball,icehockey`, with
`ANOMALY_EXTRA_HORIZON_H_BY_SPORT["icehockey"] = 72`. Hockey is AF-shaped —
a large board with a cheap detail page — but the deeper an event sits from
kickoff, the more likely CB has published only one of the two full-game
moneylines that `ot_vs_regulation` needs. 72 h keeps a whole weekend slate in
view without reaching for friendlies weeks out.

---

## 9. One shared fix this pass forced

`edge._find_pin_match` ignored the **selection shape**. Until hockey, every
sport agreed with Pinnacle on how many ways a market ran — basketball 2, soccer
3 — so a 3-way could never meet a 2-way. Hockey breaks that in two places at
once: books price a 3-way regulation result alongside a 2-way incl-OT winner,
and Pinnacle's opening-period moneyline is 2-way where several books' is 3-way.

Pairing across that gap fails silently **and in the worse direction**, because
`_fair_pairs` branches on *Pinnacle's* shape: a 3-way CB row meeting a 2-way
Pinnacle row is devigged as 2-way, and the CB regulation home price — which
carries a tie it can lose to — is scored against a fair price with no tie in
it. Every event then shows the tie probability as edge.

`set(p.selections) == set(cb.selections)` is now part of the join key. It costs
nothing where the shapes already agree, and the full suite passed unchanged.
