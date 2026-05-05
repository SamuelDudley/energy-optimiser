/* eslint-disable no-console */
// Energy Optimiser dashboard — vanilla JS + Plotly.
//
// Hard rule: every value rendered comes from a real API response. When a
// field is null/missing, render "—" or a gap. No fabricated values, no
// noise added "for visual interest".
//
// Data sources, in order of authority:
//   /plan/current        — the in-memory TickSnapshot. Drives "now" and
//                          all future projections. Polled every 15 s.
//   /telemetry           — historical 5-min rows. Past lines.
//   /dashboard/config    — battery config (soc_floor_pct etc.).
//   /logs                — recent operational events.
//
// Layout: one Plotly figure (#ts-figure) holds 6 stacked subplots that
// share a single x-axis: prices, decision ribbon, solar, SOC, grid,
// cost. A separate Plotly figure (#sankey-figure) holds the energy-flow
// Sankey, redrawn whenever the cursor moves. Status strip and loads /
// events are plain DOM.

"use strict";

// ── Constants ──────────────────────────────────────────────────────

const POLL_INTERVAL_MS = 15_000;
const HISTORY_LOOKBACK_MS = 24 * 3600 * 1000;       // past 24h
const FUTURE_HORIZON_MS = 48 * 3600 * 1000;          // x-axis right edge

const TOKEN_LS_KEY = "eo_dashboard_token";

// Slot semantics — must stay in sync with optimiser/lp/constants.py.
const SLOT_MINUTES = 5;
const SLOT_MS = SLOT_MINUTES * 60 * 1000;
const DEADBAND_KW = 0.1;
const MODE_SWITCH_HYSTERESIS_KW = 0.05;

// Decision categories driving the ribbon.
const DECISION = {
  CHARGE_GRID: 0,
  CHARGE_PV:   1,
  IDLE:        2,
  DISCHARGE:   3,
  UNKNOWN:     4,
};
const DECISION_COLORS = {
  [DECISION.CHARGE_GRID]: "#d29922", // amber — pay to fill
  [DECISION.CHARGE_PV]:   "#3fb950", // green — soak free PV
  [DECISION.IDLE]:        "#444c56", // dark gray — hold
  [DECISION.DISCHARGE]:   "#bc8cff", // purple — earn
  [DECISION.UNKNOWN]:     "#21262d", // near-bg — no data
};
const DECISION_LABELS = {
  [DECISION.CHARGE_GRID]: "charge (grid)",
  [DECISION.CHARGE_PV]:   "charge (PV)",
  [DECISION.IDLE]:        "idle",
  [DECISION.DISCHARGE]:   "discharge",
  [DECISION.UNKNOWN]:     "—",
};

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
  { id: "ribbon",   axis: "y2", height: 0.04, label: "DECISION" },
  { id: "solar",    axis: "y3", height: 0.18, label: "PV",      units: "kW" },
  { id: "soc",      axis: "y4", height: 0.16, label: "SOC",     units: "%" },
  { id: "load",     axis: "y7", height: 0.14, label: "LOAD",    units: "kW" },
  { id: "managed",  axis: "y8", height: 0.12, label: "MANAGED", units: "kW" },
  { id: "grid",     axis: "y5", height: 0.14, label: "GRID",    units: "kW" },
  { id: "cost",     axis: "y6", height: 0.16, label: "COST",    units: "c/h" },
];

