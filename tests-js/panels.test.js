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

  // Helper: build once, return panels keyed by id.
  async function buildByPanel(mutate) {
    const { buildTsFigure } = await import("../src/optimiser/api/static/panels.js");
    const model = makeModel();
    if (mutate) mutate(model);
    const root = makeEl("ts-figure");
    const ids = ["prices", "ribbon", "mode", "solar", "soc", "load", "grid", "cost"];
    for (const id of ids) { const c = makeEl(); c.dataset.panel = id; root.children.push(c); }
    buildTsFigure(root, model);
    const byPanel = {};
    captured.forEach((c, i) => { byPanel[ids[i]] = c; });
    return byPanel;
  }

  it("arcsinh y-axes supply their own label filter + stringifying values", async () => {
    // Regression: for distr:4 (asinh) uPlot installs a default log filter that
    // blanks any split whose mantissa isn't "nice" — it collapsed the price axis
    // [5,10,15,20,40] to only "10","15". namedAxis must supply its OWN filter
    // (which wins over the distr default) so the niceness-blanking is gone; that
    // filter re-adds overlap thinning (see thinByPixelGap test).
    const byPanel = await buildByPanel();
    for (const id of ["prices", "grid", "cost"]) {
      const yAxis = byPanel[id].opts.axes[1];
      expect(byPanel[id].opts.scales.y.distr).toBe(4);   // asinh scale
      expect(typeof yAxis.filter).toBe("function");
      // With well-spaced positions, the filter keeps EVERY named split (no
      // niceness-blanking). Fake u.valToPos returns 40px-apart positions.
      const fakeU = { valToPos: (v) => v * 40 };
      const sample = [-50, -10, 0, 5, 15, 20, 200];
      expect(yAxis.filter(fakeU, sample)).toEqual(sample);
      // values stringifies finite splits and blanks thinned (null) ones.
      expect(yAxis.values(null, [5, null, 20])).toEqual(["5", "", "20"]);
    }
  });

  it("thinByPixelGap blanks labels closer than the min gap, keeps spaced ones", async () => {
    const { thinByPixelGap } = await import("../src/optimiser/api/static/panels.js");
    // Ascending splits; positions bunch near zero (6-12px), 50 is far. minGap=13.
    const splits = [-20, -10, 0, 10, 20, 50];
    const positions = [100, 94, 88, 80, 66, 20];
    // -20 anchors; -10(6px) & 0(12px) blanked; 10(20px from -20) kept; 20(14px
    // from 10) kept; 50 far kept. → no two kept labels within 13px.
    expect(thinByPixelGap(splits, positions, 13)).toEqual([-20, null, null, 10, 20, 50]);
    // Well-spaced input keeps everything; nulls pass through as nulls.
    expect(thinByPixelGap([1, 2, 3], [60, 40, 20], 13)).toEqual([1, 2, 3]);
    expect(thinByPixelGap([1, null, 3], [60, 50, 40], 13)).toEqual([1, null, 3]);
  });

  it("decision/mode ribbons disable the horizontal cursor crosshair", async () => {
    const byPanel = await buildByPanel();
    expect(byPanel.ribbon.opts.cursor.y).toBe(false);
    expect(byPanel.mode.opts.cursor.y).toBe(false);
    // Data panels keep the crosshair available (CSS gates it to :hover).
    expect(byPanel.soc.opts.cursor.y).not.toBe(false);
    expect(byPanel.prices.opts.cursor.y).not.toBe(false);
  });

  it("price panel has a top, single-line time axis (no clipping date row)", async () => {
    const byPanel = await buildByPanel();
    const xAxis = byPanel.prices.opts.axes[0];
    expect(xAxis.side).toBe(0);                  // top
    expect(typeof xAxis.values).toBe("function");
    // Single line: the formatter never emits a second-tier date (no newline).
    const out = xAxis.values(null, [0, 3 * 3600, 6 * 3600]);
    expect(out.every((s) => typeof s === "string" && !s.includes("\n"))).toBe(true);
    // The bottom (cost) axis keeps uPlot's default two-tier time+date values.
    expect(byPanel.cost.opts.axes[0].values).toBeUndefined();
  });
});
