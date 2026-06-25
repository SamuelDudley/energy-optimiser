/**
 * panels.js — per-panel uPlot builders + figure assembly for #ts-figure.
 *
 * Builds 8 stacked, x-synced uPlot instances (one per PANEL_LAYOUT entry) over
 * a single shared union-x timeline (epoch seconds). Replaces the Plotly
 * #ts-figure. The price panel uses arcsinh (distr:4) spike compression; the
 * two ribbon panels render via ribbonPlugin; bands via native uPlot bands fed
 * from bandColumns(); shapes (now/cursor vlines, floor/zero hlines, buy/sell
 * rects, asinh threshold line) via shapesPlugin.
 *
 * Public: buildTsFigure(rootEl, model) -> { instances, update(model), destroy() }.
 *
 * model shape (built in dashboard.js from `state`):
 *   {
 *     unionX: number[],                      // epoch sec, ascending
 *     price: { importRealised, importPredicted, exportRealised, exportPredicted,
 *              bands: { importLo, importHi, exportLo, exportHi } },
 *     pv:   { p50, measured, actual?|null, bandLo, bandHi },
 *     soc:  { measured, planned, floorPct },
 *     load: { stacks: [{ id, color, cum }], measuredEnv, plannedEnv },
 *     grid: { inverter, shelly?|null, planned },
 *     cost: { realised, planned, settled?|null },
 *     decisionCats: number[], modeCats: number[],
 *     nowSec, cursorSec, regions: [{x0,x1,kind}], thresholdC: 30,
 *   }
 * All y-column arrays are already aligned 1:1 with unionX (null = gap).
 *
 * Conditional-series membership (PV-actual markers, Shelly grid line, settled
 * cost, the set of managed-load stacks) is decided ONCE at build time from the
 * first model. update(model) only swaps data columns in the SAME membership —
 * it never adds/removes series (uPlot throws on a series/data length mismatch).
 * If membership genuinely needs to change, the caller rebuilds (destroy + new).
 */
import uPlot from "./uplot.esm.js";
import { cursorDragOpts, makeSyncHub, registry } from "./chart-core.js";
import { shapesPlugin } from "./shapes.js";
import { ribbonPlugin } from "./ribbon.js";
import { spikeLabelsPlugin } from "./spike-labels.js";
import {
  DECISION_COLORS, DECISION_LABELS, MODE_COLORS, MODE_LABELS,
} from "./classify.js";
import { hexToRgba } from "./derive.js";

// Shared font stack — read once from the body so panels match the page.
const FONT_FAMILY =
  (typeof document !== "undefined" && document.body && getComputedStyle(document.body).fontFamily) ||
  "sans-serif";
const AXIS_FONT = "12px " + FONT_FAMILY;

// Fixed left-gutter (px) — identical on every panel so the synced crosshair
// lands at the same screen-x across the stack (spec §3.1, §6 item 1). Sized to
// the widest arcsinh price tick ("1500") plus the date tick.
const FIXED_GUTTER = 52;

const GRID_STROKE = "#21262d";
const TICK_STROKE = "#21262d";
const AXIS_STROKE = "#c9d1d9";

// arcsinh price ticks (refinement 1d) — named splits filtered to the live
// [min,max]. uPlot's auto distr:4 ticks are decades and miss 0/30 + negatives.
const PRICE_SPLITS = [-40, 0, 10, 30, 100, 300, 1000, 1500];

const baseAxis = (extra = {}) => ({
  stroke: AXIS_STROKE,
  grid: { stroke: GRID_STROKE, width: 1 },
  ticks: { stroke: TICK_STROKE, width: 1 },
  font: AXIS_FONT,
  ...extra,
});

// A hidden x-axis (non-bottom panels). Bottom (COST) panel shows time labels.
const xAxisHidden = () => ({ scale: "x", show: false, size: 0 });
const xAxisShown = () => baseAxis({ scale: "x", size: 34, space: 60 });

const yAxis = (extra = {}) => baseAxis({ scale: "y", size: FIXED_GUTTER, ...extra });
// A y-axis that occupies the gutter but draws nothing (ribbon lanes).
const yAxisBlank = () => ({ scale: "y", show: false, size: FIXED_GUTTER });

// ── series factory helpers ─────────────────────────────────────────────────

// Realised / measured line: nulls become gaps (spanGaps:false).
const lineGaps = (stroke, width) => ({
  scale: "y", stroke, width, spanGaps: false, points: { show: false },
});
// Planned / predicted line: dotted, bridges gaps (spanGaps:true).
const lineDotted = (stroke, width) => ({
  scale: "y", stroke, width, dash: [3, 3], spanGaps: true, points: { show: false },
});
// Invisible bound series for a native band (so uPlot has data to fill between).
// show:true so uPlot's band machinery runs; width:0 + points.show:false hides the line itself.
const bandBound = () => ({ scale: "y", show: true, width: 0, points: { show: false } });

