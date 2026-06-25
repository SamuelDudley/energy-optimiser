/* eslint-disable no-console */
// Energy Optimiser dashboard — vanilla JS + Plotly.
//
// Hard rule: every value rendered comes from a real API response. When a
// field is null/missing, render "—" or a gap. No fabricated values, no
// noise added "for visual interest".
//
// Data sources, in order of authority:
//   /dashboard/stream    — SSE push of TickSnapshots. Primary "now"
//                          source; fires once per tick (~60 s). The
//                          `/plan/current` poll below is a fallback
//                          used only while the stream is disconnected.
//   /plan/current        — fallback snapshot fetch when SSE is down.
//   /telemetry           — historical 5-min rows. Past lines.
//   /dashboard/config    — battery config (soc_floor_pct etc.).
//   /logs                — recent operational events.
//
// Layout: one Plotly figure (#ts-figure) holds 6 stacked subplots that
// share a single x-axis: prices, decision ribbon, solar, SOC, grid,
// cost. A second figure (#sankey-today-figure) holds the today/range
// energy-flow Sankey, summed from telemetry. Status strip and loads /
// events are plain DOM.

"use strict";

import { SLOT_MS, nearestSlotAt, toNemDate, toEpochSec } from "./time-utils.js";
import {
  DECISION, MODE, MODE_COLORS,
  DEADBAND_KW, MODE_SWITCH_HYSTERESIS_KW,
  decisionFor, decisionFromTelemetry, modeFromSlot, modeFromTelemetry,
} from "./classify.js";
import {
  mergePriceForecasts, mergePVForecasts, pickPriceAt, coalesce,
} from "./price-merge.js";
import {
  colorForLoadId, hexToRgba, marginalCost,
} from "./derive.js";
import { buildUnionX, alignSeries } from "./timeline.js";
import { bandColumns } from "./bands.js";
import { buildTsFigure } from "./panels.js";

// ── Constants ──────────────────────────────────────────────────────

const POLL_INTERVAL_MS = 15_000;
const HISTORY_LOOKBACK_MS = 24 * 3600 * 1000;       // past 24h
const FUTURE_HORIZON_MS = 48 * 3600 * 1000;          // x-axis right edge
// Telemetry rows land every 5 min; refresh history at twice that rate so
// the chart's right edge keeps marching forward without the SSE-driven
// cursor pulling away from the last drawn sample.
const HISTORY_REFRESH_MS = 150_000;

const TOKEN_LS_KEY = "eo_dashboard_token";

// Sankey nodes. Index order matters — referenced by source/target.
// Labels are made distinct (Plotly groups same-labelled nodes oddly in
// some layouts, and "Battery" appears as both a source and a sink).
//
// `x`/`y` are explicit so the solver always renders sources on the
// left and sinks on the right with a fixed top-to-bottom order:
//   LEFT  (top → bottom): PV, Battery, Grid
//   RIGHT (top → bottom): Battery, House, Grid
// `arrangement: "fixed"` (set in buildSankeyTrace) makes Plotly honour
// these exactly. Coordinates avoid 0 and 1 because nodes drawn at the
// extreme borders are clipped to single-pixel slivers.
const SANKEY_NODES = [
  { name: "PV",                      x: 0.01, y: 0.05 }, // 0
  { name: "Grid (import)",           x: 0.01, y: 0.95 }, // 1
  { name: "Battery (discharging)",   x: 0.01, y: 0.50 }, // 2 — source side
  { name: "House",                   x: 0.99, y: 0.50 }, // 3
  { name: "Battery (charging)",      x: 0.99, y: 0.05 }, // 4 — sink side
  { name: "Grid (export)",           x: 0.99, y: 0.95 }, // 5
];
const SANKEY_NODE_COLORS = [
  "#f2cc60",  // PV
  "#f0883e",  // grid in
  "#79c0ff",  // batt out
  "#c9d1d9",  // house
  "#79c0ff",  // batt in
  "#56d364",  // grid out
];
// Each link entry: [sourceIdx, targetIdx, color, label]
const SANKEY_LINK_DEFS = [
  [0, 3, "rgba(242,204, 96, 0.45)", "PV → House"],
  [0, 4, "rgba(242,204, 96, 0.45)", "PV → Battery"],
  [0, 5, "rgba(242,204, 96, 0.45)", "PV → Export"],
  [1, 3, "rgba(240,136, 62, 0.45)", "Grid → House"],
  [1, 4, "rgba(240,136, 62, 0.45)", "Grid → Battery"],
  [2, 3, "rgba(121,192,255, 0.45)", "Battery → House"],
  [2, 5, "rgba(121,192,255, 0.45)", "Battery → Export"],
];

// Below this kW magnitude, treat a flow as numerical noise and hide it
// from the Sankey. Conservative — small flows shouldn't dominate the
// view but should still be visible. 30 W is below the inverter's
// readability for most channels.
const SANKEY_NOISE_KW = 0.03;

// Service-state value (string, from /readyz) → CSS class for the badge.
const STATE_CLASS = {
  active:           "status-state-active",
  active_no_price:  "status-state-active",
  degraded:         "status-state-degraded",
  fallback:         "status-state-fallback",
  initialise:       "status-state-unknown",
};

// EventTypes worth surfacing in the events ticker. Anything else is hidden.
const NOTABLE_EVENT_PREFIXES = [
  "fallback", "breaker", "verify_deviation", "export_blocked", "price_stale",
  "modbus_error", "validation_reject", "hw_cycle_fault",
  "load_cycle_fault", "mode2_trim_blind", "pv_curtailment",
];

// Subplot vertical layout (top → bottom). Domain values are cumulative.
// `label` renders horizontally at the top-left of each panel domain — much
// easier to scan than rotated y-axis titles. `units` is a separate hint
// shown next to the label.
const PANEL_LAYOUT = [
  { id: "prices",   axis: "y",  height: 0.22, label: "PRICE",   units: "c/kWh" },
  { id: "ribbon",   axis: "y2", height: 0.035, label: "DECISION" },
  { id: "mode",     axis: "y9", height: 0.035, label: "MODE" },
  { id: "solar",    axis: "y3", height: 0.18, label: "PV",      units: "kW" },
  { id: "soc",      axis: "y4", height: 0.16, label: "SOC",     units: "%" },
  { id: "load",     axis: "y7", height: 0.22, label: "LOAD",    units: "kW" },
  { id: "grid",     axis: "y5", height: 0.14, label: "GRID",    units: "kW" },
  { id: "cost",     axis: "y6", height: 0.16, label: "COST",    units: "c/h" },
];

// Stable colour per managed-load id (hash → palette). Distinct from the
// chart's other panel colours so a managed-load trace doesn't visually
// collide with grid / load lines that may share screen space at narrow
// widths. (LOAD_PALETTE, colorForLoadId, hexToRgba imported from derive.js)
const PANEL_GAP = 0.03;

// Shared figure styling. One font stack used everywhere so the dashboard
// reads consistently across panels.
const FONT_FAMILY =
  'Inter, "Segoe UI Variable", "Segoe UI", ui-sans-serif, system-ui, ' +
  '-apple-system, Roboto, "Helvetica Neue", Arial, sans-serif';
const HOVER_LABEL = {
  bgcolor: "#161b22",
  bordercolor: "#444c56",
  font: { family: FONT_FAMILY, size: 12, color: "#e8edf2" },
};

// Plotly reserves a fixed pixel margin for axes — at 44 px (the desktop
// default) that's a meaningful chunk of a phone-width plot. Detect the
// narrow viewport via the same breakpoint as the CSS so margins shrink
// in lockstep with panel padding. `automargin: true` on each y-axis
// means these are minimums; Plotly will grow them if a long tick label
// (e.g. "1234") would otherwise clip.
// Thin alias around the shared helper in chart-utils.js — keeps existing
// call sites untouched while the breakpoint and matchMedia plumbing live
// in one place. New chart code should call `eoChart.isNarrow()` directly.
function isNarrowViewport() {
  return window.eoChart ? window.eoChart.isNarrow() : false;
}

// ── State ──────────────────────────────────────────────────────────

const state = {
  token: null,
  config: null,
  snapshot: null,
  ready: null,                    // /readyz response: { ok, state, sigenergy_connected }
  sseConnected: false,            // true while /dashboard/stream is live; falls back to polling when false
  modes: [],                      // active user-strategy modes; refreshed from snap.active_modes
  history: {
    rows: [],                    // telemetry rows ascending by ts
    priceForecast: [],           // latest forecast band per (interval_start, resolution)
    pvForecast: [],              // latest p10/p50/p90 per period_end
    amberUsage: [],              // amber_usage rows (settled per-5-min spend)
    loadTelemetry: [],           // load_telemetry rows (per-load 5-min power/energy)
    dailySpend: [],              // /daily_spend rows (descending by nem_date)
    loadedAt: 0,                 // ms epoch of last successful loadHistory()
    inFlight: false,             // guard against overlapping refreshes
  },
  events: [],                     // recent notable events
  cursor: {
    time: null,                   // Date | null
    pinned: false,                // true ⇒ user moved cursor; don't auto-advance
  },
  // Historical-view range. null ⇒ live mode (last 24h + 48h forecast).
  // {from: Date, to: Date} ⇒ historical mode: only telemetry/forecasts from
  // the past, no snapshot forward overlay, x-axis fixed to the range.
  range: null,
  activePreset: "live",
  built: { ts: false, sankeyToday: false, spend: false },
};

function isHistorical() { return state.range != null; }

// ── Utilities ──────────────────────────────────────────────────────

