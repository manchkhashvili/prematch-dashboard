# American football — the seven-book reference

Phase 3.2, added **2026-08-12**. Fourth sport on the dashboard, after
basketball, soccer and tennis.

Everything below was surveyed against the **whole live board**, not a sample:
all 177 in-scope CrystalBet games plus every one of their detail pages
(39 distinct market titles), and a full sport-id / market-code census on each
of the other six books. That discipline is the point — the tennis pass in
August 2026 shipped a classifier that covered 29 % of the board because it was
written off a 30-match sample, and every trap listed here was invisible at
sample size.

---

## 1. Sport ids

| Book | Identifier | Board size (2026-08-12) | Notes |
|---|---|---|---|
| CrystalBet | `sport_id = 27` | 177 games | nav label "American Football" |
| Pinnacle | `sport_id = 15` | 81 real matchups (+311 specials) | Pinnacle calls it **"Football"**; soccer is "Soccer" (29) |
| 1xbet | `sport = 13` | 130 events / 6 champs | |
| Lider-Bet | `section = "s:34"` | 53 matches / 3 tournaments | |
| Betlive | `sportId = 15` | 96 events / 5 leagues | coincidentally the same number as Pinnacle |
| Crocobet | `sportId = 16` | 122 events | Georgian `ამ. ფეხბურთი` |
| Setanta | `sport_code = "AF"` | 176 events / 6 tournaments | two letters — every other sport is one |

Leagues are the same four everywhere: NFL, NFL Preseason, NCAA, CFL.

---

## 2. CrystalBet

### 2.1 List view — byte-identical to basketball

All 177 containers shipped Format B with exactly 8 cols, and the loadinfo
carries basketball's 8-entry layout:

```
[0] '1'         handicap=''          ML home
[1] '2'         handicap=''          ML away
[2] '1'         handicap=''          AH home
[3] 'Handicap'  handicap='handicap'  AH line landmark  '-4.0 +4.0'
[4] '2'         handicap=''          AH away
[5] 'Und'       handicap=''          OU under
[6] 'Tot'       handicap='total'     OU line landmark  '44.0'
[7] 'Over'      handicap=''          OU over
```

Two cosmetic differences from basketball, neither of which the parser cares
about:

* entry `[1]` is a bare `"2"` — basketball and soccer ship `"\t2"` with a
  tab/space prefix. `_identify_loadinfo_roles` resolves ML-vs-AH away by
  **position** relative to the handicap landmark (everything before `ah_home`
  is moneyline), so the missing prefix changes nothing.
* the OU landmark is named `'Tot'` (basketball `'Point'`, tennis `'Game'`).
  Only the `handicap='total'` flag is read.

So `sports/americanfootball.py` delegates both list parsers to basketball,
exactly as tennis does.

The list-view moneyline is the **incl-OT 2-way** price — the same number the
detail page serves as "Winner (incl. overtime)", checked cell-for-cell
(Seattle–New England: list 1.45/2.40, detail 1.45/2.40). The 3-way regulation
result never appears on the list view.

### 2.2 Detail page — the full title census

Counts are events carrying the title, out of 177.

| n | % | Title | → |
|---|---|---|---|
| 169 | 95.5 | `Winner (incl. overtime)` | moneyline FT |
| 169 | 95.5 | `Handicap (incl. overtime)*` | spread FT |
| 141 | 79.7 | `Total Points(incl. overtime)*` | total FT |
| 46 | 26.0 | `Main result` | **3-way regulation** — permissive path only |
| 29 | 16.4 | `HomeTeam Total (incl. overtime)*` | team_total FT home |
| 29 | 16.4 | `AwayTeam Total (incl. overtime)*` | team_total FT away |
| 14 | 7.9 | `Odd/even`, `Odd/Even (incl. overtime)*` | skip |
| 13 | 7.3 | `Will there be overtime*` | skip |
| 10 | 5.6 | `Home Team odd/even`, `Away Team odd/even` | skip |
| 4 | 2.3 | `Halftime/fulltime` | htft FT — permissive path only |
| 4 | 2.3 | `1st Half Result*` | 3-way regulation H1 — permissive only |
| 4 | 2.3 | `1st Half - Draw No Bet` / `- Handicap` / `- Total Points*` | H1 |
| 4 | 2.3 | five `2nd Half - …` variants | H2 (no Period slot) |
| 4 | 2.3 | `1st Quarter - Draw No Bet` … `4th. Quarter - Draw No Bet` | moneyline Q1–Q4 |
| 4 | 2.3 | `1st Quarter - Total Points`, `2 quarter - total`, … | total Q1–Q4 |
| 4 | 2.3 | `1st. Quarter - Handicap`, `2 quarter - handicap`, … | spread Q1–Q4 |
| 4 | 2.3 | `Winning margin`, `Winner (including OT) & Total (…)` | skip |
| 1 | 0.6 | `Handicap (including OT) & Total (including OT) -4.5/63.5` ×4 | skip |