// ── per-panel spec builders ─────────────────────────────────────────────────
//
// Each builder is called ONCE at figure build with the first model + a getState
// closure. It returns:
//   { series, scaleY?, yAxisCfg?, bands?, plugins?, dataFn(model) }
// where series[] (sans the x placeholder) and dataFn(model) stay in strict
// lockstep — dataFn returns exactly series.length columns aligned to unionX.
// The assembler prepends the shared unionX as series[0]/data[0].

function pricePanel(model, getState) {
  const series = [
    bandBound(),                 // 0: import lo
    bandBound(),                 // 1: import hi
    bandBound(),                 // 2: export lo
    bandBound(),                 // 3: export hi
    lineGaps("#f0883e", 1.6),    // 4: import realised
    lineDotted("#f0883e", 1.0),  // 5: import predicted
    lineGaps("#56d364", 1.6),    // 6: export realised
    lineDotted("#56d364", 1.0),  // 7: export predicted
  ];
  // Native band refs are FINAL series indices (after x prepend => +1).
  // import: fill between hi(local1->2) and lo(local0->1).
  // export: fill between hi(local3->4) and lo(local2->3).
  const bands = [
    { series: [2, 1], fill: "rgba(240,136,62,0.15)" },
    { series: [4, 3], fill: "rgba(86,211,100,0.15)" },
  ];
  const yAxisCfg = yAxis({
    splits: (u, _a, min, max) => PRICE_SPLITS.filter((v) => v >= min && v <= max),
    values: (u, splits) => splits.map((v) => String(v)),
  });
  const dataFn = (m) => {
    const p = m.price;
    return [
      p.bands.importLo, p.bands.importHi, p.bands.exportLo, p.bands.exportHi,
      p.importRealised, p.importPredicted, p.exportRealised, p.exportPredicted,
    ];
  };
  return {
    series, dataFn,
    scaleY: { distr: 4, asinh: 30 },
    yAxisCfg, bands,
    plugins: [
      shapesPlugin({ getState, kind: "price" }),
      // Refinement 1g: spike peak/trough markers with $ value labels.
      // seriesIdxImport=5 and seriesIdxExport=7 match the data[] positions
      // after the x prepend:  0=x, 1=importLo, 2=importHi, 3=exportLo,
      // 4=exportHi, 5=importRealised, 6=importPredicted, 7=exportRealised.
      spikeLabelsPlugin({ seriesIdxImport: 5, seriesIdxExport: 7 }),
    ],
  };
}

function ribbonPanel(key, getCats, tooltipEl) {
  const isDecision = key === "decision";
  const colors = isDecision ? DECISION_COLORS : MODE_COLORS;
  const labels = isDecision ? DECISION_LABELS : MODE_LABELS;
  // One transparent placeholder series so uPlot has a data column.
  const series = [{ scale: "y", show: false, points: { show: false } }];
  // dataFn returns a zeros column aligned to the current model's unionX.
  const dataFn = (m) => [m.unionX.map(() => 0)];
  return {
    series, dataFn,
    scaleY: { range: [0, 1] },
    yAxisCfg: yAxisBlank(),
    plugins: [
      ribbonPlugin({
        getCats,
        colorOf: (c) => colors[c] ?? "#21262d",
        labelOf: (c) => labels[c] ?? "—",
        glyphOf: (c) => (labels[c] ?? "").replace(/^(charge|discharge|idle|mode \d+) ?·? ?/, "").slice(0, 3) || "·",
        tooltipEl,
      }),
    ],
  };
}

function pvPanel(model, getState) {
  const hasActual = !!(model.pv.actual && model.pv.actual.some((v) => v != null));
  const series = [
    bandBound(),                 // 0: P10 (lo)
    bandBound(),                 // 1: P90 (hi)
    lineDotted("#f2cc60", 1.6),  // 2: P50
    lineGaps("#f2cc60", 1.6),    // 3: PV measured
  ];
  if (hasActual) {
    series.push({
      scale: "y", stroke: "#f2cc60", width: 0, spanGaps: false,
      points: { show: true, size: 4, stroke: "#f2cc60", fill: "rgba(0,0,0,0)", width: 1 },
    });
  }
  const bands = [{ series: [2, 1], fill: "rgba(242,204,96,0.22)" }];
  const dataFn = (m) => {
    const pv = m.pv;
    const cols = [pv.bandLo, pv.bandHi, pv.p50, pv.measured];
    if (hasActual) cols.push(pv.actual || []);
    return cols;
  };
  return {
    series, dataFn,
    scaleY: {}, bands,
    plugins: [shapesPlugin({ getState, kind: "pv" })],
  };
}

