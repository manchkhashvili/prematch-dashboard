# Volleyball — the first LSport-only sport, and what its board is made of

Added 2026-09-21, the day after the provider split was found (`docs/lsport.md`).
Volleyball runs **only** in the New inconsistencies cycle: no price poll, no
Pinnacle reference (it prices ~12 matchups), no Arbs tab. It is here because
**75 % of CrystalBet's volleyball board is LSport** — the largest LSport share
of any sport — and because a best-of-5 match has the structure that made
tennis's set checks the productive ones: correct score, sets handicap, total
sets, set winners and match winner are all functions of the same six
outcomes.

Everything below was measured on the live board before a line of the sport
module was written, on the 15 LSport games available (22 on the board, 14
LSport, plus one from an earlier snapshot). That is a small board and the
numbers should be re-measured once the cycle has a few weeks of history.

---

## 1. Why this sport, and not the other two

Three uncovered sports carry LSport games. Measured the same evening:

| candidate | LSport games | shape | what the measurement said |
|---|---|---|---|
| **volleyball** | 14 / 22 (75 %) | best-of-5 sets: CS, sets AH, total sets, set winners | derived from the match price (below), **one independent price pair** on the board |
| futsal | 4 / 4 | soccer-shaped (1X2, DC, AH, H1, HT/FT) | soccer engine → 0 flags; H1 result is `1st Period Winner`, which the soccer classifier does not read |
| handball | 4 / 35 | soccer-shaped with DNB and a 0.0 rung | soccer engine → 0 flags on the 4 |

Also measured on the way, on **LSport soccer** (74 games), since Double Chance
is unclassified on every LSport soccer game and the catalog's DC×1X2 zero was
board-wide (91 % other-feed): DC sits within ±2pp of its own legs, 0
dominance violations, cheapest DC+leg cover 1.098; the AH ±0.5 twins within a
few %. Derived there too — recorded so nobody re-measures it.

## 2. What the LSport volleyball feed posts

Coverage over the 15 games (title → classification in
`src/scrapers/sports/volleyball.py`):

| title | games | reads as |
|---|---|---|
| `total points`, `1 Set - Total Points` | 15 | total FT / total H1 |
| `Main result` | 14 | moneyline FT |
| `1st Period Winner` | 14 | moneyline H1 |
| `Point handicap` | 14 | spread FT |
| `Correct Score` (`3:0`…`0:3`) | 12 | correct_score FT |
| `1st Period - Home/Away Team` | 8 | team_total H1 |
| `Asian Handicap Sets` | 8 | spread FT, **submarket `sets`** |
| `Total sets` | 6 | total FT, **submarket `sets`** |
| `2nd Period Winner`, `2 set - …`, `2nd Period - …` | 5–7 | H2 |
| `Total hometeam`, `Away Team` | 6 | team_total FT |
| `3rd Period …`, odd/even, `Race To`, yes/no props | — | unclassified |

The sets markets sit on their own submarket so a −1.5 **sets** rung never
shares a ladder or a period view with a −11.5 **points** rung on the same
(FT, spread). The consistency engine reaches across the boundary explicitly
for the sets identities.

## 3. Three measurements, and what each one ruled in or out

### 3.1 The first-set winner is an inversion of the match price

Invert the devigged match price to a per-set probability under independent
sets (best-of-5: `P = p³(1 + 3q + 6q²)`) and compare with the posted
`1st Period Winner`:

```
n = 13    median 0.0pp    p90 1.1pp    max 1.3pp
hard-order violations (set favourite ≥ 0.60 priced ≥ 3pp above the match): 0
```

The feed derives the set-1 price from the match price, IID. The tennis B8
check is carried over as `vb_set_match` because it costs one comparison and
the bound is exact, but on this feed it is a tripwire, not coverage.

### 3.2 The correct score is a correlated model of the match price

Posted (devigged, proportional) minus the **IID** best-of-5 fair, pp per cell:

| cell | n | median | min | max |
|---|---|---|---|---|
| 3-0 | 11 | **+5.3** | +4.1 | +10.2 |
| 0-3 | 11 | **+4.6** | +3.3 | +7.0 |
| 3-1 | 11 | −1.4 | −6.6 | −0.4 |
| 1-3 | 11 | −0.9 | −3.4 | −0.6 |
| 3-2 | 11 | **−4.3** | −5.3 | −1.7 |
| 2-3 | 11 | **−2.8** | −4.8 | −1.7 |

