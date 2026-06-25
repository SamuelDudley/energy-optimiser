import { describe, it, expect } from "vitest";
import {
  DECISION, MODE, decisionFor, decisionFromTelemetry, modeFromSlot, modeFromTelemetry,
} from "../src/optimiser/api/static/classify.js";

describe("decisionFor", () => {
  it("idle within deadband", () => expect(decisionFor({ battery_kw: 0.05 })).toBe(DECISION.IDLE));
  it("discharge when negative", () => expect(decisionFor({ battery_kw: -2 })).toBe(DECISION.DISCHARGE));
  it("charge-grid when grid dominates", () =>
    expect(decisionFor({ battery_kw: 3, grid_to_battery_kw: 2.5, pv_to_battery_kw: 0.5 })).toBe(DECISION.CHARGE_GRID));
  it("charge-pv when pv dominates", () =>
    expect(decisionFor({ battery_kw: 3, grid_to_battery_kw: 0.2, pv_to_battery_kw: 2.8 })).toBe(DECISION.CHARGE_PV));
  it("unknown on null/non-finite", () => {
    expect(decisionFor(null)).toBe(DECISION.UNKNOWN);
    expect(decisionFor({ battery_kw: null })).toBe(DECISION.UNKNOWN);
  });
});

describe("modeFromSlot", () => {
  it("idle within deadband", () => expect(modeFromSlot({ battery_kw: 0 })).toBe(MODE.M2_IDLE));
  it("discharge mode 5 with PV", () => expect(modeFromSlot({ battery_kw: -1, pv_kw: 1 })).toBe(MODE.M5_DIS_PV));
  it("discharge mode 6 without PV", () => expect(modeFromSlot({ battery_kw: -1, pv_kw: 0 })).toBe(MODE.M6_DIS_ESS));
});

describe("telemetry decoders", () => {
  it("planner_action wins", () => {
    expect(modeFromTelemetry({ planner_action: "charge_grid" })).toBe(MODE.M3_CHARGE);
    expect(decisionFromTelemetry({ planner_action: "discharge_ess" })).toBe(DECISION.DISCHARGE);
  });
  it("falls back to ems_mode when action absent", () =>
    expect(modeFromTelemetry({ ems_mode: 6 })).toBe(MODE.M6_DIS_ESS));
});