617 of 744 title instances land on a market; the 127 skipped are genuinely
unrepresentable shapes plus the three permissive-only ones.

### 2.3 Four traps

**(a) The missing space.** `Total Points(incl. overtime)` ships with no space
before the paren. A pattern written as `total points \(` drops 141 events.
Basketball's `[\s(]+` already tolerates it; ours keeps that.

**(b) Two quarter spellings on the SAME page.** CB serves

```
1st Quarter - Total Points     ordinal, "Points"
2 quarter - total              bare digit, no "Points"
```

Basketball's rules only know the ordinal form, so 2/3/4-quarter ladders fell
through the strict path — and worse, basketball's permissive `_derive_period`
matches `\b(1st|first|1)\b.*quarter` for Q1 but `\b(2nd|second)\b` for Q2, so
a bare `"2 quarter - total"` derives period **FT**. Its ~17-point rungs then
interleave with the ~45-point full-game total ladder and the monotonicity
detector reports an anomaly on every rung. `americanfootball.py` therefore
carries its own period deriver that accepts bare digits for all four quarters.

**(c) Regulation vs incl-OT.** American football can be tied at the end of
regulation, and CB prices that leg (13.2 on Seattle–New England, ≈5 % after
devig — which matches the ~6-7 % of NFL games that actually reach overtime).
Pinnacle's AF moneyline is 2-way incl-OT: 217 entries on the live board, not
one carrying a `draw` designation. So `Main result` must never become the
matched moneyline, or a regulation price would be scored against an incl-OT
one. It is emitted on the **permissive path only**, where it feeds
`ot_vs_regulation` (§4).

**(d) H2 has no Period slot,** same as basketball. The rules still classify it
so the anomaly scanner groups those ladders separately instead of dumping them
into FT; nothing downstream matches an H2 row because Pinnacle's `PERIOD_MAP`
stops at H1.

---

## 3. The other six books

Every one of them is a **near copy of another sport's mapping with exactly one
substitution**. Copying a table wholesale silently drops a market — no
exception, no log line, just fewer rows.

### Pinnacle (15)
`moneyline` / `spread` / `total` / `team_total`, periods 0 (FT) and 1 (H1)
only — 1518 period-0 entries and 36 period-1 on the live board, nothing else.
No quarters. Zero `parentId` entries, so the corners/games child-folding path
is inert. **team_total is in scope** (unlike basketball): Pinnacle ships 136
`side=home/away` entries and CB prices the counterpart on 16 % of games, so
both legs of the pair exist and the rows are matchable rather than phantom.

### 1xbet (13)
Basketball's codes exactly — `G=101 T401/402` moneyline incl-OT, `G=2 T7/T8`
spread, `G=17 T9/T10` total, `G=15 T11/12` home team total, `G=62 T13/14` away.
Sub-game panels are basketball's too: `1 Half`, `1st`–`4th quarter`, all with
empty `TG`.

**But the horizon default made it emit nothing.** `HORIZON_HOURS=36` assumes a
board that always has games today; AF plays weekly slots, so on 2026-08-12 all
130 enumerated events sat beyond the window and 1xbet contributed 0 rows.
`HORIZON_HOURS_BY_SPORT["americanfootball"] = 240` fixes it, and the board is
small enough (~130 `GetGameZip` calls) that a wide horizon is cheap.

### Lider-Bet (s:34)
Ships `Winner (OT)` / `Handicap (OT)` / `Total (OT)`, all three already in
`_classify_market`'s incl-overtime aliases. The section id was the entire
change.

### Betlive (15)
The curated `getLeagueEvents` tier prices exactly one usable market for AF and
it is named **`Match Winner (12)`** — the column pair is part of the name.
Without that alias AF emitted 0 rows from 96 events. The only other named
market is a sub-period `1st Half Total`, which the existing `_SUBPERIOD` guard
drops. So Betlive contributes moneyline only (53 rows), which is still a book
on the cross-book ML comparison for two cheap GETs.

