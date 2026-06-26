/**
 * ops-charts.js — uPlot replacements for the 4 Ops-tab Plotly charts.
 *
 * Exports buildOpsCharts() → { updateSolve(solveData), updateModbus(modbusData), resize() }.
 *
 * Charts:
 *   ops-solve-series   — scatter points per-status on a union-x timeline; hand-built tooltip
 *   ops-solve-histogram — single-series bars over pre-binned buckets
 *   ops-solve-status   — per-bar status-coloured bars (or "no solves" text)
 *   ops-modbus-writes  — grouped ok/err bars per register (or "no writes" text)
 *
 * Construct lazily: buildOpsCharts() is called AFTER the ops tab's hidden=false
 * flip so instances are never built at width=0.
 */
import uPlot from "./uplot.esm.js";
import { buildUnionX, alignSeries } from "./timeline.js";
import { registry, isNarrow } from "./chart-core.js";

// ── Colour constants (match ops.js / dashboard theme) ─────────────────────

const STATUS_COLOR = {
  optimal:    "#3fb950",
  feasible:   "#58a6ff",
  infeasible: "#f85149",
  unknown:    "#8b949e",
};

const COLOR_OK  = "#3fb950";  // green
const COLOR_ERR = "#f85149";  // red
const COLOR_HIST = "#58a6ff"; // blue

const GRID_STROKE = "#21262d";
const AXIS_STROKE = "#c9d1d9";
const TICK_STROKE = "#21262d";

// ── Shared layout helpers ──────────────────────────────────────────────────

function axisFont() {
  const body =
    typeof document !== "undefined" && document.body
      ? getComputedStyle(document.body).fontFamily
      : "sans-serif";
  return "12px " + body;
}

// Left-gutter convention matched to the Energy panels (panels.js): the y-axis
// SIZE is the entire left gutter and padding-left is 0. The earlier ops charts
// doubled up (padding.left 48 + y-axis size 48 = ~96px of wasted left space);
// this is the "excess left padding" the data ran far in from the panel edge.
// Narrow stays at 48 (not tighter): a numeric uPlot axis.size is a FIXED
// reservation and the solve-series y-axis shows ms — up to 5 digits ("20000" at
// the ~20s solver timeout). 48px clears that; 40 clipped the leading digit on
// mobile. Removing the OLD doubled gutter (padding-left 36 + size 48 = 84px) is
// still the win — mobile drops 84 → 48.
const GUTTER_WIDE = 52, GUTTER_NARROW = 48;
const opsGutter = () => (isNarrow() ? GUTTER_NARROW : GUTTER_WIDE);
// [top, right, bottom] — left is always 0 (the gutter is the y-axis size). Top
// clears the absolutely-positioned overlay legend (top:4 + ~15px tall) so it
// doesn't graze the data; bottom carries the x-axis category/time labels.
function opsMargin() {
  return isNarrow()
    ? { top: 14, right: 8,  bottom: 30 }
    : { top: 16, right: 16, bottom: 34 };
}
// Shared y-axis spec (the left gutter). Used by every ops chart so the gutter
// width and font are defined once.
function opsYAxis() {
  return {
    scale: "y",
    size:  opsGutter(),
    stroke: AXIS_STROKE,
    grid:  { stroke: GRID_STROKE },
    ticks: { stroke: TICK_STROKE },
    font:  axisFont(),
  };
}

// Reusable hover-value tooltip for an ops chart. `formatAt(u, idx)` returns the
// inner HTML for the hovered x-index, or null/"" to hide. Creates one absolutely-
// positioned `.ops-tooltip` inside `el` and follows the cursor, flipping left of
// it near the right edge. Mirrors the original solve-series tooltip so all four
// ops charts now surface their values on hover.
function tooltipPlugin(el, formatAt) {
  let tip = null;
  return {
    hooks: {
      init: [() => {
        el.style.position = "relative";
        tip = document.createElement("div");
        tip.className = "ops-tooltip";
        el.appendChild(tip);
      }],
      setCursor: [(u) => {
        if (!tip) return;
        const idx = u.cursor.idx;
        const html = idx == null ? null : formatAt(u, idx);
        if (!html) { tip.style.display = "none"; return; }
        tip.innerHTML = html;
        tip.style.display = "block";
        // u.cursor.left is relative to the plot area (.u-over); the tooltip is
        // positioned within `el`, whose left edge sits a y-axis gutter to the
        // left of the plot. Add that offset so the tooltip tracks the actual
        // cursor instead of landing gutter-px to its left.
        const overLeft = u.over.getBoundingClientRect().left - el.getBoundingClientRect().left;
        const base = u.cursor.left + overLeft;
        const elW = el.clientWidth;
        const ttW = tip.offsetWidth || 120;
        const ttLeft = base + 12 + ttW > elW ? base - ttW - 8 : base + 12;
        tip.style.left = Math.max(0, ttLeft) + "px";
        tip.style.top  = "8px";
      }],
    },
  };
}