function fmtKW(v) {
  if (v == null || !Number.isFinite(v)) return "—";
  return `${v.toFixed(2)} kW`;
}
function fmtPct(v) {
  if (v == null || !Number.isFinite(v)) return "—";
  return `${v.toFixed(1)}%`;
}
function fmtTime(ts) {
  if (!ts) return "—";
  const d = ts instanceof Date ? ts : new Date(ts);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function fmtDate(d) {
  if (!d) return "—";
  const dd = d instanceof Date ? d : new Date(d);
  return dd.toLocaleDateString([], { year: "numeric", month: "short", day: "numeric" });
}
function fmtDateInput(d) {
  // YYYY-MM-DD in local time, suitable for an <input type="date">.
  const dd = d instanceof Date ? d : new Date(d);
  const y = dd.getFullYear();
  const m = String(dd.getMonth() + 1).padStart(2, "0");
  const day = String(dd.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}
function fmtRangeShort(range) {
  if (!range) return "";
  const fromS = fmtDate(range.from);
  // `to` is end-exclusive at midnight of the day after; subtract 1 ms to
  // get the inclusive end-day for display.
  const toIncl = new Date(+range.to - 1);
  const toS = fmtDate(toIncl);
  return fromS === toS ? fromS : `${fromS} → ${toS}`;
}

// Plotly's date axis interprets timezone-aware ISO strings as UTC and
// renders tick labels in UTC. The fix the Plotly team recommends is to
// feed it tz-naive strings that already represent local wall-clock
// time. This helper does that conversion: takes a UTC moment (Date or
// ISO string with tz), returns a tz-naive ISO string in the browser's
// local timezone. Internal logic still uses Date objects throughout —
// we only convert at the trace/layout boundary.
function toPlotlyTime(d) {
  if (d == null) return null;
  const date = d instanceof Date ? d : new Date(d);
  if (isNaN(+date)) return null;
  // getTimezoneOffset is positive when local is behind UTC. Subtract
  // the offset (negative when ahead) so the resulting toISOString —
  // which always stamps as UTC — actually carries the local wall-clock
  // hour/minute.
  const offMs = date.getTimezoneOffset() * 60_000;
  return new Date(+date - offMs).toISOString().replace(/Z$/, "");
}
function toPlotlyTimeArr(arr) {
  return arr.map(toPlotlyTime);
}

// NEM date (UTC+10, never DST) for a given instant. Used to align the
// daily-spend cursor: spend bars are bucketed by nem_date in the API, so
// the time-series cursor maps to a spend bar by adding 10h and taking
// the YYYY-MM-DD prefix of the resulting UTC clock time.
function showError(msg) {
  const bar = document.getElementById("error-bar");
  bar.textContent = msg;
  bar.classList.remove("hidden");
}
function clearError() {
  document.getElementById("error-bar").classList.add("hidden");
}

// ── API ────────────────────────────────────────────────────────────

async function apiFetch(path, opts = {}) {
  if (!state.token) throw new Error("no token");
  const headers = Object.assign({}, opts.headers || {}, {
    "Authorization": `Bearer ${state.token}`,
  });
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  if (res.status === 401) {
    localStorage.removeItem(TOKEN_LS_KEY);
    state.token = null;
    throw new Error("unauthorized — token cleared, reload to re-enter");
  }
  if (res.status === 503) {
    // Caller decides how to handle "not ready yet".
    const err = new Error("service not ready");
    err.status = 503;
    throw err;
  }
  if (!res.ok) {
    throw new Error(`HTTP ${res.status} on ${path}`);
  }
  return res.json();
}

async function fetchSnapshot() { return apiFetch("/plan/current"); }
async function fetchConfig()   { return apiFetch("/dashboard/config"); }
async function fetchReady() {
  // /readyz is unauthenticated but may legitimately return 503 when the
  // service is in FALLBACK / DEGRADED. Read the body either way.
  const res = await fetch("/readyz");
  try { return await res.json(); } catch { return null; }
}
// The telemetry table is ~50 columns wide (alarms, MPPT strings, lifetime
// counters, cell temps, phase voltages…) but the dashboard plots only
// these. Projecting server-side cuts the /telemetry payload ~5x.
const TELEMETRY_COLUMNS = [
  "ts", "soc_pct", "battery_kw", "pv_kw", "grid_kw", "grid_kw_shelly",
  "house_load_kw", "import_price", "export_price", "ems_mode", "planner_action",
];
async function fetchTelemetry(sinceISO, untilISO) {
  return await fetchTablePaged("telemetry", sinceISO, untilISO, "ts", {
    columns: TELEMETRY_COLUMNS,
  });
}
async function fetchLoadTelemetry(sinceISO, untilISO) {
  // 1 row per load per 5-min boundary. With ~2 loads × 288 slots/day,
  // 7d window = ~4k rows — single page covers it; 2 pages for headroom.
  return await fetchTablePaged(
    "load_telemetry", sinceISO, untilISO, "ts",
    { limit: 5000, maxPages: 2 },
  );
}
async function fetchPriceForecastLog(sinceISO, untilISO) {
  // Server-side reduced view. The raw price_forecast_log holds ~14.5k
  // rows/24h (Amber re-logs the whole horizon every 60s), which used to
  // be paged down the wire (~MBs) and deduped client-side. /dashboard/
  // price_forecast now does that reduction in SQL — latest forecast per
  // interval, best resolution — and returns the ~hundreds of rows the
  // chart actually renders.
  const params = new URLSearchParams();
  if (sinceISO) params.set("since", sinceISO);
  if (untilISO) params.set("until", untilISO);
  const data = await apiFetch(`/dashboard/price_forecast?${params.toString()}`);
  return data.rows || [];
}
async function fetchPVForecastLog(sinceISO, untilISO) {
  // Server-side reduced view: latest forecast per period_end. See
  // fetchPriceForecastLog for the rationale.
  const params = new URLSearchParams();
  if (sinceISO) params.set("since", sinceISO);
  if (untilISO) params.set("until", untilISO);
  const data = await apiFetch(`/dashboard/pv_forecast?${params.toString()}`);
  return data.rows || [];
}
async function fetchAmberUsage(sinceISO, untilISO) {
  // 576 rows/day × 2 days ≈ 1200 rows — single page covers the time-series
  // window. The 5-min cost overlay only needs the last ~24h.
  return await fetchTablePaged("amber_usage", sinceISO, untilISO, "ts",
    { limit: 2000, maxPages: 2 });
}
async function fetchDailySpend(limit = 60) {
  const data = await apiFetch(`/daily_spend?limit=${limit}`);
  return data.rows || [];
}

// Generic paged fetcher: walks the time-ordered table by advancing
// `since` past the last row each page. Stops when a page returns less
// than the limit (no more rows) or when maxPages is hit (defensive,
// avoids runaway loops on a misconfigured server). Time is the table's
// canonical time column — `ts` for telemetry, `fetched_at` for forecast
// logs (see optimiser/api/handlers/tables.TABLE_TIME_COLUMNS).
async function fetchTablePaged(table, sinceISO, untilISO, timeCol, opts = {}) {
  const LIMIT = opts.limit ?? 1000;
  const MAX_PAGES = opts.maxPages ?? 6;
  // Optional server-side column projection. timeCol is force-included so
  // the paging cursor (page[…][timeCol]) always has a value to advance on.
  const cols = opts.columns
    ? [...new Set([timeCol, ...opts.columns])].join(",")
    : null;
  let cursor = sinceISO || null;
  const rows = [];
  for (let i = 0; i < MAX_PAGES; i++) {
    const params = new URLSearchParams();
    if (cursor) params.set("since", cursor);
    if (untilISO) params.set("until", untilISO);
    params.set("limit", String(LIMIT));
    if (cols) params.set("columns", cols);
    const data = await apiFetch(`/${table}?${params.toString()}`);
    const page = data.rows || [];
    if (page.length === 0) break;
    rows.push(...page);
    if (page.length < LIMIT) break;
    // Advance cursor 1 µs past the last row's time column. Microsecond
    // precision matches DuckDB's TIMESTAMPTZ resolution, so this avoids
    // re-reading the same row without skipping any.
    const last = page[page.length - 1][timeCol];
    cursor = bumpMicrosecond(last);
    if (!cursor) break;
  }
  return rows;
}

function bumpMicrosecond(iso) {
  if (!iso) return null;
  // ISO strings from DuckDB look like "2026-04-28T13:30:00+00:00" or
  // "...2026-04-28T13:30:00.123456+00:00". Parse, add 1 µs, re-emit.
  // JS Date is millisecond-precision so we stuff the µs into a fudge
  // factor and re-encode — cheaper to just add 1 ms (which is 1000 µs
  // past the last row, still safe — we'll skip at most one duplicate).
  const d = new Date(iso);
  if (isNaN(+d)) return null;
  return new Date(+d + 1).toISOString();
}

async function fetchLogs(limit = 200) {
  const data = await apiFetch(`/logs?limit=${limit}`);
  return data.records || [];
}

// ── Token bootstrap ────────────────────────────────────────────────

function ensureToken() {
  let t = localStorage.getItem(TOKEN_LS_KEY);
  if (!t) {
    t = window.prompt(
      "Enter the API bearer token (matches your config's bearer_token_env).\n" +
      "Stored in localStorage on this device only."
    );
    if (!t) {
      showError("No token entered — dashboard cannot fetch data.");
      return false;
    }
    localStorage.setItem(TOKEN_LS_KEY, t.trim());
  }
  state.token = t.trim();
  return true;
}

// ── Data: priority-cascade disambiguation for measured Sankey ──────

function disambiguateFlows({ pv, batt, grid, load }) {
  // pv ≥ 0, batt signed (+ charge / − discharge), grid signed (+ import
  // / − export), load ≥ 0. If any required input is null, return null —
  // we won't synthesise a balance from incomplete signals.
  if (pv == null || batt == null || grid == null || load == null) return null;
  if (![pv, batt, grid, load].every(Number.isFinite)) return null;

  let pvRem = Math.max(pv, 0);
  let loadRem = Math.max(load, 0);
  const out = {
    pv_to_load: 0, pv_to_batt: 0, pv_to_export: 0,
    grid_to_load: 0, grid_to_batt: 0,
    batt_to_load: 0, batt_to_export: 0,
  };

  // 1) PV → Load
  out.pv_to_load = Math.min(pvRem, loadRem);
  pvRem  -= out.pv_to_load;
  loadRem -= out.pv_to_load;

  // 2) Charge path (battery is a sink): PV first, then grid.
  if (batt > 0) {
    out.pv_to_batt = Math.min(pvRem, batt);
    pvRem -= out.pv_to_batt;
    out.grid_to_batt = Math.max(0, batt - out.pv_to_batt);
  }

  // 3) Discharge path (battery is a source): house load first, then export.
  if (batt < 0) {
    const dis = -batt;
    out.batt_to_load = Math.min(dis, loadRem);
    loadRem -= out.batt_to_load;
    out.batt_to_export = Math.max(0, dis - out.batt_to_load);
  }

  // 4) Grid serves remaining load.
  out.grid_to_load = Math.max(0, loadRem);
  // 5) PV exports whatever's left.
  out.pv_to_export = Math.max(0, pvRem);

  return out;
}

// ── Cursor model ───────────────────────────────────────────────────

function nowFromSnapshot() {
  if (!state.snapshot) return null;
  return new Date(state.snapshot.timestamp);
}

// Live import/export price covering "now". Pulls from the same merged
// 5-min/30-min array the price chart uses, so the panel-label readout
// always agrees with the leftmost forecast point. Returns c/kWh; null
// when there's no snapshot or no row covers now.
function currentLivePrices() {
  const now = nowFromSnapshot();
  if (!now) return null;
  const past = state.history?.priceForecast || [];
  const fut = state.snapshot?.price_forecast || [];
  const merged = mergePriceForecasts(past, fut);
  if (!merged.length) return null;
  const tNow = +now;
  let row = null;
  for (const p of merged) {
    const t = +new Date(p.start);
    if (!Number.isFinite(t)) continue;
    if (t <= tNow) row = p;
    else break;
  }
  if (!row) return null;
  return {
    importCpkwh: coalesce(row.forecast_predicted, row.import_per_kwh),
    exportCpkwh: coalesce(row.export_forecast_predicted, row.export_per_kwh),
  };
}

function effectiveCursor() {
  if (state.cursor.pinned && state.cursor.time) return state.cursor.time;
  if (state.range) {
    // In historical mode, "live" cursor points at the most recent
    // telemetry row inside the range (the top of the visible window).
    const rows = state.history.rows;
    if (rows.length) return new Date(rows[rows.length - 1].ts);
    return state.range.to;
  }
  return nowFromSnapshot();
}

function setCursor(time, { pinned } = {}) {
  state.cursor.time = time;
  if (pinned !== undefined) state.cursor.pinned = pinned;
  renderCursorReadout();
  redrawCursorLine();
  redrawSpendCursor();
}

function snapToNow() {
  state.cursor.pinned = false;
  // In historical mode, "now" doesn't apply — effectiveCursor() will
  // resolve to the latest in-range telemetry row instead.
  state.cursor.time = isHistorical() ? null : nowFromSnapshot();
  renderCursorReadout();
  redrawCursorLine();
  redrawSpendCursor();
}

// ── Status strip ───────────────────────────────────────────────────

// Refresh just the "Ns · vX.Y.Z" freshness chip from the cached snapshot.
// Cheap (one DOM write); designed to be called from a 1 s interval so
// the age counter advances smoothly even though full snapshots only
// arrive once per tick.
function renderTickAge() {
  const el = document.getElementById("status-tick-age");
  if (!el) return;
  const snap = state.snapshot;
  if (!snap) { el.textContent = "—"; return; }
  const tickAgeS = (Date.now() - new Date(snap.timestamp).getTime()) / 1000;
  // Two spans so mobile CSS can hide "· v0.2.0" without dropping the
  // freshness indicator. Desktop keeps the full text.
  el.innerHTML =
    `<span class="tick-age">${tickAgeS.toFixed(0)}s</span>` +
    `<span class="tick-version"> · v${escapeHtml(snap.version)}</span>`;
}

function renderStatusStrip() {
  const snap = state.snapshot;
  const stateEl = document.getElementById("status-state");
  const tickAgeEl = document.getElementById("status-tick-age");
  const socEl = document.getElementById("status-soc");
  const sohEl = document.getElementById("status-soh");

  if (!snap) {
    stateEl.textContent = "no plan";
    stateEl.className = "status-value status-state-unknown";
    tickAgeEl.textContent = "—";
    socEl.textContent = "—"; sohEl.textContent = "—";
    setModeTile(null);
    setTile("pv", null); setTile("batt", null); setTile("grid", null); setTile("load", null);
    return;
  }

  // State badge priority:
  //   1. Plumbing problems (FALLBACK / DEGRADED / INITIALISE) — these
  //      dominate over LP solve quality, so they win whenever ready.state
  //      is anything other than "active".
  //   2. LP solve status (OPTIMAL / FEASIBLE / INFEASIBLE / TIMEOUT) —
  //      when the service is healthy, the user-facing question is "is the
  //      LP doing its job", not "is the process running".
  //   3. Whatever ready.state says, as a last resort.
  // We never invent "ACTIVE" without evidence — the LP-status path only
  // triggers when there's a real lp_solution attached to the snapshot.
  // "LP " prefix dropped to keep the badge compact; the "Service" label
  // above the badge already supplies the context.
  const ready = state.ready;
  const lp = snap.lp_solution;
  const lpStatus = lp ? lp.status : null;
  let badgeKey = "unknown", badgeText = "—";
  if (ready && ready.state && ready.state !== "active") {
    badgeKey = ready.state;
    badgeText = ready.state.toUpperCase();
    if (ready.sigenergy_connected === false) badgeText += " (NO INV)";
  } else if (lpStatus) {
    badgeKey = (lpStatus === "optimal" || lpStatus === "feasible") ? "active" : "degraded";
    badgeText = lpStatus.toUpperCase();
    if (ready && ready.sigenergy_connected === false) badgeText += " (NO INV)";
  } else if (ready && ready.state) {
    badgeKey = ready.state;
    badgeText = ready.state.toUpperCase();
    if (ready.sigenergy_connected === false) badgeText += " (NO INV)";
  }
  stateEl.textContent = badgeText;
  stateEl.className = `status-value ${STATE_CLASS[badgeKey] || "status-state-unknown"}`;

  // Tick-age is its own render so a 1 s interval can refresh it
  // between snapshots — under SSE, full status renders only fire on
  // each new tick (~60 s), but the age counter should still tick
  // smoothly so the "freshness" signal is honest.
  renderTickAge();

  // SOC + SOH from system_state (post-dispatch preferred).
  const ss = snap.system_state_post_dispatch || snap.system_state;
  socEl.textContent = fmtPct(ss?.soc_pct);
  sohEl.textContent = ss?.soh_pct != null ? `SOH ${ss.soh_pct.toFixed(1)}%` : "SOH —";

  // Mode lives inline with the live-flow tiles. setModeTile colours the
  // value text in the matching MODE-ribbon hue and stashes the verbose
  // detail (mode name + cap + intent) in the tile's `title` attribute
  // so a hover surfaces the diagnostic line that used to live below.
  setModeTile(snap.lp_dispatch);

  setTile("pv",   ss?.pv_power_kw);
  setTile("batt", ss?.battery_power_kw);
  // Grid uses the pre-dispatch read: post-dispatch captures the inverter
  // mid-adaptive-trim (5 s after the cap write, before the cascade settles)
  // so it can read ~0 while the actual steady-state flow is still ±kW.
  // Pre-dispatch is the previous slot's settled reading and matches the
  // chart's "grid measured (inverter)" trace (which sources `grid_kw` from
  // telemetry, also pre-dispatch).
  setTile("grid", snap.system_state?.grid_power_kw);
  setTile("load", ss?.house_load_kw);
}

// Render the MODE tile in the live-flow tile-row. Value text is coloured
// to match the chart's MODE ribbon, so the strip and the chart share one
// vocabulary. Verbose detail (mode name + cap + intent) goes on the tile
// `title` for a hover-on tooltip; the tile shape itself is the same as
// PV/BATTERY/GRID/HOUSE for visual consistency.
function setModeTile(disp) {
  const tile = document.getElementById("tile-mode");
  if (!tile) return;
  const v = tile.querySelector(".tile-value");
  if (!disp) {
    v.textContent = "—";
    tile.style.borderLeftColor = "";
    tile.title = "";
    return;
  }
  const modeKey = modeFromDispatch(disp);
  const c = MODE_COLORS[modeKey];
  // Colour the tile's left edge in the MODE-ribbon hue — `.tile-mode`
  // sets a thicker left border in CSS; this paints it.
  tile.style.borderLeftColor = c;
  v.textContent = `m${disp.mode} · ${disp.kind.toLowerCase()}`;
  const modeName = (function () {
    switch (disp.mode) {
      case 0: return "PCS_REMOTE_CONTROL";
      case 1: return "STANDBY";
      case 2: return "MAX_SELF_CONSUME";
      case 3: return "CHARGE_GRID_FIRST";
      case 4: return "CHARGE_PV_FIRST";
      case 5: return "DISCHARGE_PV_FIRST";
      case 6: return "DISCHARGE_ESS_FIRST";
      default: return `mode ${disp.mode}`;
    }
  })();
  tile.title =
    `${modeName} · cap ${disp.cap_kw.toFixed(2)} kW · intent ${disp.signed_intent_kw.toFixed(2)} kW`;
}

// Map an LPDispatch (mode + kind) to one of the MODE enum values so the
// strip's mode tile picks the same colour as the chart's MODE ribbon.
function modeFromDispatch(disp) {
  if (!disp) return MODE.UNKNOWN;
  const m = disp.mode;
  const k = (disp.kind || "").toUpperCase();
  if (m === 2) {
    return k === "CHARGE" ? MODE.M2_CHARGE : MODE.M2_IDLE;
  }
  if (m === 3) return MODE.M3_CHARGE;
  if (m === 5) return MODE.M5_DIS_PV;
  if (m === 6) return MODE.M6_DIS_ESS;
  if (m === 0 || m === 1) return MODE.M0_STANDBY;
  return MODE.UNKNOWN;
}

function setTile(id, value) {
  const tile = document.getElementById(`tile-${id}`);
  if (!tile) return;
  const v = tile.querySelector(".tile-value");
  // Wrap the unit in a span so mobile CSS can hide " kW" without
  // touching the number — keeps tabular alignment under tight tiles.
  if (value == null || !Number.isFinite(value)) {
    v.textContent = "—";
  } else {
    v.innerHTML = `${value.toFixed(2)}<span class="tile-unit"> kW</span>`;
  }
}

function renderCursorReadout() {
  const t = effectiveCursor();
  document.getElementById("cursor-time").textContent = fmtTime(t);
  document.getElementById("cursor-mode").textContent = state.cursor.pinned ? "pinned" : "live";
  document.getElementById("cursor-now-btn").disabled = !state.cursor.pinned;
  // Toggle .pinned on the cursor block so mobile CSS can show it only
  // when scrubbing — in live mode it's redundant with the tab-bar chip.
  const block = document.getElementById("cursor-now-btn").closest(".status-block");
  if (block) block.classList.toggle("pinned", !!state.cursor.pinned);
}

// ── Loads + events ────────────────────────────────────────────────

function renderLoads() {
  const grid = document.getElementById("loads-grid");
  const loads = state.snapshot?.managed_loads || [];
  if (loads.length === 0) {
    grid.innerHTML = '<div class="muted">no managed loads</div>';
    return;
  }
  // Index configured loads by id so we can pick the right target unit.
  const cfgByLoad = {};
  for (const c of (state.config?.managed_loads || [])) cfgByLoad[c.load_id] = c;
  grid.innerHTML = loads.map((l) => {
    const relayCls = l.relay_on === true ? "relay-on" : "relay-off";
    const relayTxt = l.relay_on === true ? "relay ON" : (l.relay_on === false ? "relay off" : "—");
    const cycle = l.cycle_state || "—";
    const cfg = cfgByLoad[l.load_id] || {};
    let progressText = "—";
    if (cfg.daily_run_minutes != null) {
      // Time mode — relay-on minutes today vs target.
      const got = l.relay_on_minutes_today != null ? l.relay_on_minutes_today : 0;
      progressText = `${got.toFixed(0)} / ${cfg.daily_run_minutes} min today`;
    } else if (l.energy_today_kwh != null) {
      // Energy mode — kWh delivered vs target (target may be unset for
      // observable loads; show absolute kWh in that case). Energy is net
      // (imp − exp) so a bidirectional CT (mains) can read negative —
      // label sign explicitly so "import 0.5" vs "export 25" is unambiguous.
      if (cfg.daily_target_kwh != null) {
        progressText = `${l.energy_today_kwh.toFixed(2)} / ${cfg.daily_target_kwh.toFixed(2)} kWh today`;
      } else {
        const v = l.energy_today_kwh;
        const dir = v >= 0 ? "imported" : "exported";
        progressText = `${Math.abs(v).toFixed(2)} kWh ${dir} today`;
      }
    }
    return `
      <div class="load-card">
        <div class="load-card-header">
          <span class="load-card-name">${escapeHtml(l.load_id)}</span>
          <span class="load-card-state">${escapeHtml(cycle)}</span>
        </div>
        <div class="load-card-power">${fmtKW(l.power_kw)}</div>
        <div class="load-card-energy">
          ${escapeHtml(progressText)}
          · <span class="${relayCls}">${escapeHtml(relayTxt)}</span>
        </div>
      </div>`;
  }).join("");
}

function renderEvents() {
  const list = document.getElementById("events-list");
  if (state.events.length === 0) {
    list.innerHTML = '<li class="muted">no notable events</li>';
    return;
  }
  list.innerHTML = state.events.slice(0, 30).map((e) => {
    // Ring-buffer log records carry message + level, not the structured
    // event payload — surface what we have.
    const ts = e.timestamp || e.ts || "";
    const lvl = (e.level || "").toUpperCase();
    const cls = lvl === "ERROR" || lvl === "CRITICAL" ? "event-bad" :
                lvl === "WARNING" ? "event-warn" : "";
    const msg = e.message || e.event || "";
    return `<li><span class="event-ts">${escapeHtml(fmtTime(ts))}</span>` +
           `<span class="event-type ${cls}">${escapeHtml(lvl || "")}</span>` +
           `${escapeHtml(msg)}</li>`;
  }).join("");
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

// ── Time-series figure ────────────────────────────────────────────


// ── uPlot model builder ────────────────────────────────────────────────────
//
// Build the `model` consumed by buildTsFigure (panels.js). Mirrors the data
// SOURCES of buildTraces() exactly (same state/snapshot/history fields, same
// merge/coalesce/marginalCost helpers), but emits columns aligned to ONE
// shared unionX (epoch seconds) via alignSeries — never Plotly traces. All
// panel instances render against model.unionX; never per-panel-trim x.
//
// Returns null when there is nothing to render (mirrors buildTraces' early
// returns), so the caller can skip the figure build.
function buildModel() {
  const snap = state.snapshot;
  const hist = state.history.rows;
  if (!isHistorical() && !snap) return null;
  if (isHistorical() && hist.length === 0) return null;

  // ── Future side (snapshot forward trajectory) ──
  const fwd = isHistorical() ? [] : (snap?.lp_solution?.forward_trajectory || []);

  // ── Merged forecasts (price + PV) — past log + snapshot future ──
  const pastPriceFC = state.history.priceForecast;
  const futurePriceFC = isHistorical() ? [] : (snap?.price_forecast || []);
  const priceFCMerged = mergePriceForecasts(pastPriceFC, futurePriceFC);

  const pastPVFC = state.history.pvForecast;
  const futurePVFC = isHistorical() ? [] : (snap?.pv_forecast || []);
  const pvFCMerged = mergePVForecasts(pastPVFC, futurePVFC);

  // ── Build the single ascending union-x (epoch seconds) ──
  // Sources: past telemetry ts, future slot starts, merged price/pv forecast
  // starts, pv-actual period_ends, settled-cost ts.
  const settledCost = aggregateAmberUsageCostsPerSlotSec(state.history.amberUsage);
  const unionX = buildUnionX(
    hist.map((r) => toEpochSec(r.ts)),
    fwd.map((s) => toEpochSec(s.slot_start)),
    priceFCMerged.map((p) => toEpochSec(p.start)),
    pvFCMerged.map((p) => toEpochSec(p.start)),
    pastPVFC.map((p) => toEpochSec(p.period_end)),
    settledCost.xSec,
  );

  // ── PRICE panel ──
  // Realised (telemetry) import/export; predicted (merged forecast) import/export.
  const importRealised = alignSeries(unionX, hist, (r) => toEpochSec(r.ts), (r) => r.import_price);
  const exportRealised = alignSeries(unionX, hist, (r) => toEpochSec(r.ts), (r) => r.export_price);
  const importPredicted = alignSeries(
    unionX, priceFCMerged, (p) => toEpochSec(p.start),
    (p) => coalesce(p.forecast_predicted, p.import_per_kwh));
  const exportPredicted = alignSeries(
    unionX, priceFCMerged, (p) => toEpochSec(p.start),
    (p) => coalesce(p.export_forecast_predicted, p.export_per_kwh));
  const importBand = bandColumns(priceFCMerged, "forecast_low", "forecast_high",
    unionX, (p) => toEpochSec(p.start));
  const exportBand = bandColumns(priceFCMerged, "export_forecast_low", "export_forecast_high",
    unionX, (p) => toEpochSec(p.start));

  // ── PV panel ──
  const pvP50 = alignSeries(unionX, pvFCMerged, (p) => toEpochSec(p.start), (p) => p.pv_estimate_kw);
  const pvMeasured = alignSeries(unionX, hist, (r) => toEpochSec(r.ts), (r) => r.pv_kw);
  const pvBand = bandColumns(pvFCMerged, "pv_estimate10_kw", "pv_estimate90_kw",
    unionX, (p) => toEpochSec(p.start));
  // PV actual (Solcast estimated-actuals) keyed by period_end. Conditional —
  // null column if all-null (panels.js drops the markers series at build time).
  const pvActualAligned = alignSeries(unionX, pastPVFC, (p) => toEpochSec(p.period_end), (p) => p.actual_kw);
  const pvActual = pvActualAligned.some((v) => v != null) ? pvActualAligned : null;

  // ── SOC panel ──
  const socMeasured = alignSeries(unionX, hist, (r) => toEpochSec(r.ts), (r) => r.soc_pct);
  const socPlanned = alignSeries(unionX, fwd, (s) => toEpochSec(s.slot_start), (s) => s.soc_pct_end);
  const floorPct = (() => {
    const f = state.config?.battery?.soc_floor_pct;
    return f != null && Number.isFinite(f) ? f : null;
  })();

  // ── LOAD panel ──
  // Per-managed-load stacked areas. IDs sorted lexically for deterministic
  // stacking; OBSERVABLE-category ids excluded (they belong on GRID).
  const loadRows = state.history.loadTelemetry || [];
  const observableIds = new Set();
  for (const r of loadRows) {
    if ((r.category || "").toLowerCase() === "observable") observableIds.add(r.load_id);
  }
  // Past per-load value indexed by epoch-sec → { id: power_kw }.
  const pastLoadByX = new Map();
  const pastLoadIds = new Set();
  for (const r of loadRows) {
    if (observableIds.has(r.load_id)) continue;
    pastLoadIds.add(r.load_id);
    const x = toEpochSec(r.ts);
    if (x == null) continue;
    if (!pastLoadByX.has(x)) pastLoadByX.set(x, {});
    pastLoadByX.get(x)[r.load_id] = r.power_kw;
  }
  // Future per-load value indexed by epoch-sec → { id: load_kw }.
  const futLoadByX = new Map();
  const futLoadIds = new Set();
  for (const s of fwd) {
    if (!s.load_kw) continue;
    const x = toEpochSec(s.slot_start);
    if (x == null) continue;
    const m = {};
    for (const k of Object.keys(s.load_kw)) {
      if (observableIds.has(k)) continue;
      futLoadIds.add(k);
      m[k] = s.load_kw[k];
    }
    futLoadByX.set(x, m);
  }
  // Union of all (past+future) managed-load ids, sorted lexically.
  const allLoadIds = [...new Set([...pastLoadIds, ...futLoadIds])].sort();
  // Per-id raw column (0 where the load is present-in-range but idle, null
  // outside its data window so the stepped area doesn't bridge past↔future).
  const perIdRaw = new Map();
  for (const id of allLoadIds) {
    perIdRaw.set(id, unionX.map((x) => {
      const past = pastLoadByX.get(x);
      if (past) {
        const v = past[id];
        return v != null && Number.isFinite(v) ? v : 0;
      }
      const fut = futLoadByX.get(x);
      if (fut && Object.prototype.hasOwnProperty.call(fut, id)) {
        const v = fut[id];
        return v != null && Number.isFinite(v) ? v : 0;
      }
      // x belongs to a future slot but this load has no entry there, or x is
      // outside any load row entirely → null (gap).
      if (fut) return 0;
      return null;
    }));
  }
  // Cumulative stacked columns (running sum in sorted-id order).
  const stacks = [];
  const running = unionX.map(() => null);
  for (const id of allLoadIds) {
    const raw = perIdRaw.get(id);
    const cum = unionX.map((_x, i) => {
      const v = raw[i];
      if (v == null) return running[i]; // keep prior running (may be null)
      running[i] = (running[i] == null ? 0 : running[i]) + v;
      return running[i];
    });
    stacks.push({ id, color: colorForLoadId(id), cum });
  }
  // Measured envelope = max(house_load, Σ managed) at each past ts.
  const loadMeasuredEnv = alignSeries(unionX, hist, (r) => toEpochSec(r.ts), (r) => {
    const m = r.house_load_kw;
    if (m == null || !Number.isFinite(m)) return null;
    const x = toEpochSec(r.ts);
    const past = pastLoadByX.get(x) || {};
    let sumManaged = 0;
    for (const id of allLoadIds) {
      const v = past[id];
      if (v != null && Number.isFinite(v)) sumManaged += v;
    }
    return Math.max(m, sumManaged);
  });
  // Planned envelope from the slot energy balance, clamped ≥ Σ managed-planned.
  const loadPlannedEnv = alignSeries(unionX, fwd, (s) => toEpochSec(s.slot_start), (s) => {
    const pvToHouse = s.pv_to_house_kw ?? 0;
    const batDischarge = Math.max(0, -(s.battery_kw ?? 0));
    const gridImp = s.grid_import_kw ?? 0;
    const gridToBat = s.grid_to_battery_kw ?? 0;
    const gridExp = s.grid_export_kw ?? 0;
    const pvToExp = s.pv_to_export_kw ?? 0;
    const f = pvToHouse + batDischarge + gridImp - gridToBat - (gridExp - pvToExp);
    let sumManaged = 0;
    if (s.load_kw) {
      for (const id of allLoadIds) {
        const v = s.load_kw[id];
        if (v != null && Number.isFinite(v)) sumManaged += v;
      }
    }
    if (f == null || !Number.isFinite(f)) return sumManaged > 0 ? sumManaged : null;
    return Math.max(f, sumManaged);
  });

  // ── GRID panel ──
  const gridInverter = alignSeries(unionX, hist, (r) => toEpochSec(r.ts), (r) => r.grid_kw);
  const gridShellyAligned = alignSeries(unionX, hist, (r) => toEpochSec(r.ts), (r) => r.grid_kw_shelly);
  const gridShelly = gridShellyAligned.some((v) => v != null) ? gridShellyAligned : null;
  const gridPlanned = alignSeries(unionX, fwd, (s) => toEpochSec(s.slot_start), (s) => {
    const imp = s.grid_import_kw;
    const exp = s.grid_export_kw ?? 0;
    if (imp == null) return null;
    return imp - exp; // net: import positive, export negative
  });

  // ── COST panel ──
  const costRealised = alignSeries(unionX, hist, (r) => toEpochSec(r.ts),
    (r) => marginalCost(r.import_price, r.export_price, r.grid_kw));
  const costPlanned = alignSeries(unionX, fwd, (s) => toEpochSec(s.slot_start), (s) => {
    const ip = pickPriceAt(futurePriceFC, s.slot_start, "import");
    const ep = pickPriceAt(futurePriceFC, s.slot_start, "export");
    if (ip == null || ep == null) return null;
    return ip * (s.grid_import_kw ?? 0) - ep * (s.grid_export_kw ?? 0);
  });
  // Settled cost keyed by epoch sec (already c/h via ×12).
  let costSettled = null;
  if (settledCost.xSec.length > 0) {
    const byX = new Map();
    for (let i = 0; i < settledCost.xSec.length; i++) byX.set(settledCost.xSec[i], settledCost.y[i]);
    const aligned = unionX.map((x) => (byX.has(x) ? byX.get(x) : null));
    if (aligned.some((v) => v != null)) costSettled = aligned;
  }

  // ── Ribbons (decision + mode) ──
  // Single category per unionX slot: realised from telemetry where a past row
  // exists, else planned from the slot, else UNKNOWN.
  const pastDecisionByX = new Map();
  const pastModeByX = new Map();
  for (const r of hist) {
    const x = toEpochSec(r.ts);
    if (x == null) continue;
    pastDecisionByX.set(x, decisionFromTelemetry(r));
    pastModeByX.set(x, modeFromTelemetry(r));
  }
  const futDecisionByX = new Map();
  const futModeByX = new Map();
  for (const s of fwd) {
    const x = toEpochSec(s.slot_start);
    if (x == null) continue;
    futDecisionByX.set(x, decisionFor(s));
    futModeByX.set(x, modeFromSlot(s));
  }
  const decisionCats = unionX.map((x) => {
    if (pastDecisionByX.has(x)) return pastDecisionByX.get(x);
    if (futDecisionByX.has(x)) return futDecisionByX.get(x);
    return DECISION.UNKNOWN;
  });
  const modeCats = unionX.map((x) => {
    if (pastModeByX.has(x)) return pastModeByX.get(x);
    if (futModeByX.has(x)) return futModeByX.get(x);
    return MODE.UNKNOWN;
  });

  // ── Shapes: buy/sell regions, now-line, cursor ──
  const regions = [];
  for (const s of fwd) {
    const x0 = toEpochSec(s.slot_start);
    const x1 = toEpochSec(new Date(+new Date(s.slot_start) + SLOT_MS));
    if (x0 == null || x1 == null) continue;
    if ((s.grid_to_battery_kw ?? 0) > DEADBAND_KW) regions.push({ x0, x1, kind: "charge" });
    if ((s.grid_export_kw ?? 0) > DEADBAND_KW) regions.push({ x0, x1, kind: "export" });
  }
  const nowT = nowFromSnapshot();
  const nowSec = nowT && !isHistorical() ? toEpochSec(nowT) : null;
  const cursorT = effectiveCursor();
  const cursorSec = cursorT ? toEpochSec(cursorT) : null;

  return {
    unionX,
    price: {
      importRealised, importPredicted, exportRealised, exportPredicted,
      bands: {
        importLo: importBand.lo, importHi: importBand.hi,
        exportLo: exportBand.lo, exportHi: exportBand.hi,
      },
    },
    pv: { p50: pvP50, measured: pvMeasured, actual: pvActual, bandLo: pvBand.lo, bandHi: pvBand.hi },
    soc: { measured: socMeasured, planned: socPlanned, floorPct },
    load: { stacks, measuredEnv: loadMeasuredEnv, plannedEnv: loadPlannedEnv },
    grid: { inverter: gridInverter, shelly: gridShelly, planned: gridPlanned },
    cost: { realised: costRealised, planned: costPlanned, settled: costSettled },
    decisionCats, modeCats,
    nowSec, cursorSec, regions, thresholdC: 30,
  };
}

// Aggregate amber_usage rows into one net-cost-per-slot column, keyed by
// epoch SECONDS (for unionX alignment). c/5-min → c/h via ×12. Returns
// parallel xSec/y arrays sorted ascending. (Seconds variant of the legacy
// aggregateAmberUsageCostsPerSlot below, which returns Plotly-time strings.)
function aggregateAmberUsageCostsPerSlotSec(rows) {
  if (!rows || rows.length === 0) return { xSec: [], y: [] };
  const byTs = new Map();
  for (const r of rows) {
    if (r.cost_cents == null) continue;
    const cur = byTs.get(r.ts) ?? 0;
    byTs.set(r.ts, cur + r.cost_cents);
  }
  const sorted = [...byTs.entries()].sort((a, b) => +new Date(a[0]) - +new Date(b[0]));
  return {
    xSec: sorted.map(([ts]) => toEpochSec(ts)),
    y: sorted.map(([, c]) => c * 12),
  };
}

// Aggregate amber_usage rows into one net-cost-per-slot trace. Sum
// cost_cents across channels per `ts` (general's positive + feedIn's
// negative = net for that slot), convert to c/h (×12 because each row
// is a 5-min interval), return parallel x/y arrays sorted ascending.
function aggregateAmberUsageCostsPerSlot(rows) {
  if (!rows || rows.length === 0) return { x: [], y: [] };
  const byTs = new Map();
  for (const r of rows) {
    if (r.cost_cents == null) continue;
    const cur = byTs.get(r.ts) ?? 0;
    byTs.set(r.ts, cur + r.cost_cents);
  }
  const sorted = [...byTs.entries()].sort((a, b) =>
    +new Date(a[0]) - +new Date(b[0])
  );
  return {
    x: sorted.map(([ts]) => toPlotlyTime(ts)),
    y: sorted.map(([, c]) => c * 12),
  };
}

function computeXRange() {
  if (state.range) {
    return [toEpochSec(state.range.from), toEpochSec(state.range.to)];
  }
  const now = nowFromSnapshot();
  if (!now) return undefined;
  const lo = new Date(+now - HISTORY_LOOKBACK_MS);
  const hi = new Date(+now + FUTURE_HORIZON_MS);
  return [toEpochSec(lo), toEpochSec(hi)];
}

// The assembled uPlot figure (8 synced panels). Built once on first redraw;
// subsequent redraws call .update(model) — never a full rebuild — so zoom and
// pinned-cursor state survive. Membership of conditional series is fixed at
// build time; if it must change we tear down and rebuild (see below).
let tsFigure = null;
// Snapshot of which conditional series were present at build time, so we can
// detect a membership change and trigger a rebuild rather than crash uPlot.
let tsMembership = null;

function membershipKey(model) {
  return [
    model.pv.actual != null,
    model.grid.shelly != null,
    model.cost.settled != null,
    (model.load.stacks || []).map((s) => s.id).join(","),
  ].join("|");
}

// Create one child .uplot-panel div per PANEL_LAYOUT entry inside #ts-figure
// (the flex column). Heights come from PANEL_LAYOUT[i].height fractions via
// flex-grow so the column fills the configured --ts-figure-h.
function assembleTsContainers(root) {
  root.innerHTML = "";
  for (const p of PANEL_LAYOUT) {
    const div = document.createElement("div");
    div.className = "uplot-panel";
    div.dataset.panel = p.id;
    div.style.flexGrow = String(p.height);
    div.style.flexBasis = "0";
    root.appendChild(div);
  }
  // Shared tooltip element for the two ribbon lanes (hit-test readout).
  const tip = document.createElement("div");
  tip.className = "ribbon-tooltip";
  tip.style.display = "none";
  root.appendChild(tip);
}

// Tracks the active x-range so we re-apply setScale('x') only when the range
// preset/window actually changes — not on every live snapshot (which would
// stomp a user's manual zoom).
let tsXRangeKey = null;

function applyTsXRange(force) {
  if (!tsFigure) return;
  const xr = computeXRange();
  if (!xr || xr[0] == null || xr[1] == null) return;
  const key = `${xr[0]}|${xr[1]}`;
  if (!force && key === tsXRangeKey) return;
  tsXRangeKey = key;
  for (const u of tsFigure.instances) {
    u.setScale("x", { min: xr[0], max: xr[1] });
  }
}

function redrawTSFigure() {
  const div = document.getElementById("ts-figure");
  if (!div) return;
  const model = buildModel();
  if (!model) return;

  const key = membershipKey(model);
  if (tsFigure && tsMembership !== key) {
    // Conditional-series membership changed — rebuild from scratch.
    tsFigure.destroy();
    tsFigure = null;
  }

  if (!tsFigure) {
    assembleTsContainers(div);
    tsFigure = buildTsFigure(div, model);
    tsMembership = key;
    tsXRangeKey = null;
    state.built.ts = true;
    applyTsXRange(true);
  } else {
    tsFigure.update(model);
    applyTsXRange(false);
  }
}

function redrawCursorLine() {
  if (!state.built.ts || !tsFigure) return;
  // Cheap cursor-only repaint (spec §5.7): update just the shapes-plugin
  // cursor/now state and redraw — no data rebuild, so zoom + band geometry
  // are untouched.
  const cursorT = effectiveCursor();
  const nowT = nowFromSnapshot();
  tsFigure.setShapes({
    cursorSec: cursorT ? toEpochSec(cursorT) : null,
    nowSec: nowT && !isHistorical() ? toEpochSec(nowT) : null,
  });
}

function onPlotlyHover(ev) {
  const p = ev.points && ev.points[0];
  if (!p) return;
  const x = p.x;
  if (!x) return;
  const t = nearestSlotAt(new Date(x));
  setCursor(t, { pinned: true });
}

function onPlotlyClick(ev) {
  // Click pins (same as hover but clearer intent). Double-click via the
  // mode-bar autoscale is handled by Plotly; we don't override it.
  onPlotlyHover(ev);
}

// ── Sankey ─────────────────────────────────────────────────────────

// Plotly Sankey collapses links with value=0, which would make absent
// flows disappear. We always emit all 7 link defs (so every source ↔
// sink relationship is visible at all times, growing/shrinking rather
// than appearing/disappearing) by clamping to a tiny epsilon when the
// real flow is sub-noise. The label/hover still shows the actual value
// (which is rendered as 0.00 below the noise floor).
const SANKEY_LINK_EPSILON = 1e-3;

function buildSankeyTrace(flows, unit = "kW", precision = 2) {
  const valuesByDef = [
    flows.pv_to_load,
    flows.pv_to_batt,
    flows.pv_to_export,
    flows.grid_to_load,
    flows.grid_to_batt,
    flows.batt_to_load,
    flows.batt_to_export,
  ];
  const sources = [], targets = [], values = [], colors = [], labels = [];
  for (let i = 0; i < SANKEY_LINK_DEFS.length; i++) {
    const raw = valuesByDef[i];
    const real = (raw != null && Number.isFinite(raw) && raw > 0) ? raw : 0;
    const [s, t, c, lbl] = SANKEY_LINK_DEFS[i];
    sources.push(s); targets.push(t);
    values.push(Math.max(real, SANKEY_LINK_EPSILON));
    colors.push(c);
    labels.push(`${lbl}: ${real.toFixed(precision)} ${unit}`);
  }
  return {
    type: "sankey",
    // `fixed` honours the explicit node.x / node.y exactly — no
    // re-ordering by the solver. This keeps the vertical layout stable
    // (left: PV / Battery / Grid; right: Battery / House / Grid) so the
    // diagram is comparable across ticks and across the cursor / today
    // figures, regardless of which links are dominant in any given tick.
    arrangement: "fixed",
    orientation: "h",
    node: {
      label: SANKEY_NODES.map((n) => n.name),
      color: SANKEY_NODE_COLORS,
      x: SANKEY_NODES.map((n) => n.x),
      y: SANKEY_NODES.map((n) => n.y),
      pad: 18, thickness: 16,
      line: { color: "#0e1116", width: 0.5 },
    },
    link: {
      source: sources, target: targets, value: values,
      color: colors, label: labels,
      hovertemplate: "%{label}<extra></extra>",
    },
  };
}

function sankeyLayout() {
  const narrow = isNarrowViewport();
  return {
    margin: narrow
      ? { l: 4, r: 4, t: 4, b: 4 }
      : { l: 12, r: 12, t: 12, b: 12 },
    paper_bgcolor: "#161b22",
    font: { color: "#e8edf2", size: 12, family: FONT_FAMILY },
  };
}

// Sum disambiguated kW flows over today's telemetry rows (since local
// midnight) → kWh per link. Each row covers `telemetry_write_interval_s`
// (5 min by default), so dt is ~constant per row; this is fine for a
// rolling daily total even if the service was restarted mid-day.
// Returns null if no rows lie in today's window.
function dailyFlowsKWh() {
  const rows = state.history.rows || [];
  if (!rows.length) return null;
  // Window: in historical mode use the picked range; live mode uses
  // local midnight → now.
  let sinceMs, untilMs;
  if (state.range) {
    sinceMs = +state.range.from;
    untilMs = +state.range.to;
  } else {
    const now = new Date();
    const localMidnight = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    sinceMs = +localMidnight;
    untilMs = +now;
  }

  const totals = {
    pv_to_load: 0, pv_to_batt: 0, pv_to_export: 0,
    grid_to_load: 0, grid_to_batt: 0,
    batt_to_load: 0, batt_to_export: 0,
  };
  let counted = 0;
  // Step through the rows; each row's dt is the gap to the *next* row,
  // capped at 5 min so a missing-row gap doesn't inflate today's total.
  // The final row uses (now - r.ts) clamped the same way, so totals
  // track real time without an artificial trailing zero.
  const MAX_DT_H = 5 / 60;       // 5 minutes
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    const tMs = +new Date(r.ts);
    if (tMs < sinceMs || tMs >= untilMs) continue;
    const tNextMs = (i + 1 < rows.length) ? +new Date(rows[i + 1].ts) : untilMs;
    const dtH = Math.min(MAX_DT_H, Math.max(0, (tNextMs - tMs) / 3600_000));
    if (dtH <= 0) continue;
    const flows = disambiguateFlows({
      pv: r.pv_kw, batt: r.battery_kw,
      grid: r.grid_kw, load: r.house_load_kw,
    });
    if (!flows) continue;
    for (const k of Object.keys(totals)) totals[k] += flows[k] * dtH;
    counted++;
  }
  if (counted === 0) return null;
  return { flows: totals, counted };
}

async function redrawDailySankey() {
  const div = document.getElementById("sankey-today-figure");
  const subtitle = document.getElementById("sankey-today-subtitle");
  if (!div) return;
  const result = dailyFlowsKWh();
  // Update the panel heading: "Today" in live mode, "Range" in historical.
  const headingEl = document.getElementById("sankey-today-heading");
  if (headingEl) headingEl.textContent = state.range ? "Range total" : "Today";

  if (!result) {
    subtitle.textContent = state.range
      ? "no telemetry in range" : "no telemetry yet today";
    if (state.built.sankeyToday) Plotly.purge(div);
    state.built.sankeyToday = false;
    return;
  }
  // Total energy in (PV generation + grid import) is a reasonable
  // single-number summary for the subtitle.
  const f = result.flows;
  const pvTotal = f.pv_to_load + f.pv_to_batt + f.pv_to_export;
  const gridIn = f.grid_to_load + f.grid_to_batt;
  const gridOut = f.pv_to_export + f.batt_to_export;
  const prefix = state.range
    ? `${fmtRangeShort(state.range)} · `
    : "since 00:00 · ";
  subtitle.textContent =
    `${prefix}PV ${pvTotal.toFixed(1)} kWh · ` +
    `import ${gridIn.toFixed(1)} kWh · export ${gridOut.toFixed(1)} kWh`;
  const trace = buildSankeyTrace(f, "kWh", 1);
  Plotly.purge(div);
  await Plotly.newPlot(div, [trace], sankeyLayout(), {
    responsive: true, displaylogo: false,
  });
  state.built.sankeyToday = true;
  window.eoChart.registerPlot("sankey-today-figure");
}

// ── Daily spend panel ──────────────────────────────────────────────

async function redrawDailySpend() {
  const div = document.getElementById("spend-figure");
  const subtitle = document.getElementById("spend-subtitle");
  if (!div) return;

  const rows = state.history.dailySpend || [];
  if (rows.length === 0) {
    subtitle.textContent = "no settled bill data yet";
    if (state.built.spend) Plotly.purge(div);
    state.built.spend = false;
    return;
  }

  // /daily_spend returns DESC by nem_date; we want ASC for the bar chart
  // so the most recent day is at the right.
  const asc = [...rows].sort((a, b) => a.nem_date.localeCompare(b.nem_date));
  const dates = asc.map((r) => r.nem_date);
  const importCost   = asc.map((r) => r.import_cost_aud);
  // Show export revenue as a NEGATIVE bar: visually below zero, the
  // savings dipping the day's bar down toward (or past) zero.
  const exportRev    = asc.map((r) => r.export_revenue_aud != null ? -r.export_revenue_aud : null);
  const netCost      = asc.map((r) => r.net_cost_aud);

  // Subtitle: 30-day net total (or whatever's available) + average.
  const netVals = netCost.filter((v) => v != null && Number.isFinite(v));
  const total = netVals.reduce((a, b) => a + b, 0);
  const avg = netVals.length ? total / netVals.length : 0;
  subtitle.textContent =
    `${asc.length} days · net $${total.toFixed(2)} · avg $${avg.toFixed(2)}/day`;

  const traces = [
    {
      type: "bar",
      x: dates,
      y: importCost,
      name: "import cost",
      marker: { color: "#f0883e" },
      hovertemplate: "%{x}<br>import cost $%{y:.2f}<extra></extra>",
    },
    {
      type: "bar",
      x: dates,
      y: exportRev,
      name: "export revenue",
      marker: { color: "#56d364" },
      hovertemplate: "%{x}<br>export revenue $%{customdata:.2f}<extra></extra>",
      customdata: asc.map((r) => r.export_revenue_aud ?? 0),
    },
    {
      type: "scatter",
      mode: "lines+markers",
      x: dates,
      y: netCost,
      name: "net (bill)",
      line: { color: "#bc8cff", width: 2 },
      marker: { color: "#bc8cff", size: 5 },
      hovertemplate: "%{x}<br>net $%{y:.2f}<extra></extra>",
    },
  ];

  const narrow = isNarrowViewport();
  const layout = {
    margin: narrow
      ? { l: 32, r: 4,  t: 22, b: 32 }
      : { l: 50, r: 16, t: 26, b: 40 },
    paper_bgcolor: "#161b22",
    plot_bgcolor: "#161b22",
    font: { color: "#e8edf2", family: FONT_FAMILY, size: 12 },
    // Categorical x-axis — "zoom" is the desktop default; on narrow we
    // disable drag so vertical touch-scroll keeps the page moving.
    ...window.eoChart.mobileLayoutFragment({ desktopDrag: "zoom" }),
    barmode: "relative",
    showlegend: true,
    legend: {
      orientation: "h", x: 0, y: 1.10,
      font: { family: FONT_FAMILY, size: 11, color: "#c9d1d9" },
    },
    hovermode: "x unified",
    hoverlabel: HOVER_LABEL,
    shapes: spendCursorShapes(dates),
    xaxis: {
      type: "category",
      gridcolor: "#21262d",
      tickfont: { size: 11, color: "#c9d1d9" },
      tickcolor: "#444c56",
      ticklen: 3,
      automargin: true,
    },
    yaxis: {
      title: { text: "AUD / day", font: { size: 11, color: "#7d8590" }, standoff: 6 },
      gridcolor: "#21262d",
      zeroline: true,
      zerolinecolor: "#444c56",
      tickfont: { size: 12, color: "#c9d1d9" },
      tickcolor: "#444c56",
      ticklen: 3,
      automargin: true,
    },
  };

  if (!state.built.spend) {
    await Plotly.newPlot(div, traces, layout, window.eoChart.mobileConfig());
    state.built.spend = true;
    window.eoChart.registerPlot("spend-figure");
  } else {
    await Plotly.react(div, traces, layout);
  }
}

// Translucent overlay highlighting the spend bar that matches the
// time-series cursor's NEM date. `dates` is the list of category
// labels (YYYY-MM-DD) currently on the x-axis; the shape is anchored
// at the matching category, padded ±0.45 either side so it covers the
// bar group without bleeding into neighbours.
function spendCursorShapes(dates) {
  const cursorT = effectiveCursor();
  if (!cursorT || !dates || !dates.length) return [];
  const target = toNemDate(cursorT);
  const idx = dates.indexOf(target);
  if (idx < 0) return [];
  return [{
    type: "rect", xref: "x", yref: "paper",
    x0: idx - 0.45, x1: idx + 0.45, y0: 0, y1: 1,
    fillcolor: "rgba(88,166,255,0.12)",
    line: { color: "#58a6ff", width: 1 },
    layer: "above",
  }];
}

// Cheap cursor-only relayout — called from setCursor. Avoids rebuilding
// traces (a full redraw of dailySpend is dozens of bars + a line).
function redrawSpendCursor() {
  if (!state.built.spend) return;
  const div = document.getElementById("spend-figure");
  const rows = state.history.dailySpend || [];
  const asc = [...rows].sort((a, b) => a.nem_date.localeCompare(b.nem_date));
  const dates = asc.map((r) => r.nem_date);
  Plotly.relayout(div, { shapes: spendCursorShapes(dates) });
}

// ── Live snapshot stream + auto-refresh ────────────────────────────

// Apply a freshly-arrived snapshot — shared between the SSE push path
// and the polling fallback path (used when SSE is disconnected).
async function applySnapshot(snap) {
  state.snapshot = snap;
  state.modes = snap.active_modes || [];
  clearError();

  // Auto-advance cursor when not pinned. In historical mode, leave
  // it on the latest in-range telemetry row (effectiveCursor handles
  // that) — never jump it to live "now", which would be off-chart.
  if (!state.cursor.pinned && !isHistorical()) {
    state.cursor.time = nowFromSnapshot();
  }

  renderStatusStrip();
  renderLoads();
  renderCursorReadout();
  ModesUI.render();
  // In historical mode the time-series figure is driven by static
  // history; redrawing it on every snapshot poll just wastes work and
  // can fight a hovered cursor. Status strip / loads / events still
  // refresh because they reflect live operational state.
  if (!isHistorical()) {
    await redrawTSFigure();
    await redrawDailySankey();
    await redrawDailySpend();
  }
}

// Long-lived SSE client. Uses fetch + ReadableStream (not EventSource)
// so the bearer token rides on the Authorization header instead of
// leaking into the URL query string. Reconnects with exponential
// backoff on transient failures; gives up only on 401.
const SSE_BACKOFF_MS = [1000, 2000, 5000, 10000, 30000];

function parseSseFrame(frame) {
  let event = "message";
  let dataLines = [];
  for (const line of frame.split("\n")) {
    if (line === "" || line.startsWith(":")) continue;          // blank or comment
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
  }
  if (dataLines.length === 0) return null;
  return { event, data: dataLines.join("\n") };
}

async function streamSnapshots() {
  let attempt = 0;
  while (true) {
    if (!state.token) {
      // Token not entered yet (or just cleared by a 401). Hold off.
      await new Promise((r) => setTimeout(r, 1000));
      continue;
    }
    try {
      const res = await fetch("/dashboard/stream", {
        headers: { "Authorization": `Bearer ${state.token}` },
        cache: "no-store",
      });
      if (res.status === 401) {
        localStorage.removeItem(TOKEN_LS_KEY);
        state.token = null;
        showError("SSE unauthorized — reload to re-enter token");
        return;
      }
      if (!res.ok || !res.body) throw new Error(`SSE HTTP ${res.status}`);

      state.sseConnected = true;
      attempt = 0;

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        // SSE frames are separated by a blank line. The server emits
        // LF-only separators (see api/handlers/stream.py), so a plain
        // "\n\n" indexOf is sufficient.
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const parsed = parseSseFrame(frame);
          if (!parsed) continue;
          if (parsed.event === "snapshot") {
            try {
              const snap = JSON.parse(parsed.data);
              applySnapshot(snap).catch((err) => console.warn("applySnapshot failed", err));
            } catch (err) {
              console.warn("bad SSE snapshot payload", err);
            }
          }
        }
      }
    } catch (err) {
      console.warn("SSE stream error", err);
    } finally {
      state.sseConnected = false;
    }
    const delay = SSE_BACKOFF_MS[Math.min(attempt, SSE_BACKOFF_MS.length - 1)];
    attempt += 1;
    await new Promise((r) => setTimeout(r, delay));
  }
}