### Crocobet (16)
Three of five codes are basketball's, **but the total is not**:

| gameType | market | Georgian name |
|---|---|---|
| `-2527` | moneyline FT (OT) | `1 2 ∪ ① (OT)` |
| `-2950` | spread FT (OT) | `ფორა -7.5 / +7.5 (OT)` |
| **`-30172`** | **total FT (OT)** | `ქულების რაოდენობა 37.5 (OT)` |
| `182` / `183` | team_total home / away | `I / II გუნდის ქულების რ-ბა (OT)` |
| `1` | 3-way regulation result | `ძირითადი შედეგი` (not emitted) |

Basketball's total is `-2966` and it does not appear on the AF board at all.
Copying basketball's table would have produced moneyline + spread + team totals
and **no totals** — 628 of the 1030 live rows.

### Setanta (AF)
`mt 1` moneyline 2-way (0=home, 3=away) — **tennis's code**, not basketball's
145. `mt 5` total, `mt 4` spread, `mt 7` team total.

`mt 2` *does* exist on AF, at periods 1–4 and 4010, where it is the **3-way
regulation result for that period**. Mistaking it for the moneyline would pair
a regulation price against Pinnacle's incl-OT one.

Periods follow **basketball's** convention, not soccer's:
`0=FT, 4010=H1, 1=Q1 … 4=Q4`. Confirmed by the lines themselves — period 4010
totals sit at 28.5 (a half), period 1 totals at 10.5 (a quarter). Soccer's map,
where `1` means H1, would have priced a quarter as a half.

---

## 4. Price verification

Every mapping above was verified the way the house rule requires — by devigged
price agreement against Pinnacle on live matched fixtures, not by reading a
label. Median absolute gap in percentage points:

| Book | matched events | moneyline | spread | total | team_total |
|---|---|---|---|---|---|
| CrystalBet | 77 | 0.80 | 1.29 | 1.06 | 1.06 |
| Lider-Bet | 34 | 0.89 | 1.22 | 1.02 | — |
| Betlive | 36 | 0.82 | — | — | — |
| Crocobet | 10 | 0.59 | 1.28 | 1.34 | — |
| Setanta | 75 | 0.68 | 1.03 | 0.90 | — |

Worst single observation across all books and market types: 5.65pp. For
comparison the basketball mappings were accepted at 0.4–2.8pp and tennis at
0.99–1.45pp medians.

---

## 5. Matching

CB appends the **mascot** to the school (`Rutgers Scarlet Knights`) where
Pinnacle uses the bare school (`Rutgers`). That alone needs no alias —
`token_set_ratio` scores the pair 100 because the Pinnacle side is a subset.

Two things do break the join, and both are handled in `team_aliases.yaml`:

1. the school is an initialism or a different word (`Umass Minutemen` vs
   `Massachusetts` scores **28.6**, `Fiu Panthers` vs `Florida International`,
   `St. Wolfpack` vs `NC State`);
2. CB has a typo (`Tusla Golden Hurricane`, `Lousville Cardinals`,
   `Stenford Cardinal`, `Norh Dakota State Bison`).

Each alias was confirmed by its **opponent** matching in the same fixture.
NCAA teams Pinnacle does not currently carry are deliberately not aliased —
that is a coverage gap, not a naming one.

Betlive still carries `Edmonton Eskimos`, the CFL team's pre-2021 name.

**The collision that turned out not to be real.** College mascots overlap NFL
nicknames badly on single names — `Miami Hurricanes` vs `Miami Dolphins`
scores 66.7, `Fresno State Bulldogs` vs `NC State` 76.9, both above the
`SCORE_TIGHT` floor of 65. It looked like AF would need a league-family guard.
Measured instead: **zero cross-family name pairs clear SCORE_TIGHT**, because
the matcher requires *both* sides to score and a coincidental one-side
collision never pairs with a second one. Of 177 CB events, 0 accepted matches
crossed a league family. No guard was added.

Of the 100 CB events with no Pinnacle counterpart, only 12 scored ≥ 60 — the
other 88 are simply not on Pinnacle's board yet (81 events to CB's 177;
Pinnacle opens later NFL weeks and most of NCAA closer to kickoff).

---

## 6. What American football unlocks