/** Build standard uPlot opts common to all ops bar charts. */
function baseBarOpts(containerId, width, height, extra) {
  const margin = opsMargin();
  return Object.assign(
    {
      id: containerId,
      width,
      height,
      // left:0 — the gutter IS the y-axis size (Energy north star).
      padding: [margin.top, margin.right, margin.bottom, 0],
      scales: {
        x: { time: false, range: (u, min, max) => [min - 0.5, max + 0.5] },
        y: { range: (u, min, max) => [0, max <= 0 ? 1 : max * 1.1] },
      },
      axes: [
        // x axis — category labels
        {
          scale: "x",
          stroke: AXIS_STROKE,
          grid:  { stroke: GRID_STROKE },
          ticks: { stroke: TICK_STROKE },
          font:  axisFont(),
        },
        opsYAxis(),
      ],
      legend: { show: false },
      // Hover enabled (per-chart value tooltip); vertical crosshair only, no drag
      // (small charts, mobile-friendly).
      cursor: { show: true, y: false, drag: { x: false, y: false, setScale: false } },
    },
    extra || {},
  );
}

// ── Solve series (the hard one) ────────────────────────────────────────────

/**
 * Build or rebuild the ops-solve-series uPlot.
 *
 * Data shape: solveData.series = [{ ts: ISO string, ms: number, status: string }, ...]
 *
 * Strategy:
 *   1. Group points by status.
 *   2. Convert each point's ts to epoch SECONDS.
 *   3. Build a union-x across all statuses.
 *   4. Align each status onto that union-x (null-padded).
 *   5. One points-only series per status (sorted alphabetically for stable ordering).
 *   6. Hand-built unified tooltip via setCursor hook.
 *   7. y-scale starts at 0 (rangemode: tozero equivalent).
 */
// On-chart overlay legend (top-right) with click-to-toggle. Replaces uPlot's
// built-in legend, which renders below the canvas and overflowed the fixed-
// height ops containers. `items` = [[seriesIdx, label, color], ...].
function addOverlayLegend(el, u, items) {
  const old = el.querySelector(".chart-legend");
  if (old) old.remove();
  if (!items || !items.length) return;
  const legend = document.createElement("div");
  legend.className = "chart-legend";
  for (const [idx, label, color] of items) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "chart-legend-item";
    item.innerHTML = `<span class="sw" style="background:${color}"></span>${label}`;
    item.addEventListener("click", () => {
      const show = !u.series[idx].show;
      u.setSeries(idx, { show });
      item.classList.toggle("off", !show);
    });
    legend.appendChild(item);
  }
  el.appendChild(legend);
}

