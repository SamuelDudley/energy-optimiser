import { describe, it, expect } from "vitest";
import { topKExtrema } from "../src/optimiser/api/static/spike-detect.js";

// Helper: build xs/ys of length n with a bump at index i.
function flat(n, val = 5) { return new Array(n).fill(val); }

// ── basic peak selection ────────────────────────────────────────────────────

describe("topKExtrema – peaks", () => {
  it("returns the single peak above severityC", () => {
    const xs = [0, 1, 2, 3, 4];
    const ys = [5, 5, 200, 5, 5];
    const result = topKExtrema(xs, ys, { k: 3, viewMin: 0, viewMax: 4, severityC: 100 });
    expect(result.peaks).toHaveLength(1);
    expect(result.peaks[0]).toMatchObject({ x: 2, y: 200 });
  });

  it("selects top-K peaks by |y| when multiple exceed severityC", () => {
    // Five bumps above 100; we want only the top 3 by magnitude.
    const xs = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14];
    const ys = [5, 200, 5, 5, 500, 5, 5, 300, 5, 5, 150, 5, 5, 120, 5];
    //         idx 1=200, 4=500, 7=300, 10=150, 13=120 — all local maxima > 100
    const result = topKExtrema(xs, ys, { k: 3, viewMin: 0, viewMax: 14, severityC: 100 });
    expect(result.peaks).toHaveLength(3);
    const picked = result.peaks.map((p) => p.y);
    expect(picked).toContain(500);
    expect(picked).toContain(300);
    expect(picked).toContain(200);
    // 150 and 120 should NOT be picked (k=3)
    expect(picked).not.toContain(150);
  });

  it("severity filter excludes small bumps below severityC", () => {
    const xs = [0, 1, 2, 3, 4];
    const ys = [5, 50, 5, 80, 5];   // 50 and 80 are local maxima but below 100
    const result = topKExtrema(xs, ys, { k: 3, viewMin: 0, viewMax: 4, severityC: 100 });
    expect(result.peaks).toHaveLength(0);
  });

  it("only considers points within viewMin..viewMax (inclusive)", () => {
    const xs = [0, 1, 2, 3, 4, 5, 6];
    const ys = [5, 500, 5, 5, 5, 400, 5];  // peaks at idx 1 and 5
    // Window excludes idx 1 (x=1 < viewMin=2) and includes idx 5 (x=5)
    const result = topKExtrema(xs, ys, { k: 3, viewMin: 2, viewMax: 6, severityC: 100 });
    expect(result.peaks).toHaveLength(1);
    expect(result.peaks[0].x).toBe(5);
  });
});

// ── separation guard ────────────────────────────────────────────────────────

describe("topKExtrema – separation guard", () => {
  it("skips a near neighbour within minSepPx when valToPx is provided", () => {
    // Two peaks 5 units apart; minSepPx=20 and 1 unit = 10 px → 5 units = 50 px > 20 px
    // → both qualify. Reduce valToPx so 5 units = 10 px < 20 px → only one survives.
    const xs = [0, 2, 4, 6, 8];
    const ys = [5, 500, 5, 400, 5];   // peaks at x=2 and x=6
    // valToPx: x → x * 2px (so gap = 4 units = 8 px < 20 px → skip second)
    const valToPx = (x) => x * 2;
    const result = topKExtrema(xs, ys, {
      k: 3, viewMin: 0, viewMax: 8, severityC: 100, minSepPx: 20, valToPx,
    });
    // Only the larger (500) should survive; 400 is within 20px of 500.
    expect(result.peaks).toHaveLength(1);
    expect(result.peaks[0].y).toBe(500);
  });

  it("allows a near neighbour when its px gap exceeds minSepPx", () => {
    const xs = [0, 2, 4, 6, 8];
    const ys = [5, 500, 5, 400, 5];
    // valToPx: x → x * 20px (gap = 4 units * 20 = 80 px > 30 → both survive)
    const valToPx = (x) => x * 20;
    const result = topKExtrema(xs, ys, {
      k: 3, viewMin: 0, viewMax: 8, severityC: 100, minSepPx: 30, valToPx,
    });
    expect(result.peaks).toHaveLength(2);
  });

  it("falls back to x-value separation when valToPx is absent", () => {
    // minSepPx without valToPx → skip if |x_candidate - x_picked| < minSepPx
    const xs = [0, 1, 2, 10, 11, 12];
    //          peaks at x=1(500) and x=11(400); gap=10 x-units
    const ys = [5, 500, 5, 5, 400, 5];
    // minSepPx=5 in x-space: 10 > 5 → both survive
    const result1 = topKExtrema(xs, ys, {
      k: 3, viewMin: 0, viewMax: 12, severityC: 100, minSepPx: 5,
    });
    expect(result1.peaks).toHaveLength(2);

    // minSepPx=15 in x-space: 10 < 15 → only 500 survives
    const result2 = topKExtrema(xs, ys, {
      k: 3, viewMin: 0, viewMax: 12, severityC: 100, minSepPx: 15,
    });
    expect(result2.peaks).toHaveLength(1);
    expect(result2.peaks[0].y).toBe(500);
  });
});

