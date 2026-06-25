import { describe, it, expect } from "vitest";
import { bandColumns } from "../src/optimiser/api/static/bands.js";

const getSec = (p) => p.s;
const x = [10, 20, 30, 40, 50];

describe("bandColumns", () => {
  it("aligns lo/hi onto unionX", () => {
    const iv = [
      { s: 10, lo: 1, hi: 5 }, { s: 20, lo: 2, hi: 6 },
      { s: 30, lo: 3, hi: 7 }, { s: 40, lo: 4, hi: 8 }, { s: 50, lo: 5, hi: 9 },
    ];
    expect(bandColumns(iv, "lo", "hi", x, getSec)).toEqual({ lo: [1,2,3,4,5], hi: [5,6,7,8,9] });
  });
  it("nulls BOTH bounds where either is null", () => {
    const iv = [{ s: 10, lo: 1, hi: null }, { s: 20, lo: 2, hi: 6 }, { s: 30, lo: 3, hi: 7 }];
    const out = bandColumns(iv, "lo", "hi", [10,20,30], getSec);
    expect(out.lo[0]).toBeNull(); expect(out.hi[0]).toBeNull();
  });
  it("drops contiguous non-null runs shorter than 2", () => {
    // only index 30 has both bounds → a 1-long run → dropped
    const iv = [{ s: 10, lo: null, hi: null }, { s: 20, lo: null, hi: 6 }, { s: 30, lo: 3, hi: 7 }, { s: 40, lo: null, hi: null }];
    const out = bandColumns(iv, "lo", "hi", x.slice(0,4), getSec);
    expect(out.lo).toEqual([null, null, null, null]);
  });
});
