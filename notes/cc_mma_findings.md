# MMA discovery findings — 2026-05-27

## Sport IDs
- CrystalBet MMA sport_id: **69**
- Pinnacle MMA sport_id: **22**  (returned 3 leagues: UFC, LFA, Road to the UFC)

## Captured sample
- Path: `data/raw/cb_prematch_sample_mma.html`
- Size: 1,106,320 bytes
- Containers: 58
- Format-A (loadinfo): 53
- Format-B (col-divs): 5

## List-view structure

Compare to basketball/tennis (8-entry loadinfo with ML/AH/OU):

- loadinfo entries per container: **5** (not 8 — no AH)
- AH columns present? **no**
- OU columns present? **yes — but sparse**: 15/53 loadinfo games have `Over` bet populated; 38/53 show Over blank (` `). The `Tot` entry has `HasAdditionalOdds=True` on all 53 — meaning the actual line value is behind the detail page, not inline. The 15 that ARE populated show a standard 2.5/Over-Under structure.
- ML columns present? **yes** — always `name="1"` and `name="\t2"` (same `\t` whitespace quirk as soccer/tennis)
- Any new market type? No. Only ML + OU on the list view. No AH, no Method-of-Victory, no "goes the distance" — those would be detail-page only.

### Loadinfo positional layout (all 5 entries, consistent across all 53 containers)

```
[0] name=          '1'  handicap=        ''  bet=<ML home>
[1] name=         '\t2'  handicap=        ''  bet=<ML away>   ← same \t quirk as soccer/tennis
[2] name=        'Und'  handicap=   'total'  bet=<line or ' '>   ← total line label (often blank)
[3] name=        'Tot'  handicap=   'total'  bet=<line value or ' '>   ← HasAdditionalOdds=True
[4] name=       'Over'  handicap=        ''  bet=<over odds or ' '>
```

Note: `Und` and `Over` share `handicap=''` but `Tot` uses `handicap='total'` — same anchor pattern as basketball/tennis. `Tot.HasAdditionalOdds=True` on all games regardless of whether the line value is shown.

### Sample loadinfo with Over populated

```
[0] name='1'      handicap=''       bet='1.80'
[1] name=' 2'     handicap=''       bet='1.85'
[2] name='Und'    handicap='total'  bet='1.90'     ← Under odds
[3] name='Tot'    handicap='total'  bet='2.5'      ← line value (rounds total)
[4] name='Over'   handicap=''       bet='1.75'
```

### Format-B col layout (5 containers, sample)

```
col0  game69  Snatch          → ML home odds (e.g. '1.95')
col1  game69  Snatch          → ML away odds (e.g. '1.71')
col2  game69  EmptySnatch     → '' (no AH home — MMA has no handicap)
col3  game69  HandicapSnatch total HasAdditionalOdds  → '' (total line, detail only)
col4  game69  EmptySnatch     → '' (no AH away)
```

Notably col2 and col4 are `EmptySnatch` — confirming no AH on MMA. The Format-B column
indices mirror tennis (col0=home, col1=away, col3=total anchor) but col2/col4 are empty
rather than AH values.

### Structural analysis output

```
Total MMA list-view containers: 58
div.game_loading[data-loadinfo]:  53
div.x_loop_res/x_loop_h_res:     204

loadinfo games with Over bet populated:  15
loadinfo games with ML only (Over blank): 38
'Tot' entries with HasAdditionalOdds=True: 53
```

## Verdict

- [x] **4-entry effective layout (ML + Total only, no AH)** — technically 5 entries
      but entry [2] (`Und`) is the Under odds and [3] (`Tot`) is the line value, making
      the effective structure: `[ML_home, ML_away, under, line_value, over]`.
      
      This is a narrower variant of the tennis 8-entry layout. There is **no AH** in MMA
      list view at all (col2/col4 are `EmptySnatch`). The parser needs to:
      1. Skip the AH section entirely (no `handicap='handicap'` entry exists)
      2. Parse `Tot` for the line value using `handicap='total'` anchor — same as basketball/tennis
      3. Handle Over/Under bet often being blank (` `) — only 15/53 games have live OU odds
      4. Strip `\t` from `name='\t2'` — same as all other sports

## Anything weird?

- **OU odds frequently blank**: 38/53 games show `bet=' '` for Over/Under even though
  `Tot.HasAdditionalOdds=True`. This likely means the OU line exists in detail but wasn't
  syndicated to list view at capture time. The parser should treat blank bet as absent
  (skip that Odds row), same as how basketball handles missing total lines.

- **No `name='X'` / draw** — confirmed. Pure 2-way ML. No 3-way handling needed.

- **No AH at all** — neither in loadinfo nor Format-B col-divs (col2/col4 are EmptySnatch).
  MMA has no spread market on CB list view.

- **`\t` whitespace quirk on away name** — present, same as soccer/tennis. Existing `strip()`
  handling covers it.

- **58 containers total** — reasonable for a capture day that includes UFC 316 (May 2026);
  includes prelims, main card, and other promotions (LFA etc.).

- **Tot.HasAdditionalOdds=True on all 53 loadinfo games** — even when bet is blank. This
  means detail-page expansion would reveal OU lines for all games. Since we're running
  list-only mode, we accept the 38/53 blank-OU games and only price what's visible.
