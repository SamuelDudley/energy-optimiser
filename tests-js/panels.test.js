import { describe, it, expect, vi, beforeEach } from "vitest";

// We can't render real uPlot in node (no canvas/DOM), and we don't need to —
// the load-bearing invariant for panels.js is that EVERY uPlot instance is
// constructed with series[].length === data[].length (uPlot throws otherwise),
// and that every panel shares the IDENTICAL model.unionX as its x column.
// Mock uPlot to capture (opts, data, el) per instance and assert the lockstep.

const captured = [];

vi.mock("../src/optimiser/api/static/uplot.esm.js", () => {
  function FakeUPlot(opts, data, el) {
    captured.push({ opts, data, el });
    this.opts = opts;
    this.data = data;
    this.root = el;
    this.setData = vi.fn();
    this.setScale = vi.fn();
    this.setSize = vi.fn();
    this.redraw = vi.fn();
    this.destroy = vi.fn();
  }
  FakeUPlot.sync = () => ({ sub() {}, unsub() {}, pub() {} });
  FakeUPlot.paths = {
    stepped: () => () => ({}),
    linear: () => () => ({}),
    bars: () => () => ({}),
  };
  return { default: FakeUPlot };
});

// Minimal DOM so chart-core / panels import-time code (getComputedStyle, etc.)
// doesn't throw under the node environment.
function makeEl(id) {
  const el = {
    className: "",
    dataset: {},
    style: {},
    clientWidth: 600,
    clientHeight: 80,
    offsetParent: {},
    children: [],
    querySelector(sel) {
      const m = sel.match(/data-panel="([^"]+)"/);
      if (m) return this.children.find((c) => c.dataset.panel === m[1]) || null;
      if (sel.includes("ribbon-tooltip")) return null;
      return null;
    },
    appendChild(c) { this.children.push(c); return c; },
    set innerHTML(_v) { this.children = []; },
    get innerHTML() { return ""; },
  };
  if (id) el.id = id;
  return el;
}

beforeEach(() => {
  captured.length = 0;
  globalThis.window = globalThis;
  globalThis.devicePixelRatio = 1;
  globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  globalThis.document = {
    body: {},
    createElement: () => makeEl(),
  };
  globalThis.getComputedStyle = () => ({ fontFamily: "sans-serif" });
});

function nullCol(n) { return new Array(n).fill(null); }

function makeModel() {
  const unionX = [100, 200, 300, 400];
  const n = unionX.length;
  return {
    unionX,
    price: {
      importRealised: [10, 11, null, 13],
      importPredicted: [10, 11, 12, 13],
      exportRealised: [1, 2, null, 4],
      exportPredicted: [1, 2, 3, 4],
      bands: { importLo: nullCol(n), importHi: nullCol(n), exportLo: nullCol(n), exportHi: nullCol(n) },
    },
    pv: { p50: [0, 1, 2, 1], measured: [0, 1, null, 1], actual: [null, 1, null, 1], bandLo: nullCol(n), bandHi: nullCol(n) },
    soc: { measured: [50, 51, 52, 53], planned: [50, 51, 52, 53], floorPct: 20 },
    load: {
      stacks: [
        { id: "a", color: "#7ee787", cum: [1, 1, 1, 1] },
        { id: "b", color: "#79c0ff", cum: [2, 2, 2, 2] },
      ],
      measuredEnv: [1, 2, null, 2],
      plannedEnv: [1, 2, 3, 2],
    },
    grid: { inverter: [0, -1, null, 1], shelly: [0, -1, null, 1], planned: [0, -1, 1, 1] },
    cost: { realised: [0, 1, null, 2], planned: [0, 1, 2, 2], settled: [null, 1, 2, null] },
    decisionCats: [2, 0, 3, 2],
    modeCats: [0, 2, 4, 0],
    nowSec: 250,
    cursorSec: 300,
    regions: [{ x0: 250, x1: 300, kind: "charge" }],
    thresholdC: 30,
  };
}

describe("panels.js buildTsFigure", () => {
  it("builds 8 panels with series[]/data[] in lockstep on shared unionX", async () => {
    const { buildTsFigure } = await import("../src/optimiser/api/static/panels.js");
    const model = makeModel();

    // root with one child per panel id (the dashboard assembler does this).
    const root = makeEl("ts-figure");
    const ids = ["prices", "ribbon", "mode", "solar", "soc", "load", "grid", "cost"];
    for (const id of ids) {
      const child = makeEl();
      child.dataset.panel = id;
      root.children.push(child);
    }

    const fig = buildTsFigure(root, model);
    expect(fig.instances.length).toBe(8);

    for (const { opts, data } of captured) {
      // series[0] is the x placeholder; data[0] is unionX.
      expect(opts.series.length).toBe(data.length);
      // x column identity: every panel shares the same unionX reference.
      expect(data[0]).toBe(model.unionX);
      // every y column has the same length as unionX.
      for (let k = 1; k < data.length; k++) {
        expect(data[k].length).toBe(model.unionX.length);
      }
    }

    // Spot-check membership: PV-actual present -> price has 8 series (+x=9),
    // pv has band(2)+p50+measured+actual=5 (+x=6).
    const byPanel = {};
    captured.forEach((c, i) => { byPanel[ids[i]] = c; });
    expect(byPanel.prices.opts.series.length).toBe(9);   // x + 4 band + 4 line
    expect(byPanel.solar.opts.series.length).toBe(6);    // x + 2 band + p50 + measured + actual
    expect(byPanel.grid.opts.series.length).toBe(4);     // x + inverter + shelly + planned
    expect(byPanel.cost.opts.series.length).toBe(4);     // x + realised + planned + settled
    expect(byPanel.load.opts.series.length).toBe(5);     // x + 2 stacks + 2 envelopes
  });

  it("update(model) keeps series/data lockstep via setData", async () => {
    const { buildTsFigure } = await import("../src/optimiser/api/static/panels.js");
    const model = makeModel();
    const root = makeEl("ts-figure");
    const ids = ["prices", "ribbon", "mode", "solar", "soc", "load", "grid", "cost"];
    for (const id of ids) {
      const child = makeEl();
      child.dataset.panel = id;
      root.children.push(child);
    }
    const fig = buildTsFigure(root, model);
    fig.update(makeModel());
    fig.instances.forEach((u, i) => {
      expect(u.setData).toHaveBeenCalled();
      const [data] = u.setData.mock.calls[0];
      // data length must equal the built series length for that instance.
      expect(data.length).toBe(captured[i].opts.series.length);
    });
  });

  it("omits conditional series when data absent (no actual/shelly/settled)", async () => {
    const { buildTsFigure } = await import("../src/optimiser/api/static/panels.js");
    const model = makeModel();
    model.pv.actual = null;
    model.grid.shelly = null;
    model.cost.settled = null;
    const root = makeEl("ts-figure");
    const ids = ["prices", "ribbon", "mode", "solar", "soc", "load", "grid", "cost"];
    for (const id of ids) {
      const child = makeEl();
      child.dataset.panel = id;
      root.children.push(child);
    }
    buildTsFigure(root, model);
    const byPanel = {};
    captured.forEach((c, i) => { byPanel[ids[i]] = c; });
    expect(byPanel.solar.opts.series.length).toBe(5);  // no actual markers
    expect(byPanel.grid.opts.series.length).toBe(3);   // no shelly
    expect(byPanel.cost.opts.series.length).toBe(3);   // no settled
    // lockstep still holds everywhere.
    for (const { opts, data } of captured) {
      expect(opts.series.length).toBe(data.length);
    }
  });
});