async function pollOnce() {
  // Fetch /readyz first — it's public and tells us the actual service
  // state. Failure here just means we'll keep the prior value.
  try {
    state.ready = await fetchReady();
  } catch (err) {
    console.warn("readyz fetch failed", err);
  }

  // Snapshot path is normally driven by SSE. Fall back to polling
  // /plan/current only while the SSE stream is disconnected (initial
  // connect, reconnect backoff, or hard auth/network failure).
  if (!state.sseConnected) {
    try {
      const snap = await fetchSnapshot();
      await applySnapshot(snap);
    } catch (err) {
      if (err.status === 503) {
        showError("Service hasn't completed a tick yet (HTTP 503). Will retry.");
      } else {
        showError(`fetch failed: ${err.message}`);
        console.warn(err);
      }
    }
  }

  // Logs are independent — refresh on a slower cadence (every 4th tick).
  if (Math.random() < 0.25) {
    try {
      const recs = await fetchLogs(200);
      state.events = recs.filter((r) => isNotable(r));
      renderEvents();
    } catch (err) {
      console.warn("logs fetch failed", err);
    }
  }
}

function isNotable(r) {
  if (!r) return false;
  const lvl = (r.level || "").toUpperCase();
  if (lvl === "WARNING" || lvl === "ERROR" || lvl === "CRITICAL") return true;
  const msg = (r.message || "").toLowerCase();
  return NOTABLE_EVENT_PREFIXES.some((p) => msg.includes(p));
}

