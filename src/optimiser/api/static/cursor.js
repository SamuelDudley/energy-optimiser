/**
 * cursor.js — uPlot cursor wiring for the energy-optimiser dashboard.
 *
 * Installs a setCursor hook on each uPlot instance so that hovering the
 * chart pins the cursor model (state.cursor) and updates:
 *   - the status strip (#cursor-time, #cursor-mode, #cursor-now-btn)  [parity]
 *   - the multi-panel readout column (#cursor-readout)                 [1b]
 *   - the decision/mode chip (#decision-chip / #mode-chip)             [1c]
 *   - the price readout in true c/kWh                                  [1a]
 *
 * The pinned-cursor vertical LINE is drawn by shapesPlugin (Task 11) — not here.
 * This module only drives the DOM readout + the cursor model.
 *
 * Usage:
 *   import { wireCursor } from "./cursor.js";
 *   wireCursor(tsFigure.instances, () => currentModel, { setCursor, nearestSlotAt });
 */
"use strict";

import { DECISION_COLORS, DECISION_LABELS, MODE_COLORS, MODE_LABELS } from "./classify.js";

// Representative series index (1-based into u.data, after the x column) for
// each panel's "headline" value shown in the readout column.  These indices
// must stay in sync with the series arrays built by panels.js panel builders.
//
// Panel order (PANEL_ORDER in panels.js):
//   prices (0), ribbon/decision (1), mode (2), solar (3),
//   soc (4), load (5), grid (6), cost (7)
//
// For each panel we pick the most-informative non-band series.
// Indices are into u.data[seriesIdx] where 0 is always unionX.
const PANEL_READOUT = [
  // prices: import realised (idx 5 = local band×4 + realised import)
  // Series layout in pricePanel: [importLo, importHi, exportLo, exportHi,
  //   importRealised(4), importPredicted(5), exportRealised(6), exportPredicted(7)]
  // +1 for the x column => data indices 5, 6, 7, 8
  // importRealised = data[5], importPredicted = data[6],
  // exportRealised = data[7], exportPredicted = data[8]
  { panel: "prices",   label: "Price",   unit: "c/kWh",  // handled specially below
    importIdx: 5, exportIdx: 7, importPredIdx: 6, exportPredIdx: 8 },
  // ribbon / decision: placeholder series only — no numeric value; use decisionCats
  { panel: "ribbon",   label: "Decision", unit: null, catField: "decision" },
  // mode: placeholder series only; use modeCats
  { panel: "mode",     label: "Mode",     unit: null, catField: "mode" },
  // solar: PV measured (series local idx 3 in pvPanel, +1 for x => data[4])
  // Layout: [bandLo(1), bandHi(2), p50(3), measured(4), actual?(5)]
  { panel: "solar",    label: "PV",       unit: "kW",    dataIdx: 4, altIdx: 3 },
  // soc: measured (local 0, +1 => data[1])
  // Layout: [measured(1), planned(2)]
  { panel: "soc",      label: "SOC",      unit: "%",     dataIdx: 1, altIdx: 2 },
  // load: measured envelope — it's pushed last in loadPanel after the stacks;
  //   positions vary by stack count, so we scan from the right below.
  { panel: "load",     label: "Load",     unit: "kW",    scanFromEnd: true },
  // grid: inverter (local 0, data[1])
  // Layout: [inverter(1), shelly?(2 if present), planned]
  // grid: inverter (data[1]) preferred; planned is the LAST column but its index
  // shifts with the optional Shelly series, so fall back via an end-scan.
  { panel: "grid",     label: "Grid",     unit: "kW",    dataIdx: 1, altIdx: null, endAlt: true },
  // cost: realised (local 0, data[1])
  // Layout: [realised(1), planned(2), settled?(3)]
  { panel: "cost",     label: "Cost",     unit: "c/h",   dataIdx: 1, altIdx: 2 },
];

// Format a numeric value with thousands-separator and fixed decimal places.
// Matches the spec "thousands-sep, 1 dp" requirement for 1a price readout.
function fmtValue(v, dp = 1) {
  if (v == null || !Number.isFinite(v)) return "—";
  // toLocaleString handles thousands-sep; fix decimal places with toFixed.
  const n = parseFloat(v.toFixed(dp));
  return n.toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp });
}