function buildSolveSeries(containerId, solveData) {
  const el = typeof document !== "undefined" && document.getElementById(containerId);
  if (!el) return null;

  const rows = (solveData && Array.isArray(solveData.series)) ? solveData.series : [];

  // Empty-state (e.g. no solves in the window, or the snapshot file couldn't be
  // parsed) — show a message instead of an empty plot with a misleading axis.
  if (rows.length === 0) {
    el.innerHTML = '<div class="muted">no solves in window</div>';
    registry.unregister(containerId);
    return null;
  }

  // Group points by status
  const byStatus = {};
  for (const p of rows) {
    const s = p.status || "unknown";
    if (!byStatus[s]) byStatus[s] = [];
    byStatus[s].push(p);
  }

  // Stable ordering: known statuses first, then alphabetical unknowns
  const knownOrder = ["optimal", "feasible", "infeasible", "unknown"];
  const statusKeys = [
    ...knownOrder.filter(k => byStatus[k]),
    ...Object.keys(byStatus).filter(k => !knownOrder.includes(k)).sort(),
  ];

  // Convert ts → epoch seconds
  function toSec(isoStr) {
    const ms = Date.parse(isoStr);
    return Number.isFinite(ms) ? ms / 1000 : null;
  }

  // Build union x from all statuses
  const allTsArrays = statusKeys.map(s => byStatus[s].map(p => toSec(p.ts)).filter(v => v != null));
  const unionX = buildUnionX(...allTsArrays);

  // Align each status onto unionX
  const alignedSeries = statusKeys.map(s =>
    alignSeries(
      unionX,
      byStatus[s],
      p => toSec(p.ts),
      p => (typeof p.ms === "number" ? p.ms : null),
    )
  );

  // uPlot data: [xArray, ...seriesArrays]
  const data = [unionX, ...alignedSeries];

  const width  = el.clientWidth  > 0 ? el.clientWidth  : 500;
  const height = el.clientHeight > 0 ? el.clientHeight : 220;
  const margin = opsMargin();

  // Tooltip content for the hovered x-index: one line per status with its ms.
  function solveFormatAt(u, idx) {
    const lines = [];
    for (let si = 0; si < statusKeys.length; si++) {
      const val = u.data[si + 1][idx];
      if (val == null) continue;
      const s = statusKeys[si];
      const color = STATUS_COLOR[s] || "#8b949e";
      lines.push(`<span style="color:${color}">&#9679;</span> ${s}: <b>${val.toFixed(0)} ms</b>`);
    }
    if (lines.length === 0) return null;
    const tsSec = u.data[0][idx];
    const tsLabel = tsSec != null ? new Date(tsSec * 1000).toLocaleTimeString() : "";
    return (tsLabel ? `<div class="tt-title">${tsLabel}</div>` : "") + lines.join("<br>");
  }

  const opts = {
    id: containerId,
    width,
    height,
    padding: [margin.top, margin.right, margin.bottom, 0],
    scales: {
      x: { time: true },
      y: {
        // y starts at 0 (tozero)
        range: (u, dmin, dmax) => [0, dmax <= 0 ? 100 : dmax * 1.1],
      },
    },
    axes: [
      // x: time axis
      {
        scale: "x",
        stroke: AXIS_STROKE,
        grid:  { stroke: GRID_STROKE },
        ticks: { stroke: TICK_STROKE },
        font:  axisFont(),
      },
      opsYAxis(),  // y: ms — shared left gutter
    ],
    // One series per status — points only (no line)
    series: [
      {}, // x placeholder
      ...statusKeys.map(s => ({
        label:  s,
        scale:  "y",
        stroke: STATUS_COLOR[s] || "#8b949e",
        width:  0,          // no line
        points: { show: true, size: 5, fill: STATUS_COLOR[s] || "#8b949e" },
        paths:  () => null, // suppress line path entirely
      })),
    ],
    legend: { show: false },   // custom overlay legend added after construction
    cursor: {
      show: true,
      y: false,   // vertical crosshair only
      // no drag (ops charts are small; mobile compat)
      drag: { x: false, y: false, setScale: false },
    },
    plugins: [tooltipPlugin(el, solveFormatAt)],
  };

  el.innerHTML = "";
  el.style.position = "relative";

  const u = new uPlot(opts, data, el);
  // v1 a11y hygiene (spec §10.2).
  const cvs0 = u.ctx && u.ctx.canvas;
  if (cvs0) { cvs0.setAttribute("role", "img"); cvs0.setAttribute("aria-label", "Solve time series, ms"); }
  registry.register(containerId, u);
  addOverlayLegend(el, u, statusKeys.map((s, i) => [i + 1, s, STATUS_COLOR[s] || "#8b949e"]));
  return u;
}

// ── Histogram ──────────────────────────────────────────────────────────────

/**
 * Build or rebuild ops-solve-histogram.
 * solveData.histogram = [{ bucket: string, count: number }, ...]
 */