async function loadHistory() {
  // Window depends on mode:
  //   live       → last 24h … now (forecast bands extend into the future
  //                via the snapshot's price_forecast / pv_forecast)
  //   historical → user-selected range, end-exclusive at midnight of `to+1`.
  // Partial failure is OK — each panel guards against its own data being
  // missing.
  if (state.history.inFlight) return;
  state.history.inFlight = true;
  try {
    let sinceISO, untilISO;
    if (state.range) {
      sinceISO = state.range.from.toISOString();
      untilISO = state.range.to.toISOString();
    } else {
      const now = new Date();
      const since = new Date(+now - HISTORY_LOOKBACK_MS);
      sinceISO = since.toISOString();
      untilISO = now.toISOString();
    }

    const [tel, priceLog, pvLog, amberUsage, dailySpend, loadTel] = await Promise.allSettled([
      fetchTelemetry(sinceISO, untilISO),
      fetchPriceForecastLog(sinceISO, untilISO),
      fetchPVForecastLog(sinceISO, untilISO),
      // amber_usage only contains settled NEM days, so the most recent
      // entries cover roughly the older half of the time-series window.
      fetchAmberUsage(sinceISO, untilISO),
      fetchDailySpend(60),
      fetchLoadTelemetry(sinceISO, untilISO),
    ]);

    if (tel.status === "fulfilled") state.history.rows = tel.value;
    else console.warn("telemetry fetch failed", tel.reason);

    // Both forecast feeds arrive already reduced by the server (latest
    // forecast per interval), so they're assigned straight through — the
    // old bucketLatest* client-side dedup moved into SQL.
    if (priceLog.status === "fulfilled") {
      state.history.priceForecast = priceLog.value;
    } else console.warn("price_forecast_log fetch failed", priceLog.reason);

    if (pvLog.status === "fulfilled") {
      state.history.pvForecast = pvLog.value;
    } else console.warn("pv_forecast_log fetch failed", pvLog.reason);

    if (amberUsage.status === "fulfilled") {
      state.history.amberUsage = amberUsage.value;
    } else console.warn("amber_usage fetch failed", amberUsage.reason);

    if (dailySpend.status === "fulfilled") {
      state.history.dailySpend = dailySpend.value;
    } else console.warn("daily_spend fetch failed", dailySpend.reason);

    if (loadTel.status === "fulfilled") state.history.loadTelemetry = loadTel.value;
    else console.warn("load_telemetry fetch failed", loadTel.reason);

    state.history.loadedAt = Date.now();
  } finally {
    state.history.inFlight = false;
  }
}

