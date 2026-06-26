import { describe, it, expect } from "vitest";
import { panelValueHtml } from "../src/optimiser/api/static/cursor.js";
import { DECISION_LABELS } from "../src/optimiser/api/static/classify.js";

// panelValueHtml(u, spec, rowIdx, model) — the per-panel on-chart hover tooltip
// content. u only needs `.data` (array of columns); pure logic, no DOM.
const fakeU = (cols) => ({ data: cols });

describe("panelValueHtml", () => {
  it("price panel shows import & export in c/kWh (realised preferred, in order)", () => {
    // [x, 4 bands, importRealised(5), importPred(6), exportRealised(7), exportPred(8)]
    const cols = [[0], null, null, null, null, [24.7], [24.0], [9.5], [9.0]];
    const spec = { importIdx: 5, exportIdx: 7, importPredIdx: 6, exportPredIdx: 8 };
    const html = panelValueHtml(fakeU(cols), spec, 0, null);
    // Assert ORDER (imp then exp), not just membership — guards an idx swap.
    expect(html).toMatch(/imp\s*<b>24\.7<\/b>\s*\/\s*exp\s*<b>9\.5<\/b>/);
    expect(html).toContain("c/kWh");
  });

  it("price falls back to predicted when realised is null (future slot)", () => {
    const cols = [[0], null, null, null, null, [null], [22.0], [null], [8.0]];
    const spec = { importIdx: 5, exportIdx: 7, importPredIdx: 6, exportPredIdx: 8 };
    const html = panelValueHtml(fakeU(cols), spec, 0, null);
    expect(html).toMatch(/imp\s*<b>22\.0<\/b>\s*\/\s*exp\s*<b>8\.0<\/b>/);
  });

  it("numeric panel shows value + unit (2 dp)", () => {
    const cols = [[0], [28]]; // soc measured at data[1]
    const spec = { dataIdx: 1, altIdx: 2, unit: "%" };
    const html = panelValueHtml(fakeU(cols), spec, 0, null);
    expect(html).toContain("28.00");
    expect(html).toContain("%");
  });

  it("load panel uses the measured envelope (past slot), not a stack", () => {
    // [x, stackA, stackB, measuredEnv(3.5), plannedEnv(4)]
    const cols = [[0], [1], [2], [3.5], [4]];
    const spec = { scanFromEnd: true, unit: "kW" };
    const html = panelValueHtml(fakeU(cols), spec, 0, null);
    expect(html).toContain("3.50");
    expect(html).not.toContain("4.00");
  });

  it("load panel shows the PLANNED envelope (not a stack) in the future", () => {
    // future: measuredEnv is null → must return plannedEnv(1.38), NOT the stack
    // (0.90). This is the regression: scanFromEnd fell into the stack column.
    const cols = [[0], [0.9], [null], [1.38]]; // [x, stack(0.9), measuredEnv(null), plannedEnv(1.38)]
    const spec = { scanFromEnd: true, unit: "kW" };
    const html = panelValueHtml(fakeU(cols), spec, 0, null);
    expect(html).toContain("1.38");
    expect(html).not.toContain("0.90");
  });

  it("load panel hides (null over wrong) when both envelopes are null even if a stack has a value", () => {
    // grid-sensor-offline past slot: house load unknown so both envelopes are
    // null, but a managed-load stack still carries its value. Showing the stack
    // as the TOTAL would understate true load — return null instead.
    const cols = [[0], [0.9], [null], [null]];
    const spec = { scanFromEnd: true, unit: "kW" };
    expect(panelValueHtml(fakeU(cols), spec, 0, null)).toBeNull();
  });

  it("normalises negative zero (tiny negative planned cost) to 0.00, not -0.00", () => {
    const cols = [[0], [null], [-0.0009]]; // realised null → planned -0.0009 → "-0.00"
    const spec = { dataIdx: 1, altIdx: 2, unit: "c/h" };
    const html = panelValueHtml(fakeU(cols), spec, 0, null);
    expect(html).toContain("0.00");
    expect(html).not.toContain("-0.00");
  });

  it("decision ribbon shows the category LABEL, not a raw number", () => {
    const spec = { catField: "decision" };
    const html = panelValueHtml(fakeU([[0]]), spec, 0, { decisionCats: [2] });
    expect(html).toContain(DECISION_LABELS[2]);
  });

  it("returns null when there is no value (null data, null idx, missing cat)", () => {
    const numSpec = { dataIdx: 1, altIdx: 2, unit: "%" };
    expect(panelValueHtml(fakeU([[0], [null]]), numSpec, 0, null)).toBeNull();
    expect(panelValueHtml(fakeU([[0], [5]]), numSpec, null, null)).toBeNull();
    expect(panelValueHtml(fakeU([[0]]), { catField: "mode" }, 0, { modeCats: [] })).toBeNull();
  });
});