function buildHistogram(containerId, solveData) {
  const el = typeof document !== "undefined" && document.getElementById(containerId);
  if (!el) return null;

  const buckets = (solveData && Array.isArray(solveData.histogram)) ? solveData.histogram : [];
  const totalCount = buckets.reduce((s, b) => s + (b.count || 0), 0);

  if (buckets.length === 0 || totalCount === 0) {
    el.innerHTML = '<div class="muted">no solves in window</div>';
    registry.unregister(containerId);
    return null;
  }

  const labels = buckets.map(b => b.bucket);
  const counts = buckets.map(b => b.count);
  const xData  = labels.map((_, i) => i); // integer indices

  const width  = el.clientWidth  > 0 ? el.clientWidth  : 400;
  const height = el.clientHeight > 0 ? el.clientHeight : 180;

  const opts = baseBarOpts(containerId, width, height, {
    scales: {
      x: {
        time: false,
        range: (u, min, max) => [min - 0.5, max + 0.5],
      },
      y: {
        range: (u, dmin, dmax) => [0, dmax <= 0 ? 1 : dmax * 1.1],
      },
    },
    axes: [
      {
        scale: "x",
        stroke: AXIS_STROKE,
        grid:  { stroke: GRID_STROKE },
        ticks: { stroke: TICK_STROKE },
        font:  axisFont(),
        splits: (u) => xData,
        values: (u, splits) => splits.map(i => labels[i] ?? ""),
      },
      opsYAxis(),
    ],
    series: [
      {}, // x
      {
        label:  "count",
        scale:  "y",
        stroke: COLOR_HIST,
        fill:   COLOR_HIST,
        paths:  uPlot.paths.bars({ align: 1, size: [0.8] }),
        points: { show: false },
      },
    ],
    plugins: [tooltipPlugin(el, (u, idx) => {
      const label = labels[idx];
      const count = u.data[1][idx];
      if (label == null || count == null) return null;
      return `<div class="tt-title">${label}</div><b>${count}</b> solve${count === 1 ? "" : "s"}`;
    })],
  });

  el.innerHTML = "";
  const u = new uPlot(opts, [xData, counts], el);
  // v1 a11y hygiene (spec §10.2).
  const cvs1 = u.ctx && u.ctx.canvas;
  if (cvs1) { cvs1.setAttribute("role", "img"); cvs1.setAttribute("aria-label", "Solve time histogram"); }
  registry.register(containerId, u);
  return u;
}

// ── Status mix ────────────────────────────────────────────────────────────

/**
 * Build or rebuild ops-solve-status.
 * solveData.status_counts = { optimal: N, feasible: M, ... }
 */
function buildStatusBars(containerId, solveData) {
  const el = typeof document !== "undefined" && document.getElementById(containerId);
  if (!el) return null;

  const sc = (solveData && solveData.status_counts) ? solveData.status_counts : {};
  const statuses = Object.keys(sc);

  if (statuses.length === 0) {
    el.innerHTML = '<div class="muted">no solves in window</div>';
    registry.unregister(containerId);
    return null;
  }

  // Stable order: known first, rest alphabetical
  const knownOrder = ["optimal", "feasible", "infeasible", "unknown"];
  const ordered = [
    ...knownOrder.filter(k => sc[k] != null),
    ...statuses.filter(k => !knownOrder.includes(k)).sort(),
  ];

  const xData   = ordered.map((_, i) => i);
  const yData   = ordered.map(k => sc[k]);
  const colors  = ordered.map(k => STATUS_COLOR[k] || "#8b949e");

  const width  = el.clientWidth  > 0 ? el.clientWidth  : 400;
  const height = el.clientHeight > 0 ? el.clientHeight : 180;

  const opts = baseBarOpts(containerId, width, height, {
    scales: {
      x: {
        time: false,
        range: (u, min, max) => [min - 0.5, max + 0.5],
      },
      y: {
        range: (u, dmin, dmax) => [0, dmax <= 0 ? 1 : dmax * 1.1],
      },
    },
    axes: [
      {
        scale: "x",
        stroke: AXIS_STROKE,
        grid:  { stroke: GRID_STROKE },
        ticks: { stroke: TICK_STROKE },
        font:  axisFont(),
        splits: (u) => xData,
        values: (u, splits) => splits.map(i => ordered[i] ?? ""),
      },
      opsYAxis(),
    ],
    series: [
      {}, // x
      {
        label:  "count",
        scale:  "y",
        // width:0 is required for disp.fill per-bar colours to activate
        // (multiPath logic checks strokeWidth==0 || dispStrokes!=null).
        width:  0,
        stroke: "#8b949e",  // fallback stroke (hidden when width=0)
        fill:   "#8b949e",  // fallback fill (overridden by disp.fill)
        paths:  uPlot.paths.bars({
          align: 1,
          size:  [0.7],
          disp:  {
            fill: {
              values: (u, si, i0, i1) => colors.slice(i0, i1 + 1),
            },
          },
        }),
        points: { show: false },
      },
    ],
    plugins: [tooltipPlugin(el, (u, idx) => {
      const label = ordered[idx];
      const count = u.data[1][idx];
      if (label == null || count == null) return null;
      const color = STATUS_COLOR[label] || "#8b949e";
      return `<span style="color:${color}">&#9679;</span> <b>${label}</b>: ${count}`;
    })],
  });

  el.innerHTML = "";
  const u = new uPlot(opts, [xData, yData], el);
  // v1 a11y hygiene (spec §10.2).
  const cvs2 = u.ctx && u.ctx.canvas;
  if (cvs2) { cvs2.setAttribute("role", "img"); cvs2.setAttribute("aria-label", "Solve status counts"); }
  registry.register(containerId, u);
  return u;
}

