/*
 * The Anomalies alert settings panel — markup lives on the page, behaviour
 * lives here, and both boards use it.
 *
 * Extracted 2026-09-24 when the owner asked for the same panel on the New
 * inconsistencies tab. Copying ~300 lines onto a second page would have made
 * a third place for the alert chain to drift, and this chain has already
 * shipped broken three separate ways: a dead feed wiring, a `ConsistencyFlag`
 * with no `odds` field for the band to read, and defaults that were DISPLAYED
 * but never persisted. Two copies of ALERT_DEFAULT_OFF is already one too
 * many. So there is one implementation, and the boards differ only by their
 * localStorage prefix.
 *
 *   AlertPanel.init("anom_")   Anomalies        -> /api/anomalies/alerts
 *   AlertPanel.init("lsp_")    New inconsist.   -> /api/new_inconsistencies/alerts
 *
 * The prefixes give each board its OWN thresholds, which is the point — the
 * LSport board is a different, narrower set of rows and deserves its own bars.
 * It also keeps the two seen-maps apart: the boards share event ids and check
 * kinds, so one shared map would let whichever board polled first mute the
 * other's chimes entirely.
 *
 * Every key is `<prefix>` + a fixed suffix, so "anom_" reproduces exactly the
 * names that shipped in 2026-08 and no existing setting is orphaned.
 *
 * alerts.js reads these keys. The two files talk ONLY through key strings and
 * a typo fails silently in the worst way — the panel looks saved and the alert
 * simply never fires — so tests/test_anomaly_alerts_wiring.py pins the names
 * in both directions, for both prefixes.
 */
