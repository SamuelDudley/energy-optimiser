import { describe, it, expect } from "vitest";
import { buildUnionX, alignSeries } from "../src/optimiser/api/static/timeline.js";

describe("buildUnionX", () => {
  it("merges, sorts ascending, dedupes", () => {
    expect(buildUnionX([30, 10, 20], [20, 40], [])).toEqual([10, 20, 30, 40]);
  });
  it("ignores null/NaN entries", () => {
    expect(buildUnionX([10, null, NaN, 20])).toEqual([10, 20]);
  });
});

describe("alignSeries", () => {
  const x = [10, 20, 30, 40];
  const pts = [{ t: 40, v: 4 }, { t: 20, v: 2 }]; // unsorted, sparse
  it("places values at matching x, null elsewhere", () => {
    expect(alignSeries(x, pts, (p) => p.t, (p) => p.v)).toEqual([null, 2, null, 4]);
  });
  it("passes through nulls from getVal", () => {
    expect(alignSeries([10], [{ t: 10, v: null }], (p) => p.t, (p) => p.v)).toEqual([null]);
  });
});