// ── Modbus writes (grouped bars) ──────────────────────────────────────────

/**
 * Build or rebuild ops-modbus-writes.
 *
 * modbusData.writes = [{ register: number|string, event: "modbus_write"|"modbus_error", n: number }, ...]
 *
 * Grouped bars: ok (green) left of centre, err (red) right of centre, per register.
 * Registers sorted numerically.
 *
 * Grouped-bar layout:
 *   Both series share the same integer x-indices (one per register).
 *   Each series uses disp.x0 (data-unit left-edge) to shift its bar left/right of centre.
 *   bar half-width = 0.37 units on the integer scale.
 *   ok:  left edge = index - 0.40, width = 0.37 (right edge = index - 0.03)
 *   err: left edge = index + 0.03, width = 0.37 (right edge = index + 0.40)
 *   Total span: 80% of column. Gap in middle: 6% of column.
 */
function buildModbusWrites(containerId, modbusData) {
  const el = typeof document !== "undefined" && document.getElementById(containerId);
  if (!el) return null;

  const writes = (modbusData && Array.isArray(modbusData.writes)) ? modbusData.writes : [];

  if (writes.length === 0) {
    el.innerHTML = '<div class="muted">no writes in window</div>';
    registry.unregister(containerId);
    return null;
  }

  // Aggregate by register
  const byReg = {};
  for (const w of writes) {
    const k = String(w.register);
    if (!byReg[k]) byReg[k] = { ok: 0, err: 0 };
    if (w.event === "modbus_write")  byReg[k].ok  = w.n;
    else                              byReg[k].err = w.n;
  }

  // Sort registers numerically
  const regs = Object.keys(byReg).sort((a, b) => Number(a) - Number(b));
  const xData  = regs.map((_, i) => i);
  const okData  = regs.map(r => byReg[r].ok);
  const errData = regs.map(r => byReg[r].err);

  // Grouped bar offsets (in scale/data units):
  //   ok:  left at x - 0.40, width 0.37  → right at x - 0.03
  //   err: left at x + 0.03, width 0.37  → right at x + 0.40
  const BAR_W  = 0.37;
  const okX0   = xData.map(v => v - 0.40);
  const errX0  = xData.map(v => v + 0.03);
  const sizes  = xData.map(() => BAR_W);

  function makeGroupedBars(x0Arr) {
    return uPlot.paths.bars({
      align: 1,
      disp: {
        x0: {
          unit:   1,  // data units (not %)
          values: (u, si, i0, i1) => x0Arr.slice(i0, i1 + 1),
        },
        size: {
          unit:   1,  // data units
          values: (u, si, i0, i1) => sizes.slice(i0, i1 + 1),
        },
      },
    });
  }

  const width  = el.clientWidth  > 0 ? el.clientWidth  : 400;
  const height = el.clientHeight > 0 ? el.clientHeight : 200;

  const opts = baseBarOpts(containerId, width, height, {
    scales: {
      x: {
        time: false,
        range: (u, min, max) => [min - 0.5, max + 0.5],
      },
      y: {
        range: (u, dmin, dmax) => {
          const maxVal = Math.max(...okData, ...errData, 1);
          return [0, maxVal * 1.15];
        },
      },
    },
    axes: [
      {
        scale: "x",
        stroke: AXIS_STROKE,
        grid:  { stroke: GRID_STROKE },
        ticks: { stroke: TICK_STROKE },
        font:  axisFont(),
        splits: (u) => xData,
        values: (u, splits) => splits.map(i => regs[i] ?? ""),
      },
      opsYAxis(),
    ],
    series: [
      {}, // x
      {
        label:  "ok",
        scale:  "y",
        stroke: COLOR_OK,
        fill:   COLOR_OK,
        paths:  makeGroupedBars(okX0),
        points: { show: false },
      },
      {
        label:  "err",
        scale:  "y",
        stroke: COLOR_ERR,
        fill:   COLOR_ERR,
        paths:  makeGroupedBars(errX0),
        points: { show: false },
      },
    ],
    legend: { show: false },   // custom overlay legend added after construction
    plugins: [tooltipPlugin(el, (u, idx) => {
      const reg = regs[idx];
      if (reg == null) return null;
      const ok = u.data[1][idx], err = u.data[2][idx];
      return `<div class="tt-title">reg ${reg}</div>` +
        `<span style="color:${COLOR_OK}">&#9679;</span> ok: <b>${ok ?? 0}</b><br>` +
        `<span style="color:${COLOR_ERR}">&#9679;</span> err: <b>${err ?? 0}</b>`;
    })],
  });

  el.innerHTML = "";
  el.style.position = "relative";
  const u = new uPlot(opts, [xData, okData, errData], el);
  // v1 a11y hygiene (spec §10.2).
  const cvs3 = u.ctx && u.ctx.canvas;
  if (cvs3) { cvs3.setAttribute("role", "img"); cvs3.setAttribute("aria-label", "Modbus write health by register"); }
  registry.register(containerId, u);
  addOverlayLegend(el, u, [[1, "ok", COLOR_OK], [2, "err", COLOR_ERR]]);
  return u;
}