function socPanel(model, getState) {
  const series = [lineGaps("#79c0ff", 1.8), lineDotted("#79c0ff", 1.6)];
  const dataFn = (m) => [m.soc.measured, m.soc.planned];
  return {
    series, dataFn,
    scaleY: { range: [0, 100] },
    plugins: [shapesPlugin({ getState, kind: "soc" })],
  };
}

function loadPanel(model, getState) {
  const stacks = model.load.stacks || [];
  const stepped = uPlot.paths.stepped({ align: 1 });
  const series = [];
  for (const s of stacks) {
    series.push({
      scale: "y", stroke: s.color, width: 1, spanGaps: false,
      paths: stepped, fill: hexToRgba(s.color, 0.5),
      points: { show: false },
    });
  }
  series.push(lineGaps("#ff9e64", 1.6));    // measured envelope
  series.push(lineDotted("#ff9e64", 1.4));  // planned envelope
  // Stack ids are fixed at build time; dataFn maps by id and tolerates absence.
  const stackIds = stacks.map((s) => s.id);
  const dataFn = (m) => {
    const byId = new Map((m.load.stacks || []).map((s) => [s.id, s.cum]));
    const cols = stackIds.map((id) => byId.get(id) || m.unionX.map(() => null));
    cols.push(m.load.measuredEnv, m.load.plannedEnv);
    return cols;
  };
  return {
    series, dataFn,
    scaleY: {},
    plugins: [shapesPlugin({ getState, kind: "load" })],
  };
}

function gridPanel(model, getState) {
  const hasShelly = !!(model.grid.shelly && model.grid.shelly.some((v) => v != null));
  const series = [lineGaps("#c9d1d9", 1.4)];
  if (hasShelly) series.push(lineGaps("#7ee787", 1.0));
  series.push(lineDotted("#c9d1d9", 1.4));
  const dataFn = (m) => {
    const cols = [m.grid.inverter];
    if (hasShelly) cols.push(m.grid.shelly || []);
    cols.push(m.grid.planned);
    return cols;
  };
  return {
    series, dataFn,
    scaleY: {},
    plugins: [shapesPlugin({ getState, kind: "grid" })],
  };
}

function costPanel(model, getState) {
  const hasSettled = !!(model.cost.settled && model.cost.settled.some((v) => v != null));
  const series = [lineGaps("#bc8cff", 1.4), lineDotted("#bc8cff", 1.4)];
  if (hasSettled) {
    series.push({
      scale: "y", stroke: "#ffd700", width: 1.2, spanGaps: false,
      paths: uPlot.paths.stepped({ align: 1 }), points: { show: false },
    });
  }
  const dataFn = (m) => {
    const cols = [m.cost.realised, m.cost.planned];
    if (hasSettled) cols.push(m.cost.settled || []);
    return cols;
  };
  return {
    series, dataFn,
    scaleY: {},
    plugins: [shapesPlugin({ getState, kind: "cost" })],
  };
}

// ── makePanel — instantiate one uPlot from a spec ───────────────────────────

function makePanel({ el, unionX, spec, model, showTime, peers, hub }) {
  const hubOpts = hub.instanceOpts(peers);
  const opts = {
    width: (el && el.clientWidth) || 600,
    height: (el && el.clientHeight) || 80,
    scales: {
      x: { time: true },
      y: spec.scaleY || {},
    },
    axes: [
      showTime ? xAxisShown() : xAxisHidden(),
      spec.yAxisCfg || yAxis(),
    ],
    series: [{}, ...spec.series],
    bands: spec.bands || [],
    legend: { show: false },
    // Merge drag opts with hub sync opts so both coexist. Spreading hubOpts
    // wholesale would overwrite cursor with only { sync }, dropping drag.
    cursor: { ...cursorDragOpts(), ...(hubOpts.cursor || {}) },
    plugins: spec.plugins || [],
    hooks: hubOpts.hooks,
  };
  const data = [unionX, ...spec.dataFn(model)];
  return new uPlot(opts, data, el);
}

// ── figure assembler ────────────────────────────────────────────────────────

// Panel id -> builder. Order MUST match PANEL_LAYOUT in dashboard.js.
const PANEL_ORDER = ["prices", "ribbon", "mode", "solar", "soc", "load", "grid", "cost"];