// Refresh history if we're in live mode and it's gone stale. Used both
// by the periodic poller and the visibilitychange handler — when a user
// returns to a backgrounded tab the cursor has advanced via SSE while
// loadHistory() was throttled, leaving the chart traces ending at the
// last sample fetched before the tab was hidden.
async function refreshLiveHistory({ force = false } = {}) {
  if (isHistorical()) return;
  if (!force && Date.now() - state.history.loadedAt < HISTORY_REFRESH_MS) return;
  await loadHistory();
  await redrawTSFigure();
  await redrawDailySankey();
  await redrawDailySpend();
}

// NOTE: the per-interval forecast reduction (latest fetched_at per
// interval, ForecastInterval-only, 5-min beats 30-min) that used to live
// here as bucketLatestPriceForecast / bucketLatestPVForecast now runs
// server-side in the /dashboard/price_forecast and /dashboard/pv_forecast
// SQL — see api/handlers/dashboard.py. Kept this note so the "where did
// the bucketing go?" question has an answer at the call site.

async function loadConfig() {
  try {
    state.config = await fetchConfig();
  } catch (err) {
    console.warn("config fetch failed", err);
  }
}

// ── Keyboard scrubbing ─────────────────────────────────────────────

function installKeyboard() {
  window.addEventListener("keydown", (ev) => {
    if (ev.target && ["INPUT", "TEXTAREA"].includes(ev.target.tagName)) return;
    let delta = 0;
    if (ev.key === "ArrowLeft")  delta = -SLOT_MS;
    else if (ev.key === "ArrowRight") delta = +SLOT_MS;
    else if (ev.key === "Home") { snapToNow(); ev.preventDefault(); return; }
    else return;

    const t = effectiveCursor();
    if (!t) return;
    let next = new Date(+t + delta);
    // Clamp inside the historical range so scrubbing can't run off the
    // edge of the loaded data.
    if (state.range) {
      const lo = +state.range.from;
      const hi = +state.range.to - SLOT_MS;
      if (+next < lo) next = new Date(lo);
      if (+next > hi) next = new Date(hi);
    }
    setCursor(next, { pinned: true });
    ev.preventDefault();
  });
}

