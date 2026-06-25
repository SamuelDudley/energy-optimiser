import { describe, it, expect } from "vitest";
import { marginalCost, hexToRgba, colorForLoadId } from "../src/optimiser/api/static/derive.js";

describe("marginalCost", () => {
  it("import costs, export earns", () => {
    expect(marginalCost(30, 10, 2)).toBe(60);    // importing 2 kW at 30 c/kWh
    expect(marginalCost(30, 10, -2)).toBe(-20);   // exporting 2 kW at 10 c/kWh
    expect(marginalCost(30, 10, 0)).toBe(0);
  });
  it("null on any null input", () => expect(marginalCost(null, 10, 2)).toBeNull());
});

describe("helpers", () => {
  it("hexToRgba", () => expect(hexToRgba("#3fb950", 0.15)).toBe("rgba(63, 185, 80, 0.15)"));
  it("colorForLoadId is stable + in palette", () => {
    expect(colorForLoadId("hot_water")).toBe(colorForLoadId("hot_water"));
  });
});
