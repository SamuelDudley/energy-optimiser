/**
 * spend-chart.js — daily-spend bar chart in uPlot.
 *
 * Replaces the Plotly spend chart (redrawDailySpend / spendCursorShapes).
 *
 * Exports:
 *   buildSpendChart(el) → { update(rows, cursorNemDate), destroy() }
 *
 * Series layout (data[] index):
 *   0  x     — integer indices [0,1,2,...] (time:false)
 *   1  import cost (orange bar) — raw value from import_cost_aud
 *   2  export revenue (green bar) — NEGATED value for drawing; raw positive
 *      kept in rawExportRev[] for tooltip
 *   3  net cost (purple line+points) — raw value from net_cost_aud
 */

"use strict";

import uPlot from "./uplot.esm.js";
import { registry, isNarrow } from "./chart-core.js";

// ── Colours ────────────────────────────────────────────────────────
const ORANGE   = "#f0883e";
const GREEN    = "#56d364";
const PURPLE   = "#bc8cff";
const BG       = "#161b22";
const GRID     = "#21262d";
const TICK_CLR = "#c9d1d9";
const ZERO_CLR = "#444c56";
const HIGHLIGHT_FILL = "rgba(88,166,255,0.12)";
const HIGHLIGHT_STROKE = "#58a6ff";

// ── Path builder (bars) ────────────────────────────────────────────
const barPaths = uPlot.paths.bars({ size: [0.8, Infinity] });

// ── buildSpendChart ────────────────────────────────────────────────

/**
 * Build a uPlot spend chart inside `el` and return a controller object.
 *
 * @param {HTMLElement} el - mount element
 * @returns {{ update(rows: object[], cursorNemDate: string|null): void, destroy(): void }}
 */