// ── Range bar ──────────────────────────────────────────────────────

function localStartOfDay(d) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

function rangeFromPreset(preset) {
  // All ranges run [from, to) end-exclusive. Days are local-midnight
  // boundaries so the displayed window matches the user's clock.
  const now = new Date();
  const startToday = localStartOfDay(now);
  if (preset === "today") {
    return { from: startToday, to: new Date(+startToday + 24 * 3600_000) };
  }
  if (preset === "yesterday") {
    const start = new Date(+startToday - 24 * 3600_000);
    return { from: start, to: startToday };
  }
  if (preset === "7d") {
    const start = new Date(+startToday - 6 * 24 * 3600_000);
    return { from: start, to: new Date(+startToday + 24 * 3600_000) };
  }
  return null;
}

function syncRangeInputs() {
  const fromEl = document.getElementById("range-from");
  const toEl = document.getElementById("range-to");
  if (state.range) {
    fromEl.value = fmtDateInput(state.range.from);
    // `to` is end-exclusive midnight; show the inclusive last day.
    toEl.value = fmtDateInput(new Date(+state.range.to - 1));
  } else {
    if (!fromEl.value) {
      const yesterday = new Date(+localStartOfDay(new Date()) - 24 * 3600_000);
      fromEl.value = fmtDateInput(yesterday);
      toEl.value = fmtDateInput(yesterday);
    }
  }
}

