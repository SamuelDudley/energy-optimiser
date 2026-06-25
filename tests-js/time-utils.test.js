import { describe, it, expect } from "vitest";
import { SLOT_MS, nearestSlotAt, toNemDate, toEpochSec } from "../src/optimiser/api/static/time-utils.js";

describe("nearestSlotAt", () => {
  it("floors to the 5-min slot start (UTC epoch)", () => {
    expect(nearestSlotAt(new Date("2026-06-25T10:03:30Z")).toISOString())
      .toBe("2026-06-25T10:00:00.000Z");
    expect(nearestSlotAt(new Date("2026-06-25T10:05:00Z")).toISOString())
      .toBe("2026-06-25T10:05:00.000Z");
  });
  it("accepts ms numbers and strings", () => {
    expect(+nearestSlotAt(1_700_000_123_000) % SLOT_MS).toBe(0);
  });
});

describe("toNemDate", () => {
  it("adds 10h (NEM = UTC+10, no DST) then takes YYYY-MM-DD", () => {
    expect(toNemDate("2026-06-25T15:00:00Z")).toBe("2026-06-26");
    expect(toNemDate("2026-06-25T13:00:00Z")).toBe("2026-06-25");
  });
  it("null on bad input", () => {
    expect(toNemDate(null)).toBeNull();
  });
});

describe("toEpochSec", () => {
  it("returns integer seconds", () => {
    expect(toEpochSec("2026-06-25T10:00:00Z")).toBe(1782381600);
    expect(toEpochSec(null)).toBeNull();
  });
});
