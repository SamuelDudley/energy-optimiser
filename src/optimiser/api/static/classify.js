// Classification helpers for decision and mode categories.
// Extracted from dashboard.js for reusability and testing.

// Slot semantics — must stay in sync with optimiser/lp/constants.py.
export const DEADBAND_KW = 0.1;
export const MODE_SWITCH_HYSTERESIS_KW = 0.05;

// Decision categories driving the ribbon.
export const DECISION = {
  CHARGE_GRID: 0,
  CHARGE_PV:   1,
  IDLE:        2,
  DISCHARGE:   3,
  UNKNOWN:     4,
};
export const DECISION_COLORS = {
  [DECISION.CHARGE_GRID]: "#d29922", // amber — pay to fill
  [DECISION.CHARGE_PV]:   "#3fb950", // green — soak free PV
  [DECISION.IDLE]:        "#444c56", // dark gray — hold
  [DECISION.DISCHARGE]:   "#bc8cff", // purple — earn
  [DECISION.UNKNOWN]:     "#21262d", // near-bg — no data
};
export const DECISION_LABELS = {
  [DECISION.CHARGE_GRID]: "charge (grid)",
  [DECISION.CHARGE_PV]:   "charge (PV)",
  [DECISION.IDLE]:        "idle",
  [DECISION.DISCHARGE]:   "discharge",
  [DECISION.UNKNOWN]:     "—",
};

// Inverter EMS mode → ribbon category. Past from telemetry.ems_mode,
// future inferred from the slot's signed battery_kw + grid-share. Modes
// 2 (self-consume) and 2-charge (PV-dominant charge with adaptive trim
// on 40032) write the same register but represent different intents —
// disambiguated using planner_action (past) or grid_to_battery (future).
// Indices kept dense (0..N-1) for the heatmap colorscale.
export const MODE = {
  M2_IDLE:    0,  // 2 + self-consume
  M2_CHARGE:  1,  // 2 + PV-dominant charge (adaptive trim)
  M3_CHARGE:  2,  // 3 — grid-first charge
  M5_DIS_PV:  3,  // 5 — discharge with PV producing
  M6_DIS_ESS: 4,  // 6 — pure ESS discharge
  M0_STANDBY: 5,  // 0 — standby / fallback target
  UNKNOWN:    6,
};
export const MODE_COLORS = {
  [MODE.M2_IDLE]:    "#3a3f47", // muted slate — passive
  [MODE.M2_CHARGE]:  "#3fb950", // green — soak free PV
  [MODE.M3_CHARGE]:  "#d29922", // amber — pay to fill
  [MODE.M5_DIS_PV]:  "#bc8cff", // purple — earn (PV present)
  [MODE.M6_DIS_ESS]: "#8957e5", // deeper purple — earn (no PV)
  [MODE.M0_STANDBY]: "#6e7681", // gray — held
  [MODE.UNKNOWN]:    "#21262d", // near-bg
};
export const MODE_LABELS = {
  [MODE.M2_IDLE]:    "mode 2 · self-consume",
  [MODE.M2_CHARGE]:  "mode 2 · PV charge",
  [MODE.M3_CHARGE]:  "mode 3 · grid charge",
  [MODE.M5_DIS_PV]:  "mode 5 · discharge (PV)",
  [MODE.M6_DIS_ESS]: "mode 6 · discharge (ESS)",
  [MODE.M0_STANDBY]: "mode 0 · standby",
  [MODE.UNKNOWN]:    "—",
};

// Derive per-slot decision from a SlotDecision object
export function decisionFor(slot) {
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
export function modeFromTelemetry(row) {
  // Source of truth is `planner_action` — that's the commanded dispatch
  // mode the LP picked for this tick. `ems_mode` in telemetry is the
  // inverter's run-state register (e.g. 7 = "discharging"), not the work
  // mode we wrote, so it can't be decoded with the same table as the LP
  // dispatch. Fall back to ems_mode only when planner_action is absent.
  if (!row) return MODE.UNKNOWN;
  const a = (row.planner_action || "").toLowerCase();
  if (a === "charge_grid")   return MODE.M3_CHARGE;
  if (a === "charge_pv")     return MODE.M2_CHARGE;
  if (a === "discharge_ess") return MODE.M6_DIS_ESS;
  if (a === "discharge_pv")  return MODE.M5_DIS_PV;
  if (a === "self_consume" || a === "standby") return MODE.M2_IDLE;
  const m = row.ems_mode;
  if (m === 0) return MODE.M0_STANDBY;
  if (m === 2) return MODE.M2_IDLE;
  if (m === 3) return MODE.M3_CHARGE;
  if (m === 5) return MODE.M5_DIS_PV;
  if (m === 6) return MODE.M6_DIS_ESS;
  return MODE.UNKNOWN;
}

export function modeFromSlot(slot) {
  // Mirror dispatch_from_slot's mode pick (lp/dispatch.py). Future PV is
  // unknown — for discharge, assume mode 6 if planned PV is below the
  // threshold the dispatcher uses (~0.2 kW), else mode 5.
  if (!slot) return MODE.UNKNOWN;
  const b = slot.battery_kw;
  if (b == null || !Number.isFinite(b)) return MODE.UNKNOWN;
  if (Math.abs(b) < DEADBAND_KW) return MODE.M2_IDLE;
  if (b > 0) {
    const g = slot.grid_to_battery_kw ?? 0;
    const p = slot.pv_to_battery_kw ?? Math.max(0, b - g);
    if (g > p + MODE_SWITCH_HYSTERESIS_KW) return MODE.M3_CHARGE;
    return MODE.M2_CHARGE;
  }
  // Discharge — mode 5 if PV producing, else mode 6.
  const pv = slot.pv_kw ?? slot.pv_to_house_kw ?? 0;
  return pv > 0.2 ? MODE.M5_DIS_PV : MODE.M6_DIS_ESS;
}

export function decisionFromTelemetry(row) {
  const a = row.planner_action;
  if (!a) return DECISION.UNKNOWN;
  if (a === "charge_grid") return DECISION.CHARGE_GRID;
  if (a === "charge_pv")   return DECISION.CHARGE_PV;
  if (a === "discharge_pv" || a === "discharge_ess") return DECISION.DISCHARGE;
  if (a === "self_consume" || a === "standby") return DECISION.IDLE;
  return DECISION.UNKNOWN;
}