Sweeps above IID on every game, five-setters below: the signature of
set-to-set correlation, the same mechanism the tennis catalog records
("when someone wins first it most likely lower odds to win next one too").
Fitted the tennis way — the latent-strength Beta model, ONE variance for the
board, least squares on the six devigged cells over the 11 games pricing
both markets:

```
sigma^2 = 0.032    RMS residual 1.14pp    (IID: 4.07pp;  tennis bo3: 0.040)
```

and every posted leg is **9–21 % under** the model fair (the board's ~25 %
overround). So `vb_correct_score` — the bo5 twin of `tennis_correct_score`,
same bars (edge ≥ 8 %, leg ≤ 8.0, moneyline legs ≥ 1.05) — fires only when a
cell is priced above its own feed's model. On the measured board: never.

### 3.3 The sets markets are the one place two prices meet

Every selection on the sets markets is a SET of the six correct-score cells
(`sets home −2.5` = {3-0}; `sets away +1.5` = everything but {3-0, 3-1};
`under 3.5` = {3-0, 0-3}; the match winner = the two halves). Model-free,
exact, half lines only — an integer line pushes and is not a subset of
anything (`over 4` vs `over 4.5` sets read as a 46 % "duplicate" until
excluded) — and a rung with one side at CB's 1.01 floor is skipped, because
the other side is whatever absorbs the overround (`1(+2.5) @1.01 / 2(−2.5)
@8.05` against `CS 0:3 @32.6` is the floor artifact, not a 305 % gap).

Three checks. **A row is a bet, not a gap** (owner, 2026-09-22: rows sitting
on top of the board with no +EV bet possible are not nice): the duplicate and
dominance rows price the leg they name off the same fair-from-the-match-price
the correct-score check uses, exist only when that leg clears it by
`VB_SETS_MIN_EV = 5 %`, and carry the **edge** as severity — the structural
gap goes in the text as corroboration. The first cut reported the gap itself,
and its three rows sat at 8–22 % while every long side was 5–15 % under fair.

| kind | rule | on the measured board |
|---|---|---|
| `vb_sets_duplicate` | the same set priced twice, ≥ 8 % apart, **and the longer price beats fair** | 0. The disagreements are there — Boca v Vélez `CS 0-3 @3.00` vs `sets away −2.5 @3.65` (22 %), Puerto Rico W v Cuba (13 %, moved to 11 % a pass later), Nigeria v Morocco (8 %) — but the long side read −6 to −15 % against fair every time |
| `vb_sets_dominance` | a subset priced shorter than a superset that contains it, **and the superset beats fair** | 0. USA W v Canada W `CS 3-0 @1.08` vs `under 3.5 sets @1.16` is a real violation, but the favourite is 88 % — outside the model's range (below) |
| `vb_sets_cover` | a set and its complement whose best prices sum < 1 | 0; cheapest 1.106 |

**Calibration range.** The σ² fit spanned devigged away probabilities of
0.31–0.63, and outside it the model extrapolates badly: at USA W v Canada W it
prices 3-0 at 1.62 where the book has 1.08. No model-based row is emitted
when the favourite's devigged match probability exceeds `VB_CS_MAX_FAV =
0.85`; covers need no model and still fire.

Ladders (point handicaps, totals, per-set totals, team totals) get family A
for free once classified: 0 crossings on the 14-game live pass, 1 in the
earlier 20-game sweep (`1st Period − Away Team`, 2.5 %).

## 4. What to expect

A quiet board, by design. The first live pass through the cycle: 14 games,
318 rows, 3.4 s; 0 ladder anomalies; with the EV gate, 0 rows. The feed
disagrees with itself by 8–22 % on three games, and none of those long sides
clears fair. That is the honest yield of a board derived from one number with
one independent seam in it — and the seam moves (13 % → 11 % between two
passes three minutes apart), which is what the cycle is there to catch. When
a row does appear it means: this leg beats the fair its own feed implies, AND
the feed is quoting the same outcome elsewhere at a different price.

One thing that had to change outside the sport: `total_additivity` fired on
every volleyball game with per-set totals ("H1+H2 = 91 vs FT = 182") because
sets 1 and 2 are filed under H1/H2 and two sets do not make a match.
`consistency.SETS_AS_PERIODS` now exempts tennis and volleyball from the
period-sum checks.

## 5. Knobs

Nothing new. Volleyball rides `lsport_scan` / `lsport_scan_sec` /
`lsport_horizon_h` / `lsport_max_sec`; `LSPORT_EXTRA_SPORTS` (default
`volleyball`) is the list of LSport-only sports, and an empty value drops
it. `VB_CS_SIGMA2` and `VB_SETS_DUP_PCT` live in `src/consistency.py`.
