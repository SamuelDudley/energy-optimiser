import { describe, it, expect, vi, beforeEach } from "vitest";

// uPlot's ESM build checks `typeof window != 'undefined'` and then immediately
// accesses `document` (line 60 of uplot.esm.js). The test's beforeEach sets
// globalThis.window but not globalThis.document, so uPlot throws
// "document is not defined" when chart-core.js is dynamically imported after
// vi.resetModules(). We mock uPlot at the module level to prevent its side
// effects from running — the breakpoint test doesn't need real uPlot at all.
vi.mock("../src/optimiser/api/static/uplot.esm.js", () => ({
  default: { sync: (_key) => ({}) },
}));

describe("chart-core breakpoint", () => {
  let listeners;
  beforeEach(() => {
    listeners = [];
    globalThis.window = globalThis;
    globalThis.matchMedia = (q) => ({
      matches: globalThis.__narrow ?? false,
      addEventListener: (_e, fn) => listeners.push(fn),
      removeEventListener: () => {},
    });
  });
  it("isNarrow reflects matchMedia + fires watchers on change", async () => {
    vi.resetModules();
    const { isNarrow, onBreakpointChange } = await import("../src/optimiser/api/static/chart-core.js");
    globalThis.__narrow = true;
    expect(isNarrow()).toBe(true);
    let fired = 0;
    onBreakpointChange(() => { fired++; });
    listeners.forEach((fn) => fn());
    expect(fired).toBe(1);
  });
});