function updateRangeIndicator() {
  const ind = document.getElementById("range-mode");
  if (!ind) return;
  // Two spans so mobile CSS can hide the verbose tail and keep just the
  // short tag inline next to Apply.
  if (state.range) {
    ind.innerHTML =
      `<span class="mode-tag">historical</span>` +
      `<span class="mode-detail"> · ${escapeHtml(fmtRangeShort(state.range))}</span>`;
    ind.className = "mode-indicator historical";
  } else {
    ind.innerHTML =
      `<span class="mode-tag">live</span>` +
      `<span class="mode-detail"> · last 24h + 48h forecast</span>`;
    ind.className = "mode-indicator live";
  }
  // Reflect the active preset on the buttons. `state.activePreset` is
  // "live" in live mode, the preset name (today/yesterday/7d) when one
  // was just clicked, or null if a custom range was applied via the
  // date inputs (in which case no preset is highlighted).
  const presetBtns = document.querySelectorAll(".range-bar [data-preset]");
  presetBtns.forEach((b) => {
    const active = b.dataset.preset === state.activePreset;
    b.setAttribute("aria-pressed", active ? "true" : "false");
    b.classList.toggle("primary", active);
  });
}

async function applyRange(range, presetName = null) {
  // null ⇒ live mode. Otherwise {from, to} (Dates, end-exclusive).
  state.range = range;
  state.activePreset = range ? presetName : "live";
  state.cursor.pinned = false;
  state.cursor.time = null;
  // Wipe past data so a stale frame doesn't sit visible while the new
  // window loads. Plotly redraws below.
  state.history.rows = [];
  state.history.priceForecast = [];
  state.history.pvForecast = [];
  state.history.amberUsage = [];

  syncRangeInputs();
  updateRangeIndicator();

  try {
    await loadHistory();
  } catch (err) {
    console.warn("loadHistory failed", err);
    showError(`load failed: ${err.message}`);
    return;
  }
  // Redraw everything that depends on the window. Snapshot-driven panels
  // (status strip, loads) keep showing live state regardless of mode.
  renderCursorReadout();
  await redrawTSFigure();
  await redrawDailySankey();
  await redrawDailySpend();
}

function installRangeBar() {
  syncRangeInputs();
  updateRangeIndicator();

  document.querySelectorAll(".range-bar [data-preset]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const p = btn.dataset.preset;
      if (p === "live") { applyRange(null, "live"); return; }
      const r = rangeFromPreset(p);
      if (r) applyRange(r, p);
    });
  });

  document.getElementById("range-apply").addEventListener("click", () => {
    const fromV = document.getElementById("range-from").value;
    const toV = document.getElementById("range-to").value;
    if (!fromV || !toV) {
      showError("pick a from and to date, then press Apply");
      return;
    }
    const from = new Date(`${fromV}T00:00:00`);
    let to = new Date(`${toV}T00:00:00`);
    // Make `to` end-exclusive at midnight of the day AFTER the picked day,
    // so picking from=2026-04-28, to=2026-04-28 gives a full 24h window.
    to = new Date(+to + 24 * 3600_000);
    if (!(from < to)) {
      showError("'to' date must be on or after 'from' date");
      return;
    }
    clearError();
    // null preset name ⇒ no preset button is highlighted (custom range).
    applyRange({ from, to }, null);
  });
}

// ── Bootstrap ──────────────────────────────────────────────────────

async function main() {
  document.getElementById("cursor-now-btn").addEventListener("click", snapToNow);
  installKeyboard();
  installRangeBar();

  // When the viewport crosses the mobile breakpoint (rotation, resize),
  // re-run the layout for each plot so the chart-margin and dragmode
  // overrides flip in/out cleanly. Plotly's `responsive: true` only
  // resizes — it doesn't re-evaluate the narrow-viewport branch.
  window.eoChart.onBreakpointChange(() => {
    if (state.built.ts) redrawTSFigure();
    if (state.built.spend) redrawDailySpend();
    if (state.built.sankeyToday) redrawDailySankey();
  });

  if (!ensureToken()) return;

  await loadConfig();
  await loadHistory();
  // Open the live SSE stream — this is the primary path for snapshot
  // updates. pollOnce() is a fallback that only fires when SSE drops.
  // Don't await: the fetch() resolves only when the stream ends.
  streamSnapshots();
  // Tick-age counter advances between snapshots. The full status strip
  // only re-renders on each new TickSnapshot (~60 s under SSE); this
  // 1 s interval just rewrites the "Ns" chip so the freshness reading
  // stays honest.
  setInterval(renderTickAge, 1000);
  await pollOnce();

  // Refresh history immediately when the tab becomes visible. Browsers
  // throttle setInterval in background tabs (Chrome: ≥60s, often more),
  // so by the time the user returns the SSE-driven cursor has marched
  // ahead while history.rows still ends at the pre-background sample.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      refreshLiveHistory({ force: true }).catch((err) =>
        console.warn("visibility refresh failed", err)
      );
    }
  });

  setInterval(() => {
    pollOnce();
    // Past edge advances deterministically — fires on the first poll
    // past HISTORY_REFRESH_MS since the last successful reload. Live
    // mode only; historical windows are fixed.
    refreshLiveHistory().catch((err) =>
      console.warn("periodic history refresh failed", err)
    );
  }, POLL_INTERVAL_MS);
}

