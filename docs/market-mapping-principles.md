<!-- Written after the Crocobet integration (2026-07-11); moved into v1 and
     extended with Setanta 2026-07-26. -->

# Mapping market positions across books without confusing them

The rule set that keeps six books' markets from colliding. Applies to every
book, existing and new.

## The core rule: map on the STABLE KEY, never the display name

Each book identifies a market two ways — a localized display name and a stable
machine key. **Always classify on the key.** Display names lie: they're
localized, decorated, and change wording.

| Book | Stable key to map on | Name trap |
|---|---|---|
| **1xbet** | `G` (group) + `T` (outcome) integer codes | names are absent — codes only |
| **Crocobet** | `gameType` integer | `/sport/en/` still returns **Georgian** names |
| **Lider-Bet** | `typeId` (`mt:2:4214`) + outcome `typeId` | names OK-ish but still text |
| **Betlive** | marketName string (no code) → normalize | must string-match |
| **CrystalBet** | market title string → permissive classify | ASP.NET titles vary |
| **Setanta** | `(resultKind, marketType, period, subPeriod)` integers | dictionary is **condition-guarded** — a naive lookup labels a football Total as "Total kicks" |

For the coded books (1xbet, Crocobet, Lider) mapping is a small integer→canonical
table. For the string books (Betlive, CB) it's a normalized-name classifier —
inherently fuzzier, so lean on those less for exotic markets.

## The confusion traps we actually hit (each cost a debugging round)

1. **Outcome order is not positional.** Crocobet returns a market's outcomes in
   *different array orders across events*. Map each outcome by its own label
   (`"1"→home`, `"2"→away`, `"მეტი"→over`, `"ნაკლები"→under`), never by index.
2. **The line's sign and which side owns it.** 1xbet basketball handicap lists
   each side at its OWN signed line (T7@+L pairs T8@−L); Lider/Crocobet put the
   signed home line in one field (`special`/`argument`). Always define the line
   as the **home** line, signed, and pair sides explicitly.
3. **Incl-OT vs regulation time.** Basketball books ship BOTH a full-incl-OT
   total and a regulation total under different codes (Crocobet -2966 vs -2965;
   Lider "Total (OT)" vs "Total (RT)"). Pick one convention (we use incl-OT for
   FT, matching the reference) and map the OTHER code to nothing until needed.
4. **Period lives in the code, not a flag.** Quarters/halves each have their own
   `gameType`/`typeId`/sub-game id. Don't try to parse "Q1" out of a name —
   map the period-specific code to the period.
5. **Team totals reuse the over/under shape** but with a different code and a
   team side — map `tt_home`/`tt_away` from the code, not from the label.
6. **Period numbering is per-sport, not global.** Setanta reuses `1..4` as
   QUARTERS for basketball while `4010` is the 1st half; for soccer `1` *is*
   the 1st half. Mapping the number without the sport pairs a quarter against
   a half.
7. **A "period" field may carry a minute window.** Setanta's `subPeriod`
   turns `period=1` into "first 15 minutes" — priced nothing like a half
   (draw 1.22 vs 1.94). Require it null unless you have a slot for it. This
   one shipped 130 %+ phantom edges before it was caught.
8. **Exclude the reference's submarkets when verifying.** Pinnacle folds
   corners/bookings into the parent fixture tagged with `submarket`; comparing
   a goals spread against a corners spread of the same line produced a 27.8pp
   p90 that looked like a sign bug and was purely a verification-script
   artifact.

## The verification gate (non-negotiable before emitting a mapping)

A guessed mapping is a silent wrong-price generator. Before a `(book, code) →
(market, period, line, side)` entry ships, **cross-price it against a book whose
mapping is already trusted**, on live matched games:

1. Join the two books' same fixture (SR id if both expose it — exact; else
   name+time).
2. For each shared market/line, devig both sides (Shin) and compare the fair
   probabilities.
3. Require a **median gap ≤ ~3pp** across ≥10 games. Our verified maps land at
   **0.00–1.6pp**; anything above ~3pp means the codes don't mean the same
   thing — do not ship it.

Record the result in `notes/build_log.md` with the numbers. The SR id is the
best verification lever: Crocobet `remoteId` == Lider `sr:match` == Betlive
`providerEventId`, so those three join exactly and check each other for free.

## Canonical vocabulary (what everything maps TO)

- `market_type`: `moneyline` | `spread` | `total` | `team_total`
- `period`: `FT` (incl OT for basketball) | `H1` | `H2` | `Q1`–`Q4`
- `selections`: ml → `{home[,draw],away}`; spread → `{home,away}`;
  total/team_total → `{over,under}`
- `line`: the **home** line for spread (signed), the total line otherwise;
  `None` for moneyline
- `sr_match_id`: bare SR id where the book exposes it — the exact join key

## Status of each book's map (2026-07-11)

| Book | Verified markets | Gap | Not yet mapped |
|---|---|---|---|
| 1xbet | bball ML/spread/total/team_total FT+H1+Q1–4; soccer 1X2/total/spread FT+H1 | 0.4–2.8pp | tennis totals; corners/cards |
| Crocobet | bball ML/total/spread FT; soccer ML/total FT (+spread by shape) | 0.00pp | team totals, quarters/halves, exotics |
| Lider | bball + soccer ML/total/spread/team_total + periods | (reference) | — |
| Betlive | ML/total/spread FT (name-classified) | in prod | sub-periods |
| CrystalBet | full ladder incl corners/cards (permissive) | in prod | — |
| Setanta | soccer ML/total/spread/team_total FT+H1; basketball ML(incl OT)/total/spread/team_total FT+H1+Q1–4; tennis ML/total/spread FT | **0.29–0.99pp** median vs Pinnacle (4 957 / 112 / 566 comparisons) | corners & cards (`resultKind` 4/8), 2nd half, tennis sets |

Extend a row only through the verification gate above.