// ── symmetric troughs ───────────────────────────────────────────────────────

describe("topKExtrema – troughs", () => {
  it("returns local minima below -severityC", () => {
    const xs = [0, 1, 2, 3, 4];
    const ys = [5, -50, 5, 5, 5];
    const result = topKExtrema(xs, ys, { k: 3, viewMin: 0, viewMax: 4, severityC: 30 });
    expect(result.troughs).toHaveLength(1);
    expect(result.troughs[0]).toMatchObject({ x: 1, y: -50 });
  });

  it("selects top-K troughs by |y| descending", () => {
    const xs = [0, 1, 2, 3, 4, 5, 6, 7, 8];
    const ys = [5, -60, 5, 5, -90, 5, 5, -45, 5];
    const result = topKExtrema(xs, ys, { k: 2, viewMin: 0, viewMax: 8, severityC: 30 });
    expect(result.troughs).toHaveLength(2);
    const picked = result.troughs.map((t) => t.y);
    expect(picked).toContain(-90);
    expect(picked).toContain(-60);
    expect(picked).not.toContain(-45);
  });

  it("trough severity uses same threshold but negative: must be < -severityC", () => {
    const xs = [0, 1, 2];
    const ys = [5, -10, 5];  // -10 is NOT below -30 threshold
    const result = topKExtrema(xs, ys, { k: 3, viewMin: 0, viewMax: 2, severityC: 30 });
    expect(result.troughs).toHaveLength(0);
  });
});

// ── null handling ───────────────────────────────────────────────────────────

describe("topKExtrema – null handling", () => {
  it("skips null values in ys without crashing", () => {
    const xs = [0, 1, 2, 3, 4];
    const ys = [null, 500, null, -60, null];
    // x=1 is a peak (neighbours are null, treated as skipped)
    // x=3 is a trough
    const result = topKExtrema(xs, ys, { k: 3, viewMin: 0, viewMax: 4, severityC: 30 });
    expect(result.peaks).toHaveLength(1);
    expect(result.peaks[0]).toMatchObject({ x: 1, y: 500 });
    expect(result.troughs).toHaveLength(1);
    expect(result.troughs[0]).toMatchObject({ x: 3, y: -60 });
  });

  it("returns empty peaks/troughs when all ys are null", () => {
    const xs = [0, 1, 2];
    const ys = [null, null, null];
    const result = topKExtrema(xs, ys, { k: 3, viewMin: 0, viewMax: 2, severityC: 10 });
    expect(result.peaks).toHaveLength(0);
    expect(result.troughs).toHaveLength(0);
  });
});

// ── k cap ───────────────────────────────────────────────────────────────────

describe("topKExtrema – k cap", () => {
  it("caps peaks and troughs independently at k", () => {
    // 5 peaks + 5 troughs, k=2 → at most 2 peaks and 2 troughs
    const xs = Array.from({ length: 20 }, (_, i) => i);
    const ys = xs.map((x) => {
      if ([1, 4, 7, 10, 13].includes(x)) return 100 + x * 10;   // peaks
      if ([2, 5, 8, 11, 14].includes(x)) return -(100 + x * 10); // troughs
      return 0;
    });
    const result = topKExtrema(xs, ys, { k: 2, viewMin: 0, viewMax: 19, severityC: 50 });
    expect(result.peaks.length).toBeLessThanOrEqual(2);
    expect(result.troughs.length).toBeLessThanOrEqual(2);
  });
});

// ── index field ─────────────────────────────────────────────────────────────

describe("topKExtrema – returned shape", () => {
  it("each extremum has x, y, and idx fields", () => {
    const xs = [0, 1, 2, 3, 4];
    const ys = [5, 5, 500, 5, 5];
    const result = topKExtrema(xs, ys, { k: 1, viewMin: 0, viewMax: 4, severityC: 100 });
    expect(result.peaks[0]).toHaveProperty("x");
    expect(result.peaks[0]).toHaveProperty("y");
    expect(result.peaks[0]).toHaveProperty("idx");
    expect(result.peaks[0].idx).toBe(2);
  });
});