// Concise aria-label for each panel's main canvas (spec §10.2 v1 a11y hygiene).
const PANEL_ARIA_LABEL = {
  prices: "Price, c/kWh time series",
  ribbon: "Battery decision ribbon",
  mode:   "Mode ribbon",
  solar:  "Solar PV, kW time series",
  soc:    "State of charge, % time series",
  load:   "Load, kW time series",
  grid:   "Grid power, kW time series",
  cost:   "Cost, c/h time series",
};

/**
 * Build the 8 synced uPlot panels into rootEl's per-panel child divs.
 *
 * rootEl must already contain one `<div class="uplot-panel" data-panel="ID">`
 * per PANEL_ORDER entry (created by the dashboard assembler so heights come
 * from CSS). buildTsFigure does NOT create those divs.
 *
 * @param {HTMLElement} rootEl
 * @param {object} model
 * @returns {{ instances: import("uplot").default[], update:(m)=>void, destroy:()=>void }}
 */
export function buildTsFigure(rootEl, model) {
  const hub = makeSyncHub("ts");
  const instances = [];
  const specs = [];          // parallel to instances; holds dataFn per panel
  const ids = [];            // parallel to instances; holds panel id
  const peers = () => instances;

  // Shared mutable model the plugins close over; update() reassigns it.
  let cur = model;
  const getState = () => ({
    cursorSec: cur.cursorSec,
    nowSec: cur.nowSec,
    regions: cur.regions,
    socFloorPct: cur.soc && cur.soc.floorPct,
    thresholdC: cur.thresholdC,
  });

  const ribbonTooltip = rootEl.querySelector(".ribbon-tooltip") || null;

  const specFor = (id) => {
    if (id === "prices") return pricePanel(model, getState);
    if (id === "ribbon")
      return ribbonPanel("decision", () => cur.decisionCats || [], ribbonTooltip);
    if (id === "mode")
      return ribbonPanel("mode", () => cur.modeCats || [], ribbonTooltip);
    if (id === "solar") return pvPanel(model, getState);
    if (id === "soc")   return socPanel(model, getState);
    if (id === "load")  return loadPanel(model, getState);
    if (id === "grid")  return gridPanel(model, getState);
    if (id === "cost")  return costPanel(model, getState);
    return null;
  };

  for (const id of PANEL_ORDER) {
    const el = rootEl.querySelector(`.uplot-panel[data-panel="${id}"]`);
    if (!el) continue;
    const spec = specFor(id);
    if (!spec) continue;
    const u = makePanel({
      el, unionX: model.unionX, spec, model,
      showTime: id === "cost", peers, hub,
    });
    // v1 a11y hygiene (spec §10.2): give each main canvas a role + label so
    // screen readers can identify the chart. Full offscreen data-table deferred.
    const canvas = u.ctx && u.ctx.canvas;
    if (canvas) {
      canvas.setAttribute("role", "img");
      canvas.setAttribute("aria-label", PANEL_ARIA_LABEL[id] || id);
    }
    registry.register(`ts-${id}`, u);
    instances.push(u);
    specs.push(spec);
    ids.push(id);
  }

  // Initial fit to container widths once mounted. Run once synchronously, then
  // again after the next frame in case the flex layout hadn't flushed yet
  // (panel children get their height from flex-grow, which can be 0 on the
  // first synchronous read right after append).
  registry.resizeAll();
  if (typeof requestAnimationFrame === "function") {
    requestAnimationFrame(() => registry.resizeAll());
  }

  function update(m) {
    cur = m;
    for (let i = 0; i < instances.length; i++) {
      const u = instances[i];
      const spec = specs[i];
      try {
        u.setData([m.unionX, ...spec.dataFn(m)], false);
      } catch (e) {
        console.warn(`[panels] setData failed for ${ids[i]}:`, e);
      }
    }
  }

  // Cheap cursor/now-line refresh (spec §5.7). Mutates only the shapes-plugin
  // state the figure closes over, then repaints — no data rebuild, so a manual
  // zoom and the band/line geometry are untouched. Task 14 drives this.
  function setShapes({ cursorSec, nowSec, regions } = {}) {
    if (cursorSec !== undefined) cur.cursorSec = cursorSec;
    if (nowSec !== undefined) cur.nowSec = nowSec;
    if (regions !== undefined) cur.regions = regions;
    for (const u of instances) u.redraw(false, false);
  }

  function destroy() {
    for (const id of ids) registry.unregister(`ts-${id}`);
    for (const u of instances) {
      try { u.destroy(); } catch (e) { /* ignore */ }
    }
    instances.length = 0;
    specs.length = 0;
    ids.length = 0;
  }

  return { instances, update, setShapes, destroy };
}