(function (global) {
  "use strict";

  const KIND_LABEL = {
    ml_vs_spread: "ML vs handicap",
    pickem_arb: "pick'em lock (push-aware)",
    // Both compare markets that settle IDENTICALLY — a 0.0 handicap and a Draw
    // No Bet both void on the draw — so neither needs a model. Unlike
    // ml_vs_spread, which reports the same disagreement and names no bet, these
    // two name the position.
    pickem_duplicate: "same bet, two prices (0.0 vs DNB)",
    pickem_dominance: "0.0 rung longer than the 1X2",
    // The only CORRELATION on this list — every other check is an identity. It
    // fires on a favourite FLIP, not on the size of the gap; see _fts_vs_ml.
    fts_vs_ml: "first scorer vs the 1X2",
    favourite_flip: "favourite flip",
    total_additivity: "period totals",
    quarter_ml_extreme: "quarter ML extreme",
    htft_combo: "HT/FT combo",
    htft_fair: "HT/FT fair (model)",
    tennis_set_match: "tennis: set vs match",
    tennis_correct_score: "tennis: correct score",
    duplicate_fixture: "same match listed twice",
    duplicate_live: "same LIVE match listed twice",
    half_result_vs_ft: "half result vs full-time",
    // Both sports post a regulation market and an incl-OT one and tie them by
    // arithmetic; hockey does it across REG and FT, AF within one period.
    ot_vs_regulation: "OT vs regulation",
    ot_monotone: "hockey: incl-OT below regulation",
    betlive_flip: "betlive: favourite flip",
    betlive_ot_fold: "betlive: OT-fold",
    soccer_htft: "soccer HT/FT (soft)",
    basketball_fav: "basketball fav disagreement",
    soccer_fair: "soccer EV (model)",
    soccer_identity: "soccer identity",
    soccer_curve: "soccer curve",
    // Volleyball (2026-09-21) — the tennis set checks in best-of-5, plus the
    // model-free identities across the sets markets. See docs/volleyball.md.
    vb_set_match: "volleyball: set vs match",
    vb_correct_score: "volleyball: correct score",
    vb_sets_duplicate: "volleyball: same outcome, two prices",
    vb_sets_dominance: "volleyball: subset shorter than its superset",
    vb_sets_cover: "volleyball: locked cover",
    combo_cover: "lider combo: locked cover",
    combo_dominance: "lider combo: containment",
    combo_duplicate: "lider combo: same event, two prices",
    combo_fair: "lider combo: vs model fair",
  };

  const KIND_UNIT = {
    ml_vs_spread: "pp", favourite_flip: "pp",
    // pickem_arb severity is the locked return on outlay, not a probability gap.
    pickem_arb: "%", total_additivity: "pts",
    // pickem_duplicate severity is the locked return on outlay; pickem_dominance
    // is how much better the 0.0 rung's price is than the 1X2 it strictly beats.
    pickem_duplicate: "%", pickem_dominance: "%",
    // fts_vs_ml severity is the disagreement between the two markets about the
    // same quantity — home strength — in percentage points.
    fts_vs_ml: "pp",
    quarter_ml_extreme: "pp", htft_combo: "%", htft_fair: "%",
    betlive_flip: "pp", betlive_ot_fold: "pp", soccer_htft: "%",
    basketball_fav: "pp", soccer_fair: "%", soccer_identity: "pp",
    soccer_curve: "pp",
    // tennis_set_match severity is a probability gap in percentage points —
    // either P(set1) − P(match) on the hard order violation, or the per-set
    // disagreement on the soft one. Both are pp.
    tennis_set_match: "pp",
    // tennis_correct_score severity is the BETTING EDGE, not the pp gap. The two
    // come apart badly on this check: a 10pp move on a 1.18 leg is worth +5%
    // while 7.7pp on a 4.45 leg is worth +18%, so sorting the tab by pp would
    // bury the bets under the arithmetic. The pp gap is in the detail text.
    tennis_correct_score: "%",
    // duplicate_fixture carries a FLAT severity of 100 — a book listing one
    // match twice always alerts rather than competing on size. The real numbers
    // (how far the two listings disagree, and the best-of-both cover) are in the
    // row's detail text, not the severity.
    duplicate_fixture: "%",
    // duplicate_live: flat 100, same convention as duplicate_fixture.
    duplicate_live: "%",
    // half_result_vs_ft severity is the probability gap on the most generous
    // outcome of the half, in pp against what the full-time 1X2 implies.
    half_result_vs_ft: "pp",
    // ot_vs_regulation severity is how far P(win incl OT) sits outside the
    // [P(win reg), P(win reg)+P(tie)] box, or the coin-flip gap inside it — pp.
    ot_vs_regulation: "pp",
    // ot_monotone severity is how far the REGULATION goal ladder sits ABOVE the
    // incl-overtime one at the same line. The true bound is zero — overtime only
    // adds goals — so any positive value is the size of the contradiction, in pp.
    ot_monotone: "pp",
    // Both Lider combo kinds are a straight return on outlay: combo_cover is the
    // locked profit on a full cover, combo_dominance is how much more the
    // superset pays than the subset sitting inside it. Both %.
    // Volleyball: vb_set_match is a probability gap in pp (like its tennis
    // parent); vb_correct_score is the betting EDGE in %; the vb_sets_* three
    // are % — a price gap, a free upgrade, a locked return on outlay.
    vb_set_match: "pp", vb_correct_score: "%",
    vb_sets_duplicate: "%", vb_sets_dominance: "%", vb_sets_cover: "%",
    combo_cover: "%", combo_dominance: "%",
    // combo_duplicate severity is how much more the longer of two prices for the
    // SAME event pays; combo_fair is model EV. Both %.
    combo_duplicate: "%", combo_fair: "%",
  };

  function lsGet(k, dflt) {
    try { const v = localStorage.getItem(k); return v === null ? dflt : v; }
    catch (e) { return dflt; }
  }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

  const ALERT_DEFAULT_OFF = new Set(["ml_vs_spread"]);

  // The severity column's colouring. Lives here because its 12 is the same 12
  // as the panel's default bar — the table goes bold exactly where the alert
  // would fire — and because both boards render the column identically.
  function consClass(sev) {
    return sev >= 12 ? "edge-pos-bold" : sev >= 6 ? "edge-pos" : "";
  }


  /* ── Muted games ──────────────────────────────────────────────────────────
   *
   * "Mute" silences the CHIME for every finding on one fixture, across both
   * findings boards. Distinct from Marks, which means "I have money on this
   * game" and is server-backed; this is a local preference about noise.
   *
   * ONE list, not one per board (unlike the thresholds). A game you have
   * looked at and dismissed is dismissed — having to mute it twice, once per
   * tab, would be a bug, not a feature.
   *
   * KEYED BY GAME, on purpose. The owner asked to mute games, and a row-level
   * mute would not hold: the checks re-fire under a different kind, period or
   * line as prices move, so each new row would chime again on a fixture that
   * had already been dismissed.
   *
   * The key is `sport|match_label`, because that is the ONLY identity both
   * sides can compute. alerts.js sees the compact alert feed
   * (_ladder_alert_rows / _consistency_alert_rows), which carries `sport` and
   * `match_label` and no event id it shares with the tables. Marks.key() is
   * richer — it prefers pin_event_id — but the poller cannot reproduce it, and
   * a key one side cannot build is a mute that never applies.
   *
   * Muted rows are still TRACKED by the poller's seen-map; they simply do not
   * sound. Same rule as the Arbs mute: un-muting must not retro-fire every
   * finding that appeared while the game was quiet.
   */
  const MUTED_KEY = "findings_muted_games_v1";

  function muteKey(row) {
    return (row.sport || "") + "|" +
           String(row.match_label || "").trim().toLowerCase();
  }

  function loadMuted() {
    try {
      const raw = localStorage.getItem(MUTED_KEY);
      return new Set(raw ? JSON.parse(raw) : []);
    } catch (e) { return new Set(); }
  }

  function saveMuted(set) {
    try { localStorage.setItem(MUTED_KEY, JSON.stringify([...set])); } catch (e) {}
  }

  function isMuted(row) { return loadMuted().has(muteKey(row)); }

  function toggleMuted(key) {
    const set = loadMuted();
    if (set.has(key)) set.delete(key); else set.add(key);
    saveMuted(set);
    return set.has(key);
  }

  function muteRowClass(row) { return isMuted(row) ? "game-muted" : ""; }

  function muteButtonHTML(row) {
    const k = muteKey(row);
    const on = loadMuted().has(k);
    const title = on
      ? "Muted — findings on this game stay on the board but never chime, on "
        + "either tab. Click to unmute."
      : "Mute this game: it stays on the board, and no finding on it chimes "
        + "again, on either tab.";
    return `<button type="button" class="mute-btn${on ? " muted" : ""}"`
         + ` data-mute-key="${encodeURIComponent(k)}"`
         + ` title="${title.replace(/"/g, "&quot;")}">`
         + (on ? "Muted" : "Mute") + `</button>`;
  }

  function muteBind(root, rerender) {
    if (!root || root.__mutesBound) return;
    root.__mutesBound = true;
    root.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button.mute-btn");
      if (!btn) return;
      ev.preventDefault();
      ev.stopPropagation();
      const k = decodeURIComponent(btn.dataset.muteKey || "");
      if (!k) return;
      toggleMuted(k);
      if (typeof rerender === "function") rerender();
    });
  }

  function init(prefix) {
    /* Written here, read by the shared alerts.js so the chimes follow you
     * across pages. Every name is the board's prefix plus a fixed suffix;
     * alerts.js builds the same names from the same suffixes. */
    const LAD_ON      = prefix + "ladder_alert_enabled";
    const LAD_PCT     = prefix + "ladder_alert_pct";
    const LAD_DELTA   = prefix + "ladder_alert_delta";
    const LAD_STEP    = prefix + "ladder_alert_step";
    const CONS_ON     = prefix + "cons_alert_enabled";
    const CONS_DEF    = prefix + "cons_alert_default";
    const CONS_KINDS  = prefix + "cons_alert_kinds";
    // Odds vetoes (2026-08-27). Longshots are where the book's margin is
    // worst, so a flag on a 40.0 leg is noise however big its severity looks.
    // These GATE the other criteria rather than joining them.
    const LAD_MAXODDS  = prefix + "ladder_alert_max_odds";
    const LAD_MINODDS  = prefix + "ladder_alert_min_odds";
    const CONS_MAXODDS = prefix + "cons_alert_max_odds";
    const CONS_MINODDS = prefix + "cons_alert_min_odds";
    // Panel-open state is per board and deliberately NOT matching
    // <prefix>*alert* — that pattern is the panel<->poller contract and the
    // orphan-key test demands alerts.js read anything matching it.
    const PANEL_OPEN = prefix + "panel_open";


    function loadKindCfg() {
      try { return JSON.parse(localStorage.getItem(CONS_KINDS) || "{}") || {}; }
      catch (e) { return {}; }
    }
    function saveKindCfg(cfg) { lsSet(CONS_KINDS, JSON.stringify(cfg)); }

    const ladOn    = document.getElementById("lad-alert-on");
    const ladPct   = document.getElementById("lad-alert-pct");
    const ladDelta = document.getElementById("lad-alert-delta");
    const ladStep  = document.getElementById("lad-alert-step");
    const consOn   = document.getElementById("cons-alert-on");
    const consDef  = document.getElementById("cons-alert-default");
    const ladMaxOdds  = document.getElementById("lad-alert-max-odds");
    const ladMinOdds  = document.getElementById("lad-alert-min-odds");
    const consMaxOdds = document.getElementById("cons-alert-max-odds");
    const consMinOdds = document.getElementById("cons-alert-min-odds");

    // Defaults chosen to line up with what the tables already highlight: the % off
    // column goes bold at 10, and consClass() goes bold at severity 12.
    ladOn.checked   = lsGet(LAD_ON, "0") === "1";
    ladPct.value    = lsGet(LAD_PCT, "10");
    ladDelta.value  = lsGet(LAD_DELTA, "");
    ladStep.value   = lsGet(LAD_STEP, "");
    consOn.checked  = lsGet(CONS_ON, "0") === "1";
    consDef.value   = lsGet(CONS_DEF, "12");
    ladMaxOdds.value  = lsGet(LAD_MAXODDS, "");
    consMaxOdds.value = lsGet(CONS_MAXODDS, "");
    ladMinOdds.value  = lsGet(LAD_MINODDS, "");
    consMinOdds.value = lsGet(CONS_MINODDS, "");

    /* PERSIST WHAT THE PANEL SHOWS (2026-09-05).
     *
     * The two lines above with a non-empty fallback — ladPct "10" and consDef "12"
     * — were DISPLAYED but never written, because lsSet only runs from the change
     * handler. So on a fresh profile the panel read "sev >= 12" while localStorage
     * held nothing, alerts.js's cfgNum() returned null for the bar, and consPasses
     * rejected every row:
     *
     *     const bar = (k.sev ?? dflt);
     *     if (bar === null) return false;      // <- every consistency flag, always
     *
     * Same for the ladder side: with no pct, delta or step stored, ladderPasses
     * fell through all three criteria and returned false. Both alert kinds were
     * dead on arrival and the panel showed a configured threshold the whole time —
     * which is the failure this file's tests exist to catch, arriving through the
     * one path they were not checking: a default that is rendered but not saved.
     *
     * Seeding only the ABSENT keys, and only the scalar thresholds, so an explicit
     * choice (including a deliberately blank box) is never overwritten, and the
     * per-kind grid is left to ALERT_DEFAULT_OFF rather than frozen into storage.
     */
    function seedIfAbsent(key, value) {
      try {
        if (localStorage.getItem(key) === null && value !== "") {
          localStorage.setItem(key, value);
        }
      } catch (e) {}
    }
    seedIfAbsent(LAD_PCT, ladPct.value.trim());
    seedIfAbsent(CONS_DEF, consDef.value.trim());

    // Checks that are DIAGNOSTICS, not opportunities — listed and shown on the
    // tab, but not chiming unless you deliberately switch them on.
    //
    // ml_vs_spread earns its place here by measurement. It names no bet by
    // construction: it says the 1X2 and the pick'em (line-0) rung disagree, not
    // which one is wrong or how to take it. Its bettable sibling is pickem_arb,
    // which prices the same three legs push-aware and only fires when the cover
    // genuinely locks. Measured over the whole collected CrystalBet history
    // (2 147 events, 1 709 of them pricing BOTH markets):
    //
    //     soccer            1 745 events / 1 572 eligible -> 2 flags, 0 locks
    //     american football   201 events /   103 eligible -> 0 flags
    //     basketball           76 events /    34 eligible -> 0 flags
    //     ice hockey          125 events /     0 eligible  (ladder starts at 1.5)
    //
    //     conversion to a bet: 0 of 2. Largest gap ever seen: 6pp.
    //
    // So it fires on ~0.1 % of eligible events and has never once produced
    // something to bet. Keep it visible — a book contradicting itself is worth

    function renderKindGrid() {
      const cfg = loadKindCfg();
      const grid = document.getElementById("cons-kind-grid");
      grid.innerHTML = Object.keys(KIND_LABEL).map(k => {
        const c = cfg[k] || {};
        // Default ON, except for the diagnostics above. An explicit choice in
        // this grid always wins — untick/tick is stored and beats the default.
        const on = c.on === undefined ? !ALERT_DEFAULT_OFF.has(k) : c.on !== false;
        const sev = (c.sev === null || c.sev === undefined) ? "" : c.sev;
        return `
          <label title="Untick to never alert on this check">
            <input type="checkbox" data-kind-on="${k}" ${on ? "checked" : ""} />
            ${KIND_LABEL[k]}
          </label>
          <input type="number" data-kind-sev="${k}" step="0.5" min="0"
                 value="${sev}" placeholder="default" />
          <span class="kind-unit">${KIND_UNIT[k] || ""}</span>`;
      }).join("");
    }

    function readKindGrid() {
      const cfg = {};
      for (const el of document.querySelectorAll("[data-kind-on]")) {
        const k = el.getAttribute("data-kind-on");
        cfg[k] = cfg[k] || {};
        cfg[k].on = el.checked;
      }
      for (const el of document.querySelectorAll("[data-kind-sev]")) {
        const k = el.getAttribute("data-kind-sev");
        cfg[k] = cfg[k] || {};
        const raw = el.value.trim();
        cfg[k].sev = raw === "" ? null : Number(raw);
      }
      return cfg;
    }

    function syncAlertCfg() {
      lsSet(LAD_ON, ladOn.checked ? "1" : "0");
      lsSet(LAD_PCT, ladPct.value.trim());
      lsSet(LAD_DELTA, ladDelta.value.trim());
      lsSet(LAD_STEP, ladStep.value.trim());
      lsSet(LAD_MAXODDS, ladMaxOdds.value.trim());
      lsSet(CONS_MAXODDS, consMaxOdds.value.trim());
      lsSet(LAD_MINODDS, ladMinOdds.value.trim());
      lsSet(CONS_MINODDS, consMinOdds.value.trim());
      lsSet(CONS_ON, consOn.checked ? "1" : "0");
      lsSet(CONS_DEF, consDef.value.trim());
      saveKindCfg(readKindGrid());
      paintAlertCfg();
    }

    function paintAlertCfg() {
      document.getElementById("lad-alert-set").classList.toggle("off", !ladOn.checked);
      document.getElementById("cons-alert-set").classList.toggle("off", !consOn.checked);
      // Summarise in the closed state so the settings are legible without opening.
      const parts = [];
      if (ladOn.checked) {
        const c = [];
        if (ladPct.value.trim())   c.push(`${ladPct.value.trim()}% off`);
        if (ladDelta.value.trim()) c.push(`Δ${ladDelta.value.trim()}`);
        if (ladStep.value.trim())  c.push(`step ${ladStep.value.trim()}`);
        parts.push(c.length ? `ladders: ${c.join(" or ")}` : "ladders: no criterion set");
      }
      if (consOn.checked) {
        const cfg = loadKindCfg();
        const off = Object.keys(KIND_LABEL).filter(k => (cfg[k] || {}).on === false).length;
        parts.push(`consistency: sev ≥ ${consDef.value.trim() || "?"}`
                   + (off ? ` (${off} check${off === 1 ? "" : "s"} silenced)` : ""));
      }
      document.getElementById("alert-cfg-summary").textContent =
        parts.length ? "— " + parts.join(" · ") : "— off";
    }

    renderKindGrid();
    paintAlertCfg();
    // The four odds boxes were missing here. syncAlertCfg() writes them, but
    // nothing invoked it when only an odds box changed — so typing a veto and
    // clicking away saved nothing until some OTHER control was touched.
    for (const el of [ladOn, ladPct, ladDelta, ladStep, consOn, consDef,
                      ladMaxOdds, consMaxOdds, ladMinOdds, consMinOdds]) {
      el.addEventListener("change", syncAlertCfg);
    }
    document.getElementById("cons-kind-grid").addEventListener("change", syncAlertCfg);

    // Remember whether the panel is open (2026-08-16) — see the same block in
    // arbs.html. <details> defaults to closed and browsers do not persist that, so
    // every reload hid the whole config and made the settings hard to find at all.
    (function rememberPanelOpen() {
      const d = document.getElementById("alert-config");
      // Deliberately NOT named anom_*alert* — that pattern is reserved for the
      // panel↔poller contract, and the orphan-key test rightly demands alerts.js
      // read anything matching it. This is UI state the poller has no use for.
      const KEY = PANEL_OPEN;
      try { d.open = localStorage.getItem(KEY) === "1"; } catch (e) {}
      d.addEventListener("toggle", () => {
        try { localStorage.setItem(KEY, d.open ? "1" : "0"); } catch (e) {}
      });
    })();
  }

  global.AlertPanel = {
    init: init,
    KIND_LABEL: KIND_LABEL,
    KIND_UNIT: KIND_UNIT,
    ALERT_DEFAULT_OFF: ALERT_DEFAULT_OFF,
    lsGet: lsGet,
    lsSet: lsSet,
    consClass: consClass,
    // The game-mute, shared by both findings boards. MUTED_KEY is also read
    // by alerts.js — see the contract test.
    Mutes: {
      KEY: MUTED_KEY, key: muteKey, load: loadMuted, has: isMuted,
      toggle: toggleMuted, rowClass: muteRowClass,
      buttonHTML: muteButtonHTML, bind: muteBind,
    },
    // The suffixes alerts.js must mirror. Exported so a test can read the
    // contract from one place instead of restating it.
    KEYS: ["ladder_alert_enabled", "ladder_alert_pct", "ladder_alert_delta",
           "ladder_alert_step", "ladder_alert_max_odds", "ladder_alert_min_odds",
           "cons_alert_enabled", "cons_alert_default", "cons_alert_kinds",
           "cons_alert_max_odds", "cons_alert_min_odds"],
  };
})(typeof window !== "undefined" ? window : globalThis);