// Nearest non-null value to rowIdx within ±span (closest distance wins). Used
// as a fallback for SPARSE forecast columns: e.g. the 30-min Amber price
// forecast only has values at its own timestamps, so it's null at the in-between
// 5-min grid points the cursor can land on. This surfaces the forecast in effect.
function nearestNonNull(col, rowIdx, span) {
  if (!col) return null;
  for (let d = 1; d <= span; d++) {
    const b = col[rowIdx - d];
    if (b != null && Number.isFinite(b)) return b;
    const f = col[rowIdx + d];
    if (f != null && Number.isFinite(f)) return f;
  }
  return null;
}

// Pick the best value at rowIdx: exact primary (realised/measured) → exact
// altIdx (predicted/planned) → NEAREST altIdx (for sparse forecast grids).
// Realised is only looked up exactly (never nearest) so a future slot never
// shows a stale past realised value.
function pickVal(u, idx, altIdx, rowIdx, span = 12) {
  if (idx == null || rowIdx == null) return null;
  const v = u.data[idx]?.[rowIdx];
  if (v != null && Number.isFinite(v)) return v;
  if (altIdx != null) {
    const col = u.data[altIdx];
    const v2 = col?.[rowIdx];
    if (v2 != null && Number.isFinite(v2)) return v2;
    return nearestNonNull(col, rowIdx, span);
  }
  return null;
}

// Scan u.data from the right to find the last non-band series column that has a
// value at rowIdx (the LOAD envelope is always last after a variable number of
// stack columns).
function pickValFromEnd(u, skipLast, rowIdx) {
  if (rowIdx == null) return null;
  const len = u.data.length;
  for (let i = len - 1 - skipLast; i >= 1; i--) {
    const v = u.data[i]?.[rowIdx];
    if (v != null && Number.isFinite(v)) return v;
  }
  return null;
}

// Formatted value HTML for one panel at rowIdx — the per-panel on-chart hover
// tooltip. Mirrors updateReadout's per-panel logic. Returns null when there's
// no value to show (so the tooltip stays hidden). Exported for unit testing.
export function panelValueHtml(u, spec, rowIdx, model) {
  if (rowIdx == null) return null;
  // Decision / mode ribbons — the category label + its colour dot.
  if (spec.catField) {
    const cats   = spec.catField === "decision" ? (model && model.decisionCats) : (model && model.modeCats);
    const labels = spec.catField === "decision" ? DECISION_LABELS : MODE_LABELS;
    const colors = spec.catField === "decision" ? DECISION_COLORS : MODE_COLORS;
    const c = cats && rowIdx < cats.length ? cats[rowIdx] : null;
    if (c == null) return null;
    return `<span style="color:${colors[c] ?? "#8b949e"}">&#9679;</span> ${labels[c] ?? "—"}`;
  }
  // Price — import & export, realised preferred then predicted.
  if (spec.importIdx != null) {
    const imp = pickVal(u, spec.importIdx, spec.importPredIdx, rowIdx);
    const exp = pickVal(u, spec.exportIdx, spec.exportPredIdx, rowIdx);
    if (imp == null && exp == null) return null;
    const impStr = imp != null ? fmtValue(imp, 1) : "—";
    const expStr = exp != null ? fmtValue(exp, 1) : "—";
    return `imp <b>${impStr}</b> / exp <b>${expStr}</b> c/kWh`;
  }
  // Load — scan from the right for the envelope.
  if (spec.scanFromEnd) {
    const v = pickValFromEnd(u, 1, rowIdx) ?? pickValFromEnd(u, 0, rowIdx);
    return v != null ? `<b>${fmtValue(v, 2)}</b> ${spec.unit}` : null;
  }
  // Everything else — dataIdx with altIdx (and grid's end-scan) fallback.
  let v = pickVal(u, spec.dataIdx, spec.altIdx, rowIdx);
  if (v == null && spec.endAlt) v = pickValFromEnd(u, 0, rowIdx);
  if (v == null) return null;
  return spec.unit ? `<b>${fmtValue(v, 2)}</b> ${spec.unit}` : `<b>${fmtValue(v, 2)}</b>`;
}

/**
 * wireCursor(instances, getModel, { setCursor, nearestSlotAt })
 *
 * @param {import("uplot").default[]} instances — the uPlot instance array from buildTsFigure
 * @param {()=>object|null} getModel — returns the current model (may be null)
 * @param {{ setCursor: Function, nearestSlotAt: Function }} hooks
 */
