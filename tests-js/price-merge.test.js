import { describe, it, expect } from "vitest";
import { mergePriceForecasts, pickPriceAt, coalesce } from "../src/optimiser/api/static/price-merge.js";

const fut = [
  { start: "2026-06-25T10:00:00Z", end: "2026-06-25T10:05:00Z", forecast_predicted: 12, export_forecast_predicted: 3 },
  { start: "2026-06-25T10:30:00Z", end: "2026-06-25T11:00:00Z", forecast_predicted: 50, export_forecast_predicted: 9 },
  // non-monotonic: a 5-min entry emitted after a later 30-min entry
  { start: "2026-06-25T10:05:00Z", end: "2026-06-25T10:10:00Z", forecast_predicted: 14, export_forecast_predicted: 4 },
];
const past = [
  { interval_start: "2026-06-25T09:55:00Z", interval_end: "2026-06-25T10:00:00Z", per_kwh: 8, export_per_kwh: 2 },
  // overlaps the future window — must be dropped
  { interval_start: "2026-06-25T10:00:00Z", interval_end: "2026-06-25T10:05:00Z", per_kwh: 99, export_per_kwh: 99 },
];

describe("mergePriceForecasts", () => {
  const merged = mergePriceForecasts(past, fut);
  it("sorts ascending by start", () => {
    const starts = merged.map((p) => p.start);
    expect(starts).toEqual([...starts].sort());
  });
  it("dedupes by start (5-min wins) and drops past overlapping future", () => {
    expect(merged.filter((p) => p.start === "2026-06-25T10:00:00Z")).toHaveLength(1);
    expect(merged.find((p) => p.start === "2026-06-25T10:00:00Z").forecast_predicted).toBe(12);
  });
});

describe("pickPriceAt", () => {
  it("returns the interval containing t; import prefers predicted", () => {
    const list = mergePriceForecasts(past, fut);
    expect(pickPriceAt(list, "2026-06-25T10:02:00Z", "import")).toBe(12);
    expect(pickPriceAt(list, "2026-06-25T10:02:00Z", "export")).toBe(3);
    expect(pickPriceAt(list, "2026-06-25T08:00:00Z", "import")).toBeNull();
  });
});

describe("coalesce", () => {
  it("first non-null", () => expect(coalesce(null, undefined, 7, 9)).toBe(7));
});