// Stable colour per managed-load id (hash → palette). Distinct from the
// chart's other panel colours so a managed-load trace doesn't visually
// collide with grid / load lines that may share screen space at narrow
// widths.
const LOAD_PALETTE = ["#7ee787", "#79c0ff", "#ffa657", "#ff7b72", "#bc8cff"];
function colorForLoadId(loadId) {
  let h = 0;
  for (let i = 0; i < loadId.length; i++) h = (h * 31 + loadId.charCodeAt(i)) | 0;
  return LOAD_PALETTE[Math.abs(h) % LOAD_PALETTE.length];
}
const PANEL_GAP = 0.02;

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
  history: {
    rows: [],                    // telemetry rows ascending by ts
    priceForecast: [],           // latest forecast band per (interval_start, resolution)
    pvForecast: [],              // latest p10/p50/p90 per period_end
    amberUsage: [],              // amber_usage rows (settled per-5-min spend)
    loadTelemetry: [],           // load_telemetry rows (per-load 5-min power/energy)
    dailySpend: [],              // /daily_spend rows (descending by nem_date)
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
  built: { ts: false, sankey: false, sankeyToday: false, spend: false },
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
async function fetchTelemetry(sinceISO, untilISO) {
  return await fetchTablePaged("telemetry", sinceISO, untilISO, "ts");
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
  // ~600 rows/hr (Amber polled every 60s with ~50 rows/round + 5-min poll
  // every 5 min). 24h ≈ 14.5k rows. Server cap is 10000/page; allow up to
  // 4 pages so the full window is covered with margin. The earlier
  // 1000×6 default truncated 24h to roughly the first 10h, so the
  // `export_forecast_*` columns (only populated since 2026-04-28)
  // disappeared from the bucketed result on any window that started
  // before they existed.
  return await fetchTablePaged(
    "price_forecast_log", sinceISO, untilISO, "fetched_at",
    { limit: 10000, maxPages: 4 },
  );
}
async function fetchPVForecastLog(sinceISO, untilISO) {
  return await fetchTablePaged("pv_forecast_log", sinceISO, untilISO, "fetched_at");
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
  let cursor = sinceISO || null;
  const rows = [];
  for (let i = 0; i < MAX_PAGES; i++) {
    const params = new URLSearchParams();
    if (cursor) params.set("since", cursor);
    if (untilISO) params.set("until", untilISO);
    params.set("limit", String(LIMIT));
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

// ── Data: derive per-slot decision from a SlotDecision object ──────

function decisionFor(slot) {
  if (!slot) return DECISION.UNKNOWN;
  const b = slot.battery_kw;
  if (b == null || !Number.isFinite(b)) return DECISION.UNKNOWN;
  if (Math.abs(b) < DEADBAND_KW) return DECISION.IDLE;
  if (b < 0) return DECISION.DISCHARGE;
  // Charging — split by grid-vs-PV contribution, matching dispatch_from_slot.
  const g = slot.grid_to_battery_kw ?? 0;
  const p = slot.pv_to_battery_kw ?? 0;
  if (g > p + MODE_SWITCH_HYSTERESIS_KW) return DECISION.CHARGE_GRID;
  return DECISION.CHARGE_PV;
}

// Realised category from a telemetry row's planner_action. The string
// values come straight from BatteryAction enum names, so we match those.
function decisionFromTelemetry(row) {
  const a = row.planner_action;
  if (!a) return DECISION.UNKNOWN;
  if (a === "charge_grid") return DECISION.CHARGE_GRID;
  if (a === "charge_pv")   return DECISION.CHARGE_PV;
  if (a === "discharge_pv" || a === "discharge_ess") return DECISION.DISCHARGE;
  if (a === "self_consume" || a === "standby") return DECISION.IDLE;
  return DECISION.UNKNOWN;
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

// Same shape directly off a SlotDecision (no inference).
function flowsFromSlot(slot) {
  if (!slot) return null;
  const b = slot.battery_kw ?? 0;
  const battDischarge = b < 0 ? -b : 0;
  // The slot carries pv_to_export and grid_export; battery's contribution
  // to export is whatever export isn't covered by PV.
  const grid_export = slot.grid_export_kw ?? 0;
  const pv_to_export = slot.pv_to_export_kw ?? 0;
  const batt_to_export = Math.max(0, grid_export - pv_to_export);
  const batt_to_load = Math.max(0, battDischarge - batt_to_export);
  // grid_import covers grid→batt and grid→load.
  const grid_to_batt = slot.grid_to_battery_kw ?? 0;
  const grid_to_load = Math.max(0, (slot.grid_import_kw ?? 0) - grid_to_batt);
  return {
    pv_to_load: slot.pv_to_house_kw ?? 0,
    pv_to_batt: slot.pv_to_battery_kw ?? 0,
    pv_to_export: pv_to_export,
    grid_to_load: grid_to_load,
    grid_to_batt: grid_to_batt,
    batt_to_load: batt_to_load,
    batt_to_export: batt_to_export,
  };
}

// ── Cursor model ───────────────────────────────────────────────────

function nowFromSnapshot() {
  if (!state.snapshot) return null;
  return new Date(state.snapshot.timestamp);
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
  redrawSankey();
  redrawCursorLine();
}

function snapToNow() {
  state.cursor.pinned = false;
  // In historical mode, "now" doesn't apply — effectiveCursor() will
  // resolve to the latest in-range telemetry row instead.
  state.cursor.time = isHistorical() ? null : nowFromSnapshot();
  renderCursorReadout();
  redrawSankey();
  redrawCursorLine();
}

function nearestSlotAt(time) {
  // Round `time` down to the start of the 5-min slot it falls in.
  const t = time instanceof Date ? time.getTime() : +time;
  return new Date(Math.floor(t / SLOT_MS) * SLOT_MS);
}

// ── Status strip ───────────────────────────────────────────────────

function renderStatusStrip() {
  const snap = state.snapshot;
  const stateEl = document.getElementById("status-state");
  const tickAgeEl = document.getElementById("status-tick-age");
  const socEl = document.getElementById("status-soc");
  const sohEl = document.getElementById("status-soh");
  const dispModeEl = document.getElementById("status-dispatch-mode");
  const dispDetailEl = document.getElementById("status-dispatch-detail");

  if (!snap) {
    stateEl.textContent = "no plan";
    stateEl.className = "status-value status-state-unknown";
    tickAgeEl.textContent = "—";
    socEl.textContent = "—"; sohEl.textContent = "—";
    dispModeEl.textContent = "—"; dispDetailEl.textContent = "—";
    setTile("pv", null); setTile("batt", null); setTile("grid", null); setTile("load", null);
    return;
  }

  // State badge: prefer the actual ServiceState from /readyz; if that
  // hasn't arrived yet, fall back to the LP solve status from the
  // snapshot — we never invent "ACTIVE" without evidence.
  const ready = state.ready;
  const lp = snap.lp_solution;
  const lpStatus = lp ? lp.status : null;
  let badgeKey = "unknown", badgeText = "—";
  if (ready && ready.state) {
    badgeKey = ready.state;
    badgeText = ready.state.toUpperCase();
    if (ready.sigenergy_connected === false) badgeText += " (NO INV)";
  } else if (lpStatus) {
    badgeKey = (lpStatus === "optimal" || lpStatus === "feasible") ? "active" : "degraded";
    badgeText = `LP ${lpStatus.toUpperCase()}`;
  }
  stateEl.textContent = badgeText;
  stateEl.className = `status-value ${STATE_CLASS[badgeKey] || "status-state-unknown"}`;

  const tickAgeS = (Date.now() - new Date(snap.timestamp).getTime()) / 1000;
  tickAgeEl.textContent = `tick ${tickAgeS.toFixed(0)}s ago · v${snap.version}`;

  // SOC + SOH from system_state (post-dispatch preferred).
  const ss = snap.system_state_post_dispatch || snap.system_state;
  socEl.textContent = fmtPct(ss?.soc_pct);
  sohEl.textContent = ss?.soh_pct != null ? `SOH ${ss.soh_pct.toFixed(1)}%` : "SOH —";

  // Dispatch (mode + cap + signed intent).
  const disp = snap.lp_dispatch;
  if (!disp) {
    dispModeEl.textContent = "—"; dispDetailEl.textContent = "—";
  } else {
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
    dispModeEl.textContent = `${disp.kind} · m${disp.mode}`;
    dispDetailEl.textContent =
      `${modeName} · cap ${disp.cap_kw.toFixed(2)} kW · intent ${disp.signed_intent_kw.toFixed(2)} kW`;
  }

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

function setTile(id, value) {
  const tile = document.getElementById(`tile-${id}`);
  if (!tile) return;
  const v = tile.querySelector(".tile-value");
  v.textContent = fmtKW(value);
}

function renderCursorReadout() {
  const t = effectiveCursor();
  document.getElementById("cursor-time").textContent = fmtTime(t);
  document.getElementById("cursor-mode").textContent = state.cursor.pinned ? "pinned" : "live";
  document.getElementById("cursor-now-btn").disabled = !state.cursor.pinned;
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

function panelDomains() {
  // Returns { id: [yLow, yHigh] } with PANEL_GAP between panels.
  // Panels listed top-to-bottom; domain values are 0=bottom, 1=top.
  const totalGaps = (PANEL_LAYOUT.length - 1) * PANEL_GAP;
  const totalH = PANEL_LAYOUT.reduce((a, p) => a + p.height, 0);
  const scale = (1 - totalGaps) / totalH;
  let cursor = 1;
  const domains = {};
  for (const p of PANEL_LAYOUT) {
    const h = p.height * scale;
    const top = cursor;
    const bot = cursor - h;
    domains[p.id] = [bot, top];
    cursor = bot - PANEL_GAP;
  }
  return domains;
}

function buildTraces() {
  const snap = state.snapshot;
  const hist = state.history.rows;
  // In historical mode the snapshot's forward_trajectory / price_forecast
  // / pv_forecast all describe "now + 48h" — irrelevant to a past range
  // and confusing if rendered. We require a snapshot in live mode (so the
  // time-series traces have a current frame of reference); historical
  // mode renders from telemetry alone.
  if (!isHistorical() && !snap) return [];
  if (isHistorical() && hist.length === 0) return [];

  // ── Past series (from telemetry) ──
  // Timestamps are converted to local-naive at trace boundary. Past +
  // future arrays are kept aligned by index — the conversion is the
  // last step before they go to Plotly.
  const pastTs = hist.map((r) => toPlotlyTime(r.ts));
  const importPast  = hist.map((r) => r.import_price);
  const exportPast  = hist.map((r) => r.export_price);
  const pvPast      = hist.map((r) => r.pv_kw);
  const socPast     = hist.map((r) => r.soc_pct);
  const loadPast    = hist.map((r) => r.house_load_kw);
  const gridImpPast = hist.map((r) => r.grid_kw != null ? Math.max(0,  r.grid_kw) : null);
  const gridExpPast = hist.map((r) => r.grid_kw != null ? Math.max(0, -r.grid_kw) : null);
  const costPast    = hist.map((r) => marginalCost(r.import_price, r.export_price, r.grid_kw));

  // ── Future series (from snapshot) ──
  // Empty in historical mode — the snapshot's forward arrays describe
  // "now + 48h" and would either render outside the fixed x-range or
  // (worse) drag the x-range into the live present.
  const fwd = isHistorical() ? [] : (snap?.lp_solution?.forward_trajectory || []);
  const slotTs = fwd.map((s) => toPlotlyTime(s.slot_start));

  // Past forecasts (from /price_forecast_log + /pv_forecast_log) get
  // joined with the snapshot's current+future arrays. The latest-forecast
  // bucketing happens in loadHistory(); here we just concatenate.
  const pastPriceFC = state.history.priceForecast;
  const futurePriceFC = isHistorical() ? [] : (snap?.price_forecast || []);
  const priceFCMerged = mergePriceForecasts(pastPriceFC, futurePriceFC);

  const pastPVFC = state.history.pvForecast;
  const futurePVFC = isHistorical() ? [] : (snap?.pv_forecast || []);
  const pvFCMerged = mergePVForecasts(pastPVFC, futurePVFC);

  const priceTsFut = priceFCMerged.map((p) => toPlotlyTime(p.start));
  const importFut  = priceFCMerged.map((p) => coalesce(p.forecast_predicted, p.import_per_kwh));
  const exportFut  = priceFCMerged.map((p) => coalesce(p.export_forecast_predicted, p.export_per_kwh));

  const pvTsFut = pvFCMerged.map((p) => toPlotlyTime(p.start));
  const pvP50 = pvFCMerged.map((p) => p.pv_estimate_kw);
  const pvP10 = pvFCMerged.map((p) => p.pv_estimate10_kw);
  const pvP90 = pvFCMerged.map((p) => p.pv_estimate90_kw);
  // PV measured (from pv_forecast_log.actual_kw, backfilled by Solcast's
  // estimated-actuals job). Falls back to telemetry pv_kw averaged into
  // 30-min buckets — but that backfill isn't always run, so just use
  // actual_kw where present.
  const pvActualPast = pastPVFC.map((p) => p.actual_kw);
  const pvActualPastTs = pastPVFC.map((p) => toPlotlyTime(p.period_end));

  const socFut = fwd.map((s) => s.soc_pct_end);
  const gridImpFut = fwd.map((s) => s.grid_import_kw ?? null);
  const gridExpFut = fwd.map((s) => -(s.grid_export_kw ?? 0)); // negative for symmetry
  // Reconstruct planned house+managed load from the slot's energy balance.
  // System balance: pv_to_house + bat_discharge + grid_import - grid_to_battery
  //                 - (grid_export - pv_to_export)  ==  house_base + load_total
  // Battery share of export = grid_export - pv_to_export, subtracted because
  // that part of the discharge leaves the meter, it doesn't serve load.
  const loadFut = fwd.map((s) => {
    const pvToHouse = s.pv_to_house_kw ?? 0;
    const batDischarge = Math.max(0, -(s.battery_kw ?? 0));
    const gridImp = s.grid_import_kw ?? 0;
    const gridToBat = s.grid_to_battery_kw ?? 0;
    const gridExp = s.grid_export_kw ?? 0;
    const pvToExp = s.pv_to_export_kw ?? 0;
    return pvToHouse + batDischarge + gridImp - gridToBat - (gridExp - pvToExp);
  });
  const costFut = fwd.map((s) => {
    const ip = pickPriceAt(futurePriceFC, s.slot_start, "import");
    const ep = pickPriceAt(futurePriceFC, s.slot_start, "export");
    if (ip == null || ep == null) return null;
    return ip * (s.grid_import_kw ?? 0) - ep * (s.grid_export_kw ?? 0);
  });

  // ── Decision ribbon (heatmap) ──
  // x are slot starts; y has two values to give the heatmap rectangular
  // height; z is one row of category indices, one per slot.
  const ribbonZ = fwd.map((s) => decisionFor(s));
  const ribbonZPast = hist.map((r) => decisionFromTelemetry(r));
  const ribbonX = [...pastTs, ...slotTs];
  const ribbonZRow = [...ribbonZPast, ...ribbonZ];

  const traces = [];

  // Prices (yaxis y) — bands drawn first (behind), then the predicted /
  // realised lines. Each contiguous run of valid (low, high) is its own
  // `fill: "toself"` polygon trace: forward along LOW, back along HIGH.
  // One trace per run rather than null-separated runs in a single trace
  // because Plotly's `toself` closes the polygon across `(null, null)`
  // separators rather than treating each segment as a distinct closed
  // shape, which produced visible vertical wedges at run boundaries.
  for (const poly of bandPolygons(priceFCMerged, "forecast_low", "forecast_high")) {
    traces.push({
      x: poly.x, y: poly.y,
      type: "scatter", mode: "lines",
      line: { width: 0, color: "rgba(0,0,0,0)" }, fill: "toself",
      fillcolor: "rgba(240,136,62,0.15)",
      hoverinfo: "skip", showlegend: false,
      yaxis: "y", name: "import band",
    });
  }
  for (const poly of bandPolygons(priceFCMerged, "export_forecast_low", "export_forecast_high")) {
    traces.push({
      x: poly.x, y: poly.y,
      type: "scatter", mode: "lines",
      line: { width: 0, color: "rgba(0,0,0,0)" }, fill: "toself",
      fillcolor: "rgba(86,211,100,0.15)",
      hoverinfo: "skip", showlegend: false,
      yaxis: "y", name: "export band",
    });
  }
  // Three lines per side now:
  //   • realised — telemetry import_price/export_price (past only)
  //   • predicted (history) — what Amber forecast at planning time (past)
  //   • predicted (future) — same field, but from the snapshot
  // Realised is solid and primary; predicted is a thin dotted overlay so
  // calibration drift is visible without dominating the panel.
  traces.push({
    x: pastTs, y: importPast,
    type: "scatter", mode: "lines",
    line: { color: "#f0883e", width: 1.6 },
    yaxis: "y", name: "import realised", connectgaps: false,
  });
  traces.push({
    x: priceTsFut, y: importFut,
    type: "scatter", mode: "lines",
    line: { color: "#f0883e", width: 1.0, dash: "dot" },
    yaxis: "y", name: "import predicted", connectgaps: false,
  });
  traces.push({
    x: pastTs, y: exportPast,
    type: "scatter", mode: "lines",
    line: { color: "#56d364", width: 1.6 },
    yaxis: "y", name: "export realised", connectgaps: false,
  });
  traces.push({
    x: priceTsFut, y: exportFut,
    type: "scatter", mode: "lines",
    line: { color: "#56d364", width: 1.0, dash: "dot" },
    yaxis: "y", name: "export predicted", connectgaps: false,
  });

  // Decision ribbon (yaxis y2) — heatmap. Build a stepped colorscale:
  // each category gets a color held constant across its z-range so
  // Plotly doesn't interpolate between adjacent categories. Z values are
  // the exact category indices; zmin/zmax bracket the full range.
  const decisionVals = Object.values(DECISION);
  const dMin = 0, dMax = decisionVals.length - 1;
  const colorscale = [];
  for (let i = 0; i < decisionVals.length; i++) {
    const lo = i / decisionVals.length;
    const hi = (i + 1) / decisionVals.length;
    const c = DECISION_COLORS[decisionVals[i]];
    colorscale.push([lo, c]);
    colorscale.push([hi, c]);
  }
  traces.push({
    x: ribbonX,
    y: [0, 1],
    z: [ribbonZRow],
    type: "heatmap",
    colorscale,
    showscale: false,
    hoverinfo: "text",
    text: [ribbonZRow.map((v, i) => `${fmtTime(ribbonX[i])} — ${DECISION_LABELS[v] ?? "—"}`)],
    yaxis: "y2",
    name: "decision",
    zmin: dMin, zmax: dMax,
  });

  // Solar (yaxis y3).
  if (pvP10.some((v) => v != null) && pvP90.some((v) => v != null)) {
    traces.push({
      x: pvTsFut, y: pvP10, type: "scatter", mode: "lines",
      line: { width: 0 }, hoverinfo: "skip",
      showlegend: false, yaxis: "y3", name: "pv-p10",
    });
    traces.push({
      x: pvTsFut, y: pvP90, type: "scatter", mode: "lines",
      line: { width: 0 }, fill: "tonexty",
      fillcolor: "rgba(242,204,96,0.18)",
      hoverinfo: "skip", showlegend: false,
      yaxis: "y3", name: "PV P10–P90",
    });
  }
  traces.push({
    x: pvTsFut, y: pvP50, type: "scatter", mode: "lines",
    line: { color: "#f2cc60", width: 1.6, dash: "dot" },
    yaxis: "y3", name: "PV P50",
  });
  traces.push({
    x: pastTs, y: pvPast, type: "scatter", mode: "lines",
    line: { color: "#f2cc60", width: 1.6 },
    yaxis: "y3", name: "PV measured", connectgaps: false,
  });
  // Solcast estimated-actuals (30-min) — only renders cells where the
  // backfill job has populated actual_kw. If all-null, this trace is
  // empty and silently absent. Distinct dot marker so it's separable
  // from the inverter-side measured line.
  if (pvActualPast.some((v) => v != null)) {
    traces.push({
      x: pvActualPastTs, y: pvActualPast,
      type: "scatter", mode: "markers",
      marker: { color: "#f2cc60", size: 4, symbol: "circle-open" },
      yaxis: "y3", name: "PV actual (Solcast)",
    });
  }

  // SOC (yaxis y4).
  traces.push({
    x: pastTs, y: socPast, type: "scatter", mode: "lines",
    line: { color: "#79c0ff", width: 1.8 },
    yaxis: "y4", name: "SOC measured", connectgaps: false,
  });
  traces.push({
    x: slotTs, y: socFut, type: "scatter", mode: "lines",
    line: { color: "#79c0ff", width: 1.6, dash: "dot" },
    yaxis: "y4", name: "SOC planned",
  });

  // House load (yaxis y7). Measured = telemetry house_load_kw (total
  // metered consumption, includes managed loads). Planned = reconstructed
  // from slot-decision energy balance — see loadFut above.
  traces.push({
    x: pastTs, y: loadPast,
    type: "scatter", mode: "lines",
    line: { color: "#ff9e64", width: 1.6 },
    yaxis: "y7", name: "load measured", connectgaps: false,
  });
  traces.push({
    x: slotTs, y: loadFut,
    type: "scatter", mode: "lines",
    line: { color: "#ff9e64", width: 1.4, dash: "dot" },
    yaxis: "y7", name: "load planned",
  });

  // Managed loads (yaxis y8). One measured trace per load_id from
  // load_telemetry, plus a planned trace per load_id from the LP's
  // forward_trajectory[*].load_kw[loadId]. Filters out OBSERVABLE-category
  // entries (e.g. the grid CT) which are measurement-only and belong on
  // the GRID panel, not here.
  const loadRows = state.history.loadTelemetry || [];
  const byLoad = new Map();
  const observableIds = new Set();
  for (const r of loadRows) {
    if ((r.category || "").toLowerCase() === "observable") {
      observableIds.add(r.load_id);
      continue;
    }
    if (!byLoad.has(r.load_id)) byLoad.set(r.load_id, []);
    byLoad.get(r.load_id).push(r);
  }
  for (const [loadId, rows] of byLoad) {
    rows.sort((a, b) => (a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : 0));
    traces.push({
      x: rows.map((r) => toPlotlyTime(r.ts)),
      y: rows.map((r) => r.power_kw),
      type: "scatter", mode: "lines",
      line: { color: colorForLoadId(loadId), width: 1.4, shape: "hv" },
      yaxis: "y8", name: `${loadId} measured`, connectgaps: false,
    });
  }
  // Planned: union of load_ids the LP wrote across all forward slots. The
  // shape is hv (step) so a sequence of zeros / draw_kw renders as a
  // visible "ON during these slots" rectangle aligned with the LP's
  // commitment. Skip OBSERVABLE ids (the LP doesn't drive them).
  const plannedLoadIds = new Set();
  for (const s of fwd) {
    if (!s.load_kw) continue;
    for (const k of Object.keys(s.load_kw)) {
      if (observableIds.has(k)) continue;
      plannedLoadIds.add(k);
    }
  }
  for (const loadId of plannedLoadIds) {
    traces.push({
      x: slotTs,
      y: fwd.map((s) => (s.load_kw && s.load_kw[loadId] != null ? s.load_kw[loadId] : null)),
      type: "scatter", mode: "lines",
      line: { color: colorForLoadId(loadId), width: 1.2, dash: "dot", shape: "hv" },
      yaxis: "y8", name: `${loadId} planned`, connectgaps: false,
    });
  }

  // Grid (yaxis y5) — import positive, export negative.
  // Two measured sources: the inverter's house-meter register (Modbus
  // 30004; sensor on the same bus that runs the LP) and the Shelly Pro
  // EM ch1 CT clamp (independent path; cross-check). Same sign convention.
  traces.push({
    x: pastTs, y: hist.map((r) => r.grid_kw),
    type: "scatter", mode: "lines",
    line: { color: "#c9d1d9", width: 1.4 },
    yaxis: "y5", name: "grid measured (inverter)", connectgaps: false,
  });
  if (hist.some((r) => r.grid_kw_shelly != null)) {
    traces.push({
      x: pastTs, y: hist.map((r) => r.grid_kw_shelly),
      type: "scatter", mode: "lines",
      line: { color: "#7ee787", width: 1.0 },
      yaxis: "y5", name: "grid measured (Shelly)", connectgaps: false,
    });
  }
  traces.push({
    x: slotTs, y: gridImpFut.map((v, i) => v != null ? v + gridExpFut[i] : null),
    type: "scatter", mode: "lines",
    line: { color: "#c9d1d9", width: 1.4, dash: "dot" },
    yaxis: "y5", name: "grid planned (net)", connectgaps: false,
  });

  // Cost (yaxis y6).
  traces.push({
    x: pastTs, y: costPast,
    type: "scatter", mode: "lines",
    line: { color: "#bc8cff", width: 1.4 },
    yaxis: "y6", name: "cost realised c/h", connectgaps: false,
  });
  traces.push({
    x: slotTs, y: costFut,
    type: "scatter", mode: "lines",
    line: { color: "#bc8cff", width: 1.4, dash: "dot" },
    yaxis: "y6", name: "cost planned c/h", connectgaps: false,
  });
  // Settled per-5-min cost from amber_usage. Overlaid as a step line so
  // the operator can see the bill-level reality alongside the in-tick
  // marginal estimate. Only covers fully-settled NEM days, so the trace
  // tail ends ~yesterday-NEM-midnight (= 14:00Z yesterday). Convert
  // c/5-min → c/h by ×12 to share the y-axis.
  const settledCost = aggregateAmberUsageCostsPerSlot(state.history.amberUsage);
  if (settledCost.x.length > 0) {
    traces.push({
      x: settledCost.x, y: settledCost.y,
      type: "scatter", mode: "lines",
      line: { color: "#ffd700", width: 1.2, shape: "hv" },
      yaxis: "y6", name: "cost settled c/h", connectgaps: false,
    });
  }

  return traces;
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

// Convert a /price_forecast_log row (past) to PriceInterval-shape so the
// trace builders treat past + future uniformly. Field renames:
//   per_kwh         → import_per_kwh
//   interval_start  → start
//   interval_end    → end
// Build closed polygons for `fill: "toself"` band traces. One polygon
// per contiguous run of intervals where both bounds are non-null,
// returned as an array — caller renders one trace per polygon. Each
// polygon walks forward along LOW then back along HIGH so the closing
// edge is implicit. Returning separate polygons rather than a single
// (null, null)-separated trace because Plotly's `toself` closes the
// path across the separator instead of treating each segment as a
// distinct shape, which produced visible self-crossing artifacts at
// run boundaries.
function bandPolygons(intervals, lowKey, highKey) {
  const polys = [];
  let runX = [], runLo = [], runHi = [];
  const flushRun = () => {
    if (runX.length < 2) {
      // A single-point polygon is a degenerate vertical line — skip.
      runX = []; runLo = []; runHi = [];
      return;
    }
    const x = [], y = [];
    for (let i = 0; i < runX.length; i++) { x.push(runX[i]); y.push(runLo[i]); }
    for (let i = runX.length - 1; i >= 0; i--) { x.push(runX[i]); y.push(runHi[i]); }
    polys.push({ x, y });
    runX = []; runLo = []; runHi = [];
  };
  for (const p of intervals) {
    const L = p[lowKey], H = p[highKey];
    if (L != null && H != null) {
      runX.push(toPlotlyTime(p.start));
      runLo.push(L);
      runHi.push(H);
    } else {
      flushRun();
    }
  }
  flushRun();
  return polys;
}

function priceLogToInterval(r) {
  return {
    start: r.interval_start, end: r.interval_end,
    import_per_kwh: r.per_kwh, export_per_kwh: r.export_per_kwh,
    forecast_predicted: r.forecast_predicted,
    forecast_low: r.forecast_low, forecast_high: r.forecast_high,
    export_forecast_predicted: r.export_forecast_predicted,
    export_forecast_low: r.export_forecast_low,
    export_forecast_high: r.export_forecast_high,
  };
}

function mergePriceForecasts(pastRows, futureIntervals) {
  // Past rows from /price_forecast_log → PriceInterval-shape, with the
  // future side appended. Drop past entries that overlap the future
  // (snapshot's current+future supersedes the forecast log for those).
  //
  // The snapshot's `price_forecast` is the LP's `prices_planning`
  // list — 5-min intervals first (covering ~current + 30 min) then
  // 30-min intervals interleaved for the rest of the horizon. The
  // first 30-min entry (e.g. 23:00) is emitted *after* the last 5-min
  // entry (e.g. 23:55), which makes the array non-monotonic in
  // `start`. The LP doesn't mind — its `_price_at` linear scan picks
  // the first match, so 5-min wins where both are present. The
  // dashboard's polygon builder DOES mind: a non-monotonic forward
  // path crosses itself and renders the band as a self-intersecting
  // shape. Sort by start and dedupe-by-start (stable sort preserves
  // 5-min first when both share a start).
  const futureStartMs = futureIntervals.length
    ? Math.min(...futureIntervals.map((p) => +new Date(p.start)))
    : Infinity;
  const past = pastRows
    .map(priceLogToInterval)
    .filter((p) => +new Date(p.start) < futureStartMs);
  const merged = [...past, ...futureIntervals]
    .slice()
    .sort((a, b) => +new Date(a.start) - +new Date(b.start));
  const seen = new Set();
  const dedup = [];
  for (const p of merged) {
    if (seen.has(p.start)) continue;
    seen.add(p.start);
    dedup.push(p);
  }
  return dedup;
}

function mergePVForecasts(pastRows, futureIntervals) {
  // Past rows have only period_end; synthesise start = period_end - 30 min
  // (Solcast 30-min cadence). Drop past entries that overlap the future
  // window so the line doesn't double-back.
  const futureStartMs = futureIntervals.length
    ? +new Date(futureIntervals[0].start) : Infinity;
  const past = pastRows
    .map((r) => ({
      start: new Date(+new Date(r.period_end) - 30 * 60_000).toISOString(),
      end: r.period_end,
      pv_estimate_kw: r.pv_estimate_kw,
      pv_estimate10_kw: r.pv_estimate10_kw,
      pv_estimate90_kw: r.pv_estimate90_kw,
    }))
    .filter((p) => +new Date(p.start) < futureStartMs);
  return [...past, ...futureIntervals];
}

function pickPriceAt(priceList, t, side) {
  // Linear scan — fine at this size (≲ 500 entries). Returns the price
  // whose [start,end) interval contains t. Null if none.
  const ts = +new Date(t);
  for (const p of priceList) {
    const s = +new Date(p.start), e = +new Date(p.end);
    if (s <= ts && ts < e) {
      if (side === "import") return coalesce(p.forecast_predicted, p.import_per_kwh);
      return coalesce(p.export_forecast_predicted, p.export_per_kwh);
    }
  }
  return null;
}

function coalesce(...vals) {
  for (const v of vals) if (v != null) return v;
  return null;
}

function marginalCost(ip, ep, grid) {
  if (ip == null || ep == null || grid == null) return null;
  // grid: + import, − export. Cost = ip * import_kw − ep * export_kw.
  const imp = Math.max(0,  grid);
  const exp = Math.max(0, -grid);
  return ip * imp - ep * exp;
}

function buildLayout() {
  const domains = panelDomains();
  const xRange = computeXRange();
  const cursorT = effectiveCursor();

  const shapes = [];
  // Buy/sell shading on the prices subplot. Coordinates pass through
  // toPlotlyTime so the rectangle sits at the right local-time x.
  const fwd = state.snapshot?.lp_solution?.forward_trajectory || [];
  for (const s of fwd) {
    const t0 = toPlotlyTime(s.slot_start);
    const t1 = toPlotlyTime(new Date(+new Date(s.slot_start) + SLOT_MS));
    if ((s.grid_to_battery_kw ?? 0) > DEADBAND_KW) {
      shapes.push({
        type: "rect", xref: "x", yref: "y domain",
        x0: t0, x1: t1, y0: 0, y1: 1,
        fillcolor: "rgba(210,153,34,0.10)",
        line: { width: 0 }, layer: "below",
      });
    }
    if ((s.grid_export_kw ?? 0) > DEADBAND_KW) {
      shapes.push({
        type: "rect", xref: "x", yref: "y domain",
        x0: t0, x1: t1, y0: 0, y1: 1,
        fillcolor: "rgba(63,185,80,0.10)",
        line: { width: 0 }, layer: "below",
      });
    }
  }

  // SOC floor line (yaxis y4).
  const floor = state.config?.battery?.soc_floor_pct;
  if (floor != null && Number.isFinite(floor)) {
    shapes.push({
      type: "line", xref: "paper", yref: "y4",
      x0: 0, x1: 1, y0: floor, y1: floor,
      line: { color: "#f85149", width: 1, dash: "dash" },
    });
  }
  // Grid zero-line for the grid panel (helps read +import/−export).
  shapes.push({
    type: "line", xref: "paper", yref: "y5",
    x0: 0, x1: 1, y0: 0, y1: 0,
    line: { color: "#444c56", width: 0.6 },
  });
  // Cost zero-line.
  shapes.push({
    type: "line", xref: "paper", yref: "y6",
    x0: 0, x1: 1, y0: 0, y1: 0,
    line: { color: "#444c56", width: 0.6 },
  });

  // "Now" marker — vertical line at the snapshot timestamp (local time).
  // Hidden in historical mode: "now" lives outside (or anachronistically
  // inside) the fixed range, and the green dotted line is associated
  // with the live planning horizon, not with arbitrary timestamps.
  const nowT = nowFromSnapshot();
  if (nowT && !isHistorical()) {
    const nowLocal = toPlotlyTime(nowT);
    shapes.push({
      type: "line", xref: "x", yref: "paper",
      x0: nowLocal, x1: nowLocal, y0: 0, y1: 1,
      line: { color: "#56d364", width: 1, dash: "dot" },
      layer: "below",
    });
  }
  // Cursor — single vline spanning all subplots.
  if (cursorT) {
    const cursorLocal = toPlotlyTime(cursorT);
    shapes.push({
      type: "line", xref: "x", yref: "paper",
      x0: cursorLocal, x1: cursorLocal, y0: 0, y1: 1,
      line: { color: "#58a6ff", width: 1.4 },
      name: "cursor",
    });
  }

  // Per-panel labels rendered horizontally at the top-left of each
  // domain (above the y-axis tick numbers). Replaces the rotated
  // y-axis titles that were small and hard to scan.
  const annotations = [];
  for (const p of PANEL_LAYOUT) {
    const label = p.label;
    if (!label) continue;
    const text = p.units
      ? `<b>${label}</b>  <span style="color:#7d8590">${p.units}</span>`
      : `<b>${label}</b>`;
    annotations.push({
      xref: "paper",
      yref: `${p.axis} domain`,
      x: 0,
      y: 1,
      xanchor: "left",
      yanchor: "bottom",
      yshift: 2,
      text,
      showarrow: false,
      font: { family: FONT_FAMILY, size: 11, color: "#c9d1d9" },
      align: "left",
    });
  }

  const narrow = isNarrowViewport();
  return {
    margin: narrow
      ? { l: 28, r: 6,  t: 14, b: 36 }
      : { l: 44, r: 20, t: 18, b: 44 },
    paper_bgcolor: "#161b22",
    plot_bgcolor: "#0e1116",
    font: { color: "#e8edf2", family: FONT_FAMILY, size: 12 },
    showlegend: false,
    hovermode: "x unified",
    hoverlabel: HOVER_LABEL,
    // Desktop: drag pans. Mobile: disable drag entirely so vertical
    // touch-scroll over the figure scrolls the page instead of moving
    // the x-axis around. Shared helper — see chart-utils.js.
    ...window.eoChart.mobileLayoutFragment({ desktopDrag: "pan" }),
    shapes,
    annotations,
    xaxis: {
      type: "date",
      gridcolor: "#21262d",
      zeroline: false,
      range: xRange,
      domain: [0, 1],
      anchor: "y6",
      tickfont: { size: 12, color: "#c9d1d9" },
      tickcolor: "#444c56",
      ticklen: 4,
      automargin: true,
    },
    yaxis:  axis(domains.prices),
    yaxis2: axis(domains.ribbon, { showticklabels: false, showgrid: false, fixedrange: true, range: [0, 1] }),
    yaxis3: axis(domains.solar),
    yaxis4: axis(domains.soc, { range: [0, 100] }),
    yaxis5: axis(domains.grid),
    yaxis6: axis(domains.cost),
    yaxis7: axis(domains.load),
    yaxis8: axis(domains.managed),
  };
}

function axis(domain, extra = {}) {
  return Object.assign({
    domain,
    gridcolor: "#21262d",
    zeroline: false,
    tickfont: { size: 12, color: "#c9d1d9" },
    tickcolor: "#444c56",
    ticklen: 3,
    automargin: true,
  }, extra);
}

function computeXRange() {
  if (state.range) {
    return [toPlotlyTime(state.range.from), toPlotlyTime(state.range.to)];
  }
  const now = nowFromSnapshot();
  if (!now) return undefined;
  const lo = new Date(+now - HISTORY_LOOKBACK_MS);
  const hi = new Date(+now + FUTURE_HORIZON_MS);
  return [toPlotlyTime(lo), toPlotlyTime(hi)];
}

async function redrawTSFigure() {
  const div = document.getElementById("ts-figure");
  const traces = buildTraces();
  const layout = buildLayout();
  if (!state.built.ts) {
    await Plotly.newPlot(div, traces, layout, window.eoChart.mobileConfig());
    state.built.ts = true;
    window.eoChart.registerPlot("ts-figure");
    div.on("plotly_hover", onPlotlyHover);
    div.on("plotly_click", onPlotlyClick);
  } else {
    await Plotly.react(div, traces, layout);
  }
}

function redrawCursorLine() {
  if (!state.built.ts) return;
  const div = document.getElementById("ts-figure");
  const layout = buildLayout();
  Plotly.relayout(div, { shapes: layout.shapes });
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

function flowsAtCursor() {
  // Returns { flows, label, source } where source ∈ {planned, measured, telemetry}.
  const t = effectiveCursor();
  if (!t) return null;
  const tMs = +t;
  // Historical mode: always read from telemetry — there's no "future" or
  // "now" to fall through to.
  if (isHistorical()) {
    const row = state.history.rows.find((r) => {
      const rs = +new Date(r.ts);
      return rs <= tMs && tMs < rs + SLOT_MS;
    });
    if (!row) return null;
    const flows = disambiguateFlows({
      pv: row.pv_kw, batt: row.battery_kw,
      grid: row.grid_kw, load: row.house_load_kw,
    });
    if (!flows) return null;
    return { flows, label: `measured · ${fmtTime(row.ts)}`, source: "telemetry" };
  }
  const now = nowFromSnapshot();
  if (!now) return null;
  const nowMs = +now;

  // Future ⇒ planned slot from forward_trajectory.
  if (tMs > nowMs) {
    const fwd = state.snapshot?.lp_solution?.forward_trajectory || [];
    const slot = fwd.find((s) => {
      const ss = +new Date(s.slot_start);
      return ss <= tMs && tMs < ss + SLOT_MS;
    });
    if (!slot) return null;
    return { flows: flowsFromSlot(slot), label: `planned · ${fmtTime(slot.slot_start)}`, source: "planned" };
  }

  // "Now" — within the most recent slot relative to snapshot. Try
  // measured (post-dispatch state) first; fall back to slot_0 plan if
  // any of the four signals is null (grid sensor offline, etc.) so the
  // operator still sees a useful flow diagram.
  if (Math.abs(tMs - nowMs) < SLOT_MS) {
    const ss = state.snapshot?.system_state_post_dispatch || state.snapshot?.system_state;
    if (ss) {
      const flows = disambiguateFlows({
        pv: ss.pv_power_kw, batt: ss.battery_power_kw,
        grid: ss.grid_power_kw, load: ss.house_load_kw,
      });
      if (flows) {
        return { flows, label: `measured · ${fmtTime(state.snapshot.timestamp)}`, source: "measured" };
      }
    }
    // Fall back to the LP's slot-0 plan — labelled clearly so the user
    // sees this is the *plan*, not measurement.
    const slot0 = state.snapshot?.lp_solution?.slot_0;
    if (slot0) {
      return { flows: flowsFromSlot(slot0), label: `slot-0 plan · ${fmtTime(slot0.slot_start)}`, source: "planned" };
    }
    return null;
  }

  // Past ⇒ telemetry row at that slot.
  const row = state.history.rows.find((r) => {
    const rs = +new Date(r.ts);
    return rs <= tMs && tMs < rs + SLOT_MS;
  });
  if (!row) return null;
  const flows = disambiguateFlows({
    pv: row.pv_kw, batt: row.battery_kw,
    grid: row.grid_kw, load: row.house_load_kw,
  });
  if (!flows) return null;
  return { flows, label: `measured · ${fmtTime(row.ts)}`, source: "telemetry" };
}

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

async function redrawSankey() {
  const div = document.getElementById("sankey-figure");
  const subtitle = document.getElementById("sankey-subtitle");
  const result = flowsAtCursor();
  if (!result) {
    subtitle.textContent = "no flow data at cursor";
    // Wipe the prior Sankey so a stale frame doesn't sit there.
    if (state.built.sankey) Plotly.purge(div);
    state.built.sankey = false;
    return;
  }
  subtitle.textContent = result.label;
  const trace = buildSankeyTrace(result.flows);
  // Plotly.react sometimes leaves Sankey in a half-rendered state when
  // the node count is constant but link counts change. The cheap-but-
  // reliable fix is purge + newPlot each redraw. Sankey is small —
  // 6 nodes, ≤7 links — so the redraw cost is trivial.
  Plotly.purge(div);
  await Plotly.newPlot(div, [trace], sankeyLayout(), {
    responsive: true, displaylogo: false,
  });
  state.built.sankey = true;
  window.eoChart.registerPlot("sankey-figure");
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
      ? { l: 32, r: 4,  t: 14, b: 32 }
      : { l: 50, r: 16, t: 18, b: 40 },
    paper_bgcolor: "#161b22",
    plot_bgcolor: "#0e1116",
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

// ── Polling + auto-refresh ─────────────────────────────────────────

async function pollOnce() {
  // Fetch /readyz first — it's public and tells us the actual service
  // state. Failure here just means we'll keep the prior value.
  try {
    state.ready = await fetchReady();
  } catch (err) {
    console.warn("readyz fetch failed", err);
  }

  try {
    const snap = await fetchSnapshot();
    state.snapshot = snap;
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
    // In historical mode the time-series figure is driven by static
    // history; redrawing it on every snapshot poll just wastes work and
    // can fight a hovered cursor. Status strip / loads / events still
    // refresh because they reflect live operational state.
    if (!isHistorical()) {
      await redrawTSFigure();
      await redrawSankey();
      await redrawDailySankey();
      await redrawDailySpend();
    }
  } catch (err) {
    if (err.status === 503) {
      showError("Service hasn't completed a tick yet (HTTP 503). Will retry.");
    } else {
      showError(`fetch failed: ${err.message}`);
      console.warn(err);
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

  if (priceLog.status === "fulfilled") {
    state.history.priceForecast = bucketLatestPriceForecast(priceLog.value);
  } else console.warn("price_forecast_log fetch failed", priceLog.reason);

  if (pvLog.status === "fulfilled") {
    state.history.pvForecast = bucketLatestPVForecast(pvLog.value);
  } else console.warn("pv_forecast_log fetch failed", pvLog.reason);

  if (amberUsage.status === "fulfilled") {
    state.history.amberUsage = amberUsage.value;
  } else console.warn("amber_usage fetch failed", amberUsage.reason);

  if (dailySpend.status === "fulfilled") {
    state.history.dailySpend = dailySpend.value;
  } else console.warn("daily_spend fetch failed", dailySpend.reason);

  if (loadTel.status === "fulfilled") state.history.loadTelemetry = loadTel.value;
  else console.warn("load_telemetry fetch failed", loadTel.reason);
}

// For each (interval_start, resolution), keep the latest fetched_at row
// that still has band data populated. Bands are only populated on
// ForecastInterval rows (not ActualInterval / CurrentInterval), so we
// pick the most recent forecast made *while the interval was still in
// the future* — that's what the LP planned against. 5-min beats 30-min
// when both exist (5-min covers ~current+30min on Amber's 5-min API).
function bucketLatestPriceForecast(rows) {
  const buckets = new Map();   // key: "interval_start|resolution" → row
  for (const r of rows) {
    if (r.forecast_predicted == null && r.forecast_low == null) continue;
    const key = `${r.interval_start}|${r.resolution}`;
    const prev = buckets.get(key);
    if (!prev || new Date(r.fetched_at) > new Date(prev.fetched_at)) {
      buckets.set(key, r);
    }
  }
  // Now pick the best resolution per interval_start: 5-min beats 30-min.
  const byStart = new Map();
  for (const r of buckets.values()) {
    const prev = byStart.get(r.interval_start);
    if (!prev || r.resolution < prev.resolution) byStart.set(r.interval_start, r);
  }
  return [...byStart.values()].sort((a, b) =>
    new Date(a.interval_start) - new Date(b.interval_start)
  );
}

function bucketLatestPVForecast(rows) {
  const buckets = new Map();
  for (const r of rows) {
    const key = r.period_end;
    const prev = buckets.get(key);
    if (!prev || new Date(r.fetched_at) > new Date(prev.fetched_at)) {
      buckets.set(key, r);
    }
  }
  return [...buckets.values()].sort((a, b) =>
    new Date(a.period_end) - new Date(b.period_end)
  );
}

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
  if (state.range) {
    ind.textContent = `historical · ${fmtRangeShort(state.range)}`;
    ind.className = "mode-indicator historical";
  } else {
    ind.textContent = "live · last 24h + 48h forecast";
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
  await redrawSankey();
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
    if (state.built.sankey) redrawSankey();
    if (state.built.sankeyToday) redrawDailySankey();
  });

  if (!ensureToken()) return;

  await loadConfig();
  await loadHistory();
  await pollOnce();

  setInterval(() => {
    pollOnce();
    // Re-pull history every ~5 min so the past edge keeps moving forward.
    // Only meaningful in live mode — historical windows are fixed.
    if (!isHistorical() && Math.random() < 0.05) loadHistory();
  }, POLL_INTERVAL_MS);
}

document.addEventListener("DOMContentLoaded", main);
