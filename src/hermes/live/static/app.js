/* Hermes — terminal de supervision (lecture seule). Aucune dépendance hors TradingView Lightweight Charts
   (fourni localement). Toutes les chaînes venant du serveur passent par esc() avant d'entrer dans le DOM. */
"use strict";
(() => {
const LW = window.LightweightCharts;
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = x => String(x ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[c]);
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const store = {get(k, d) {try {const v = localStorage.getItem("hermes." + k); return v == null ? d : JSON.parse(v)} catch (e) {return d}},
  set(k, v) {try {localStorage.setItem("hermes." + k, JSON.stringify(v))} catch (e) {/* private mode */}}};
const fin = x => typeof x === "number" && isFinite(x);
const NF = {};
const nf = (d, sign) => NF[d + "" + sign] ||= new Intl.NumberFormat("fr-FR", {minimumFractionDigits: d, maximumFractionDigits: d, signDisplay: sign ? "exceptZero" : "auto"});
const num = (x, d = 2, sign = false) => fin(x) ? nf(d, sign).format(x).replace("-", "−") : "—";
const pct = (x, d = 1, sign = false) => fin(x) ? num(100 * x, d, sign) + "\u00a0%" : "—";
const usd = (x, d = 0, sign = false) => fin(x) ? num(x, d, sign) : "—";
const compact = x => {if (!fin(x)) return "—"; const a = Math.abs(x);
  return a >= 1e6 ? num(x / 1e6, 2) + " M" : a >= 1e4 ? num(x / 1e3, 1) + " k" : num(x, 0)};
const pdec = p => !fin(p) || p <= 0 ? 2 : p >= 1000 ? 2 : p >= 10 ? 3 : p >= 1 ? 4 : p >= 0.1 ? 5 : p >= 0.001 ? 6 : 8;
const price = p => fin(p) ? num(p, pdec(p)) : "—";
const cls = x => !fin(x) || x === 0 ? "" : x > 0 ? "up" : "down";
const MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."];
const two = n => String(n).padStart(2, "0");
const toMs = t => typeof t === "number" ? (t < 1e12 ? t * 1000 : t) : new Date(t).getTime();
const dt = t => {const d = new Date(toMs(t)); return isFinite(d) ? `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${two(d.getUTCHours())}:${two(d.getUTCMinutes())}` : "—"};
const hm = t => {const d = new Date(toMs(t)); return isFinite(d) ? `${two(d.getUTCHours())}:${two(d.getUTCMinutes())}` : "—"};
const dur = s => {if (!fin(s) || s < 0) return "—"; const m = Math.floor(s / 60), h = Math.floor(m / 60), d = Math.floor(h / 24);
  return d ? `${d} j ${h % 24} h` : h ? `${h} h ${two(m % 60)}` : `${m} min`};
const sideTag = s => s === "long" ? '<span class="side long">▲ LONG</span>' : '<span class="side short">▼ SHORT</span>';
const BAR_MIN = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240};
/* The engine decides at the close of bars whose open time is a multiple of (every x bar) since 1970, and publishes
   its status only then: the next decision, and the age beyond which it is late, follow that grid. */
const cadence = (bar, every) => {const b = (BAR_MIN[bar] || 30) * 60000, e = Math.max(1, every || 1) * b;
  return {b, e, next: Math.ceil((Date.now() - b) / e) * e + b, late: 2 * e + 3 * 60000}};
const TARGET = {style: "rendement résiduel net des styles", beta: "rendement résiduel (bêta retiré)", mean: "rendement moins la moyenne", none: "rendement brut"};
const ENSEMBLE = {ic_weighted: "pondéré par l'IC de validation", equal: "poids égaux"};
const MODE_LABEL = {paper: "Papier", demo: "Démo OKX", live: "Réel"};
const VIEWS = [["terminal", "Terminal"], ["positions", "Positions"], ["history", "Historique"], ["signals", "Signaux"],
  ["risk", "Risque"], ["model", "Modèle"], ["system", "Système"], ["journal", "Journal"]];

const S = {
  mode: store.get("mode", "paper"), view: store.get("view", "terminal"), tf: store.get("tf", null), sym: null,
  modes: [], snap: null, candles: null, fills: null, sort: {}, filt: {side: "all", q: "", lvl: "all"}, loaded: false, sig: "",
  allFills: store.get("allFills", false), eqDays: +store.get("eqDays", 0) || 0,
};
document.documentElement.dataset.theme = store.get("theme", "dark");

async function getJSON(url) {
  try {const r = await fetch(url, {credentials: "same-origin", cache: "no-store"}); return r.ok ? await r.json() : null} catch (e) {return null}
}

/* ---------------------------------------------------------------- derived state */
function derive(snap) {
  const st = snap.status || {}, strat = st.strategy || {}, b = st.bundle || {}, rs = b.research || {};
  const bar = strat.bar || rs.bar || "30m";
  const eq = (snap.equity || []).map(r => ({t: toMs(r[0]), eq: r[1], g: r[2], n: r[3], dd: r[4], ic: r[5], np: r[6], v: r[7]})).filter(r => isFinite(r.t) && fin(r.eq));
  const acc = st.account || {};
  const e0 = fin(acc.initial) ? acc.initial : eq.length ? eq[0].eq : null;
  let pos = Array.isArray(st.positions_detail) ? st.positions_detail : Object.entries(st.positions || {}).map(([s, v]) =>
    ({symbol: s, side: v > 0 ? "long" : "short", notional: v, weight: null}));
  pos = pos.filter(p => fin(p.notional) && p.notional !== 0);
  const capital = (st.equity || 0) * (st.capital_fraction || strat.capital_fraction || 1);
  pos.forEach(p => {if (!fin(p.weight) && capital) p.weight = p.notional / capital});
  const longs = pos.filter(p => p.notional > 0).sort((a, b) => b.notional - a.notional);
  const shorts = pos.filter(p => p.notional < 0).sort((a, b) => a.notional - b.notional);
  const ageMin = st.updated ? (Date.now() - toMs(st.updated)) / 60000 : Infinity;
  const barMin = BAR_MIN[bar] || 30;
  let health = "none";
  const cad = cadence(bar, strat.rebalance_every);
  if (st.updated) health = st.halted ? "crit" : ageMin * 60000 > cad.late ? "warn" : "good";
  const now = Date.now(), dayAgo = eq.filter(r => r.t <= now - 864e5).at(-1);
  return {st, strat, b, rs, bar, barMin, cad, eq, e0, pos, longs, shorts, capital, ageMin, health,
    pnl: fin(st.equity) && fin(e0) ? st.equity - e0 : null, pnl24: dayAgo && fin(st.equity) ? st.equity - dayAgo.eq : null,
    pnl24base: dayAgo ? dayAgo.eq : null, trades: snap.trades || {closed: [], open: {}, stats: {}},
    decision: snap.decision || null, series: snap.series || {}, events: snap.events || [], fills: snap.fills || []};
}

/* ---------------------------------------------------------------- top bar */
function renderModes() {
  const avail = S.modes.length ? S.modes : [{mode: "paper", has_state: true}, {mode: "live", has_state: false}];
  const shown = avail.filter(m => m.mode !== "demo" || m.has_state);
  $("#modes").innerHTML = shown.map(m => {
    const age = m.updated ? (Date.now() - toMs(m.updated)) / 60000 : Infinity;
    const dot = !m.has_state ? "" : m.halted ? "crit" : age * 60000 > cadence(m.bar || "30m", m.rebalance_every).late ? "warn" : "good";
    return `<button role="tab" aria-selected="${m.mode === S.mode}" data-mode="${esc(m.mode)}"><span class="dot ${dot} ${dot === "good" ? "live-pulse" : ""}"></span>${esc(MODE_LABEL[m.mode] || m.mode)}${m.has_state ? "" : '<span class="tag">INACTIF</span>'}</button>`;
  }).join("");
  $$("#modes button").forEach(b => b.onclick = () => {S.mode = b.dataset.mode; store.set("mode", S.mode); S.snap = null; S.sym = null; S.candles = null; S.fills = null; destroyCharts(); refresh()});
}
function renderStatus(D) {
  const el = $("#status");
  if (!D) {el.innerHTML = `<span class="chip"><span class="dot"></span>Inactif</span>`; return}
  const {st, health, barMin, strat} = D;
  const lab = {good: "Moteur actif", warn: `En retard (${Math.round(D.ageMin)} min)`, crit: "Arrêté", none: "Inactif"}[health];
  const left = Math.max(0, D.cad.next - Date.now());
  el.innerHTML = `<span class="chip"><span class="dot ${health} ${health === "good" ? "live-pulse" : ""}"></span><b>${esc(lab)}</b>${st.halted && st.halt_reason ? " · " + esc(st.halt_reason) : ""}</span>
   <span class="chip opt">Bougie <b class="num">${st.bar ? hm(toMs(st.bar)) : "—"}</b></span>
   <span class="chip opt">Prochaine décision <b class="num" id="cd">${health === "good" ? `${Math.floor(left / 60000)}:${two(Math.floor(left / 1000) % 60)}` : "en attente"}</b></span>
   <span class="chip opt"><b class="num" id="clock">${hm(Date.now())}:${two(new Date().getUTCSeconds())}</b> UTC</span>`;
}
function renderNav(D) {
  const counts = D ? {positions: D.pos.length, history: (D.trades.stats || {}).n || 0, journal: D.events.filter(e => e[1] !== "INFO").length} : {};
  const badge = S.mode === "live" ? '<span class="mode-badge live">RÉEL</span>' : S.mode === "demo" ? '<span class="mode-badge">DÉMO OKX</span>' : '<span class="mode-badge paper">PAPIER · ARGENT FICTIF</span>';
  $("#nav").innerHTML = (D ? VIEWS : []).map(([k, l]) => `<button role="tab" data-view="${k}" aria-selected="${S.view === k}">${l}${counts[k] ? `<span class="count">${counts[k]}</span>` : ""}</button>`).join("") +
    `<span class="banner">${D && D.b.promoted === false ? '<span class="pill warn opt">Modèle non promu</span>' : ""}${badge}</span>`;
  $$("#nav button").forEach(b => b.onclick = () => {S.view = b.dataset.view; store.set("view", S.view); render()});
}

/* ---------------------------------------------------------------- KPIs */
function spark(vals, color, fill = true) {
  const v = vals.filter(fin);
  if (v.length < 3) return "";
  const lo = Math.min(...v), hi = Math.max(...v), span = hi - lo || 1, n = v.length - 1;
  const pts = v.map((x, i) => `${(100 * i / n).toFixed(2)},${(22 - 20 * (x - lo) / span).toFixed(2)}`);
  const id = "g" + Math.random().toString(36).slice(2, 8);
  return `<svg class="spark" viewBox="0 0 100 24" preserveAspectRatio="none" aria-hidden="true">
    <defs><linearGradient id="${id}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" style="stop-color:${color};stop-opacity:.28"/><stop offset="1" style="stop-color:${color};stop-opacity:0"/></linearGradient></defs>
    ${fill ? `<path d="M0,24 L${pts.join(" L")} L100,24 Z" style="fill:url(#${id})"/>` : ""}<polyline points="${pts.join(" ")}" style="fill:none;stroke:${color};stroke-width:1.4;vector-effect:non-scaling-stroke"/></svg>`;
}
function kpi(label, value, sub, bar, sp) {
  return `<div class="kpi"><div class="label">${label}</div><div class="v">${value}</div><div class="s">${sub || "&nbsp;"}</div>${bar != null ? `<div class="bar"><i style="width:${Math.max(0, Math.min(100, bar.w * 100)).toFixed(1)}%;background:${bar.c}"></i></div>` : ""}${sp || ""}</div>`;
}
function renderKPIs(D) {
  const el = $("#kpis");
  if (!D) {el.style.display = "none"; return}
  el.style.display = "";
  const {st, strat, rs} = D, r = st.risk || {}, lim = st.risk_limits || {};
  const dd = fin(r.drawdown) ? r.drawdown : D.eq.length ? D.eq.at(-1).dd : null, hard = lim.drawdown_hard || strat.drawdown_hard || 0.25;
  const gmax = lim.gross_max || strat.gross_max || 2.5;
  const lv = D.longs.reduce((a, p) => a + p.notional, 0), sv = -D.shorts.reduce((a, p) => a + p.notional, 0);
  el.innerHTML = [
    kpi("Équité", `${usd(st.equity, 0)}<small>USDT</small>`, fin(D.e0) ? `départ ${usd(D.e0, 0)} USDT` : "", null,
      spark(D.eq.filter(r => r.t >= Date.now() - 7 * 864e5).map(r => r.eq), css("--accent"))),
    kpi("P&L total", `<span class="${cls(D.pnl)}">${usd(D.pnl, 2, true)}</span>`, fin(D.pnl) && D.e0 ? `<span class="${cls(D.pnl)}">${pct(D.pnl / D.e0, 2, true)}</span> depuis le départ` : "", null,
      spark(D.eq.map(r => r.eq), fin(D.pnl) && D.pnl < 0 ? css("--short") : css("--long"))),
    kpi("P&L 24 h", `<span class="${cls(D.pnl24)}">${usd(D.pnl24, 2, true)}</span>`, fin(D.pnl24) && D.pnl24base ? `<span class="${cls(D.pnl24)}">${pct(D.pnl24 / D.pnl24base, 2, true)}</span> sur 24 h` : "moins de 24 h d'historique", null,
      spark(D.eq.filter(r => r.t >= Date.now() - 864e5).map(r => r.eq), fin(D.pnl24) && D.pnl24 < 0 ? css("--short") : css("--long"))),
    kpi("Drawdown", pct(fin(dd) ? -dd : null, 2), `seuils ${pct(lim.drawdown_soft || strat.drawdown_soft, 0)} · ${pct(hard, 0)}`, {w: (dd || 0) / hard, c: (dd || 0) > (lim.drawdown_soft || 0.1) ? css("--crit") : css("--s1")},
      ),
    kpi("Exposition", `${num(st.gross, 2)}×<small>brute</small>`, `nette ${num(st.net, 2, true)}× · max ${num(gmax, 1)}×`, {w: (st.gross || 0) / gmax, c: css("--s1")}),
    kpi("Positions", `<span class="up">${D.longs.length}▲</span> <span class="down">${D.shorts.length}▼</span>`, D.pos.length ? `${compact(lv)} long · ${compact(sv)} short` : "livre à plat"),
    kpi("IC estimé", num(st.ic_est, 3), fin((st.risk || {}).ic_sizing) ? `papier : taille nominale (IC ${num(st.risk.ic_sizing, 3)})` : fin(rs.ic) ? `recherche ${num(rs.ic, 3)}` : "pilote la taille"),
    kpi("Vol ex ante", pct(r.ex_ante_vol, 1), `cible ${pct(strat.vol_target, 0)} par an`, fin(r.ex_ante_vol) && strat.vol_target ? {w: r.ex_ante_vol / strat.vol_target, c: css("--s3")} : null),
  ].join("");
}

/* ---------------------------------------------------------------- charts (Lightweight Charts) */
const CH = {};
function destroyCharts() {for (const k of Object.keys(CH)) {try {CH[k].chart.remove()} catch (e) {/* already gone */} delete CH[k]}}
function lwBase(extra = {}) {
  return Object.assign({
    autoSize: true,
    layout: {background: {type: "solid", color: css("--panel")}, textColor: css("--mute"), fontFamily: "JetBrains Mono, ui-monospace, monospace", fontSize: 11,
      attributionLogo: true, panes: {separatorColor: css("--line-2"), separatorHoverColor: css("--line-3"), enableResize: true}},
    grid: {vertLines: {color: css("--line")}, horzLines: {color: css("--line")}},
    rightPriceScale: {borderColor: css("--line-2"), scaleMargins: {top: 0.12, bottom: 0.08}},
    timeScale: {borderColor: css("--line-2"), timeVisible: true, secondsVisible: false, rightOffset: 1},
    crosshair: {mode: LW.CrosshairMode.Normal, vertLine: {color: css("--line-3"), labelBackgroundColor: css("--panel-3"), style: LW.LineStyle.Dashed},
      horzLine: {color: css("--line-3"), labelBackgroundColor: css("--panel-3"), style: LW.LineStyle.Dashed}},
    localization: {locale: "fr-FR"},
    handleScroll: {vertTouchDrag: false},
  }, extra);
}
function chartIn(id, host, build) {
  let c = CH[id];
  if (c && c.host !== host) {try {c.chart.remove()} catch (e) {/* */} delete CH[id]; c = null}
  if (!c) {
    host.innerHTML = "";
    $$(".tip", host.parentElement).forEach(t => t.remove());
    const chart = LW.createChart(host, lwBase(build.options || {}));
    c = CH[id] = {chart, host, s: {}, lines: [], markers: null};
    build.init(c);
  }
  return c;
}
const utc = t => Math.floor(toMs(t) / 1000);
function dedupe(points) {const out = []; let last = -Infinity; for (const p of points) {if (p.time > last) {out.push(p); last = p.time} else if (p.time === last) out[out.length - 1] = p} return out}

/* --- price chart with entries, exits, stop and entry lines */
function symbolsFor(D) {
  const seen = new Set(), out = [];
  const add = (s, tag) => {if (s && !seen.has(s)) {seen.add(s); out.push([s, tag])}};
  D.pos.slice().sort((a, b) => Math.abs(b.notional) - Math.abs(a.notional)).forEach(p => add(p.symbol, p.notional > 0 ? "▲ long" : "▼ short"));
  const sc = (D.decision && D.decision.scores) || {};
  Object.entries(sc).sort((a, b) => b[1] - a[1]).forEach(([s]) => add(s, "univers"));
  D.fills.forEach(f => add(f.symbol, "historique"));
  add("BTCUSDT", "");
  return out;
}
function chartPanel(D) {
  const syms = symbolsFor(D);
  if (!S.sym || !syms.some(x => x[0] === S.sym)) S.sym = syms.length ? syms[0][0] : "BTCUSDT";
  const tf = S.tf || D.bar;
  const opts = syms.map(([s, tag]) => `<option value="${esc(s)}" ${s === S.sym ? "selected" : ""}>${esc(s)}${tag ? "  · " + esc(tag) : ""}</option>`).join("");
  return `<div class="panel">
   <div class="chart-head">
    <select class="sym-select" id="sym" aria-label="Contrat">${opts}</select>
    <div class="tfs" id="tfs">${["5m", "15m", "30m", "1h", "4h", "1d"].map(x => `<button aria-pressed="${x === tf}" data-tf="${x}">${x}</button>`).join("")}</div>
    <div class="ohlc" id="ohlc"></div>
    <button class="btn-s" id="allfills" aria-pressed="${!!S.allFills}" title="Afficher chaque exécution plutôt que les ouvertures et fermetures">Toutes les exécutions</button>
    <div class="right lg" style="margin-left:auto" id="chart-legend"></div>
   </div>
   <div class="chart-wrap"><div class="lw" id="c-price"></div><div class="chart-note" id="c-price-note"></div></div>
   <div class="levels" id="levels"></div>
  </div>`;
}
function wireChartPanel(D) {
  $("#allfills").onclick = e => {S.allFills = !S.allFills; e.currentTarget.setAttribute("aria-pressed", S.allFills); store.set("allFills", S.allFills); drawPrice(derive(S.snap))};
  $("#sym").onchange = e => {S.sym = e.target.value; S.candles = null; S.fills = null; S.trades = null; loadCandles(); const D2 = derive(S.snap); renderLevels(D2); drawPrice(D2); markSelected()};
  $$("#tfs button").forEach(b => b.onclick = () => {S.tf = b.dataset.tf; store.set("tf", S.tf); $$("#tfs button").forEach(x => x.setAttribute("aria-pressed", x === b)); S.candles = null; loadCandles()});
}
function posOf(D, sym) {return D.pos.find(p => p.symbol === sym) || null}
function renderLevels(D) {
  const el = $("#levels"); if (!el) return;
  const p = posOf(D, S.sym), o = (D.trades.open || {})[S.sym];
  const lg = $("#chart-legend");
  if (lg) lg.innerHTML = `<span class="up">▲</span><span class="down" style="margin-left:-10px">▼</span><span>ouverture</span><span>● sortie</span><span class="warnc">■ stop</span><span><i style="border-top-color:${css("--accent")}" class="dash"></i>Entrée</span><span><i style="border-top-color:${css("--crit")}"></i>Stop</span><span class="muted">TP : aucun</span>`;
  if (!p) {
    el.innerHTML = `<div style="grid-column:1/-1"><div class="label">Pas de position sur ${esc(S.sym)}</div><div class="x" style="margin-top:4px">Les flèches montrent les exécutions passées sur ce contrat ; sans position, ni entrée ni stop.</div></div>`;
    return;
  }
  const opened = p.opened ? toMs(p.opened) : o ? toMs(o.opened) : null;
  el.innerHTML = `
   <div><div class="label">Position</div><div class="v">${sideTag(p.side)} <span class="num">${usd(Math.abs(p.notional), 0)}</span></div><div class="x">${pct(Math.abs(p.weight), 1)} du capital · cible ${usd(Math.abs(p.target), 0)}</div></div>
   <div><div class="label">Entrée</div><div class="v">${price(p.entry)}</div><div class="x">prix moyen</div></div>
   <div><div class="label">Prix actuel</div><div class="v">${price(p.mark)}</div><div class="x ${cls(p.upnl_pct)}">${pct(p.upnl_pct, 2, true)}</div></div>
   <div><div class="label">P&L latent</div><div class="v ${cls(p.upnl)}">${usd(p.upnl, 2, true)}</div><div class="x">${o && fin(o.realized) && Math.abs(o.realized) > 0.005 ? `réalisé partiel <span class="${cls(o.realized)}">${usd(o.realized, 2, true)}</span> · ` : ""}frais payés ${usd(o && o.fees, 2)}</div></div>
   <div><div class="label">Stop catastrophe</div><div class="v down">${price(p.stop)}</div><div class="x">${fin(p.stop_dist) ? pct(p.stop_dist, 1) + " du prix · " + num(D.strat.stop_sigmas, 0) + " σ jour" : "—"}</div></div>
   <div><div class="label">Take-profit</div><div class="v muted">Aucun</div><div class="x">${opened ? "ouverte il y a " + dur((Date.now() - opened) / 1000) : "sortie par rééquilibrage"}</div></div>`;
}
function drawPrice(D) {
  const host = $("#c-price"); if (!host) return;
  const note = $("#c-price-note");
  const c = chartIn("price", host, {
    options: {rightPriceScale: {borderColor: css("--line-2"), scaleMargins: {top: 0.1, bottom: 0.05}}},
    init: c => {
      const up = css("--long"), dn = css("--short");
      c.levels = [];
      // The entry and the stop stay in view: the price scale always includes them.
      c.s.candles = c.chart.addSeries(LW.CandlestickSeries, {upColor: up, downColor: dn, borderUpColor: up, borderDownColor: dn, wickUpColor: up, wickDownColor: dn, priceLineColor: css("--ink-2"),
        autoscaleInfoProvider: original => {const r = original(); if (!r || !c.levels.length) return r;
          r.priceRange.minValue = Math.min(r.priceRange.minValue, ...c.levels); r.priceRange.maxValue = Math.max(r.priceRange.maxValue, ...c.levels); return r}});
      c.s.vol = c.chart.addSeries(LW.HistogramSeries, {priceFormat: {type: "custom", formatter: compact, minMove: 1}, priceLineVisible: false, lastValueVisible: false}, 1);
      try {c.chart.panes()[0].setStretchFactor(4); c.chart.panes()[1].setStretchFactor(1)} catch (e) {/* older API */}
      c.markers = LW.createSeriesMarkers(c.s.candles, []);
      c.markInfo = new Map();
      c.chart.subscribeCrosshairMove(prm => {
        const d = prm && prm.seriesData ? prm.seriesData.get(c.s.candles) : null;
        showOHLC(d || c.last, d && prm.time != null ? c.markInfo.get(prm.time) : null);
      });
    },
  });
  const rows = S.candles && S.candles.symbol === S.sym ? S.candles.candles : null;
  if (!rows) {note.textContent = "Chargement des bougies…"; return}
  if (!rows.length) {
    note.textContent = S.candles.error ? `Bougies indisponibles pour ${S.sym} (${S.candles.error}).` : `Aucune bougie pour ${S.sym}.`;
    c.s.candles.setData([]); c.s.vol.setData([]); return;
  }
  note.textContent = "";
  const last = rows.at(-1)[4], d = pdec(last);
  c.s.candles.applyOptions({priceFormat: {type: "custom", formatter: x => num(x, d), minMove: Math.pow(10, -d)}});
  const data = rows.map(r => ({time: r[0], open: r[1], high: r[2], low: r[3], close: r[4]}));
  c.s.candles.setData(data);
  const up = css("--long"), dn = css("--short");
  c.s.vol.setData(rows.map(r => ({time: r[0], value: r[5], color: (r[4] >= r[1] ? up : dn) + "66"})));
  c.last = data.at(-1); showOHLC(c.last);
  // entry and stop lines
  c.lines.forEach(l => {try {c.s.candles.removePriceLine(l)} catch (e) {/* */}}); c.lines = [];
  const p = posOf(D, S.sym);
  c.levels = p ? [p.entry, p.stop].filter(fin) : [];
  if (p) {
    if (fin(p.entry)) c.lines.push(c.s.candles.createPriceLine({price: p.entry, color: css("--accent"), lineWidth: 1, lineStyle: LW.LineStyle.Dashed, axisLabelVisible: true, title: `Entrée ${p.side === "long" ? "▲" : "▼"}`}));
    if (fin(p.stop)) c.lines.push(c.s.candles.createPriceLine({price: p.stop, color: css("--crit"), lineWidth: 1, lineStyle: LW.LineStyle.Solid, axisLabelVisible: true, title: `Stop · ${pct(p.stop_dist, 1)} du prix`}));
  }
  // Markers without labels (they would pile up): each position's opening and closing, stops highlighted, or
  // every execution on demand; the details of a candle's markers show next to its prices on hover.
  const step = rows.length > 1 ? rows[1][0] - rows[0][0] : 1800, t0 = rows[0][0], tl = rows.at(-1)[0];
  const snap = ts => {ts = Math.floor(ts); return ts < t0 || ts > tl + step ? null : t0 + Math.floor((ts - t0) / step) * step};
  const info = new Map(), say = (t, txt) => {(info.get(t) || info.set(t, []).get(t)).push(txt)};
  let marks = [];
  if (S.allFills) {
    const agg = new Map();
    (S.fills && S.fills.symbol === S.sym ? S.fills.rows : D.fills.filter(f => f.symbol === S.sym)).forEach(f => {
      const bt = snap(toMs(f.ts) / 1000); if (bt == null) return;
      const key = bt + f.side + (f.kind === "stop" ? "s" : "");
      const a = agg.get(key) || {time: bt, side: f.side, stop: f.kind === "stop", usdt: 0};
      a.usdt += Math.abs(f.notional || f.qty * f.price || 0); agg.set(key, a);
    });
    marks = [...agg.values()].map(a => {
      say(a.time, `${a.stop ? "■ stop" : a.side === "buy" ? "▲ achat" : "▼ vente"} ${usd(a.usdt, 0)} USDT`);
      return {time: a.time, position: a.side === "buy" ? "belowBar" : "aboveBar", shape: a.stop ? "square" : "circle", color: a.stop ? css("--warn") : a.side === "buy" ? up : dn, size: 0.5};
    });
  } else {
    const T = S.trades && S.trades.symbol === S.sym ? S.trades.data : {closed: (D.trades.closed || []).filter(t => t.symbol === S.sym), open: (D.trades.open || {})[S.sym]};
    const eps = (T.closed || []).slice();
    if (T.open) eps.push({side: T.open.side, opened: T.open.opened, closed: null});
    eps.forEach(t => {
      const L = t.side === "long", ot = snap(t.opened);
      if (ot != null) {marks.push({time: ot, position: L ? "belowBar" : "aboveBar", shape: L ? "arrowUp" : "arrowDown", color: L ? up : dn, size: 1.1}); say(ot, `${L ? "▲ ouverture LONG" : "▼ ouverture SHORT"}`)}
      const ct = t.closed ? snap(t.closed) : null;
      if (ct != null) {
        marks.push({time: ct, position: L ? "aboveBar" : "belowBar", shape: t.exit_kind === "stop" ? "square" : "circle", color: t.exit_kind === "stop" ? css("--warn") : css("--ink-2"), size: 0.8});
        say(ct, `${t.exit_kind === "stop" ? "■ stop" : "● sortie"} ${L ? "LONG" : "SHORT"} <span class="${cls(t.pnl)}">${usd(t.pnl, 2, true)}</span>`);
      }
    });
  }
  marks.sort((a, b) => a.time - b.time);
  c.markers.setMarkers(marks);
  c.markInfo = info;
  if (c.fitKey !== S.sym + (S.tf || D.bar)) {c.chart.timeScale().fitContent(); c.fitKey = S.sym + (S.tf || D.bar)}
}
function showOHLC(d, marks) {
  const el = $("#ohlc"); if (!el || !d || d.open == null) return;
  const ch = d.close / d.open - 1;
  el.innerHTML = `<span><b>O</b>${price(d.open)}</span><span><b>H</b>${price(d.high)}</span><span><b>B</b>${price(d.low)}</span><span><b>C</b>${price(d.close)}</span><span class="${cls(ch)}">${pct(ch, 2, true)}</span>` +
    (marks && marks.length ? `<span class="mk">${marks.join(" · ")}</span>` : "");
}
async function loadCandles() {
  if (!S.sym || !S.snap) return;
  const sym = S.sym, tf = S.tf || derive(S.snap).bar, q = `mode=${encodeURIComponent(S.mode)}&symbol=${encodeURIComponent(sym)}`;
  const [c, f, t] = await Promise.all([getJSON(`api/candles?symbol=${encodeURIComponent(sym)}&tf=${encodeURIComponent(tf)}`),
    getJSON(`api/fills?${q}`), getJSON(`api/trades?${q}`)]);
  if (sym !== S.sym || tf !== (S.tf || derive(S.snap).bar)) return;  // a later choice won the race
  S.candles = c || {symbol: sym, candles: [], error: "réseau"};
  S.fills = {symbol: sym, rows: f || []};
  S.trades = {symbol: sym, data: t || {closed: [], open: null}};
  if (S.snap) drawPrice(derive(S.snap));
}

/* --- time-series charts */
function lineChart(id, host, spec) {
  const c = chartIn(id, host, {
    options: {rightPriceScale: {borderColor: css("--line-2"), scaleMargins: {top: 0.15, bottom: 0.1}}, localization: {locale: "fr-FR", priceFormatter: spec.fmt}},
    init: c => {
      c.include = [];
      spec.series.forEach((s, i) => {
        const def = s.type === "area" ? LW.AreaSeries : s.type === "hist" ? LW.HistogramSeries : s.type === "under" ? LW.BaselineSeries : LW.LineSeries;
        const o = s.type === "area" ? {lineColor: s.color, topColor: s.color + "40", bottomColor: s.color + "05", lineWidth: 2}
          : s.type === "under" ? {baseValue: {type: "price", price: 0}, topLineColor: s.color, topFillColor1: "rgba(0,0,0,0)", topFillColor2: "rgba(0,0,0,0)",
            bottomLineColor: s.color, bottomFillColor1: s.color + "08", bottomFillColor2: s.color + "55", lineWidth: 2}
          : s.type === "hist" ? {color: s.color} : {color: s.color, lineWidth: s.width || 2, lineStyle: s.dash ? LW.LineStyle.Dashed : LW.LineStyle.Solid};
        // The reference lines (limits, caps) stay in view: the first series' scale includes them.
        if (i === 0) o.autoscaleInfoProvider = orig => {const r = orig(); if (!r || !c.include.length) return r;
          r.priceRange.minValue = Math.min(r.priceRange.minValue, ...c.include); r.priceRange.maxValue = Math.max(r.priceRange.maxValue, ...c.include); return r};
        c.s[i] = c.chart.addSeries(def, Object.assign(o, {priceLineVisible: false, lastValueVisible: !s.quiet, crosshairMarkerVisible: !s.quiet, title: s.title || ""}));
      });
      if (spec.tip) attachTip(c, host, spec);
    },
  });
  c.include = spec.include || [];
  spec.series.forEach((s, i) => c.s[i].setData(dedupe(s.data)));
  c.lines.forEach(([ser, l]) => {try {ser.removePriceLine(l)} catch (e) {/* */}}); c.lines = [];
  (spec.refs || []).forEach(r => c.lines.push([c.s[0], c.s[0].createPriceLine({price: r.y, color: r.color || css("--ink-2"), lineWidth: 1, lineStyle: LW.LineStyle.Dashed, axisLabelVisible: true, title: r.label || ""})]));
  if (!c.fitted && spec.series.some(s => s.data.length)) {c.chart.timeScale().fitContent(); c.fitted = true}
  return c;
}
function attachTip(c, host, spec) {
  const tip = document.createElement("div"); tip.className = "tip"; host.parentElement.appendChild(tip);
  c.chart.subscribeCrosshairMove(prm => {
    if (!prm || !prm.time || !prm.point) {tip.style.display = "none"; return}
    const rows = spec.series.map((s, i) => {const v = prm.seriesData.get(c.s[i]); return v && v.value != null ? `<div class="r"><span class="dot" style="background:${s.color}"></span>${esc(s.name)}<b>${spec.fmt(v.value)}</b></div>` : ""}).join("");
    if (!rows) {tip.style.display = "none"; return}
    tip.innerHTML = `<div class="t">${dt(prm.time * 1000)} UTC</div>${rows}`;
    tip.style.display = "block";
    const w = host.clientWidth, x = prm.point.x;
    tip.style.left = Math.max(4, Math.min(x + 14, w - tip.offsetWidth - 4)) + "px"; tip.style.top = "10px";
  });
}
function legendHTML(items) {return `<div class="lg">${items.map(i => `<span><i class="${i.dash ? "dash" : ""}" style="border-top-color:${i.color}"></i>${esc(i.name)}</span>`).join("")}</div>`}

/* Expected equity, as risk systems compare predicted and realised P&L: research's net Sharpe applied to the ex-ante
   risk of the book actually held, bar after bar. Drift and variance accumulate over the time each book is held (any
   bar size), so a flat book expects nothing and the cone has no width. The cone starts at the equity at the start of
   the displayed window and continues past the last decision with the risk held now. */
function expectedCone(D, rows) {
  const rs = D.rs, SR = rs.sharpe;
  if (!fin(SR) || rows.length < 1) return null;
  const f = D.st.capital_fraction || D.strat.capital_fraction || 1, Y = 365.25 * 864e5;
  // Annualised ex-ante volatility of the strategy's book held from a point on. Points recorded before the engine
  // logged it: flat book = 0, otherwise research's volatility scaled by the exposure (gross / research's mean gross).
  const vol = r => fin(r.v) ? r.v : !fin(r.g) || r.g <= 0 ? 0 : fin(rs.vol) ? rs.vol * (fin(rs.avg_gross) && rs.avg_gross > 0 ? r.g / rs.avg_gross : 1) : 0;
  let mu = 0, v2 = 0;
  const pts = [{t: rows[0].t, mu, sd: 0}];
  for (let i = 1; i < rows.length; i++) {
    const dt = (rows[i].t - rows[i - 1].t) / Y, s = f * vol(rows[i - 1]);
    mu += SR * s * dt; v2 += s * s * dt;
    pts.push({t: rows[i].t, mu, sd: Math.sqrt(v2)});
  }
  const last = rows.at(-1), sNow = f * vol(last), step = D.barMin * 6e4;
  const span = last.t - rows[0].t, hold = (D.strat.holding_bars || rs.horizon_bars || 48) * step;
  const fwd = Math.min(hold, Math.max(2 * step, 0.25 * span)), n = Math.max(2, Math.min(24, Math.round(fwd / step)));
  for (let k = 1; k <= n; k++) {
    const dt = fwd / n / Y; mu += SR * sNow * dt; v2 += sNow * sNow * dt;
    pts.push({t: last.t + k * fwd / n, mu, sd: Math.sqrt(v2), fwd: true});
  }
  return {E0: rows[0].eq, pts, SR, sNow, flat: pts.every(p => p.sd === 0)};
}
function eqWindow(D) {
  const days = S.eqDays, last = D.eq.length ? D.eq.at(-1).t : Date.now();
  return days ? D.eq.filter(r => r.t >= last - days * 864e5) : D.eq;
}
function equitySpec(D) {
  const e = eqWindow(D), s1 = css("--accent"), ink2 = css("--ink-2"), band = css("--accent");
  const series = [{name: "Équité", color: s1, type: "area", data: e.map(r => ({time: utc(r.t), value: r.eq}))}];
  const legend = [{name: "Équité " + (MODE_LABEL[S.mode] || "").toLowerCase(), color: s1}];
  const c = e.length > 1 ? expectedCone(D, e) : null;
  let note = "";
  if (c) {
    const at = (k, p) => c.E0 * (1 + p.mu + k * p.sd);
    series.push({name: "Attendu", color: ink2, dash: true, width: 1, quiet: true, data: c.pts.map(p => ({time: utc(p.t), value: at(0, p)}))});
    [[1, "+1σ"], [-1, "−1σ"], [2, "+2σ"], [-2, "−2σ"]].forEach(([k, nm]) => series.push({name: nm, color: band + (Math.abs(k) === 1 ? "99" : "55"),
      dash: true, width: 1, quiet: true, data: c.pts.map(p => ({time: utc(p.t), value: at(k, p)}))}));
    legend.push({name: `Attendu : Sharpe de recherche ${num(c.SR, 2)} × risque tenu`, color: ink2, dash: true}, {name: "±1σ, ±2σ du risque ex ante", color: band + "99", dash: true});
    note = c.flat ? "Livre à plat sur la période : aucun risque pris, donc rien d'attendu (le cône n'a pas de largeur)."
      : `Risque tenu maintenant : ${pct(c.sNow, 1)}/an ex ante ; le cône part de l'équité au début de la fenêtre et se prolonge avec ce risque.`;
  }
  return {series, legend, note, fmt: v => usd(v, 0), tip: true};
}
function dailyMean(rows) {
  const by = {}; (rows || []).forEach(([t, v]) => {if (!fin(v)) return; const d = String(t).slice(0, 10); (by[d] ||= []).push(v)});
  return Object.keys(by).sort().map(d => [Date.parse(d + "T12:00:00Z"), by[d].reduce((a, x) => a + x, 0) / by[d].length]);
}
const roll = (dm, k) => dm.map((p, i) => {const w = dm.slice(Math.max(0, i - k + 1), i + 1); return [p[0], w.reduce((a, x) => a + x[1], 0) / w.length]});
const tailMean = (dm, k) => dm.length ? roll(dm, k).at(-1)[1] : null;

/* ---------------------------------------------------------------- tables */
function table(id, cols, rows, opts = {}) {
  const st = S.sort[id] || opts.sort || null;
  let data = rows.slice();
  if (st) {const col = cols.find(c => c.k === st.k); if (col) data.sort((a, b) => {const x = col.v ? col.v(a) : a[col.k], y = col.v ? col.v(b) : b[col.k];
    const r = (fin(x) && fin(y)) ? x - y : String(x ?? "").localeCompare(String(y ?? "")); return st.dir === "asc" ? r : -r})}
  if (opts.limit) data = data.slice(0, opts.limit);
  const head = cols.map(c => `<th class="${c.l ? "l" : ""} ${c.nosort ? "" : "sort"}" data-k="${c.k}" ${st && st.k === c.k ? `aria-sort="${st.dir === "asc" ? "ascending" : "descending"}"` : ""}>${c.h}</th>`).join("");
  const body = data.length ? data.map(r => `<tr class="${opts.click ? "click" : ""}" ${opts.click ? `data-sym="${esc(r.symbol)}"` : ""}>${cols.map(c => `<td class="${c.cl || ""} ${c.l ? "l" : ""}">${c.f ? c.f(r) : esc(r[c.k])}</td>`).join("")}</tr>`).join("")
    : `<tr><td colspan="${cols.length}"><div class="empty">${opts.empty || "Aucune donnée."}</div></td></tr>`;
  return `<table id="${id}"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}
function wireTables(root) {
  $$("table thead th.sort", root).forEach(th => th.onclick = () => {
    const id = th.closest("table").id, k = th.dataset.k, cur = S.sort[id];
    S.sort[id] = {k, dir: cur && cur.k === k && cur.dir === "desc" ? "asc" : "desc"}; render();
  });
  $$(".brow.click", root).forEach(tr => tr.onclick = () => {S.sym = tr.dataset.sym; S.candles = null; S.fills = null; S.view = "terminal"; store.set("view", "terminal"); render(); loadCandles(); window.scrollTo({top: 0, behavior: "smooth"})});
  $$("tr.click", root).forEach(tr => tr.onclick = () => {S.sym = tr.dataset.sym; S.candles = null; S.fills = null; S.view = "terminal"; store.set("view", "terminal"); render(); loadCandles(); window.scrollTo({top: 0, behavior: "smooth"})});
}

/* ---------------------------------------------------------------- positions list (terminal) */
function positionsList(D) {
  const maxw = Math.max(0.0001, ...D.pos.map(p => Math.abs(p.weight || 0)));
  const L = p => p.side === "long";
  const row = p => `<div class="prow ${p.symbol === S.sym ? "sel" : ""}" data-sym="${esc(p.symbol)}" tabindex="0" role="button" title="entrée ${price(p.entry)} · stop ${price(p.stop)}" aria-label="${esc(p.symbol)} ${L(p) ? "long" : "short"}">
    <span class="g ${L(p) ? "up" : "down"}">${L(p) ? "▲" : "▼"}</span>
    <span class="sym">${esc(p.symbol)}<span class="w">${pct(Math.abs(p.weight), 1)}</span></span>
    <span class="r">${usd(Math.abs(p.notional), 0)}</span>
    <span class="r"><span class="${cls(p.upnl)}">${usd(p.upnl, 2, true)}</span><small class="${cls(p.upnl_pct)}">${pct(p.upnl_pct, 2, true)}</small></span>
    <i class="wl" style="width:${(38 * Math.abs(p.weight || 0) / maxw).toFixed(1)}%;background:${L(p) ? css("--long") : css("--short")}"></i></div>`;
  const grp = (list, side) => {
    const tot = list.reduce((a, p) => a + Math.abs(p.notional), 0), up = list.reduce((a, p) => a + (p.upnl || 0), 0);
    return `<div class="pgroup"><div class="pgroup-h">${sideTag(side)}<span>${list.length}</span><span class="sum">${usd(tot, 0)} · <span class="${cls(up)}">${usd(up, 2, true)}</span></span></div>${list.map(row).join("") || '<div class="empty" style="padding:10px 16px">Aucune.</div>'}</div>`;
  };
  const f = S.posFilter || "all", upnl = D.pos.reduce((a, p) => a + (p.upnl || 0), 0);
  const body = !D.pos.length ? `<div class="empty"><b>Livre à plat</b>${flatReason(D)}</div>`
    : (f !== "short" ? grp(D.longs, "long") : "") + (f !== "long" ? grp(D.shorts, "short") : "");
  return `<div class="panel"><div class="ph"><h2>Positions ouvertes</h2><span class="sub">latent <span class="${cls(upnl)}">${usd(upnl, 2, true)}</span></span>
    <div class="right tfs" id="pfilt">${[["all", `Tous ${D.pos.length}`], ["long", `▲ ${D.longs.length}`], ["short", `▼ ${D.shorts.length}`]].map(([k, l]) => `<button data-f="${k}" aria-pressed="${f === k}">${l}</button>`).join("")}</div></div>
    <div class="phead"><span></span><span>Contrat</span><span class="r">Taille</span><span class="r">P&L latent</span></div>
    <div class="plist">${body}</div></div>`;
}
function driftText(r) {
  if (r.psi_error === 1) return "Le contrôle a échoué à cette bougie (voir le journal) ; le trading continue.";
  if (!fin(r.psi_max)) return "Pas encore lu : il faut une journée de données entièrement chauffées.";
  const unseen = r.psi_unseen > 0 ? num(r.psi_unseen, 0) + " variable(s) avec des valeurs jamais vues à l'entraînement (manquantes ou très hors plage : défaut de données probable). " : "";
  if (r.psi_calibrated !== 1) return unseen + "Seuils de dérive non calibrés pour ce modèle (modèle antérieur à la calibration ou historique d'entraînement trop court) : seules les valeurs jamais vues sont signalées.";
  const market = r.psi_market !== 1 ? "Variables de marché pas encore lues (une semaine d'historique chauffé)."
    : r.psi_market_calibrated !== 1 ? "Variables de marché non calibrées pour ce modèle." : "";
  return num(r.psi_drifted, 0) + " variable(s) au-delà de leur seuil calibré (le PSI que des fenêtres de même forme atteignaient sur le trimestre précédant le profil) ; la plus éloignée est à " + num(r.psi_ratio_max, 2) + " fois son seuil. Alerte au-delà de 10 % des variables. " + unseen + market;
}
function flatReason(D) {
  const ic = D.st.ic_est;
  if (D.st.halted) return "Le moteur est arrêté : " + esc(D.st.halt_reason || "");
  if (fin((D.st.risk || {}).ic_sizing)) return "Papier à taille nominale : aucune position à cette bougie (le livre se construit à la prochaine décision).";
  if (fin(ic) && ic <= 0) return "L'IC estimé est nul : la taille des positions lui est proportionnelle. Le moteur continue de décider et de mesurer l'IC réalisé ; il reprendra des positions s'il redevient positif.";
  return "Aucune position à cette bougie.";
}
function markSelected() {$$(".prow").forEach(r => r.classList.toggle("sel", r.dataset.sym === S.sym))}
function wirePositions() {
  $$("#pfilt button").forEach(b => b.onclick = () => {S.posFilter = b.dataset.f; $("#t-pos").innerHTML = positionsList(derive(S.snap)); wirePositions()});
  $$(".prow").forEach(r => {const go = () => {S.sym = r.dataset.sym; const sel = $("#sym"); if (sel) sel.value = S.sym; S.candles = null; S.fills = null; markSelected(); loadCandles(); renderLevels(derive(S.snap)); drawPrice(derive(S.snap))};
    r.onclick = go; r.onkeydown = e => {if (e.key === "Enter" || e.key === " ") {e.preventDefault(); go()}}});
}

/* ---------------------------------------------------------------- views */
function vTerminal(D) {
  const el = $("#v-terminal");
  if (!el.dataset.built) {
    el.innerHTML = `<div class="grid">
      <div class="c8" id="t-chart"></div><div class="c4 stick" id="t-pos"></div>
      <div class="c8"><div class="panel"><div class="ph"><h2>Équité face à ce que la recherche attend</h2><div class="tfs" id="eq-range">${[["1", "24 h"], ["7", "7 j"], ["30", "30 j"], ["0", "Tout"]].map(([k, l]) => `<button data-days="${k}" aria-pressed="${+k === S.eqDays}">${l}</button>`).join("")}</div><div class="right" id="t-eq-lg"></div></div>
        <div class="chart-wrap sm"><div class="lw" id="c-eq"></div><div class="chart-note" id="c-eq-note"></div></div><p class="cap eq-cap" id="c-eq-cap"></p></div></div>
      <div class="c4"><div class="panel"><div class="ph"><h2>Composition du livre</h2><span class="sub">en multiple du capital alloué</span></div><div class="pb" id="t-book"></div></div></div>
      <div class="c7"><div class="panel"><div class="ph"><h2>Dernières exécutions</h2><span class="sub" id="t-fills-sub"></span></div><div class="tw maxh" id="t-fills"></div></div></div>
      <div class="c5 stick"><div class="panel"><div class="ph"><h2>Événements</h2></div><div class="tw plist" id="t-ev"></div></div></div></div>`;
    el.dataset.built = "1";
  }
  const chartSlot = $("#t-chart");
  if (!chartSlot.dataset.sym || !$("#c-price")) {chartSlot.innerHTML = chartPanel(D); chartSlot.dataset.sym = "1"; wireChartPanel(D); delete CH.price}
  else {
    const sel = $("#sym"), syms = symbolsFor(D);
    const key = syms.map(x => x[0] + x[1]).join();
    if (sel.dataset.key !== key) {  // new symbols or tags: refresh the list, never the chart
      sel.innerHTML = syms.map(([s, tag]) => `<option value="${esc(s)}" ${s === S.sym ? "selected" : ""}>${esc(s)}${tag ? "  · " + esc(tag) : ""}</option>`).join("");
      sel.dataset.key = key;
    }
  }
  renderLevels(D); drawPrice(D);
  $("#t-pos").innerHTML = positionsList(D); wirePositions();
  const drawEq = fit => {
    const eqs = equitySpec(D);
    $("#t-eq-lg").innerHTML = legendHTML(eqs.legend);
    $("#c-eq-note").textContent = D.eq.length < 2 ? "La courbe apparaît après deux décisions." : "";
    $("#c-eq-cap").textContent = eqs.note;
    const eqc = lineChart("eq", $("#c-eq"), eqs);
    if (fit) eqc.chart.timeScale().fitContent();
  };
  drawEq(false);
  // The window changes the anchor of the cone, not only the view: each window is recomputed from its own start.
  $$("#eq-range button").forEach(b => b.onclick = () => {
    $$("#eq-range button").forEach(x => x.setAttribute("aria-pressed", x === b));
    S.eqDays = +b.dataset.days; store.set("eqDays", S.eqDays);
    drawEq(true);
  });
  $("#t-book").innerHTML = bookComposition(D);
  const F = D.fills.slice(0, window.innerWidth < 680 ? 12 : 60);  // the full list is in the History tab
  $("#t-fills-sub").textContent = D.fills.length ? `${D.fills.length} dernières chargées` : "";
  $("#t-fills").innerHTML = fillsTable("tf-fills", F);
  $("#t-ev").innerHTML = eventsHTML(D.events.slice(0, 40));
  wireTables(el);
}
function bookComposition(D) {
  const L = D.longs.reduce((a, p) => a + (p.weight || 0), 0), Sx = -D.shorts.reduce((a, p) => a + (p.weight || 0), 0);
  const gmax = (D.st.risk_limits || {}).gross_max || D.strat.gross_max || 2.5, nmax = D.strat.net_max || 0.25;
  const bar = (label, v, max, color, sub) => `<div class="meter" style="padding:10px 0"><div class="h"><span>${label}</span><span class="v">${num(v, 2, label.startsWith("Net"))}×</span></div>
    <div class="track"><div class="fill" style="width:${Math.min(100, 100 * Math.abs(v) / max).toFixed(1)}%;background:${color}"></div></div><div class="x">${sub}</div></div>`;
  const r = D.st.risk || {};
  return bar("Longs ▲", L, gmax / 2, css("--long"), `${D.longs.length} contrats`) + bar("Shorts ▼", Sx, gmax / 2, css("--short"), `${D.shorts.length} contrats`)
    + bar("Net (longs − shorts)", L - Sx, nmax, css("--s2"), `plafond ±${num(nmax, 2)}× · livre neutre au marché (bêta)`)
    + `<div class="kvline" style="display:flex;gap:18px;flex-wrap:wrap;margin-top:6px;font-size:11.5px;color:var(--mute)">
      <span>Budget de risque <b class="num" style="color:var(--ink)">${pct(r.budget, 0)}</b></span><span>ES 1 j <b class="num" style="color:var(--ink)">${pct(r.es_1d, 2)}</b></span>
      <span>Garde de régime <b class="num" style="color:var(--ink)">${fin(r.regime_scale) ? "×" + num(r.regime_scale, 2) : "inactive"}</b></span></div>`;
}
function fillsTable(id, rows, limit) {
  return table(id, [
    {k: "ts", h: "Heure (UTC)", l: true, f: r => `<span class="num">${dt(r.ts)}</span>`},
    {k: "symbol", h: "Contrat", l: true, cl: "sym"},
    {k: "side", h: "Sens", l: true, f: r => r.side === "buy" ? '<span class="up">▲ achat</span>' : '<span class="down">▼ vente</span>'},
    {k: "px", h: "Prix", cl: "num", v: r => r.px_model || r.price, f: r => price(r.px_model || r.price)},
    {k: "notional", h: "Montant", cl: "num", v: r => r.notional || Math.abs(r.qty * r.price), f: r => usd(r.notional || Math.abs(r.qty * r.price), 2)},
    {k: "fee", h: "Frais", cl: "num", f: r => usd(r.fee, 3)},
    {k: "maker", h: "Type", f: r => r.maker ? '<span class="pill">maker</span>' : '<span class="pill">taker</span>'},
    {k: "kind", h: "Origine", f: r => r.kind === "stop" ? '<span class="pill warn">stop</span>' : r.kind === "flatten" ? '<span class="pill crit">arrêt</span>' : r.kind === "external" ? '<span class="pill">hors moteur</span>' : '<span class="muted">rééquilibrage</span>'},
  ], rows, {limit, empty: "Aucune exécution.", sort: {k: "ts", dir: "desc"}});
}
function eventsHTML(ev) {
  if (!ev.length) return '<div class="empty">Aucun événement.</div>';
  return ev.map(e => `<div class="ev"><span class="when">${dt(e[0])}</span><span class="lvl ${esc(e[1])}">${esc(e[1] === "WARNING" ? "ALERTE" : e[1] === "ERROR" ? "ERREUR" : "INFO")}</span><span class="msg">${esc(e[2])}</span></div>`).join("");
}

function vPositions(D) {
  const el = $("#v-positions");
  const cols = [
    {k: "symbol", h: "Contrat", l: true, cl: "sym"},
    {k: "side", h: "Sens", l: true, f: r => sideTag(r.side)},
    {k: "notional", h: "Montant", cl: "num", v: r => Math.abs(r.notional), f: r => usd(Math.abs(r.notional), 0)},
    {k: "weight", h: "% capital", cl: "num", v: r => Math.abs(r.weight), f: r => pct(Math.abs(r.weight), 1)},
    {k: "entry", h: "Entrée", cl: "num", f: r => price(r.entry)},
    {k: "mark", h: "Prix", cl: "num", f: r => price(r.mark)},
    {k: "upnl", h: "P&L latent", cl: "num", f: r => `<span class="${cls(r.upnl)}">${usd(r.upnl, 2, true)}</span>`},
    {k: "upnl_pct", h: "%", cl: "num", f: r => `<span class="${cls(r.upnl_pct)}">${pct(r.upnl_pct, 2, true)}</span>`},
    {k: "stop", h: "Stop", cl: "num", f: r => price(r.stop)},
    {k: "stop_dist", h: "Distance stop", cl: "num", f: r => pct(r.stop_dist, 1)},
    {k: "tp", h: "TP", nosort: true, f: () => '<span class="muted">aucun</span>'},
    {k: "score", h: "Score", cl: "num", f: r => num(r.score, 2, true)},
    {k: "target", h: "Cible", cl: "num", v: r => Math.abs(r.target), f: r => usd(r.target, 0, true)},
    {k: "opened", h: "Ouverte", cl: "num", v: r => r.opened ? toMs(r.opened) : 0, f: r => r.opened ? dt(r.opened) : "—"},
    {k: "age", h: "Durée", cl: "num", v: r => r.opened ? -toMs(r.opened) : 0, f: r => r.opened ? dur((Date.now() - toMs(r.opened)) / 1000) : "—"},
  ];
  const up = D.pos.reduce((a, p) => a + (p.upnl || 0), 0), stopsN = D.pos.filter(p => fin(p.stop)).length;
  el.innerHTML = `<div class="grid">
   <div class="c12"><div class="panel"><div class="stats">
     <div><div class="label">Positions</div><div class="v">${D.pos.length}</div><div class="x"><span class="up">${D.longs.length} ▲ long</span> · <span class="down">${D.shorts.length} ▼ short</span></div></div>
     <div><div class="label">Montant long</div><div class="v up">${usd(D.longs.reduce((a, p) => a + p.notional, 0), 0)}</div><div class="x">USDT</div></div>
     <div><div class="label">Montant short</div><div class="v down">${usd(-D.shorts.reduce((a, p) => a + p.notional, 0), 0)}</div><div class="x">USDT</div></div>
     <div><div class="label">P&L latent</div><div class="v ${cls(up)}">${usd(up, 2, true)}</div><div class="x">au prix de marque, hors funding</div></div>
     <div><div class="label">Stops posés</div><div class="v">${stopsN} / ${D.pos.length}</div><div class="x">un stop catastrophe par position</div></div>
     <div><div class="label">Take-profit</div><div class="v muted">aucun</div><div class="x">sorties par rééquilibrage</div></div>
   </div></div></div>
   ${sleevePanel(D)}
   <div class="c12"><div class="panel"><div class="ph"><h2>Contribution au P&L latent</h2><span class="sub">par position, en USDT, au prix de marque</span></div>${contribution(D)}</div></div>
   <div class="c12"><div class="panel"><div class="ph"><h2>Toutes les positions ouvertes</h2><span class="sub">Cliquer une ligne l'ouvre dans le graphique.</span></div>
    <div class="tw">${table("t-pos-all", cols, D.pos, {click: true, sort: {k: "notional", dir: "desc"}, empty: `<b>Livre à plat</b>${flatReason(D)}`})}</div>
    <p class="cap" style="padding:12px 16px 0">Le stop est un ordre stop-marché posé côté bourse à ${num(D.strat.stop_sigmas, 0)} volatilités journalières du prix d'ouverture (entre 3 % et 50 %) : un garde-fou contre les krachs, pas une règle de sortie. La stratégie ne pose pas de take-profit : chaque position vit tant que le modèle la classe parmi les meilleures (ou les pires, pour un short) et le livre est rééquilibré toutes les ${D.barMin * (D.strat.rebalance_every || 1)} minutes.</p></div></div>
  </div>`;
  wireTables(el);
}

function sleevePanel(D) {
  const L = D.st.listing_sleeve || {};
  if (!L.enabled) return "";
  const openT = L.open || [], cov = L.coverage || {}, watch = Object.keys(L.watch || {}).length;
  const cal = L.calendar || {}, calAge = cal.refreshed_at ? (Date.now() - toMs(cal.refreshed_at)) / 3600000 : Infinity;
  const ent = Array.isArray(L.entries) && L.entries.length ? L.entries : [24, 72], killN = fin(L.kill_trades) ? L.kill_trades : 60, killM = fin(L.kill_mean) ? L.kill_mean : -0.03;
  const markOf = sym => {const p = posOf(D, sym); return p && fin(p.mark) ? p.mark : null};
  const rows = openT.map(t => {
    const m = markOf(t.symbol), pnl = fin(m) ? t.qty * (m - t.entry_px) : null;
    return {symbol: t.symbol, tranche: t.tranche, entry: t.entry, entry_px: t.entry_px, mark: m, notional: Math.abs(t.qty * t.entry_px), pnl, stop: t.stop_px, exit_due: t.exit_due};
  });
  const cols = [
    {k: "symbol", h: "Contrat", l: true, cl: "sym"},
    {k: "tranche", h: "Entrée", l: true, f: r => fin(ent[r.tranche]) ? "+" + num(ent[r.tranche], 0) + " h" : "n°" + (r.tranche + 1)},
    {k: "entry", h: "Ouverte", cl: "num", v: r => toMs(r.entry), f: r => dt(r.entry)},
    {k: "notional", h: "Short", cl: "num", f: r => usd(r.notional, 0)},
    {k: "entry_px", h: "Prix d'entrée", cl: "num", f: r => price(r.entry_px)},
    {k: "mark", h: "Prix", cl: "num", f: r => price(r.mark)},
    {k: "pnl", h: "P&L (hors couverture)", cl: "num", f: r => `<span class="${cls(r.pnl)}">${usd(r.pnl, 2, true)}</span>`},
    {k: "stop", h: "Stop", cl: "num", f: r => price(r.stop)},
    {k: "exit_due", h: "Sortie prévue", cl: "num", v: r => toMs(r.exit_due), f: r => dt(r.exit_due)},
  ];
  return `<div class="c12"><div class="panel"><div class="ph"><h2>Poche « nouvelles cotations »</h2><span class="sub">${esc(L.rule || "")}</span></div>
   <div class="stats">
    <div><div class="label">Tokens shortés</div><div class="v">${num(fin(L.tokens_open) ? L.tokens_open : openT.length, 0)}</div><div class="x">${openT.length} tranche(s) · ${watch} nouveau(x) token(s) surveillé(s)</div></div>
    <div><div class="label">P&L ouvert</div><div class="v ${cls(L.open_pnl)}">${usd(L.open_pnl, 2, true)}</div><div class="x">couverture BTC, funding et coûts compris</div></div>
    <div><div class="label">P&L réalisé</div><div class="v ${cls(L.closed_pnl)}">${usd(L.closed_pnl, 2, true)}</div><div class="x">${(L.closed || []).length} opération(s) récente(s)</div></div>
    <div><div class="label">Couverture OKX</div><div class="v">${num(cov.on_okx || 0, 0)} / ${num(cov.new_tokens_due || 0, 0)}</div><div class="x">nouveaux tokens cotés sur OKX à l'échéance</div></div>
    <div><div class="label">Calendrier Binance</div><div class="v ${calAge > 7 ? "down" : ""}">${cal.refreshed_at ? dt(cal.refreshed_at) : "pas encore lu"}</div><div class="x">${calAge > 7 && cal.refreshed_at ? "en retard : relu toutes les 6 h" : num(cal.listings || 0, 0) + " perpétuel(s) coté(s) depuis 7 j, dont " + watch + " nouveau(x) token(s)"}</div></div>
    <div><div class="label">État</div><div class="v ${L.suspended ? "down" : ""}">${L.suspended ? "suspendue" : "active"}</div><div class="x">${L.suspended ? "règle d'arrêt : " + killN + " dernières opérations à " + pct(killM, 0) + " ou moins en moyenne" : "règle d'arrêt fixée d'avance"}</div></div>
   </div>
   <div class="tw">${table("t-sleeve", cols, rows, {sort: {k: "entry", dir: "desc"}, empty: "<b>Aucun short ouvert</b>La poche entre sur un nouveau token " + ent.map(h => num(h, 0) + " h").join(" puis ") + " après la cotation de son perpétuel, s'il est coté sur OKX."})}</div>
   <p class="cap" style="padding:12px 16px 0">Court sur chaque nouveau token coté en perpétuel sur Binance et présent sur OKX, en tranches (${ent.map(h => num(h, 0) + " h").join(" et ")} après la cotation, chacune dans les 3 heures, jamais rattrapée), fermées 7 jours après, couvertes par un long BTC de même montant, stop à +50 % de la première entrée. Test en papier : Sharpe hors échantillon 2,1 en recherche (1,6 sans les perpétuels de pré-marché), attendu plutôt autour de 1 ; le P&L de la poche compte le funding et des coûts de recherche (0,15 % par côté), et elle s'arrête d'elle-même si ses ${killN} dernières opérations perdent en moyenne ${pct(-killM, 0)} ou plus.</p></div></div>`;
}

function contribution(D) {
  const rows = D.pos.filter(p => fin(p.upnl)).slice().sort((a, b) => b.upnl - a.upnl);
  if (!rows.length) return '<div class="empty">Aucune position ouverte.</div>';
  const m = Math.max(1e-9, ...rows.map(p => Math.abs(p.upnl)));
  return `<div class="bars contrib"><div class="brow bhead"><span class="n">Contrat</span><span></span><span class="v">USDT</span><span class="v">%</span></div>${rows.map(p => {const x = 50 * p.upnl / m;
    return `<div class="brow click" data-sym="${esc(p.symbol)}"><span class="n"><span class="g ${p.side === "long" ? "up" : "down"}">${p.side === "long" ? "▲" : "▼"}</span>${esc(p.symbol)}</span><span class="t"><span class="b" style="left:${x >= 0 ? 50 : 50 + x}%;width:${Math.abs(x).toFixed(2)}%;background:${x >= 0 ? css("--long") : css("--short")}"></span></span><span class="v ${cls(p.upnl)}">${usd(p.upnl, 2, true)}</span><span class="v ${cls(p.upnl_pct)}">${pct(p.upnl_pct, 2, true)}</span></div>`}).join("")}</div>`;
}
function bySymbol(T) {
  const rows = Object.entries(T.by_symbol || {}).map(([s, b]) => ({symbol: s, pnl: b.pnl, n: b.n, w: b.wins})).sort((a, b) => b.pnl - a.pnl);
  if (!rows.length) return '<div class="empty">Aucune position fermée pour l\'instant.</div>';
  const m = Math.max(1e-9, ...rows.map(r => Math.abs(r.pnl)));
  return `<div class="bars contrib"><div class="brow bhead"><span class="n">Contrat</span><span></span><span class="v">P&L net</span><span class="v">gagnantes</span></div>${rows.map(r => {const x = 50 * r.pnl / m;
    return `<div class="brow click" data-sym="${esc(r.symbol)}"><span class="n">${esc(r.symbol)}</span><span class="t"><span class="b" style="left:${x >= 0 ? 50 : 50 + x}%;width:${Math.abs(x).toFixed(2)}%;background:${x >= 0 ? css("--long") : css("--short")}"></span></span><span class="v ${cls(r.pnl)}">${usd(r.pnl, 2, true)}</span><span class="v">${r.w}/${r.n}</span></div>`}).join("")}</div>`;
}
function calendar(D) {
  // Daily P&L: last equity of each UTC day against the previous day's (the start equity for the first day).
  const last = {};
  D.eq.forEach(r => {last[new Date(r.t).toISOString().slice(0, 10)] = r.eq});
  const days = Object.keys(last).sort();
  if (!days.length) return '<div class="empty">Le calendrier se remplit jour après jour.</div>';
  const pnl = {};
  days.forEach((d, i) => {pnl[d] = last[d] - (i ? last[days[i - 1]] : (fin(D.e0) ? D.e0 : last[d]))});
  const m = Math.max(1e-9, ...Object.values(pnl).map(Math.abs));
  if (days.length <= 45) {  // a short history reads better as daily bars than as a mostly empty calendar
    const m2 = Math.max(1e-9, ...days.map(d => Math.abs(pnl[d])));
    return `<div class="dbars">${days.map(d => {const v = pnl[d], h = 100 * Math.abs(v) / m2;
      return `<div class="db" title="${d} : ${usd(v, 2, true)} USDT"><div class="up-h">${v >= 0 ? `<i style="height:${h.toFixed(1)}%;background:var(--long)"></i>` : ""}</div><div class="dn-h">${v < 0 ? `<i style="height:${h.toFixed(1)}%;background:var(--short)"></i>` : ""}</div><span>${d.slice(8)}/${d.slice(5, 7)}</span></div>`}).join("")}</div>
      <div class="cal-foot"><span>${days.length} jour${days.length > 1 ? "s" : ""} · ${Object.values(pnl).filter(x => x > 0).length} positifs · meilleur <span class="up">${usd(Math.max(...Object.values(pnl)), 2, true)}</span> · pire <span class="down">${usd(Math.min(...Object.values(pnl)), 2, true)}</span></span></div>`;
  }
  const start = new Date(days[0] + "T00:00:00Z"), end = new Date(days.at(-1) + "T00:00:00Z");
  start.setUTCDate(start.getUTCDate() - ((start.getUTCDay() + 6) % 7));  // back to Monday
  const cols = [];
  for (let w = new Date(start); w <= end; w.setUTCDate(w.getUTCDate() + 7)) {
    const cells = [];
    for (let k = 0; k < 7; k++) {
      const d = new Date(w); d.setUTCDate(d.getUTCDate() + k);
      const key = d.toISOString().slice(0, 10), v = pnl[key];
      const bg = !fin(v) ? "var(--hover)" : `color-mix(in srgb, ${v >= 0 ? "var(--long)" : "var(--short)"} ${Math.round(18 + 72 * Math.abs(v) / m)}%, var(--panel-2))`;
      cells.push(`<i style="background:${bg}" title="${key} : ${fin(v) ? usd(v, 2, true) + " USDT" : "—"}"></i>`);
    }
    cols.push(`<span class="wk">${cells.join("")}</span>`);
  }
  const tot = Object.values(pnl), pos = tot.filter(x => x > 0).length;
  return `<div class="cal"><span class="dl"><i>L</i><i></i><i>M</i><i></i><i>V</i><i></i><i>D</i></span>${cols.join("")}</div>
    <div class="cal-foot"><span>${days.length} jours · ${pos} positifs · meilleur <span class="up">${usd(Math.max(...tot), 2, true)}</span> · pire <span class="down">${usd(Math.min(...tot), 2, true)}</span></span>
    <span class="lg"><span>perte</span><i class="box" style="background:var(--short)"></i><i class="box" style="background:var(--hover)"></i><i class="box" style="background:var(--long)"></i><span>gain</span></span></div>`;
}
function vHistory(D) {
  const el = $("#v-history"), T = D.trades, st = T.stats || {};
  const f = S.filt, q = f.q.trim().toUpperCase();
  const closed = (T.closed || []).filter(t => (f.side === "all" || t.side === f.side) && (!q || t.symbol.includes(q)));
  const fills = D.fills.filter(x => !q || x.symbol.includes(q));
  const cols = [
    {k: "closed", h: "Fermée (UTC)", l: true, f: r => `<span class="num">${dt(r.closed * 1000)}</span>`},
    {k: "symbol", h: "Contrat", l: true, cl: "sym"},
    {k: "side", h: "Sens", l: true, f: r => sideTag(r.side)},
    {k: "opened", h: "Ouverte", cl: "num", f: r => dt(r.opened * 1000)},
    {k: "hold", h: "Durée", cl: "num", v: r => r.closed - r.opened, f: r => dur(r.closed - r.opened)},
    {k: "entry", h: "Entrée", cl: "num", f: r => price(r.entry)},
    {k: "exit", h: "Sortie", cl: "num", f: r => price(r.exit)},
    {k: "max_notional", h: "Taille max", cl: "num", f: r => usd(r.max_notional, 0)},
    {k: "pnl", h: "P&L net", cl: "num", f: r => `<span class="${cls(r.pnl)}">${usd(r.pnl, 2, true)}</span>`},
    {k: "ret", h: "Rendement", cl: "num", f: r => `<span class="${cls(r.ret)}">${pct(r.ret, 2, true)}</span>`},
    {k: "fees", h: "Frais", cl: "num", f: r => usd(r.fees, 2)},
    {k: "exit_kind", h: "Sortie par", f: r => r.exit_kind === "stop" ? '<span class="pill warn">stop</span>' : r.exit_kind === "flatten" ? '<span class="pill crit">arrêt</span>' : r.exit_kind === "external" ? '<span class="pill">hors moteur</span>' : '<span class="muted">rééquilibrage</span>'},
  ];
  el.innerHTML = `<div class="grid">
   <div class="c12"><div class="panel"><div class="stats">
    <div><div class="label">Positions fermées</div><div class="v">${st.n || 0}</div><div class="x"><span class="up">${st.n_long || 0} ▲</span> · <span class="down">${st.n_short || 0} ▼</span></div></div>
    <div><div class="label">P&L réalisé net</div><div class="v ${cls(st.pnl)}">${usd(st.pnl, 2, true)}</div><div class="x">frais déduits, hors funding</div></div>
    <div><div class="label">Taux de gain</div><div class="v">${pct(st.win_rate, 1)}</div><div class="x">gain moyen ${usd(st.avg_win, 2)} · perte ${usd(st.avg_loss, 2)}</div></div>
    <div><div class="label">Profit factor</div><div class="v">${num(st.profit_factor, 2)}</div><div class="x">gains / pertes</div></div>
    <div><div class="label">P&L long · short</div><div class="v"><span class="${cls(st.pnl_long)}">${usd(st.pnl_long, 0, true)}</span> <span class="muted">·</span> <span class="${cls(st.pnl_short)}">${usd(st.pnl_short, 0, true)}</span></div><div class="x">USDT</div></div>
    <div><div class="label">Durée moyenne</div><div class="v">${fin(st.avg_hold_h) ? dur(st.avg_hold_h * 3600) : "—"}</div><div class="x">horizon visé ${dur((D.strat.holding_bars || 48) * D.barMin * 60)}</div></div>
    <div><div class="label">Sorties par stop</div><div class="v">${st.n_stops || 0}</div><div class="x">frais payés ${usd(st.fees, 2)}</div></div>
    <div><div class="label">Meilleure · pire</div><div class="v"><span class="up">${usd(st.best, 0, true)}</span> <span class="muted">·</span> <span class="down">${usd(st.worst, 0, true)}</span></div><div class="x">USDT</div></div>
   </div></div></div>
   <div class="c5"><div class="panel" style="height:100%"><div class="ph"><h2>P&L par jour</h2><span class="sub">équité de fin de journée (UTC) face à la veille</span></div><div class="pb">${calendar(D)}</div></div></div>
   <div class="c7"><div class="panel" style="height:100%"><div class="ph"><h2>P&L réalisé par contrat</h2><span class="sub">positions fermées, net de frais</span></div>${bySymbol(T)}</div></div>
   <div class="c12"><div class="panel"><div class="ph"><h2>Positions fermées</h2><span class="sub">reconstruites des exécutions : de l'ouverture au retour à plat</span>
    <div class="right filters"><input class="search" id="q" placeholder="Filtrer un contrat…" value="${esc(f.q)}" aria-label="Filtrer">
     ${["all", "long", "short"].map(s => `<button class="btn-s" data-side="${s}" aria-pressed="${f.side === s}">${s === "all" ? "Tous" : s === "long" ? "▲ Long" : "▼ Short"}</button>`).join("")}</div></div>
    <div class="tw maxh">${table("t-closed", cols, closed, {click: true, sort: {k: "closed", dir: "desc"}, empty: "Aucune position fermée pour l'instant."})}</div></div></div>
   <div class="c12"><div class="panel"><div class="ph"><h2>Exécutions</h2><span class="sub">${num(Math.min(400, fills.length), 0)} plus récentes sur ${num(fills.length, 0)} chargées</span></div>
    <div class="tw maxh">${fillsTable("t-fills-all", fills, 400)}</div></div></div></div>`;
  const qi = $("#q"); qi.oninput = e => {S.filt.q = e.target.value; clearTimeout(qi._t); qi._t = setTimeout(() => {render(); const n = $("#q"); n.focus(); n.setSelectionRange(n.value.length, n.value.length)}, 250)};
  $$("[data-side]", el).forEach(b => b.onclick = () => {S.filt.side = b.dataset.side; render()});
  wireTables(el);
}

function vSignals(D) {
  const el = $("#v-signals"), dec = D.decision || {}, sc = dec.scores || {}, w = dec.weights || {};
  const rows = Object.entries(sc).map(([s, v]) => ({symbol: s, score: v, weight: w[s] || 0, target: (dec.targets || {})[s] || 0, pos: (posOf(D, s) || {}).notional || 0})).sort((a, b) => b.score - a.score);
  const m = Math.max(0.5, ...rows.map(r => Math.abs(r.score)));
  const bars = rows.map(r => {const x = 50 * r.score / m, col = r.score >= 0 ? css("--long") : css("--short");
    return `<div class="brow"><span class="n">${esc(r.symbol)}</span><span class="t"><span class="b" style="left:${x >= 0 ? 50 : 50 + x}%;width:${Math.abs(x).toFixed(2)}%;background:${col}"></span></span><span class="v">${num(r.score, 2, true)}</span><span class="v ${cls(r.weight)}">${r.weight ? pct(r.weight, 1, true) : "—"}</span></div>`}).join("");
  const dm = dailyMean(D.series.ic), dmr = dailyMean(D.series.ic_raw), dml = dailyMean(D.series.ic_lag);
  el.innerHTML = `<div class="grid">
   <div class="c7"><div class="panel"><div class="ph"><h2>Classement du modèle à la dernière décision</h2><span class="sub">${rows.length} contrats · ${dec.ts ? dt(dec.ts) + " UTC" : "—"}</span></div>
    <div class="pb" style="padding-bottom:0"><p class="cap">Barres : score du modèle (rendement résiduel attendu sur ${num((D.strat.holding_bars || 48) * D.barMin / 60, 0)} h, standardisé ; vert au-dessus de la moyenne, rouge en dessous). Dernière colonne : poids visé dans le livre. Le livre achète le haut, vend le bas, en neutralisant marché et styles.</p></div>
    <div class="bars" style="padding-bottom:0"><div class="brow bhead"><span class="n">Contrat</span><span style="text-align:center">score (barre)</span><span class="v">score</span><span class="v">poids</span></div></div>
    <div class="bars">${bars || '<div class="empty">Aucune décision enregistrée.</div>'}</div></div></div>
   <div class="c5"><div class="panel"><div class="ph"><h2>Qualité réalisée du classement</h2><span class="sub">IC, moyenne 7 jours</span></div>
    <div class="pb" style="padding-bottom:0">${legendHTML([{name: "Score tradé (lissé)", color: css("--s1")}, {name: "Score brut", color: css("--s2")}, {name: "Score vieux de 24 h", color: css("--s3")}].concat(fin(D.rs.ic) ? [{name: "Recherche", color: css("--ink-2"), dash: true}] : []))}</div>
    <div class="chart-wrap sm"><div class="lw" id="c-ic"></div><div class="chart-note" id="c-ic-note"></div></div>
    <div class="stats" style="border-top:1px solid var(--line)"><div><div class="label">Tradé 30 j</div><div class="v">${num(tailMean(dm, 30), 3)}</div></div><div><div class="label">Brut 30 j</div><div class="v">${num(tailMean(dmr, 30), 3)}</div></div><div><div class="label">Vieux 24 h, 30 j</div><div class="v">${num(tailMean(dml, 30), 3)}</div></div></div></div></div>
   <div class="c12"><div class="panel"><div class="ph"><h2>Univers et cibles</h2><span class="sub">Cliquer une ligne l'ouvre dans le graphique.</span></div><div class="tw maxh">${table("t-univ", [
     {k: "symbol", h: "Contrat", l: true, cl: "sym"},
     {k: "score", h: "Score", cl: "num", f: r => `<span class="${cls(r.score)}">${num(r.score, 3, true)}</span>`},
     {k: "weight", h: "Poids visé", cl: "num", f: r => pct(r.weight, 2, true)},
     {k: "target", h: "Cible USDT", cl: "num", f: r => usd(r.target, 0, true)},
     {k: "pos", h: "Position", cl: "num", f: r => r.pos ? `<span class="${cls(r.pos)}">${usd(r.pos, 0, true)}</span>` : "—"},
   ], rows, {click: true, sort: {k: "score", dir: "desc"}, empty: "Aucune décision enregistrée."})}</div></div></div></div>`;
  $("#c-ic-note").textContent = dm.length < 2 ? "L'IC réalisé apparaît une fois l'horizon écoulé (24 h)." : "";
  const s = (d, c, name, dash) => ({name, color: c, dash, data: roll(d, 7).map(([t, v]) => ({time: utc(t), value: v}))});
  lineChart("ic", $("#c-ic"), {series: [s(dm, css("--s1"), "Tradé"), s(dmr, css("--s2"), "Brut"), s(dml, css("--s3"), "Vieux 24 h")],
    refs: [{y: 0, label: "", color: css("--line-3")}].concat(fin(D.rs.ic) ? [{y: D.rs.ic, label: "recherche"}] : []), fmt: v => num(v, 3), tip: true});
  wireTables(el);
}

function meter(label, v, max, marks, fmt, sub, color) {
  const w = fin(v) && max ? Math.min(100, 100 * Math.abs(v) / max) : 0;
  return `<div class="meter"><div class="h"><span>${label}</span><span class="v">${fmt(v)}</span></div>
   <div class="track"><div class="fill" style="width:${w.toFixed(1)}%;background:${color || css("--s1")}"></div>${marks.map(m => `<span class="tick ${m.c || ""}" style="left:calc(${Math.min(100, 100 * m.v / max).toFixed(1)}% - 1px)"></span>`).join("")}</div>
   <div class="ticks">${marks.map(m => `<span style="left:${Math.min(97, Math.max(3, 100 * m.v / max)).toFixed(1)}%">${esc(m.l)}</span>`).join("")}</div>${sub ? `<div class="x">${sub}</div>` : ""}</div>`;
}
function vRisk(D) {
  const el = $("#v-risk"), r = D.st.risk || {}, lim = D.st.risk_limits || {}, s = D.strat;
  const dd = r.drawdown || 0, soft = lim.drawdown_soft || s.drawdown_soft || 0.1, hard = lim.drawdown_hard || s.drawdown_hard || 0.25;
  const gmax = lim.gross_max || s.gross_max || 2.5, es = r.es_1d, esl = s.es_limit_daily || 0.04, dl = lim.daily_loss_limit || s.daily_loss_limit || 0.03;
  const flag = (on, label, desc) => `<div class="check"><span class="ic ${on ? "ko" : "ok"}">${on ? "!" : "✓"}</span><div><div class="name">${label}</div><div class="desc">${desc}</div></div><span></span><span class="pill ${on ? "crit" : "good"}">${on ? "actif" : "non"}</span></div>`;
  el.innerHTML = `<div class="grid">
   <div class="c6"><div class="panel"><div class="ph"><h2>Limites de risque</h2><span class="sub">valeur actuelle face aux seuils</span></div>
    ${meter("Drawdown depuis le plus haut", dd, hard * 1.1, [{v: soft, l: "réduction " + pct(soft, 0), c: "warn"}, {v: hard, l: "arrêt " + pct(hard, 0), c: "crit"}], v => pct(v, 2), "Au-delà de la réduction, la taille du livre baisse progressivement ; à l'arrêt, tout est fermé et une décision humaine est requise.", dd > soft ? css("--crit") : css("--s1"))}
    ${meter("Perte attendue en queue (ES 97,5 %, 1 jour)", es, esl * 1.5, [{v: esl, l: "limite " + pct(esl, 1), c: "crit"}], v => pct(v, 2), "Au-dessus de la limite, le livre est réduit à proportion (facteur ×" + num(r.es_scale, 2) + ").")}
    ${meter("Exposition brute", D.st.gross, gmax * 1.1, [{v: gmax, l: "plafond " + num(gmax, 1) + "×", c: "crit"}], v => num(v, 2) + "×", "Longs + shorts, en multiple du capital alloué.")}
    ${meter("Exposition nette", Math.abs(D.st.net || 0), (s.net_max || 0.25) * 1.5, [{v: s.net_max || 0.25, l: "plafond ±" + num(s.net_max || 0.25, 2) + "×", c: "crit"}], () => num(D.st.net, 2, true) + "×", "Longs − shorts : le livre vise la neutralité au marché (bêta).", css("--s2"))}
    ${meter("Volatilité ex ante (annuelle)", r.ex_ante_vol, (s.vol_target || 0.2) * 1.5, [{v: s.vol_target || 0.2, l: "cible " + pct(s.vol_target || 0.2, 0)}], v => pct(v, 1), "Volatilité prévue du livre par la covariance EWMA ; la cible est atteinte quand l'IC estimé vaut la référence.", css("--s3"))}
    ${meter("Positions", D.pos.length, (s.max_positions || 40) * 1.1, [{v: s.max_positions || 40, l: "max " + (s.max_positions || 40), c: "crit"}], v => num(v, 0), "")}
   </div></div>
   <div class="c6"><div class="panel"><div class="ph"><h2>États et garde-fous</h2></div>
    ${flag(D.st.halted, "Arrêt du moteur", D.st.halted ? esc(D.st.halt_reason || "") : "Déclenché par le drawdown d'arrêt, la perte journalière (" + pct(dl, 0) + ") ou l'interrupteur d'urgence (hermes live kill).")}
    ${flag(r.reduce_only === 1, "Réductions seulement", "Après une perte journalière ou sur données périmées : aucune position ne grossit.")}
    ${flag(fin(r.regime_scale) && r.regime_scale < 1, "Garde de régime", fin(r.regime_scale) ? "Taille ×" + num(r.regime_scale, 2) + " quand BTC est à plus de " + pct((s.regime_gate || {}).drawdown, 0) + " sous son plus haut de " + esc((s.regime_gate || {}).lookback_days || 90) + " j." : "Désactivée pour ce modèle.")}
    ${flag(r.psi_alert === 1, "Dérive des variables", driftText(r))}
    <div class="stats" style="border-top:1px solid var(--line)">
     <div><div class="label">Budget de risque</div><div class="v">${pct(r.budget, 0)}</div><div class="x">selon le drawdown</div></div>
     <div><div class="label">Facteur ES</div><div class="v">×${num(r.es_scale, 2)}</div><div class="x">1 = pas de réduction</div></div>
     <div><div class="label">Amortissement coûts</div><div class="v">×${num(r.cost_scale, 2)}</div><div class="x">persistance du signal</div></div>
     <div><div class="label">NAV stratégie</div><div class="v">${usd(r.nav, 0)}</div><div class="x">capital alloué ${pct(s.capital_fraction, 0)}</div></div>
    </div></div></div>
   <div class="c6"><div class="panel"><div class="ph"><h2>Drawdown</h2></div><div class="chart-wrap sm"><div class="lw" id="c-dd"></div></div></div></div>
   <div class="c6"><div class="panel"><div class="ph"><h2>Exposition</h2><div class="right">${legendHTML([{name: "Brute", color: css("--s1")}, {name: "Nette", color: css("--s2")}])}</div></div><div class="chart-wrap sm"><div class="lw" id="c-ex"></div></div></div></div>
   <div class="c12"><div class="panel"><div class="ph"><h2>Stops catastrophe</h2><span class="sub">distance entre le prix et le stop de chaque position</span></div><div class="tw">${table("t-stops", [
     {k: "symbol", h: "Contrat", l: true, cl: "sym"}, {k: "side", h: "Sens", l: true, f: r => sideTag(r.side)},
     {k: "mark", h: "Prix", cl: "num", f: r => price(r.mark)}, {k: "stop", h: "Stop", cl: "num", f: r => price(r.stop)},
     {k: "stop_dist", h: "Distance", cl: "num", f: r => pct(r.stop_dist, 2)},
     {k: "loss", h: "Perte au stop", cl: "num", v: r => r.stop_dist * Math.abs(r.notional), f: r => fin(r.stop_dist) ? `<span class="down">${usd(-r.stop_dist * Math.abs(r.notional), 0)}</span>` : "—"},
   ], D.pos, {click: true, sort: {k: "stop_dist", dir: "asc"}, empty: "Aucune position, donc aucun stop."})}</div></div></div></div>`;
  lineChart("dd", $("#c-dd"), {series: [{name: "Drawdown", color: css("--short"), type: "under", data: D.eq.map(x => ({time: utc(x.t), value: -(x.dd || 0)}))}],
    refs: [{y: -soft, label: "réduction", color: css("--warn")}, {y: -hard, label: "arrêt", color: css("--crit")}], include: [0, -soft], fmt: v => pct(v, 1), tip: true});
  lineChart("ex", $("#c-ex"), {series: [{name: "Brute", color: css("--s1"), data: D.eq.map(x => ({time: utc(x.t), value: x.g}))}, {name: "Nette", color: css("--s2"), data: D.eq.map(x => ({time: utc(x.t), value: x.n}))}],
    refs: [{y: gmax, label: "plafond", color: css("--crit")}], include: [0, gmax], fmt: v => num(v, 2) + "×", tip: true});
  wireTables(el);
}

const GATE = {
  dsr: ["Sharpe dégonflé (DSR)", "Le Sharpe survit-il au nombre d'essais testés ?", v => num(v, 2), t => "≥ " + num(t, 2)],
  null_pvalue: ["Test nul (permutations)", "Probabilité d'un tel résultat sans signal", v => num(v, 3), t => "≤ " + num(t, 2)],
  pbo: ["Surapprentissage (PBO)", "Probabilité que le meilleur réglage soit un faux positif", v => num(v, 2), t => "≤ " + num(t, 2)],
  sharpe: ["Sharpe net", "Hors échantillon, après tous les coûts", v => num(v, 2), t => "≥ " + num(t, 2)],
  positive_years: ["Années positives", "Part des années civiles gagnantes", v => pct(v, 0), t => "≥ " + pct(t, 0)],
  oos_months: ["Mois hors échantillon", "Durée de la preuve", v => num(v, 0), t => "≥ " + num(t, 0)],
  cost_stress: ["Coûts doublés", "Sharpe si les coûts sont deux fois plus élevés", v => num(v, 2), t => "> " + num(t, 0)],
  latency_stress: ["Une bougie de retard", "Sharpe avec une exécution décalée", v => num(v, 2), t => "> " + num(t, 0)],
  stop_stress: ["Stops au pire", "Sharpe si chaque stop s'exécute au plus mauvais prix de la bougie", v => num(v, 2), t => "> " + num(t, 0)],
};
function gateHTML(g) {
  const keys = Object.keys(g || {});
  if (!keys.length) return '<div class="empty">Ce modèle a été produit avant que les critères ne soient inclus dans le modèle. Résultats : docs/RESULTS.md.</div>';
  const ok = keys.filter(k => g[k].pass).length;
  return `<div class="pb" style="padding-bottom:6px"><span class="pill ${ok === keys.length ? "good" : "warn"}">${ok} critères sur ${keys.length}</span> <span class="muted" style="font-size:11.5px;margin-left:6px">Tous sont requis pour trader de l'argent réel.</span></div>` +
    keys.map(k => {const d = GATE[k] || [k, "", v => num(v, 2), t => num(t, 2)], x = g[k];
      return `<div class="check"><span class="ic ${x.pass ? "ok" : "ko"}">${x.pass ? "✓" : "✕"}</span><div><div class="name">${esc(d[0])}</div><div class="desc">${esc(d[1])}</div></div><span class="val">${d[2](x.value)}</span><span class="thr">${d[3](x.threshold)}</span></div>`}).join("");
}
function vModel(D) {
  const el = $("#v-model"), b = D.b, rs = D.rs, se = D.series;
  const last = n => {const a = se[n] || []; return a.length ? a.at(-1)[1] : null};
  const dm = dailyMean(se.ic), dmr = dailyMean(se.ic_raw), dml = dailyMean(se.ic_lag);
  const checks = [
    ["IC du score tradé, 30 j", num(tailMean(dm, 30), 3), "dimensionne le livre (score lissé)"],
    ["IC du score brut, 30 j", num(tailMean(dmr, 30), 3), "le modèle classe-t-il encore ?"],
    ["IC du score vieux de 24 h, 30 j", num(tailMean(dml, 30), 3), "la partie lente, celle que le livre détient, tient-elle ?"],
    ["BTC face à son plus haut 90 j", pct(last("btc_dd90"), 1), "au-delà de −15 % : régime de baisse (IC historiquement plus faible)"],
    ["Rendement moyen du marché, 30 j", pct(last("mkt_ret30"), 1), "négatif : marché baissier"],
    ["Autocorrélation transversale 1 j (moyenne 30 j)", num(last("xs_ac1"), 3), "positive : la continuation remplace le retournement"],
  ];
  el.innerHTML = `<div class="grid">
   <div class="c5"><div class="panel"><div class="ph"><h2>Modèle en service</h2>${b.promoted ? '<span class="pill good">promu</span>' : '<span class="pill warn">non promu · incubation</span>'}</div>
    <div class="pb"><div class="cards" style="grid-template-columns:1fr">
     <div class="card"><h3>${esc(rs.name || "—")}</h3><dl class="kv">
      <dt>Identifiant</dt><dd>${esc(b.config_hash || "—")}</dd>
      <dt>Entraîné sur</dt><dd>${esc(String(b.train_start || "").slice(0, 10))} → ${esc(String(b.train_end || "").slice(0, 10))}</dd>
      <dt>Bougie · détention</dt><dd>${esc(D.bar)} · ${bookHolding(D)}</dd>
      <dt>Cible</dt><dd>${esc(TARGET[D.strat.target] || D.strat.target || "—")}</dd>
      <dt>Ensemble</dt><dd>GBM + ridge, ${esc(ENSEMBLE[D.strat.ensemble] || D.strat.ensemble || "—")}</dd>
      <dt>IC a priori</dt><dd>${esc(fin(b.prior_ic) ? num(b.prior_ic, 3) : "borne basse de validation")}</dd>
     </dl></div>
     <div class="card"><h3>Ce que la recherche attend</h3><dl class="kv">
      <dt>Sharpe net</dt><dd>${num(rs.sharpe, 2)}</dd><dt>Rendement annuel</dt><dd>${pct(rs.cagr, 1)}</dd>
      <dt>Volatilité</dt><dd>${pct(rs.vol, 1)}</dd><dt>Drawdown max</dt><dd>${pct(rs.max_drawdown, 1)}</dd><dt>IC moyen</dt><dd>${num(rs.ic, 3)}</dd></dl>
      <p>Hors échantillon (walk-forward purgé), net de frais, spread, impact et funding.</p></div></div></div></div></div>
   <div class="c7"><div class="panel"><div class="ph"><h2>Porte de promotion</h2><span class="sub">le mode réel refuse tout modèle qui ne la franchit pas</span></div>${gateHTML(b.gate)}</div></div>
   <div class="c12"><div class="panel"><div class="ph"><h2>Contrôles pré-enregistrés de l'incubation</h2><span class="sub">fixés avant toute donnée en direct (docs/RESULTS.md, § 10)</span></div>
    <div class="pb" style="padding-bottom:0"><p class="cap">Si l'IC remonte hors baisse de BTC : le creux de 2026 était un régime. Si l'IC brut reste positif mais celui du score vieux de 24 h nul : seule la partie lente est perdue. Si tout reste près de zéro : l'avantage est perdu.</p></div>
    <div class="tw">${table("t-checks", [{k: 0, h: "Contrôle", l: true, nosort: true}, {k: 1, h: "Valeur", cl: "num", nosort: true}, {k: 2, h: "Lecture", l: true, nosort: true, cl: "wrap muted"}], checks.map(c => ({0: c[0], 1: c[1], 2: c[2]})))}</div></div></div></div>`;
  wireTables(el);
}