// ── Public API ─────────────────────────────────────────────────────────────

/**
 * Build all 4 ops charts and return an update/resize API.
 *
 * Call AFTER the ops tab's hidden=false flip (so containers have non-zero width).
 *
 * @param {object} [initialSolveData]   - optional initial data for solve charts
 * @param {object} [initialModbusData]  - optional initial data for modbus chart
 * @returns {{ updateSolve(data), updateModbus(data), resize() }}
 */
export function buildOpsCharts(initialSolveData, initialModbusData) {
  // uPlot instances (null = empty-state innerHTML shown instead)
  let uSolveSeries   = null;
  let uHistogram     = null;
  let uStatusBars    = null;
  let uModbusWrites  = null;

  function updateSolve(solveData) {
    // Always destroy+rebuild so series membership adjusts to new statuses
    if (uSolveSeries) { uSolveSeries.destroy(); registry.unregister("ops-solve-series"); }
    if (uHistogram)   { uHistogram.destroy();   registry.unregister("ops-solve-histogram"); }
    if (uStatusBars)  { uStatusBars.destroy();  registry.unregister("ops-solve-status"); }

    uSolveSeries  = buildSolveSeries("ops-solve-series",   solveData);
    uHistogram    = buildHistogram  ("ops-solve-histogram", solveData);
    uStatusBars   = buildStatusBars ("ops-solve-status",    solveData);
  }

  function updateModbus(modbusData) {
    if (uModbusWrites) { uModbusWrites.destroy(); registry.unregister("ops-modbus-writes"); }
    uModbusWrites = buildModbusWrites("ops-modbus-writes", modbusData);
  }

  function resize() {
    // Let registry.resizeAll() handle all registered instances.
    // Also explicitly resize instances whose containers may have just become visible.
    for (const [id, u] of [
      ["ops-solve-series",    uSolveSeries],
      ["ops-solve-histogram", uHistogram],
      ["ops-solve-status",    uStatusBars],
      ["ops-modbus-writes",   uModbusWrites],
    ]) {
      if (!u) continue;
      const el = typeof document !== "undefined" && document.getElementById(id);
      if (!el || el.clientWidth === 0) continue;
      u.setSize({ width: el.clientWidth, height: el.clientHeight || 200 });
    }
  }

  // Seed with initial data if provided
  if (initialSolveData)  updateSolve(initialSolveData);
  if (initialModbusData) updateModbus(initialModbusData);

  return { updateSolve, updateModbus, resize };
}
