/* Shared odds-history chart drawer — used on matches / arbs / moves / bets.
   Data: /api/chart → change-only tick series per selection for BOTH books
   (src/ticks.py). Step lines; a gap means the market vanished.

   Usage from a page:
     chartCtxReset();                         // once per render pass
     const cid = chartCtxRegister({           // per chartable row
       sport, cb_src, pin_src, market_type, period,
       line, team_side, submarket, title, sub, selection,
     });
     `<button class="chart-btn" data-cid="${cid}">📈</button>`
   A single document-level listener opens the drawer for any .chart-btn click.
*/
(function () {
  "use strict";

  // ── styles ──
  const css = `
    .chart-btn { background: transparent; border: 1px solid var(--border);
      color: var(--text-dim); border-radius: 3px; cursor: pointer;
      font-size: 10px; padding: 0 5px; margin-left: 6px; line-height: 16px; }
    .chart-btn:hover { color: var(--accent); border-color: var(--accent); }
    #chart-drawer { position: fixed; top: 0; right: -640px; width: 600px;
      max-width: 92vw; height: 100vh; background: var(--bg-elev);
      border-left: 1px solid var(--border); box-shadow: -8px 0 30px rgba(0,0,0,0.35);
      z-index: 60; transition: right 0.18s ease; padding: 14px 18px; overflow-y: auto; }
    #chart-drawer.open { right: 0; }
    #chart-drawer h3 { margin: 0 0 2px; font-size: 14px; }
    #chart-drawer .sub { color: var(--text-dim); font-size: 11px; margin-bottom: 10px; }
    #chart-drawer .row { display: flex; gap: 6px; flex-wrap: wrap; margin: 8px 0; }
    .chip-btn { background: var(--bg); border: 1px solid var(--border);
      color: var(--text-dim); border-radius: 3px; cursor: pointer;
      font-family: var(--mono); font-size: 11px; padding: 3px 9px; }
    .chip-btn.on { color: var(--text); border-color: var(--accent); }
    #chart-close { position: absolute; top: 10px; right: 14px; background: transparent;
      border: none; color: var(--text-dim); font-size: 18px; cursor: pointer; }
    #chart-close:hover { color: var(--text); }
    .chart-legend { font-size: 11px; color: var(--text-dim); margin-top: 6px; }
    .chart-legend .cb-key  { color: var(--accent); font-weight: 600; }
    .chart-legend .pin-key { color: #b07cf7; font-weight: 600; }`;
  const style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  // ── drawer DOM ──
  const drawer = document.createElement("div");
  drawer.id = "chart-drawer";
  drawer.innerHTML = `
    <button id="chart-close" title="close">×</button>
    <h3 id="chart-title"></h3>
    <div class="sub" id="chart-sub"></div>
    <div class="row" id="chart-hours"></div>
    <div class="row" id="chart-sels"></div>
    <div id="chartbox"></div>
    <div class="chart-legend"><span class="cb-key">━ CrystalBet</span> &nbsp;
      <span class="pin-key">━ Pinnacle</span> &nbsp; step = change-only ticks;
      flat segments mean the price genuinely didn't move</div>`;
  document.body.appendChild(drawer);
  drawer.querySelector("#chart-close").addEventListener("click",
    () => drawer.classList.remove("open"));

  // ── registry ──
  let _ctxs = [];
  window.chartCtxReset = function () { _ctxs = []; };
  window.chartCtxRegister = function (ctx) { _ctxs.push(ctx); return _ctxs.length - 1; };

  let chartCtx = null;
  let chartHours = +(localStorage.getItem("chart_hours") || 12);

  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".chart-btn");
    if (!btn || btn.dataset.cid === undefined) return;
    const ctx = _ctxs[+btn.dataset.cid];
    if (ctx) { chartCtx = { ...ctx }; draw(); }
  });

  // also allow programmatic open
  window.openOddsChart = function (ctx) { chartCtx = { ...ctx }; draw(); };

  async function draw() {
    drawer.classList.add("open");
    document.getElementById("chart-title").textContent = chartCtx.title || "";
    document.getElementById("chart-sub").textContent = chartCtx.sub || "";

    const hb = document.getElementById("chart-hours");
    hb.innerHTML = "";
    [3, 12, 24, 72].forEach(h => {
      const b = document.createElement("button");
      b.className = "chip-btn" + (h === chartHours ? " on" : "");
      b.textContent = h + "h";
      b.onclick = () => { chartHours = h; localStorage.setItem("chart_hours", h); draw(); };
      hb.appendChild(b);
    });

    const q = new URLSearchParams({
      sport: chartCtx.sport, market_type: chartCtx.market_type,
      period: chartCtx.period || "FT", hours: chartHours,
    });
    if (chartCtx.cb_src)  q.set("cb_src", chartCtx.cb_src);
    if (chartCtx.pin_src) q.set("pin_src", chartCtx.pin_src);
    if (chartCtx.line !== null && chartCtx.line !== undefined && chartCtx.line !== "")
      q.set("line", chartCtx.line);
    if (chartCtx.team_side) q.set("team_side", chartCtx.team_side);
    if (chartCtx.submarket) q.set("submarket", chartCtx.submarket);

    let data;
    try {
      data = await (await fetch("/api/chart?" + q)).json();
    } catch (err) {
      document.getElementById("chartbox").innerHTML =
        `<div class="empty">chart fetch failed: ${err.message}</div>`;
      return;
    }

    const sels = [...new Set([...Object.keys(data.cb || {}), ...Object.keys(data.pin || {})])];
    const sb = document.getElementById("chart-sels");
    sb.innerHTML = "";
    if (!sels.includes(chartCtx.selection)) chartCtx.selection = sels[0];
    sels.forEach(sel => {
      const b = document.createElement("button");
      b.className = "chip-btn" + (sel === chartCtx.selection ? " on" : "");
      b.textContent = sel;
      b.onclick = () => { chartCtx.selection = sel; draw(); };
      sb.appendChild(b);
    });
    render(data, chartCtx.selection);
  }

  function render(data, sel) {
    const W = 560, H = 300, P = { l: 46, r: 12, t: 10, b: 22 };
    const now = Date.now(), t0 = now - chartHours * 3600 * 1000;
    const ser = src => (src && src[sel] ? src[sel] : [])
      .map(([ts, o]) => [new Date(ts).getTime(), o]);
    let cb = ser(data.cb), pin = ser(data.pin);
    for (const s of [cb, pin]) {
      if (s.length && s[s.length - 1][1] != null) s.push([now, s[s.length - 1][1]]);
    }
    const ys = [...cb, ...pin].map(pt => pt[1]).filter(v => v != null);
    const box = document.getElementById("chartbox");
    if (!ys.length) {
      box.innerHTML = `<div class="empty" style="padding:30px">no tick history yet for this selection — it builds up as the pollers run</div>`;
      return;
    }
    let ymin = Math.min(...ys), ymax = Math.max(...ys);
    if (ymax - ymin < 0.05) { ymin -= 0.03; ymax += 0.03; }
    const pad = (ymax - ymin) * 0.1; ymin -= pad; ymax += pad;
    const X = t => P.l + (W - P.l - P.r) * (t - t0) / (now - t0);
    const Y = v => P.t + (H - P.t - P.b) * (1 - (v - ymin) / (ymax - ymin));
    const step = s => {
      let path = "", prev = null;
      for (const [t, v] of s) {
        if (v == null) { prev = null; continue; }
        const x = Math.max(P.l, X(t)), y = Y(v);
        path += prev == null ? `M${x},${y}` : `H${x}V${y}`;
        prev = v;
      }
      return path;
    };
    let grid = "", labels = "";
    for (let i = 0; i <= 4; i++) {
      const v = ymin + (ymax - ymin) * i / 4, y = Y(v);
      grid += `<line x1="${P.l}" y1="${y}" x2="${W - P.r}" y2="${y}" stroke="var(--border)"/>`;
      labels += `<text x="${P.l - 6}" y="${y + 4}" fill="var(--text-dim)" font-size="10" text-anchor="end">${v.toFixed(2)}</text>`;
    }
    for (let i = 0; i <= 4; i++) {
      const t = t0 + (now - t0) * i / 4, dt = new Date(t);
      labels += `<text x="${X(t)}" y="${H - 6}" fill="var(--text-dim)" font-size="10" text-anchor="middle">${String(dt.getUTCHours()).padStart(2, "0")}:${String(dt.getUTCMinutes()).padStart(2, "0")}</text>`;
    }
    box.innerHTML = `<svg width="${W}" height="${H}" style="display:block;max-width:100%">
      ${grid}${labels}
      <path d="${step(pin)}" fill="none" stroke="#b07cf7" stroke-width="1.8"/>
      <path d="${step(cb)}" fill="none" stroke="var(--accent)" stroke-width="1.8"/></svg>`;
  }
})();