**`ot_vs_regulation` (new).** CB posts the same period twice — a regulation
3-way and an incl-OT 2-way — and they are tied by an identity with no model in
it:

```
P(win incl OT) = P(win in regulation) + P(tie) · P(win the overtime | tie)
```

The conditional lives in [0, 1], so the incl-OT probability is boxed:

```
P(win reg)  ≤  P(win incl OT)  ≤  P(win reg) + P(tie)
```

Stepping outside is not an aggressive price, it is an impossible one.
Calibrated over all 50 (event, period) pairs on the live board that post both
markets: floor residual p90 +0.24 / max +4.26, ceiling residual p90 +0.95 /
max +5.08, inside a box only ~3.6pp wide. `OT_BOX_PP = 3.0` flagged 3 of the
50, all three inspected by hand and real — e.g. Jacksonville–Cleveland,
regulation 1.35/12.8/3.15 (68 % + 5 % tie) against incl-OT 1.19/3.55 (78 %),
5pp above the arithmetic ceiling.

*Caveat before retuning:* residual size depends on the devig model. `src.vig`
uses a power devig, which pushes more vig onto the longshot tie leg than a
proportional one; proportional devigging shrinks the same three cases by
1–2pp. 3.0 is calibrated against the power devig the rest of the system uses.

**`ml_vs_spread` (revived).** This check had never fired in 4606 historical
flags. Soccer has the handicap side but no 2-way ML (its 1X2 is 3-way and Draw
No Bet is skipped); basketball has the ML but never a line-0 pick'em rung. AF
has **both**: 169 of 177 events carry a 2-way ML and 71 carry a 0.0 spread
rung, so the check runs on 71 events and produced its first real flag.

---

## 7. The 7-day horizon changes this board most

Added right after this pass (owner: *"I don't need any data later than 7 days to
be pulled, from any book"*). `MAX_START_DAYS = 7`, `src/horizon.py`, live on the
Config tab as `limits.max_start_days`.

American football is the sport it bites hardest, because it is the only one
whose board is mostly weeks out. Measured 2026-08-12, same fetch, cap on:

| book | before | after |
|---|---|---|
| CrystalBet | 12 921 rows / 177 events | **954 / 20** |
| Pinnacle | 1 243 / 81 | 152 / 20 |
| 1xbet | 493 / 24 | 614 / 24 |
| Lider-Bet | 4 657 / 53 | 598 / 20 |
| Betlive | 53 / 53 | 20 / 20 |
| Crocobet | 1 031 / 53 | 206 / 20 |
| Setanta | 1 199 / 173 | 140 / 20 |

Every book converges on the same 20 fixtures (1xbet's 24 are real extras — it
carries Arena Football IFL and Finland's Vaahteraliiga, which the others don't).
For contrast, the cap keeps **100 %** of Pinnacle's basketball and tennis
matchups and 89 % of soccer: those boards are already inside a week. AF was the
sport that needed it.

The 1xbet AF override stays at 240 h deliberately — `horizon.capped_hours()`
takes the tighter of the two, so it runs at 168 h today and would go back to
meaning what it says if the cap were ever raised.

---

## 8. Enabling it

```
SPORTS=basketball:full,soccer:list,tennis:list,americanfootball:full
```

Full mode is affordable here: the board is ~180 games (tennis is 500+), and a
whole-board detail sweep measured **79 s / 177 games** over the browser-free
transport.

`ANOMALY_EXTRA_SPORTS` now defaults to `soccer,tennis,americanfootball`. The
extra scan's 12 h horizon exists because a full soccer sweep is ~14 min and
never completed a pass; AF is the opposite shape, so
`ANOMALY_EXTRA_HORIZON_H_BY_SPORT["americanfootball"] = 240` — a 12 h window
would scan an empty board most days and never see the `Main result` 3-way that
`ot_vs_regulation` needs.

---

## 9. One shared bug this pass exposed

`cb_detail` keyed variant dedup on
`(period, market_type, line, submarket, team_side)`, so a 2-way and a 3-way
moneyline on the same period collided and whichever the page rendered first
silently deleted the other. That starved `ot_vs_regulation` of one of its two
inputs by construction — and it was **already** doing the same to basketball,
where CB posts `Full Time Result(1X2)*` alongside `Winner (incl. overtime)` and
`htft_combo` is written to consume the regulation legs.

Selection count is now part of the key. The strict (+EV) classifiers never emit
both shapes for one period, so only the permissive/anomaly path changes.
