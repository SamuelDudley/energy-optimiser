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

  it("load panel scans from the end for the envelope", () => {
    // [x, stackA, stackB, measuredEnv(3.5), plannedEnv] → scanFromEnd skipLast=1
    const cols = [[0], [1], [2], [3.5], [4]];
    const spec = { scanFromEnd: true, unit: "kW" };
    expect(panelValueHtml(fakeU(cols), spec, 0, null)).toContain("3.50");
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