function vSystem(D) {
  const el = $("#v-system"), st = D.st, s = D.strat, ex = st.execution || {}, r = st.risk || {}, acc = st.account || {};
  const card = (title, state, rows, p) => `<div class="card"><h3><span class="dot ${state}"></span>${title}</h3><dl class="kv">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>${p ? `<p>${p}</p>` : ""}</div>`;
  const fresh = D.health;
  el.innerHTML = `<div class="grid"><div class="c12"><div class="panel"><div class="ph"><h2>Chaîne de décision</h2><span class="sub">données → univers → modèle → portefeuille → risque → exécution, toutes les ${D.barMin * (s.rebalance_every || 1)} min</span></div><div class="pb"><div class="cards">
    ${card("Données de marché", fresh, [["Source", "Binance USDT-M (public)"], ["Dernière bougie", st.bar ? dt(st.bar) + " UTC" : "—"], ["Mise à jour", st.updated ? dt(st.updated) + " UTC" : "—"], ["Cycle", fin(st.cycle_s) ? num(st.cycle_s, 1) + " s" : "—"]], "Bougies fermées seulement ; au-delà d'une bougie de retard, le livre ne fait que réduire.")}
    ${card("Univers", "good", [["Taille", `top ${esc(s.universe_top_n || "—")} par liquidité`], ["Membres à la décision", esc(st.n_members ?? "—")], ["Filtre", s.venue === "okx" ? "listés sur OKX la veille" : "tous"], ["Positionnement", (s.positioning || []).length ? esc(s.positioning.join(", ")) : "non utilisé"]], "Point-in-time : liquidité et ancienneté calculées sur l'historique connu à la date.")}
    ${card("Modèle", D.b.promoted ? "good" : "warn", [["Nom", esc(D.rs.name || "—")], ["IC estimé", num(st.ic_est, 3)], ["Promu", D.b.promoted ? "oui" : "non"], ["Funding 8 h", s.funding_per_8h ? "oui" : "non"]], "Taille ∝ IC estimé causalement (réalisé, borne de validation en a priori).")}
    ${card("Portefeuille", "good", [["Cible de vol", pct(s.vol_target, 0)], ["Plafonds brut · net", `${num(s.gross_max, 1)}× · ${pct(s.net_max, 0)}`], ["Poids max", pct(s.weight_max, 0)], ["Neutralité", `${s.beta_neutral ? "bêta" : ""}${s.style_neutral ? " + styles" : ""}`], ["Aversion aux coûts", num(s.cost_aversion, 1)]], "Optimiseur moyenne-variance avec coûts de transaction et zone de non-échange.")}
    ${card("Risque", st.halted ? "crit" : "good", [["Drawdown réduction · arrêt", `${pct(s.drawdown_soft, 0)} · ${pct(s.drawdown_hard, 0)}`], ["Perte jour max", pct(s.daily_loss_limit, 0)], ["ES 1 j max", pct(s.es_limit_daily, 1)], ["Stops", `${num(s.stop_sigmas, 0)} σ jour`]], "Indépendant du modèle et des données : l'arrêt et l'interrupteur d'urgence agissent même si le reste échoue.")}
    ${card("Exécution", (ex.errors || []).length ? "warn" : "good", [["Courtier", S.mode === "paper" ? "papier (simulation)" : "OKX"], ["Part maker", pct(ex.maker_share, 0)], ["Écart au prix de décision", fin(ex.shortfall_bps) ? num(ex.shortfall_bps, 1) + " pb" : "—"], ["Dernier volume", usd(ex.traded, 0) + " USDT"], ["Erreurs", (ex.errors || []).length]], "Ordres passifs d'abord, puis agressifs ; stop catastrophe posé côté bourse.")}
    ${card("Compte", "good", [["Équité", usd(acc.equity ?? st.equity, 2)], ["Capital de départ", usd(acc.initial, 0)], ["Frais payés", usd(acc.fees_paid, 2)], ["Funding payé", usd(acc.funding_paid, 2)]], S.mode === "paper" ? "Compte fictif : exécutions simulées sur les prix réels (spread, glissement et frais OKX)." : "")}
   </div></div></div></div>
   <div class="c12"><div class="panel"><div class="ph"><h2>Notes de la dernière décision</h2></div><div class="pb">${(st.notes || []).length ? `<ul style="margin:0;padding-left:18px;color:var(--ink-2)">${st.notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul>` : '<span class="muted">Aucune.</span>'}</div></div></div></div>`;
}

function vJournal(D) {
  const el = $("#v-journal"), f = S.filt.lvl;
  const ev = D.events.filter(e => f === "all" || e[1] === f);
  el.innerHTML = `<div class="panel"><div class="ph"><h2>Journal du moteur</h2><span class="sub">${D.events.length} derniers événements</span><div class="right filters">${[["all", "Tous"], ["ERROR", "Erreurs"], ["WARNING", "Alertes"], ["INFO", "Infos"]].map(([k, l]) => `<button class="btn-s" data-lvl="${k}" aria-pressed="${f === k}">${l}</button>`).join("")}</div></div>${eventsHTML(ev)}</div>`;
  $$("[data-lvl]", el).forEach(b => b.onclick = () => {S.filt.lvl = b.dataset.lvl; render()});
}

/* A 1/N book (portfolio.books) holds several settings at once: show them all. */
function bookHolding(D) {
  const books = Array.isArray(D.strat.books) ? D.strat.books : [];
  if (!books.length) return dur((D.strat.holding_bars || 48) * D.barMin * 60);
  const hs = [...new Set(books.map(b => b[0]))].sort((x, y) => x - y).map(h => dur(h * D.barMin * 60));
  return `livre 1/N · ${books.length} réglages (${hs.join(", ")})`;
}

function vInactive(mode) {
  const el = $("#v-inactive");
  const live = mode === "live";
  el.innerHTML = `<div class="panel"><div class="hero">
   <div><img class="hero-mark on-dark" src="static/brand/mark.svg" alt="" width="56" height="56"><img class="hero-mark on-light" src="static/brand/mark-light.svg" alt="" width="56" height="56">
    <span class="mode-badge ${live ? "live" : ""}">${esc(MODE_LABEL[mode] || mode).toUpperCase()} · INACTIF</span>
    <h1>${live ? "Aucun argent réel n'est engagé." : "Aucun moteur ne tourne dans ce mode."}</h1>
    ${live ? `<p>Le mode réel est verrouillé par construction : le moteur refuse de trader un modèle qui n'a pas franchi le nombre requis de critères de la porte de promotion (7 sur 9), et l'activer reste une décision humaine explicite.</p>
    <p>Le modèle actuel est en incubation en papier : il décide à chaque bougie sur les vrais prix, sans argent réel, pour vérifier que son avantage tient sur des données jamais vues.</p>` : `<p>Ce mode n'a encore publié aucun état.</p>`}
    <ol class="steps">
     <li><span class="n">1</span><span><b>Porte de promotion franchie</b> — Sharpe dégonflé, test nul, surapprentissage, années positives, stress de coûts, de latence et de stops.</span></li>
     <li><span class="n">2</span><span><b>Incubation papier concluante</b> — IC réalisé et courbe d'équité conformes à la recherche (onglet Papier, section Modèle).</span></li>
     <li><span class="n">3</span><span><b>Décision humaine</b> — clés OKX, fraction du capital (25 % par défaut), puis démarrage explicite du mode réel.</span></li>
    </ol></div>
   <div><div class="card"><h3>Garde-fous du mode réel</h3><dl class="kv">
    <dt>Modèle non promu</dt><dd>refusé</dd><dt>Capital engagé</dt><dd>fraction configurée</dd><dt>Stop par position</dt><dd>côté bourse</dd>
    <dt>Drawdown d'arrêt</dt><dd>tout est fermé</dd><dt>Interrupteur d'urgence</dt><dd>hermes live kill</dd><dt>Tableau de bord</dt><dd>lecture seule</dd></dl></div></div>
  </div></div>`;
}

/* ---------------------------------------------------------------- render loop */
function render() {
  const has = S.snap && S.snap.status && Object.keys(S.snap.status).length;
  renderModes();
  const D = has ? derive(S.snap) : null;
  renderStatus(D); renderNav(D); renderKPIs(D);
  $$(".view").forEach(v => v.classList.remove("on"));
  if (!D) {if (S.loaded) {$("#v-inactive").classList.add("on"); vInactive(S.mode)} return}
  const view = VIEWS.some(v => v[0] === S.view) ? S.view : "terminal";
  const el = $("#v-" + view); el.classList.add("on");
  ({terminal: vTerminal, positions: vPositions, history: vHistory, signals: vSignals, risk: vRisk, model: vModel, system: vSystem, journal: vJournal})[view](D);
  // charts of hidden views are rebuilt when shown
  for (const k of Object.keys(CH)) if (!document.body.contains(CH[k].host) || !CH[k].host.offsetParent) {try {CH[k].chart.remove()} catch (e) {/* */} delete CH[k]}
}
async function refresh() {
  const [modes, snap] = await Promise.all([getJSON("api/modes"), getJSON(`api/snapshot?mode=${encodeURIComponent(S.mode)}`)]);
  if (modes) S.modes = modes;
  if (snap && snap.mode === S.mode) S.snap = snap;
  S.loaded = true;
  const first = !S.candles;
  // The engine publishes once per bar: when nothing changed, only the header is refreshed (no chart flicker).
  const st = (S.snap && S.snap.status) || {};
  const sig = [S.mode, S.view, st.updated, (S.snap && S.snap.fills || []).length, (S.snap && S.snap.events || []).length].join("|");
  if (sig === S.sig && !first) {renderModes(); const D = S.snap && Object.keys(st).length ? derive(S.snap) : null; renderStatus(D); return}
  S.sig = sig;
  render();
  if (S.snap && S.snap.status && Object.keys(S.snap.status).length && (first || S.view === "terminal")) loadCandles();
}
$("#theme").onclick = () => {const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = t; store.set("theme", t); destroyCharts();
  const vt = $("#v-terminal"); if (vt) {delete vt.dataset.built; vt.innerHTML = ""} render(); loadCandles()};
setInterval(() => {
  const c = $("#clock"); if (c) {const d = new Date(); c.textContent = `${two(d.getUTCHours())}:${two(d.getUTCMinutes())}:${two(d.getUTCSeconds())}`}
  const cd = $("#cd"); if (cd && S.snap) {const D = derive(S.snap), c = cadence(D.bar, D.strat.rebalance_every), left = Math.max(0, c.next - Date.now());
    cd.textContent = D.health === "good" ? `${Math.floor(left / 60000)}:${two(Math.floor(left / 1000) % 60)}` : "en attente"}
}, 1000);
document.addEventListener("keydown", e => {
  if (e.target.closest("input,select,textarea") || e.metaKey || e.ctrlKey || e.altKey) return;
  const k = +e.key;
  if (k >= 1 && k <= VIEWS.length && S.snap && S.snap.status && Object.keys(S.snap.status).length) {S.view = VIEWS[k - 1][0]; store.set("view", S.view); render()}
  else if (e.key === "t") $("#theme").click();
});
$("#kpis").innerHTML = Array.from({length: 8}, () => '<div class="kpi"><div class="skeleton" style="width:40%;height:10px"></div><div class="skeleton" style="width:70%;height:22px;margin-top:10px"></div><div class="skeleton" style="width:55%;height:10px;margin-top:10px"></div></div>').join("");
setInterval(refresh, 20000);
setInterval(() => {if (S.view === "terminal" && S.sym) loadCandles()}, 30000);
document.addEventListener("visibilitychange", () => {if (!document.hidden) refresh()});
refresh();
})();