export function buildSpendChart(el) {
  // Mutable state shared between update() calls and the draw/setCursor hooks.
  const state = {
    /** YYYY-MM-DD labels, ASC — one per data column */
    labels: [],
    /** Raw (positive) export revenue per index, for tooltip redirection */
    rawExportRev: [],
    /** The NEM date whose bar should be highlighted, or null */
    cursorNemDate: null,
  };

  // ── Tooltip DOM ──────────────────────────────────────────────────
  const tooltip = document.createElement("div");
  tooltip.style.cssText = [
    "position:absolute",
    "pointer-events:none",
    "background:#1c2128",
    "border:1px solid #30363d",
    "border-radius:4px",
    "padding:6px 10px",
    "font:12px/1.5 sans-serif",
    "color:#e8edf2",
    "white-space:pre",
    "display:none",
    "z-index:100",
  ].join(";");
  el.style.position = "relative";
  el.appendChild(tooltip);

  // ── setCursor hook — show tooltip ─────────────────────────────
  function onSetCursor(u) {
    const idx = u.cursor.idx;
    if (idx == null || idx < 0 || idx >= state.labels.length) {
      tooltip.style.display = "none";
      return;
    }

    const imp  = u.data[1][idx];
    const expN = u.data[2][idx];         // negated value used for drawing
    const net  = u.data[3][idx];
    const expRaw = state.rawExportRev[idx]; // positive raw value for tooltip

    const date = state.labels[idx] ?? "";

    const lines = [date];
    if (imp  != null) lines.push(`import cost $${imp.toFixed(2)}`);
    // export revenue shown POSITIVE (raw value, not the drawn-negative)
    if (expN != null && expRaw != null) lines.push(`export revenue $${expRaw.toFixed(2)}`);
    if (net  != null) lines.push(`net $${net.toFixed(2)}`);

    tooltip.textContent = lines.join("\n");
    tooltip.style.display = "block";

    // Position: follow cursor x, flip if near right edge.
    const cursorLeft = u.cursor.left;
    const tipW = tooltip.offsetWidth;
    const chartW = u.bbox.width / devicePixelRatio;
    let left = cursorLeft + 12;
    if (left + tipW > chartW - 8) left = cursorLeft - tipW - 8;
    tooltip.style.left = `${left}px`;
    tooltip.style.top  = "24px";
  }

  // ── draw hook — NEM-date highlight rect ───────────────────────
  function onDraw(u) {
    if (!state.cursorNemDate || !state.labels.length) return;
    const idx = state.labels.indexOf(state.cursorNemDate);
    if (idx < 0) return;

    const ctx = u.ctx;
    const b = u.bbox;

    // Map the integer x index to pixel position.
    const cx = u.valToPos(idx, "x", true);   // device px
    const barW = u.valToPos(1, "x", true) - u.valToPos(0, "x", true); // one-slot width in device px
    const halfW = barW * 0.5;

    ctx.save();
    ctx.fillStyle = HIGHLIGHT_FILL;
    ctx.strokeStyle = HIGHLIGHT_STROKE;
    ctx.lineWidth = 1;
    ctx.beginPath();
    const rectX = cx - halfW;
    ctx.rect(rectX, b.top, barW, b.height);
    ctx.fill();
    ctx.stroke();
    ctx.restore();
  }

  // ── uPlot opts ────────────────────────────────────────────────
  function buildOpts(width, height) {
    const narrow = isNarrow();
    const margin = narrow
      ? { left: 40, right: 8,  top: 22, bottom: 32 }
      : { left: 54, right: 16, top: 26, bottom: 44 };

    return {
      width,
      height,
      padding: [margin.top, margin.right, 0, margin.left],
      scales: {
        x: {
          time: false,
          // keep x range from auto-expanding beyond the data length
          range: (u, min, max) => [min, max],
        },
        y: {
          // let uPlot auto-range; zeroline drawn by plugin
          range: (u, min, max) => {
            // ensure zero is always in range
            return [Math.min(0, min), Math.max(0, max)];
          },
        },
      },
      axes: [
        {
          // x axis — YYYY-MM-DD labels via splits/values
          scale: "x",
          stroke: TICK_CLR,
          grid:  { stroke: GRID },
          ticks: { stroke: ZERO_CLR, size: 3 },
          font:  "11px sans-serif",
          // splits = indices, values = date labels
          splits: (u) => {
            const n = state.labels.length;
            // On narrow viewports show ~every other label to avoid overlap
            const stride = narrow && n > 14 ? 2 : 1;
            const out = [];
            for (let i = 0; i < n; i += stride) out.push(i);
            return out;
          },
          values: (u, splits) =>
            splits.map((i) => {
              const lbl = state.labels[i];
              if (!lbl) return "";
              // Shorten to MM-DD on narrow to save space
              return narrow ? lbl.slice(5) : lbl;
            }),
          size: margin.bottom,
        },
        {
          // y axis
          scale: "y",
          stroke: TICK_CLR,
          grid:  { stroke: GRID },
          ticks: { stroke: ZERO_CLR, size: 3 },
          font:  "12px sans-serif",
          label: "AUD / day",
          labelFont: "11px sans-serif",
          labelStroke: "#7d8590",
          size: margin.left,
          // add zero line by drawing it in the grid
          // (uPlot doesn't have a zeroline option, handled in draw hook instead)
        },
      ],
      series: [
        // series[0] — x placeholder
        {},
        // series[1] — import cost (orange bar)
        {
          label: "import cost",
          scale: "y",
          stroke: ORANGE,
          fill:   ORANGE,
          width:  1,
          paths:  barPaths,
          points: { show: false },
        },
        // series[2] — export revenue (green bar, drawn negated)
        {
          label: "export revenue",
          scale: "y",
          stroke: GREEN,
          fill:   GREEN,
          width:  1,
          paths:  barPaths,
          points: { show: false },
        },
        // series[3] — net cost (purple line+markers)
        {
          label: "net (bill)",
          scale: "y",
          stroke:  PURPLE,
          fill:    "rgba(0,0,0,0)",
          width:   2,
          points:  { show: true, size: 5, fill: PURPLE, stroke: PURPLE },
        },
      ],
      legend: {
        show: true,
        live: false,    // don't update legend values on hover (we use custom tooltip)
        // native click-to-toggle series.show provides the legend parity
      },
      cursor: {
        // Disable selection drag on spend chart (it's categorical, not temporal)
        drag: { x: false, y: false, setScale: false },
      },
      hooks: {
        draw:      [drawZeroLine, onDraw],
        setCursor: [onSetCursor],
      },
    };
  }

  // ── zero-line draw hook ────────────────────────────────────────
  function drawZeroLine(u) {
    const ctx = u.ctx;
    const b   = u.bbox;
    const y0  = u.valToPos(0, "y", true);
    if (y0 < b.top || y0 > b.top + b.height) return;
    ctx.save();
    ctx.strokeStyle = ZERO_CLR;
    ctx.lineWidth   = 1;
    ctx.beginPath();
    ctx.moveTo(b.left, y0);
    ctx.lineTo(b.left + b.width, y0);
    ctx.stroke();
    ctx.restore();
  }

  // ── initial empty data ──────────────────────────────────────────
  const EMPTY_DATA = [[],  [],  [],  []];

  let u = null;

  function _create() {
    const w = el.clientWidth  || 600;
    const h = el.clientHeight || 260;
    const opts = buildOpts(w, h);
    u = new uPlot(opts, EMPTY_DATA, el);
    registry.register("spend-figure", u);
  }

  _create();

  // ── Public API ──────────────────────────────────────────────────

  /**
   * Update chart with fresh rows from /daily_spend, highlighting the bar
   * for `cursorNemDate` (YYYY-MM-DD string, or null for no highlight).
   *
   * @param {Array<{nem_date:string, import_cost_aud:number|null, export_revenue_aud:number|null, net_cost_aud:number|null}>} rows
   * @param {string|null} cursorNemDate
   */
  function update(rows, cursorNemDate) {
    // Re-sort ASC (API returns DESC)
    const asc = [...rows].sort((a, b) => a.nem_date.localeCompare(b.nem_date));

    state.labels        = asc.map((r) => r.nem_date);
    state.cursorNemDate = cursorNemDate ?? null;
    state.rawExportRev  = asc.map((r) => r.export_revenue_aud ?? null);

    const n       = asc.length;
    const xIdx    = Array.from({ length: n }, (_, i) => i);
    const imp     = asc.map((r) => r.import_cost_aud ?? null);
    // Export revenue drawn negated so it stacks below zero
    const expNeg  = asc.map((r) =>
      r.export_revenue_aud != null ? -r.export_revenue_aud : null
    );
    const net     = asc.map((r) => r.net_cost_aud ?? null);

    // uPlot requires x range to cover [0, n-1] — set it explicitly so that
    // bars at index 0 and n-1 aren't clipped.
    const data = [xIdx, imp, expNeg, net];

    if (!u) _create();

    // Resize to container in case it changed
    const w = el.clientWidth  || 600;
    const h = el.clientHeight || 260;
    u.setSize({ width: w, height: h });

    u.setData(data, true);

    // Force x scale to cover exactly [−0.5, n−0.5] so edge bars render fully
    if (n > 0) {
      u.setScale("x", { min: -0.5, max: n - 0.5 });
    }

    // Trigger redraw for the NEM-date highlight
    u.redraw(false, false);
  }

  /**
   * Update only the NEM-date highlight (called from redrawSpendCursor).
   * @param {string|null} cursorNemDate
   */
  function setHighlight(cursorNemDate) {
    state.cursorNemDate = cursorNemDate ?? null;
    if (u) u.redraw(false, false);
  }

  function destroy() {
    if (u) {
      registry.unregister("spend-figure");
      u.destroy();
      u = null;
    }
    if (tooltip.parentNode) tooltip.parentNode.removeChild(tooltip);
  }

  return { update, setHighlight, destroy };
}