document.addEventListener("DOMContentLoaded", main);

// ── User-strategy modes ───────────────────────────────────────────────
// All HTTP calls route through apiFetch (sets the Authorization header,
// handles 401 by clearing the token). Bare fetch() would 401 silently
// against the protected /modes routes.
const ModesUI = (() => {
  const panel = document.getElementById("mode-activate-panel");
  const form = document.getElementById("mode-activate-form");
  const title = document.getElementById("mode-panel-title");
  const thresholdLabel = document.getElementById("mode-threshold-label");
  const thresholdInput = document.getElementById("mode-threshold");
  const durationSelect = document.getElementById("mode-duration");
  const hint = document.getElementById("mode-suggest-hint");
  const cancelBtn = document.getElementById("mode-panel-cancel");
  const submitBtn = document.getElementById("mode-panel-submit");
  const socCutoffField = document.getElementById("mode-soc-cutoff-field");
  const socCutoffInput = document.getElementById("mode-soc-cutoff");
  let currentKind = null;
  let currentMode = null;  // when editing, the mode being edited; else null
  let suggestSeq = 0;  // dropped-old-response guard

  // Preset duration options on the <select>. Used by edit-mode to
  // snap "remaining minutes" to the closest available preset so the
  // initial selection reflects the in-flight window.
  const DURATION_PRESETS = [15, 30, 60, 120, 240, 480, 1440, 2880];
  function closestDurationPreset(minutes) {
    if (!Number.isFinite(minutes) || minutes <= 0) return 60;
    // Prefer the smallest preset >= remaining; falls through to the max.
    for (const p of DURATION_PRESETS) if (p >= minutes) return p;
    return DURATION_PRESETS[DURATION_PRESETS.length - 1];
  }

  function paramKey(kind) {
    return kind === "buy" ? "ceiling_c_per_kwh" : "floor_c_per_kwh";
  }
  function responseKey(kind) {
    return kind === "buy" ? "suggested_ceiling_c_per_kwh" : "suggested_floor_c_per_kwh";
  }
  function thresholdLabelText(kind) {
    return kind === "buy" ? "Ceiling (c/kWh)" : "Floor (c/kWh)";
  }

  async function refreshSuggestion({ overwriteThreshold = true } = {}) {
    if (!currentKind) return;
    const seq = ++suggestSeq;
    const dur = durationSelect.value;
    hint.textContent = "Computing suggestion…";
    try {
      const body = await apiFetch(
        `/modes/suggest?kind=${currentKind}&duration_minutes=${dur}`,
      );
      if (seq !== suggestSeq) return;  // stale response
      const value = body[responseKey(currentKind)];
      if (typeof value === "number") {
        if (overwriteThreshold) thresholdInput.value = value;
        hint.textContent =
          `Suggested ${currentKind === "buy" ? "ceiling" : "floor"}: ${value} c/kWh ` +
          `(75th percentile of in-window ${currentKind === "buy" ? "import" : "export"} prices)`;
      } else {
        hint.textContent = "No suggestion available for this window.";
      }
    } catch (e) {
      if (seq !== suggestSeq) return;
      hint.textContent = `Could not load suggestion (${e.message}).`;
    }
  }

  function openActivatePanel(kind, existing = null) {
    currentKind = kind;
    currentMode = existing;
    const editing = existing !== null;
    title.textContent = `${editing ? "Edit" : "Activate"} ${kind} mode`;
    submitBtn.textContent = editing ? "Update" : "Activate";
    thresholdLabel.textContent = thresholdLabelText(kind);
    // SOC cutoff is buy-mode only.
    socCutoffField.hidden = kind !== "buy";

    if (editing) {
      // Prefill from the running mode: threshold + SOC cutoff + duration
      // closest to remaining minutes. The user can change any of these
      // before submitting; submit replaces the running mode.
      const tv = existing.params[paramKey(kind)];
      thresholdInput.value = typeof tv === "number" ? tv : "";
      const cutoff = existing.params.soc_cutoff_pct;
      socCutoffInput.value =
        kind === "buy" && typeof cutoff === "number" ? cutoff : "";
      const remainingMin = Math.max(
        1,
        Math.round((new Date(existing.end_at) - new Date()) / 60_000),
      );
      durationSelect.value = String(closestDurationPreset(remainingMin));
      hint.textContent = "Editing the running mode. Submit replaces it with the values shown.";
    } else {
      thresholdInput.value = "";
      socCutoffInput.value = "";
      durationSelect.value = "60";
      hint.textContent = "Computing suggestion…";
    }
    panel.showModal();
    // On edit, fetch the suggestion as advisory (don't overwrite the
    // user's running threshold). On activate, overwrite the blank input
    // with the suggested value as before.
    refreshSuggestion({ overwriteThreshold: !editing });
  }

  function closePanel() {
    currentKind = null;
    currentMode = null;
    suggestSeq++;  // invalidate any in-flight suggest
    panel.close();
  }

  durationSelect.addEventListener("change", () => {
    // On edit, don't clobber the user's threshold; on activate, do.
    refreshSuggestion({ overwriteThreshold: currentMode === null });
  });
  cancelBtn.addEventListener("click", closePanel);
  // Pressing Escape on the dialog also closes — the native behaviour is
  // already correct; no extra wiring needed.

  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".mode-card-action");
    if (!btn) return;
    const kind = btn.dataset.kind;
    const action = btn.dataset.action;
    if (action === "activate") {
      openActivatePanel(kind);
    } else if (action === "edit") {
      const existing = (state.modes || []).find((m) => m.kind === kind);
      if (!existing) {
        showError(`Cannot edit ${kind} mode: not active`);
        return;
      }
      openActivatePanel(kind, existing);
    } else if (action === "cancel") {
      cancelMode(kind);
    }
  });

  async function cancelMode(kind) {
    // DELETE returns 204 No Content on success; apiFetch's unconditional
    // res.json() chokes on the empty body. Use a bare auth fetch and
    // inspect status directly. 404 is benign (mode already expired
    // between render-poll and click).
    if (!state.token) return;
    let resp;
    try {
      resp = await fetch(`/modes/${kind}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${state.token}` },
      });
    } catch (e) {
      showError(`Failed to cancel ${kind} mode: ${e.message}`);
      return;
    }
    if (resp.status === 401) {
      localStorage.removeItem(TOKEN_LS_KEY);
      state.token = null;
      showError(`Unauthorised — reload and re-enter the API token.`);
      return;
    }
    if (!resp.ok && resp.status !== 404) {
      showError(`Failed to cancel ${kind} mode: HTTP ${resp.status}`);
      return;
    }
    // Optimistic local update — strip the cancelled mode from state.modes
    // so the card flips to Inactive immediately. The next SSE snapshot
    // (~60s later) will reconcile this with the server's authoritative
    // active_modes list.
    state.modes = (state.modes || []).filter((m) => m.kind !== kind);
    render();
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!currentKind) return;
    const threshold = parseFloat(thresholdInput.value);
    if (!Number.isFinite(threshold) || threshold <= 0 || threshold > 100) {
      hint.textContent = "Threshold must be a number in (0, 100] c/kWh.";
      thresholdInput.focus();
      return;
    }
    const minutes = parseInt(durationSelect.value, 10);
    const endAt = new Date(Date.now() + minutes * 60_000).toISOString();
    const body = { end_at: endAt, [paramKey(currentKind)]: threshold };
    // Optional SOC cutoff for buy mode. Server-side validation handles
    // the "cutoff not above current SOC" case; we just forward the raw
    // value when the user supplied one.
    if (currentKind === "buy" && socCutoffInput.value.trim() !== "") {
      const cutoff = parseFloat(socCutoffInput.value);
      if (!Number.isFinite(cutoff) || cutoff <= 0 || cutoff > 100) {
        hint.textContent = "SOC cutoff must be a number in (0, 100]%.";
        socCutoffInput.focus();
        return;
      }
      body.soc_cutoff_pct = cutoff;
    }
    let newMode;
    try {
      newMode = await apiFetch(`/modes/${currentKind}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
    } catch (e) {
      hint.textContent = `Activation failed: ${e.message}`;
      return;
    }
    // Optimistic local update — splice the newly-activated mode into
    // state.modes so the card flips to Active immediately. SSE snapshot
    // will reconcile.
    state.modes = (state.modes || []).filter((m) => m.kind !== newMode.kind);
    state.modes.push(newMode);
    closePanel();
    render();
  });

  function formatCountdown(minutes) {
    if (minutes <= 0) return "<1m";
    if (minutes < 60) return `${minutes}m`;
    return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
  }

  function render() {
    // Synchronous render — reads from state.modes (refreshed on every
    // SSE snapshot via applySnapshot, and mutated locally after
    // activate/cancel for instant feedback). No polling, no fetch.
    const now = new Date();
    const byKind = Object.fromEntries((state.modes || []).map((m) => [m.kind, m]));
    for (const kind of ["buy", "conserve"]) {
      const card = document.getElementById(`mode-card-${kind}`);
      if (!card) continue;
      const stateEl = card.querySelector('[data-field="state"]');
      const inactiveBody = card.querySelector('[data-state="inactive"]');
      const activeBody = card.querySelector('[data-state="active"]');
      const m = byKind[kind];
      if (!m) {
        card.dataset.active = "false";
        stateEl.textContent = "Inactive";
        inactiveBody.hidden = false;
        activeBody.hidden = true;
        continue;
      }
      card.dataset.active = "true";
      stateEl.textContent = "Active";
      inactiveBody.hidden = true;
      activeBody.hidden = false;
      const end = new Date(m.end_at);
      const minutes = Math.max(0, Math.round((end - now) / 60_000));
      activeBody.querySelector('[data-field="countdown"]').textContent =
        formatCountdown(minutes);
      const v = m.params[paramKey(kind)];
      activeBody.querySelector('[data-field="threshold"]').textContent =
        `${v} c/kWh`;
      // Show the SOC cutoff row only when the mode has one set (buy only).
      const cutoffRow = activeBody.querySelector('[data-field="soc-cutoff-row"]');
      if (cutoffRow) {
        const cutoff = m.params.soc_cutoff_pct;
        if (typeof cutoff === "number") {
          cutoffRow.hidden = false;
          activeBody.querySelector('[data-field="soc-cutoff"]').textContent =
            cutoff;
        } else {
          cutoffRow.hidden = true;
        }
      }
    }
  }

  return { render };
})();

// Countdown re-render — purely client-side. State.modes is refreshed
// from the SSE snapshot push (~60s cadence via applySnapshot); this
// 30s tick keeps the "Ends in Xm" text fresh between snapshots without
// any network call. Render is a no-op when no modes are active.
setInterval(() => ModesUI.render(), 30_000);
