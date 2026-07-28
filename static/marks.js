/* Game marks — "I have money on this game".
 *
 * A Log button records ONE POSITION (market, side, line, odds) as a bet. A
 * Mark records that you have exposure on the FIXTURE, optionally with a total
 * amount across however many positions and books. It carries no odds and no
 * settlement, never touches PnL, and exists to highlight the row wherever that
 * game appears.
 *
 * Every board page includes this file and calls the SAME gameKey(), which is
 * the whole point: a mark placed from Arbs must light the row up on Moves,
 * Matches and Anomalies too. If each page rolled its own key they would drift
 * apart and marks would appear to vanish between tabs.
 *
 * Usage per page:
 *     await Marks.load();                       // before first render
 *     const k = Marks.key(row);                 // stable game key
 *     <tr class="${Marks.rowClass(k)}"> …
 *     ${Marks.buttonHTML(k, row)}               // renders next to the Log link
 *   and once, after the table is in the DOM:
 *     Marks.bind(tableEl, renderFn);            // delegated click handling
 */
(function (global) {
  "use strict";

  const STATE = { byKey: new Map(), marks: [], loaded: false };

  // ── keys ───────────────────────────────────────────────────────────────────
  // A mark is stored under TWO keys, because no single one covers every page.
  //
  //   key(row)      "pin:<pin_event_id>" when the row has one, else nameKey.
  //                 pin_event_id is the true cross-book identity — every book
  //                 is matched against Pinnacle, so one fixture priced by
  //                 setanta, liderbet and betlive shares a single id even
  //                 though all three spell the teams differently. Measured
  //                 live: one game showed on Arbs as "Mjallby AIF" /
  //                 "Mjallby" / "Mjallby Aif" with pin_event_id 1632802084
  //                 on all three, so a name-only key split it in two.
  //   nameKey(row)  sport|home|away, normalized. The fallback, and the only
  //                 handle some rows have: consistency flags carry no
  //                 pin_event_id at all, and 871/1592 matches rows have no
  //                 Pinnacle counterpart.
  //
  // Both are sent on save and both are indexed on load, so a mark set from
  // Arbs (pin key) is still found by an Anomalies row that knows only the
  // name key, and the server upserts on either — one mark per game, whichever
  // page you set it from.
  //
  // Kickoff is in neither key: books disagree on start time by up to an hour
  // for the same fixture, which would split the mark again.
  //
  // normName mirrors the cheap half of src/normalize.normalize_team — strip
  // accents, drop club-type noise tokens ("Lahti" vs Pinnacle's "FC Lahti"),
  // then remove separators so "S.J.K.", "SJK" and "S J K" agree. It does NOT
  // resolve the alias table or bridge initialisms, so "Seinajoen JK" and "SJK"
  // still differ on the name key — that case relies on pin_event_id.
  // Keep the token list in sync with _DROP_TOKENS in src/normalize.py.
  const DROP = new Set(["fc", "bc", "bk", "sk", "sc", "ac", "kk", "nk",
                        "club", "clubs", "basketball", "basket", "basquet",
                        "baloncesto", "pallacanestro", "csk"]);

  function normName(s) {
    const plain = String(s == null ? "" : s)
      .normalize("NFD").replace(/\p{Diacritic}/gu, "")   // strip accents
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, " ")
      .trim();
    if (!plain) return "";
    const kept = plain.split(" ").filter(t => t && !DROP.has(t));
    // All tokens were noise (e.g. a team literally called "FC") — keep them
    // rather than key on an empty string and collide with every such row.
    return (kept.length ? kept : plain.split(" ")).join("");
  }

  // Accepts whatever shape a page has: {home, away} or {match_label}.
  function splitLabel(row) {
    if (row.home && row.away) return [row.home, row.away];
    const raw = String(row.match_label || "");
    // Pages use " — " (em dash); prefill URLs use " vs ".
    const parts = raw.split(/\s+(?:—|–|-{1,2}|vs\.?|v)\s+/i);
    return parts.length >= 2 ? [parts[0], parts.slice(1).join(" ")] : [raw, ""];
  }

  // The name key. Always computable, but NOT unique per game across books:
  // measured live, one fixture appeared on the Arbs page three times as
  // "Mjallby AIF" / "Mjallby" / "Mjallby Aif" from three different books.
  function nameKey(row) {
    const [h, a] = splitLabel(row);
    return [normName(row.sport), normName(h), normName(a)].join("|");
  }

  // The key a mark is stored under. Prefer pin_event_id: every book is matched
  // against Pinnacle, so all three Mjallby rows above share one pin_event_id.
  // Falls back to the name key for rows that have none — consistency flags
  // carry no pin_event_id, and 871/1592 matches rows have no Pinnacle
  // counterpart at all.
  function key(row) {
    const pid = row && row.pin_event_id;
    return (pid !== undefined && pid !== null && pid !== "")
      ? "pin:" + pid
      : nameKey(row);
  }

  function label(row) {
    if (row.match_label) return String(row.match_label).replace(" — ", " vs ");
    const [h, a] = splitLabel(row);
    return a ? `${h} vs ${a}` : h;
  }

  // ── store ──────────────────────────────────────────────────────────────────
  async function load() {
    try {
      const r = await fetch("/api/marks");
      if (!r.ok) return;
      const marks = await r.json();
      // Index under both keys: a mark stored from Arbs under "pin:123" must
      // also be found by an Anomalies row that only knows the name key.
      STATE.byKey = new Map();
      for (const m of marks) {
        STATE.byKey.set(m.game_key, m);
        if (m.alt_key) STATE.byKey.set(m.alt_key, m);
      }
      STATE.marks = marks;
      STATE.loaded = true;
    } catch (e) { /* non-fatal: the board still renders unmarked */ }
  }

  const get = k => STATE.byKey.get(k) || null;
  const has = k => STATE.byKey.has(k);

  async function save(k, row, amount) {
    const body = {
      game_key: k,
      alt_key: nameKey(row),
      match_label: label(row),
      sport: row.sport || null,
      start_time: row.start_time || null,
      amount: (amount === "" || amount == null) ? null : Number(amount),
    };
    const r = await fetch("/api/marks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "save failed");
    const m = await r.json();
    STATE.byKey.set(m.game_key, m);
    if (m.alt_key) STATE.byKey.set(m.alt_key, m);
    return m;
  }

  async function remove(k) {
    const m = STATE.byKey.get(k);
    await fetch("/api/marks/" + encodeURIComponent(k), { method: "DELETE" });
    STATE.byKey.delete(k);
    if (m) { STATE.byKey.delete(m.game_key); if (m.alt_key) STATE.byKey.delete(m.alt_key); }
  }

  // ── rendering ──────────────────────────────────────────────────────────────
  const fmtAmount = v =>
    (v == null) ? "" : (Number.isInteger(v) ? String(v) : Number(v).toFixed(2));

  function rowClass(k) { return has(k) ? "game-marked" : ""; }


  function buttonHTML(k, row) {
    const m = get(k);
    const amt = m ? fmtAmount(m.amount) : "";
    const cls = m ? "mark-btn marked" : "mark-btn";
    const title = m
      ? `Marked${amt ? " — " + amt + " on this game" : ""}. Click to change the amount or remove.`
      : "Mark this game — highlights it everywhere, with an optional total amount you have on it.";
    // data-mark-row carries just enough for the server row; the key is authoritative.
    const payload = encodeURIComponent(JSON.stringify({
      sport: row.sport || "", match_label: label(row),
      start_time: row.start_time || "",
    }));
    return `<button type="button" class="${cls}" data-mark-key="${encodeURIComponent(k)}"`
         + ` data-mark-row="${payload}" title="${title.replace(/"/g, "&quot;")}">`
         + (m ? (amt ? "✓ " + amt : "✓") : "Mark") + `</button>`;
  }

  /**
   * Delegated click handling. Call once per table; `rerender` is invoked after
   * a successful change so the row repaints.
   */
  function bind(root, rerender) {
    if (!root || root.__marksBound) return;
    root.__marksBound = true;
    root.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("button.mark-btn");
      if (!btn) return;
      ev.preventDefault();
      ev.stopPropagation();
      const k = decodeURIComponent(btn.dataset.markKey || "");
      if (!k) return;
      let row = {};
      try { row = JSON.parse(decodeURIComponent(btn.dataset.markRow || "{}")); } catch (e) {}
      const existing = get(k);
      const current = existing && existing.amount != null ? fmtAmount(existing.amount) : "";
      const answer = prompt(
        existing
          ? "Amount on this game (blank = keep marked with no amount, 0 = remove the mark):"
          : "Amount on this game (optional — blank just marks it):",
        current);
      if (answer === null) return;                       // cancelled
      const trimmed = answer.trim();
      try {
        if (existing && trimmed === "0") {
          await remove(k);
        } else if (trimmed === "") {
          await save(k, row, null);
        } else {
          const n = Number(trimmed);
          if (!isFinite(n) || n < 0) { alert("Enter a number >= 0, or 0 to remove."); return; }
          await save(k, row, n);
        }
      } catch (e) {
        alert("Could not save the mark: " + e.message);
        return;
      }
      if (typeof rerender === "function") rerender();
    });
  }

  global.Marks = { load, key, nameKey, label, get, has, save, remove,
                   rowClass, buttonHTML, bind, fmtAmount };
})(window);
