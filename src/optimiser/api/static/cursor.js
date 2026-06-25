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
  { panel: "grid",     label: "Grid",     unit: "kW",    dataIdx: 1, altIdx: null },
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

// Pick the best non-null value at idx from u.data. If idx is null/undefined,
// return null.  Falls back to altIdx when the primary is null (forecast/planned).
function pickVal(u, idx, altIdx) {
  if (idx == null || u.cursor.idx == null) return null;
  const v = u.data[idx]?.[u.cursor.idx];
  if (v != null && Number.isFinite(v)) return v;
  if (altIdx != null) {
    const v2 = u.data[altIdx]?.[u.cursor.idx];
    if (v2 != null && Number.isFinite(v2)) return v2;
  }
  return null;
}

// Scan u.data from the right to find the last non-band series column that
// has a value at cursor.idx (used for the LOAD envelope which is always last
// after a variable-length set of stack columns).
function pickValFromEnd(u, skipLast = 0) {
  if (u.cursor.idx == null) return null;
  const len = u.data.length;
  for (let i = len - 1 - skipLast; i >= 1; i--) {
    const v = u.data[i]?.[u.cursor.idx];
    if (v != null && Number.isFinite(v)) return v;
  }
  return null;
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
    if (rowEls || !readoutEl) return;
    rowEls = [];
    for (const { spec } of panelMap) {
      const tr = document.createElement("tr");
      const td1 = document.createElement("td");
      const td2 = document.createElement("td");
      td1.className = "readout-label";
      td2.className = "readout-value";
      td1.textContent = spec.label;
      td2.textContent = "—";
      tr.appendChild(td1);
      tr.appendChild(td2);
      readoutEl.appendChild(tr);
      rowEls.push({ labelCell: td1, valueCell: td2 });
    }
  }

  function updateReadout(cursorIdx) {
    ensureRows();
    if (!rowEls) return;

    const model = getModel();

    for (let i = 0; i < panelMap.length; i++) {
      const { spec, u } = panelMap[i];
      const cell = rowEls[i].valueCell;
      if (cursorIdx == null) { cell.textContent = "—"; continue; }

      // Panels driven by category arrays (ribbons) — no numeric value.
      if (spec.catField) {
        const cats = model ? (spec.catField === "decision" ? model.decisionCats : model.modeCats) : null;
        const cat = (cats && cursorIdx < cats.length) ? cats[cursorIdx] : null;
        const labels = spec.catField === "decision" ? DECISION_LABELS : MODE_LABELS;
        cell.textContent = cat != null ? (labels[cat] ?? "—") : "—";
        continue;
      }

      // PRICE panel — show import & export with true c/kWh (1a), prefer
      // realised values (measured), fall back to predicted.
      if (spec.importIdx != null) {
        const imp = pickVal(u, spec.importIdx, spec.importPredIdx);
        const exp = pickVal(u, spec.exportIdx, spec.exportPredIdx);
        const impStr = imp != null ? fmtValue(imp, 1) : "—";
        const expStr = exp != null ? fmtValue(exp, 1) : "—";
        cell.textContent = `imp ${impStr} / exp ${expStr}`;
        continue;
      }

      // LOAD panel — scan from right to find the envelope.
      if (spec.scanFromEnd) {
        // The last two data columns in loadPanel are measuredEnv, plannedEnv.
        // Try measuredEnv first (second-to-last), then plannedEnv (last).
        const v = pickValFromEnd(u, 1) ?? pickValFromEnd(u, 0);
        cell.textContent = v != null ? `${fmtValue(v, 2)} ${spec.unit}` : "—";
        continue;
      }

      // All other panels — use dataIdx with altIdx fallback.
      const v = pickVal(u, spec.dataIdx, spec.altIdx);
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

  // Install the hook on every instance. Each instance fires independently
  // but they all share the same synced cursor.idx (via uPlot.sync).
  // We only need ONE instance to drive the pin + readout — we pick the
  // first one; the rest just guard against double-pin.
  let lastPinnedIdx = null;

  for (let pi = 0; pi < instances.length; pi++) {
    const u = instances[pi];
    const isPrimary = pi === 0;

    (u.hooks.setCursor ||= []).push(() => {
      const idx = u.cursor.idx;

      // Primary instance drives the pin + full readout update.
      if (isPrimary) {
        if (idx != null) {
          // Compute the wall-clock slot from the cursor position.
          const tSec = u.posToVal(u.cursor.left, "x");
          if (Number.isFinite(tSec)) {
            const slot = nearestSlotAt(new Date(tSec * 1000));
            // Avoid redundant setCursor calls when the index didn't change.
            if (idx !== lastPinnedIdx) {
              lastPinnedIdx = idx;
              setCursor(slot, { pinned: true });
            }
          }
        }
        // When idx is null (mouse left the plot), do nothing — cursor stays
        // pinned at the last slot (matches the no-unhover behaviour in the spec).

        // Always update the readout to reflect current idx (may be null).
        updateReadout(idx);
        updateChips(idx);
      }
    });
  }
}