export function wireCursor(instances, getModel, { setCursor, nearestSlotAt }) {
  if (!instances || instances.length === 0) return;

  // Build a flat list of (instanceIndex, readout-spec) pairs so the hook
  // on each instance can update all panels cheaply from the same synced idx.
  // The panels in PANEL_READOUT are ordered to match PANEL_ORDER in panels.js
  // (prices, ribbon, mode, solar, soc, load, grid, cost) — strict 1:1 mapping.
  // If there are fewer instances than specs (e.g. during partial build), the
  // trailing entries are paired with the last available instance (safe —
  // all instances share the synced idx so u.cursor.idx is always correct).
  const panelMap = PANEL_READOUT.map((spec, i) => ({
    spec,
    u: i < instances.length ? instances[i] : instances[instances.length - 1],
  }));

  // The readout row container. One <tr> per panel is built lazily on first
  // call; after that we just update the text content.
  const readoutEl = document.getElementById("cursor-readout");
  let rowEls = null;  // array of { labelCell, valueCell } per panel

  function ensureRows() {
    if (!readoutEl) return;
    // Clear any cells left over from a previous wireCursor() call (figure
    // rebuilds re-invoke wireCursor but #cursor-readout is outside the figure
    // and is never reset by the caller, so without this it grows per rebuild).
    readoutEl.replaceChildren();
    rowEls = [];
    for (const { spec } of panelMap) {
      // Decision/Mode are shown as coloured chips in the strip, not as a
      // readout cell — push null so updateReadout skips them.
      if (spec.catField) { rowEls.push(null); continue; }
      const cell = document.createElement("span");
      cell.className = "ro-cell";
      const k = document.createElement("span");
      k.className = "ro-k";
      k.textContent = spec.label;
      const v = document.createElement("span");
      v.className = "ro-v";
      v.textContent = "—";
      cell.appendChild(k);
      cell.appendChild(v);
      readoutEl.appendChild(cell);
      rowEls.push({ valueCell: v });
    }
  }

  function updateReadout(rowIdx) {
    ensureRows();
    if (!rowEls) return;

    for (let i = 0; i < panelMap.length; i++) {
      if (!rowEls[i]) continue;   // ribbon/mode → shown as chips, no cell
      const { spec, u } = panelMap[i];
      const cell = rowEls[i].valueCell;
      if (rowIdx == null) { cell.textContent = "—"; continue; }

      // PRICE panel — show import & export with true c/kWh (1a), prefer
      // realised values (measured), fall back to predicted (future slots).
      if (spec.importIdx != null) {
        const imp = pickVal(u, spec.importIdx, spec.importPredIdx, rowIdx);
        const exp = pickVal(u, spec.exportIdx, spec.exportPredIdx, rowIdx);
        const impStr = imp != null ? fmtValue(imp, 1) : "—";
        const expStr = exp != null ? fmtValue(exp, 1) : "—";
        cell.textContent = `imp ${impStr} / exp ${expStr}`;
        continue;
      }

      // LOAD panel — scan from right to find the envelope.
      if (spec.scanFromEnd) {
        // The last two data columns in loadPanel are measuredEnv, plannedEnv.
        // Try measuredEnv first (second-to-last), then plannedEnv (last).
        const v = pickValFromEnd(u, 1, rowIdx) ?? pickValFromEnd(u, 0, rowIdx);
        cell.textContent = v != null ? `${fmtValue(v, 2)} ${spec.unit}` : "—";
        continue;
      }

      // All other panels — use dataIdx with altIdx fallback; grid also scans
      // from the end for its (conditionally-positioned) planned column.
      let v = pickVal(u, spec.dataIdx, spec.altIdx, rowIdx);
      if (v == null && spec.endAlt) v = pickValFromEnd(u, 0, rowIdx);
      if (v != null) {
        cell.textContent = spec.unit ? `${fmtValue(v, 2)} ${spec.unit}` : fmtValue(v, 2);
      } else {
        cell.textContent = "—";
      }
    }
  }

  function updateChips(cursorIdx) {
    const model = getModel();
    const decisionChip = document.getElementById("decision-chip");
    const modeChip = document.getElementById("mode-chip");

    if (!model || cursorIdx == null) {
      // Fall back to "no data" state — chips use a neutral colour.
      if (decisionChip) {
        decisionChip.textContent = "—";
        decisionChip.style.background = "";
        decisionChip.style.color = "";
      }
      if (modeChip) {
        modeChip.textContent = "—";
        modeChip.style.background = "";
        modeChip.style.color = "";
      }
      return;
    }

    const dec = (model.decisionCats && cursorIdx < model.decisionCats.length)
      ? model.decisionCats[cursorIdx] : null;
    const mode = (model.modeCats && cursorIdx < model.modeCats.length)
      ? model.modeCats[cursorIdx] : null;

    if (decisionChip && dec != null) {
      decisionChip.textContent = DECISION_LABELS[dec] ?? "—";
      decisionChip.style.background = DECISION_COLORS[dec] ?? "";
      decisionChip.style.color = "#e8edf2";
    }
    if (modeChip && mode != null) {
      modeChip.textContent = MODE_LABELS[mode] ?? "—";
      modeChip.style.background = MODE_COLORS[mode] ?? "";
      modeChip.style.color = "#e8edf2";
    }
  }

  // renderAt: drive the readout + chips from the EFFECTIVE cursor slot (now when
  // unpinned, the pinned slot when scrubbing) so they stay populated even when
  // the mouse isn't over a plot — and so a future slot shows PREDICTED prices
  // (pickVal falls back to the predicted column when realised is null there).
  const primary = instances[0];
  function findRowIdx(timeSec) {
    if (timeSec == null || !primary || !primary.data[0] || !primary.data[0].length) return null;
    const xs = primary.data[0];
    const target = Math.round(nearestSlotAt(new Date(timeSec * 1000)).getTime() / 1000);
    let best = 0, bestD = Infinity;
    for (let i = 0; i < xs.length; i++) {
      const d = Math.abs(xs[i] - target);
      if (d < bestD) { bestD = d; best = i; }
    }
    return best;
  }
  function renderAt(effectiveCursorSec) {
    const rowIdx = findRowIdx(effectiveCursorSec);
    updateReadout(rowIdx);
    updateChips(rowIdx);
  }

  // Hover PINS the cursor; the readout is refreshed via renderAt (called by the
  // dashboard on every cursor/snapshot change), so it survives the mouse leaving
  // the plot and reflects "now" when unpinned.
  let lastPinnedIdx = null;
  for (let pi = 0; pi < instances.length; pi++) {
    const u = instances[pi];
    const isPrimary = pi === 0;
    (u.hooks.setCursor ||= []).push(() => {
      if (!isPrimary) return;
      const idx = u.cursor.idx;
      if (idx == null) return;   // mouse left — keep the pinned readout intact
      const tSec = u.posToVal(u.cursor.left, "x");
      if (!Number.isFinite(tSec)) return;
      const slot = nearestSlotAt(new Date(tSec * 1000));
      if (idx !== lastPinnedIdx) {
        lastPinnedIdx = idx;
        setCursor(slot, { pinned: true });  // → dashboard re-renders via renderAt
      }
    });
  }

  // Per-panel on-chart hover tooltip: each panel shows ITS OWN value at the
  // cursor, right next to the crosshair. The setCursor hook fires on every synced
  // instance so all tooltips are filled, but CSS reveals only the hovered panel's
  // (#ts-figure .uplot:hover ~ .panel-tip.has-value) — mirroring the crosshair
  // gating. This complements the top readout strip (which shows all panels at
  // once) by putting the value where the pointer is, even when scrolled down.
  const dpr = (typeof window !== "undefined" && window.devicePixelRatio) || 1;
  for (let i = 0; i < instances.length && i < PANEL_READOUT.length; i++) {
    const u = instances[i];
    const spec = PANEL_READOUT[i];
    const panelEl = u.root && u.root.parentElement;
    if (!panelEl) continue;
    let tip = panelEl.querySelector(".panel-tip");
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "panel-tip";
      panelEl.appendChild(tip);
    }
    const tipEl = tip;
    (u.hooks.setCursor ||= []).push(() => {
      const idx = u.cursor.idx;
      const html = idx == null ? null : panelValueHtml(u, spec, idx, getModel());
      if (!html) { tipEl.classList.remove("has-value"); return; }
      tipEl.innerHTML = html;
      tipEl.classList.add("has-value");
      // u.cursor.left is plot-area-relative; u.bbox.left/dpr is the plot's left
      // offset within the panel (the y-axis gutter). Add it so the tip tracks the
      // cursor. Read width LIVE (a window resize calls setSize without re-running
      // wireCursor, so a cached width would go stale and break the edge flip); the
      // tip is absolutely positioned so this is cheap. Only measure the tip near
      // the right edge, where the flip-left actually matters.
      const overLeft = (u.bbox && u.bbox.left ? u.bbox.left : 0) / dpr;
      const base = u.cursor.left + overLeft;
      const w = panelEl.clientWidth || 1;
      let ttLeft = base + 10;
      if (base > w * 0.6) {
        const ttW = tipEl.offsetWidth || 80;
        if (base + 10 + ttW > w) ttLeft = base - ttW - 8;
      }
      tipEl.style.left = Math.max(0, ttLeft) + "px";
    });
  }

  return { renderAt };
}
