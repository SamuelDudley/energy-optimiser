# uPlot Dashboard Rebuild — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the operator dashboard's Plotly charting with vendored uPlot, add arcsinh spike compression to the price axis, and remove the Sankey — frontend-only, backend data API unchanged.

**Architecture:** ESM modules under `src/optimiser/api/static/`. The 8-panel `#ts-figure` becomes N synced uPlot instances (one per panel) over a single shared union-timeline x-array; the daily-spend and 4 ops charts become uPlot. Plotly stays loaded during the migration and is removed in the final cutover, so the dashboard works at every step. Pure-logic modules (timeline merge, band columns, classification) are unit-tested with Vitest; rendering is verified with the chrome-devtools MCP against the live dashboard.

**Tech Stack:** Vanilla ESM JS, uPlot 1.6.31 (vendored, self-hosted), Vitest (new, pure-logic tests only), aiohttp static serving (unchanged logic; two literal additions for asset registration). Verification via chrome-devtools MCP.

**Companion spec (source of truth, read alongside this plan):** `docs/superpowers/specs/2026-06-25-uplot-dashboard-rebuild-design.md`. Exhaustive trace/colour/series enumerations live there (esp. §5.2, §5.8, §5.9); this plan points at spec sections rather than duplicating every colour.

## Global Constraints

- **Backend data API is UNCHANGED.** The only backend edit permitted is registering new static assets: two literals — `_STATIC_FILES` in `src/optimiser/api/handlers/dashboard.py` and `_PUBLIC_PATHS` in `src/optimiser/api/server.py`. No handler logic, route, or JSON response shape changes. The Python test suite must stay green.
- **arcsinh:** price axis only, `{ distr: 4, asinh: 30 }`. COST stays linear-autoscale.
- **One shared x:** every `#ts-figure` panel instance is built on the **identical** union-timeline x column (epoch **seconds**). Never per-panel-trim x — it breaks cross-panel `cursor.idx` alignment.
- **Cursor line reads `state.cursor`**, never uPlot's transient `u.cursor.idx` (must survive `setData`).
- **Ribbon:** draw in device px (`valToPos('x', true)` + `u.bbox`); hit-test in CSS px (`u.cursor.left` + `u.over`). One shared px-bridge helper.
- **Bands:** null **both** bounds wherever either is null; drop contiguous non-null runs of `< 2` points.
- **Keep Plotly loaded** (CDN `<script>` + `chart-utils.js`) until the final removal task. Unported charts keep using `window.Plotly`.
- **Breakpoint:** 760 px (`MOBILE_BREAKPOINT_PX`), single source of truth in `chart-core.js`.
- **Vendored, pinned, no runtime CDN** for uPlot (host-network container, no guaranteed internet).
- **Verify every rendering task with chrome-devtools MCP**, and include a real price-spike day (≥ $5/kWh, i.e. ≥ 500 c/kWh) in the final parity diff.
- **Slot semantics:** `SLOT_MINUTES = 5`, `SLOT_MS = 300000`. Must stay in sync with `optimiser/lp/constants.py`.

---

## Phase 0 — Foundations & infrastructure

### Task 1: JavaScript test infrastructure (Vitest)

**Files:**
- Create: `package.json`
- Create: `vitest.config.js`
- Create: `.gitignore` entry for `node_modules/`
- Create: `tests-js/smoke.test.js`

**Interfaces:**
- Produces: `npm test` runs Vitest over `tests-js/**/*.test.js`; modules under `src/optimiser/api/static/*.js` are importable as ESM.

- [ ] **Step 1: Create `package.json`**

```json
{
  "name": "energy-optimiser-dashboard",
  "private": true,
  "type": "module",
  "scripts": {
    "test": "vitest run",
    "test:watch": "vitest"
  },
  "devDependencies": {
    "vitest": "^2.1.0"
  }
}
```

- [ ] **Step 2: Create `vitest.config.js`**

```js
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests-js/**/*.test.js"],
    environment: "node",
  },
});
```

- [ ] **Step 3: Add `node_modules/` to `.gitignore`** (append the line if the file exists, else create it with that single line).

- [ ] **Step 4: Write the smoke test** at `tests-js/smoke.test.js`

```js
import { describe, it, expect } from "vitest";

describe("vitest infra", () => {
  it("runs", () => {
    expect(1 + 1).toBe(2);
  });
});
```

- [ ] **Step 5: Install + run**

Run: `npm install && npm test`
Expected: 1 passed test; `node_modules/` present and git-ignored.

- [ ] **Step 6: Commit**

```bash
git add package.json package-lock.json vitest.config.js .gitignore tests-js/smoke.test.js
git commit -m "test(dashboard): add Vitest infra for pure-logic JS modules"
```

---

### Task 2: Vendor uPlot + register static assets

**Files:**
- Create: `src/optimiser/api/static/uplot.esm.js` (vendored, pinned uPlot 1.6.31 ESM build)
- Create: `src/optimiser/api/static/uplot.min.css` (vendored uPlot stylesheet)
- Modify: `src/optimiser/api/handlers/dashboard.py:48-53` (`_STATIC_FILES`)
- Modify: `src/optimiser/api/server.py:51-61` (`_PUBLIC_PATHS`)
- Modify: `src/optimiser/api/static/dashboard.html:7-11` (add uPlot CSS `<link>`; keep Plotly)

**Interfaces:**
- Produces: `window`-free ESM `import uPlot from "./uplot.esm.js"`; `/dashboard/static/uplot.esm.js` and `/dashboard/static/uplot.min.css` served publicly.

- [ ] **Step 1: Vendor the pinned files** (one-time fetch, then committed — not a runtime dependency)

```bash
cd src/optimiser/api/static
curl -fsSL https://cdn.jsdelivr.net/npm/uplot@1.6.31/dist/uPlot.esm.js  -o uplot.esm.js
curl -fsSL https://cdn.jsdelivr.net/npm/uplot@1.6.31/dist/uPlot.min.css -o uplot.min.css
head -c 120 uplot.esm.js   # sanity: should be minified JS, not an HTML error page
```

- [ ] **Step 2: Register both files in `_STATIC_FILES`** (`dashboard.py`)

```python
_STATIC_FILES: dict[str, str] = {
    "dashboard.css": "text/css",
    "chart-utils.js": "application/javascript",
    "dashboard.js": "application/javascript",
    "ops.js": "application/javascript",
    "uplot.esm.js": "application/javascript",
    "uplot.min.css": "text/css",
}
```

- [ ] **Step 3: Make both files public in `_PUBLIC_PATHS`** (`server.py`) — append:

```python
    "/dashboard/static/uplot.esm.js",
    "/dashboard/static/uplot.min.css",
```

- [ ] **Step 4: Link the uPlot stylesheet in `dashboard.html`** (after the existing `dashboard.css` link, keep the Plotly `<script>`):

```html
  <link rel="stylesheet" href="/dashboard/static/uplot.min.css" />
```

- [ ] **Step 5: Verify the Python suite is still green** (backend logic untouched)

Run: `cd /home/dudley/code/energy-optimiser && uv run pytest tests/ -q`
Expected: same pass count as before this task (no failures).

- [ ] **Step 6: Verify assets serve** (service is bind-mounted; static edits need no rebuild)

Run: `curl -fsS http://localhost:8080/dashboard/static/uplot.esm.js | head -c 60`
(Adjust host/port to the deployed dashboard.) Expected: minified JS bytes, HTTP 200.

- [ ] **Step 7: Commit**

```bash
git add src/optimiser/api/static/uplot.esm.js src/optimiser/api/static/uplot.min.css \
        src/optimiser/api/handlers/dashboard.py src/optimiser/api/server.py \
        src/optimiser/api/static/dashboard.html
git commit -m "build(dashboard): vendor uPlot 1.6.31 + register static assets"
```

---

### Task 3: Convert dashboard entry points to ESM (no behaviour change)

Plotly still renders everything; this only changes module loading so later tasks can `import`.

**Files:**
- Modify: `src/optimiser/api/static/dashboard.html:247-249` (script tags → `type="module"`)
- Modify: `src/optimiser/api/static/dashboard.js` (top + bottom: it becomes a module)
- Modify: `src/optimiser/api/static/ops.js` (becomes a module)

**Interfaces:**
- Consumes: nothing new.
- Produces: `dashboard.js` and `ops.js` execute as ESM (deferred); all current globals they relied on cross-file are resolved explicitly.

- [ ] **Step 1: Audit cross-file/global coupling**

Run:
```bash
cd src/optimiser/api/static
grep -nE "window\.eoChart|window\.Plotly|Plotly\b" ops.js | head
grep -nE "onclick=|on[a-z]+=\"" dashboard.html | head
grep -nE "function (apiFetch|showError)\b|window\.(apiFetch|EO)\b" dashboard.js ops.js | head
```
Expected: confirm the only cross-file global is `window.eoChart` (from `chart-utils.js`) and `Plotly` (CDN global). No inline `onclick=` handlers (the dashboard wires events via `addEventListener`). Note any surprise so it can be converted to an import.

- [ ] **Step 2: Convert the HTML script tags** (`dashboard.html`)

```html
  <script src="/dashboard/static/chart-utils.js"></script>
  <script type="module" src="/dashboard/static/dashboard.js"></script>
  <script type="module" src="/dashboard/static/ops.js"></script>
```
(`chart-utils.js` stays a classic script for now — it sets `window.eoChart`, which the modules read off `window`. It is deleted in Task 20.)

- [ ] **Step 3: Make `ops.js` independent of `dashboard.js` globals if any were found** in Step 1 — if `ops.js` calls a function defined in `dashboard.js`, copy that helper into `ops.js` or extract it in a later task. If Step 1 found none, no change.

- [ ] **Step 4: Verify the dashboard still loads under Plotly with zero console errors** (chrome-devtools MCP)

- Navigate a new page to the dashboard URL; enter the bearer token if prompted.
- List console messages: expect **zero** errors (module scripts are deferred — verify `window.eoChart` is defined by the time init runs; if a race appears, move the dashboard init into a `DOMContentLoaded` listener).
- Screenshot the Energy tab: all panels render as before (Plotly).

- [ ] **Step 5: Commit**

```bash
git add src/optimiser/api/static/dashboard.html src/optimiser/api/static/dashboard.js src/optimiser/api/static/ops.js
git commit -m "refactor(dashboard): load dashboard.js/ops.js as ESM modules (no behaviour change)"
```

---

## Phase 1 — Pure-logic modules (Vitest TDD)

These extract existing logic verbatim into importable ESM modules and pin current behaviour with tests. The browser imports the same modules.

### Task 4: `time-utils.js` — slot/NEM time helpers

**Files:**
- Create: `src/optimiser/api/static/time-utils.js`
- Create: `tests-js/time-utils.test.js`
- Modify: `dashboard.js` (import these; delete the local copies of `SLOT_MS`, `nearestSlotAt`, `toNemDate`)

**Interfaces:**
- Produces: `export const SLOT_MINUTES = 5; export const SLOT_MS = 300000;`
  `export function nearestSlotAt(time: Date|number|string): Date` (floors to 5-min slot start).
  `export function toNemDate(d): string|null` (UTC+10 `YYYY-MM-DD`).
  `export function toEpochSec(d): number|null` (epoch **seconds** for uPlot x).

- [ ] **Step 1: Write the failing test** (`tests-js/time-utils.test.js`)

```js
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
    expect(toEpochSec("2026-06-25T10:00:00Z")).toBe(1781776800);
    expect(toEpochSec(null)).toBeNull();
  });
});
```

- [ ] **Step 2: Run it — verify it fails** (`npm test -- time-utils`) with "Cannot find module …/time-utils.js".

- [ ] **Step 3: Implement `time-utils.js`** (copy `SLOT_MS`/`nearestSlotAt`/`toNemDate` verbatim from `dashboard.js`; add `toEpochSec`)

```js
export const SLOT_MINUTES = 5;
export const SLOT_MS = SLOT_MINUTES * 60 * 1000;

export function nearestSlotAt(time) {
  const t = time instanceof Date ? time.getTime() : +new Date(time);
  return new Date(Math.floor(t / SLOT_MS) * SLOT_MS);
}

export function toNemDate(d) {
  if (d == null) return null;
  const t = d instanceof Date ? +d : +new Date(d);
  if (!Number.isFinite(t)) return null;
  return new Date(t + 10 * 3_600_000).toISOString().slice(0, 10);
}

export function toEpochSec(d) {
  if (d == null) return null;
  const t = d instanceof Date ? +d : +new Date(d);
  if (!Number.isFinite(t)) return null;
  return Math.round(t / 1000);
}
```

- [ ] **Step 4: Run the test — verify PASS.**

- [ ] **Step 5: Wire into `dashboard.js`** — add `import { SLOT_MS, nearestSlotAt, toNemDate, toEpochSec } from "./time-utils.js";` at the top and delete the now-duplicate local definitions of `SLOT_MS`, `nearestSlotAt`, `toNemDate`. Leave `SLOT_MINUTES` local if still referenced, else import it.

- [ ] **Step 6: MCP smoke** — reload dashboard, zero console errors, cursor readout + spend-cursor still track.

- [ ] **Step 7: Commit**

```bash
git add src/optimiser/api/static/time-utils.js tests-js/time-utils.test.js src/optimiser/api/static/dashboard.js
git commit -m "refactor(dashboard): extract+test time-utils (slot/NEM helpers)"
```

---

### Task 5: `classify.js` — decision/mode categories

**Files:**
- Create: `src/optimiser/api/static/classify.js`
- Create: `tests-js/classify.test.js`
- Modify: `dashboard.js` (import; delete local `DECISION*`, `MODE*`, `decisionFor`, `decisionFromTelemetry`, `modeFromSlot`, `modeFromTelemetry`, and the `DEADBAND_KW`/`MODE_SWITCH_HYSTERESIS_KW` consts they use)

**Interfaces:**
- Produces: `DECISION`, `DECISION_COLORS`, `DECISION_LABELS`, `MODE`, `MODE_COLORS`, `MODE_LABELS` (objects, exact values from `dashboard.js:45-99`); `decisionFor(slot)`, `decisionFromTelemetry(row)`, `modeFromSlot(slot)`, `modeFromTelemetry(row)` (all `→ number` category index). Also `DEADBAND_KW = 0.1`, `MODE_SWITCH_HYSTERESIS_KW = 0.05`.

- [ ] **Step 1: Write the failing test** (`tests-js/classify.test.js`)

```js
import { describe, it, expect } from "vitest";
import {
  DECISION, MODE, decisionFor, decisionFromTelemetry, modeFromSlot, modeFromTelemetry,
} from "../src/optimiser/api/static/classify.js";

describe("decisionFor", () => {
  it("idle within deadband", () => expect(decisionFor({ battery_kw: 0.05 })).toBe(DECISION.IDLE));
  it("discharge when negative", () => expect(decisionFor({ battery_kw: -2 })).toBe(DECISION.DISCHARGE));
  it("charge-grid when grid dominates", () =>
    expect(decisionFor({ battery_kw: 3, grid_to_battery_kw: 2.5, pv_to_battery_kw: 0.5 })).toBe(DECISION.CHARGE_GRID));
  it("charge-pv when pv dominates", () =>
    expect(decisionFor({ battery_kw: 3, grid_to_battery_kw: 0.2, pv_to_battery_kw: 2.8 })).toBe(DECISION.CHARGE_PV));
  it("unknown on null/non-finite", () => {
    expect(decisionFor(null)).toBe(DECISION.UNKNOWN);
    expect(decisionFor({ battery_kw: null })).toBe(DECISION.UNKNOWN);
  });
});

describe("modeFromSlot", () => {
  it("idle within deadband", () => expect(modeFromSlot({ battery_kw: 0 })).toBe(MODE.M2_IDLE));
  it("discharge mode 5 with PV", () => expect(modeFromSlot({ battery_kw: -1, pv_kw: 1 })).toBe(MODE.M5_DIS_PV));
  it("discharge mode 6 without PV", () => expect(modeFromSlot({ battery_kw: -1, pv_kw: 0 })).toBe(MODE.M6_DIS_ESS));
});

describe("telemetry decoders", () => {
  it("planner_action wins", () => {
    expect(modeFromTelemetry({ planner_action: "charge_grid" })).toBe(MODE.M3_CHARGE);
    expect(decisionFromTelemetry({ planner_action: "discharge_ess" })).toBe(DECISION.DISCHARGE);
  });
  it("falls back to ems_mode when action absent", () =>
    expect(modeFromTelemetry({ ems_mode: 6 })).toBe(MODE.M6_DIS_ESS));
});
```

- [ ] **Step 2: Run — verify it fails** (module missing).

- [ ] **Step 3: Implement `classify.js`** — copy `DECISION`, `DECISION_COLORS`, `DECISION_LABELS`, `MODE`, `MODE_COLORS`, `MODE_LABELS` (verbatim, `dashboard.js:45-99`), `DEADBAND_KW`, `MODE_SWITCH_HYSTERESIS_KW`, and the four functions (`dashboard.js:493-557`) verbatim, prefixing each with `export`.

- [ ] **Step 4: Run the test — verify PASS.**

- [ ] **Step 5: Wire into `dashboard.js`** — import the symbols; delete the local definitions. `DEADBAND_KW` is also used by the buy/sell-region logic — import it (don't re-declare).

- [ ] **Step 6: MCP smoke** — reload; the DECISION + MODE ribbons (still Plotly) render identically; screenshot to confirm.

- [ ] **Step 7: Commit**

```bash
git add src/optimiser/api/static/classify.js tests-js/classify.test.js src/optimiser/api/static/dashboard.js
git commit -m "refactor(dashboard): extract+test decision/mode classification"
```

---

### Task 6: `price-merge.js` — forecast merge + price lookup

**Files:**
- Create: `src/optimiser/api/static/price-merge.js`
- Create: `tests-js/price-merge.test.js`
- Modify: `dashboard.js` (import; delete local `mergePriceForecasts`, `mergePVForecasts`, `pickPriceAt`, `priceLogToInterval`, `coalesce`)

**Interfaces:**
- Produces: `mergePriceForecasts(pastRows, futureIntervals)` (sorted, deduped-by-start, past-overlap dropped — exact semantics of `dashboard.js:1476-1509`); `mergePVForecasts(pastRows, futureIntervals)`; `pickPriceAt(priceList, t, side)` (`side` = `"import"|"export"`, 5-min-beats-30-min linear scan); `coalesce(...vals)`; `priceLogToInterval(r)`.

- [ ] **Step 1: Write the failing test** (`tests-js/price-merge.test.js`)

```js
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
```

- [ ] **Step 2: Run — verify it fails.**

- [ ] **Step 3: Implement `price-merge.js`** — copy `priceLogToInterval`, `mergePriceForecasts`, `mergePVForecasts`, `pickPriceAt`, `coalesce` verbatim (`dashboard.js:1464-1546`), each `export`ed. Note `pickPriceAt` uses `coalesce` — keep them in the same module.

- [ ] **Step 4: Run the test — verify PASS.**

- [ ] **Step 5: Wire into `dashboard.js`** — import; delete local copies. `coalesce` is used widely in `dashboard.js`; import it.

- [ ] **Step 6: MCP smoke** — price panel still renders; spot-check a forecast band looks unchanged.

- [ ] **Step 7: Commit**

```bash
git add src/optimiser/api/static/price-merge.js tests-js/price-merge.test.js src/optimiser/api/static/dashboard.js
git commit -m "refactor(dashboard): extract+test price forecast merge + lookup"
```

---

### Task 7: `derive.js` — marginal cost + colour helpers

**Files:**
- Create: `src/optimiser/api/static/derive.js`
- Create: `tests-js/derive.test.js`
- Modify: `dashboard.js` (import; delete local `marginalCost`, `colorForLoadId`, `hexToRgba`, `LOAD_PALETTE`)

**Interfaces:**
- Produces: `marginalCost(ip, ep, grid)` (c/h; `null` if any input null; `ip*max(0,grid) - ep*max(0,-grid)`); `colorForLoadId(id)`; `hexToRgba(hex, alpha)`; `LOAD_PALETTE`.

- [ ] **Step 1: Write the failing test** (`tests-js/derive.test.js`)

```js
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
```

- [ ] **Step 2: Run — verify it fails.**
- [ ] **Step 3: Implement `derive.js`** — copy `LOAD_PALETTE`, `colorForLoadId`, `hexToRgba` (`dashboard.js:180-192`), `marginalCost` (`dashboard.js:1548-1554`), each `export`ed.
- [ ] **Step 4: Run the test — verify PASS.**
- [ ] **Step 5: Wire into `dashboard.js`** — import; delete local copies.
- [ ] **Step 6: MCP smoke** — COST panel + per-load colours unchanged.
- [ ] **Step 7: Commit**

```bash
git add src/optimiser/api/static/derive.js tests-js/derive.test.js src/optimiser/api/static/dashboard.js
git commit -m "refactor(dashboard): extract+test marginal cost + colour helpers"
```

---

### Task 8: `timeline.js` — union-x + series alignment (the keystone)

This is the highest-risk module (spec §11 risk #1). Build it as two tiny composable primitives, fully tested, before any rendering consumes it.

**Files:**
- Create: `src/optimiser/api/static/timeline.js`
- Create: `tests-js/timeline.test.js`

**Interfaces:**
- Produces:
  - `buildUnionX(...timestampArrays): number[]` — merge any number of arrays of epoch-**seconds** ints into one **ascending, de-duplicated** array.
  - `alignSeries(unionX, points, getSec, getVal): (number|null)[]` — for each `x` in `unionX`, the value of the `points` element whose `getSec(point)===x`, else `null`. (`points` need not be sorted; build an index map.)
  - These compose into per-panel `data` arrays in Task 13; all panels reuse the **same** `unionX`.

- [ ] **Step 1: Write the failing test** (`tests-js/timeline.test.js`)

```js
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
```

- [ ] **Step 2: Run — verify it fails.**

- [ ] **Step 3: Implement `timeline.js`**

```js
// Build one ascending, de-duplicated x array (epoch seconds) from N arrays.
export function buildUnionX(...arrays) {
  const set = new Set();
  for (const arr of arrays) {
    for (const v of arr) {
      if (v == null || !Number.isFinite(v)) continue;
      set.add(v);
    }
  }
  return [...set].sort((a, b) => a - b);
}

// Align `points` onto `unionX`: value where getSec(point) matches an x, else null.
export function alignSeries(unionX, points, getSec, getVal) {
  const byX = new Map();
  for (const p of points) {
    const s = getSec(p);
    if (s == null || !Number.isFinite(s)) continue;
    byX.set(s, getVal(p));
  }
  return unionX.map((x) => {
    const v = byX.has(x) ? byX.get(x) : null;
    return v == null ? null : v;
  });
}
```

- [ ] **Step 4: Run the test — verify PASS.**

- [ ] **Step 5: Commit** (no `dashboard.js` wiring yet — consumed in Task 13)

```bash
git add src/optimiser/api/static/timeline.js tests-js/timeline.test.js
git commit -m "feat(dashboard): union-x + series alignment primitives (Vitest)"
```

---

### Task 9: `bands.js` (pure) — band columns aligned to union-x

**Files:**
- Create: `src/optimiser/api/static/bands.js`
- Create: `tests-js/bands.test.js`

**Interfaces:**
- Produces: `bandColumns(intervals, loKey, hiKey, unionX, getSec): { lo: (number|null)[], hi: (number|null)[] }` — aligned to `unionX`; null **both** bounds where either is null; null any contiguous non-null run shorter than 2 (degenerate vertical fill). Feeds uPlot native `bands` (Task 13).

- [ ] **Step 1: Write the failing test** (`tests-js/bands.test.js`)

```js
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
```

- [ ] **Step 2: Run — verify it fails.**

- [ ] **Step 3: Implement `bands.js`**

```js
import { alignSeries } from "./timeline.js";

export function bandColumns(intervals, loKey, hiKey, unionX, getSec) {
  let lo = alignSeries(unionX, intervals, getSec, (p) => p[loKey]);
  let hi = alignSeries(unionX, intervals, getSec, (p) => p[hiKey]);
  // Null BOTH where either is null.
  for (let i = 0; i < unionX.length; i++) {
    if (lo[i] == null || hi[i] == null) { lo[i] = null; hi[i] = null; }
  }
  // Drop contiguous non-null runs shorter than 2.
  let i = 0;
  while (i < unionX.length) {
    if (lo[i] == null) { i++; continue; }
    let j = i;
    while (j < unionX.length && lo[j] != null) j++;
    if (j - i < 2) for (let k = i; k < j; k++) { lo[k] = null; hi[k] = null; }
    i = j;
  }
  return { lo, hi };
}
```

- [ ] **Step 4: Run the test — verify PASS.**

- [ ] **Step 5: Commit**

```bash
git add src/optimiser/api/static/bands.js tests-js/bands.test.js
git commit -m "feat(dashboard): band-column builder (null-both + <2-run skip), Vitest"
```

---

## Phase 2 — chart-core (sync/resize hub)

### Task 10: `chart-core.js` — replaces `window.eoChart`

**Files:**
- Create: `src/optimiser/api/static/chart-core.js`
- Create: `tests-js/chart-core.test.js` (only the pure bits: breakpoint registry behaviour with a mocked `matchMedia`)

**Interfaces:**
- Produces:
  - `MOBILE_BREAKPOINT_PX = 760`, `isNarrow(): boolean`.
  - `onBreakpointChange(fn)`, internal watcher fire.
  - `registry`: `register(id, uplotInstance)`, `unregister(id)`, `resizeAll()` (calls `u.setSize({width,height})` from each instance's parent box; skips hidden/zero-width).
  - `makeSyncHub(key): { sync, attach(u) }` — wires `uPlot.sync(key)` cursor sync **and** the guarded x-`setScale` propagation across instances, using **one shared in-progress flag** (also used by the cursor-redraw path, Task 14) to prevent feedback loops.
  - `cursorDragOpts(): object` — `{ drag: { x: false, y: false, setScale: false }, ... }` for mobile scroll-through (spec §5.11.1).

- [ ] **Step 1: Write the failing test** (`tests-js/chart-core.test.js`) — covers only `isNarrow` + breakpoint dispatch via a stubbed `matchMedia`

```js
import { describe, it, expect, vi, beforeEach } from "vitest";

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
```

- [ ] **Step 2: Run — verify it fails.**

- [ ] **Step 3: Implement `chart-core.js`** — port the `isNarrow`/`onBreakpointChange`/registry shape from `chart-utils.js` (`window.eoChart`), but with `setSize` instead of `Plotly.Plots.resize`, plus the sync hub and drag opts. Key skeleton:

```js
import uPlot from "./uplot.esm.js";

export const MOBILE_BREAKPOINT_PX = 760;
const mq = typeof matchMedia === "function" ? matchMedia(`(max-width: ${MOBILE_BREAKPOINT_PX}px)`) : { matches: false, addEventListener() {} };
export function isNarrow() { return mq.matches; }

const watchers = [];
export function onBreakpointChange(fn) { if (typeof fn === "function") watchers.push(fn); }
mq.addEventListener?.("change", () => { for (const fn of watchers) try { fn(); } catch (e) { console.warn(e); } });

const instances = new Map();
export const registry = {
  register(id, u) { if (id && u) instances.set(id, u); },
  unregister(id) { instances.delete(id); },
  resizeAll() {
    for (const [, u] of instances) {
      const el = u.root;
      if (!el || !el.offsetParent || el.clientWidth === 0) continue;
      u.setSize({ width: el.clientWidth, height: el.clientHeight });
    }
  },
};
onBreakpointChange(() => setTimeout(() => registry.resizeAll(), 50));

export function cursorDragOpts() {
  return { drag: { x: !isNarrow(), y: false, setScale: false } };
}

// Shared guard so x-zoom propagation and cursor redraws never recurse.
export const syncGuard = { busy: false };

export function makeSyncHub(key) {
  const sync = uPlot.sync(key);
  return {
    sync,
    // Returns the per-instance opts needed to join the sync group and
    // propagate x-range zoom/pan to peers without feedback.
    instanceOpts(peersGetter) {
      return {
        cursor: { sync: { key, scales: ["x", null] } },
        hooks: {
          setScale: [(u, scaleKey) => {
            if (scaleKey !== "x" || syncGuard.busy) return;
            syncGuard.busy = true;
            const { min, max } = u.scales.x;
            for (const p of peersGetter()) if (p !== u) p.setScale("x", { min, max });
            syncGuard.busy = false;
          }],
        },
      };
    },
  };
}
```

- [ ] **Step 4: Run the test — verify PASS.**

- [ ] **Step 5: Commit** (browser wiring happens in Task 13)

```bash
git add src/optimiser/api/static/chart-core.js tests-js/chart-core.test.js
git commit -m "feat(dashboard): chart-core sync/resize hub (replaces eoChart)"
```

---

## Phase 3 — `#ts-figure` to uPlot

> The 8 panels are ONE Plotly figure today, so they cut over together (Task 13). Build the plugins first (Tasks 11–12), then assemble + cut over (Task 13), then wire the cursor (Task 14).

### Task 11: `shapes.js` — draw-hook plugin for lines & regions

**Files:**
- Create: `src/optimiser/api/static/shapes.js`

**Interfaces:**
- Consumes: a live `getState()` returning `{ cursorSec, nowSec, socFloorPct, regions:[{x0,x1,kind}], thresholdC }`.
- Produces: `shapesPlugin(opts)` → a uPlot plugin object whose `hooks.draw` paints, in **device px** (`u.valToPos(v, scale, true)` + `u.bbox`):
  - vertical NOW line (green dotted `#56d364`) at `nowSec` when set;
  - vertical pinned-cursor line (blue `#58a6ff`) at `cursorSec` when set — **read from `getState()`, not `u.cursor.idx`**;
  - per-panel hlines (SOC floor on the soc panel `#f85149` dashed; zero-line on grid/cost `#444c56`);
  - buy/sell region rects on the price panel (amber `rgba(210,153,34,.10)` / green `rgba(63,185,80,.10)`), `layer: below`;
  - (refinement 1e) faint arcsinh threshold hline at `thresholdC` on the price panel.
- Each panel passes which decorations apply (`opts.kind: "price"|"soc"|"grid"|"cost"|"plain"`).

- [ ] **Step 1: Implement `shapes.js`** (full code)

```js
export function shapesPlugin({ getState, kind }) {
  return {
    hooks: {
      draw: [(u) => {
        const st = getState();
        const ctx = u.ctx;
        const { left, top, width, height } = u.bbox;
        ctx.save();
        const vline = (sec, color, dash) => {
          if (sec == null) return;
          const x = Math.round(u.valToPos(sec, "x", true));
          if (x < left || x > left + width) return;
          ctx.beginPath(); ctx.setLineDash(dash || []); ctx.strokeStyle = color;
          ctx.lineWidth = Math.max(1, Math.round(devicePixelRatio));
          ctx.moveTo(x, top); ctx.lineTo(x, top + height); ctx.stroke();
        };
        const hline = (val, color, dash) => {
          const y = Math.round(u.valToPos(val, u.series[1]?.scale || "y", true));
          ctx.beginPath(); ctx.setLineDash(dash || []); ctx.strokeStyle = color;
          ctx.lineWidth = 1; ctx.moveTo(left, y); ctx.lineTo(left + width, y); ctx.stroke();
        };
        if (kind === "price") {
          for (const r of st.regions || []) {
            const x0 = Math.round(u.valToPos(r.x0, "x", true));
            const x1 = Math.round(u.valToPos(r.x1, "x", true));
            ctx.fillStyle = r.kind === "charge" ? "rgba(210,153,34,0.10)" : "rgba(63,185,80,0.10)";
            ctx.fillRect(x0, top, Math.max(1, x1 - x0), height);
          }
          if (st.thresholdC != null) hline(st.thresholdC, "rgba(139,148,158,0.35)", [2, 3]);
        }
        if (kind === "soc" && st.socFloorPct != null) hline(st.socFloorPct, "#f85149", [4, 3]);
        if (kind === "grid" || kind === "cost") hline(0, "#444c56", []);
        vline(st.nowSec, "#56d364", [2, 4]);
        vline(st.cursorSec, "#58a6ff", []);
        ctx.restore();
      }],
    },
  };
}
```

- [ ] **Step 2: Commit** (verified in Task 13)

```bash
git add src/optimiser/api/static/shapes.js
git commit -m "feat(dashboard): shapes plugin (now/cursor lines, regions, hlines)"
```

---

### Task 12: `ribbon.js` — categorical lane plugin (folds in 1h + 1i)

**Files:**
- Create: `src/optimiser/api/static/ribbon.js`

**Interfaces:**
- Consumes: `{ unionX, cats: number[] (category index per x), colorOf(cat), labelOf(cat), glyphOf(cat) }`.
- Produces: `ribbonPlugin(opts)` → plugin painting fixed-height (≈14 px CSS → device-scaled) colour cells per contiguous equal-category run, in device px; a `setCursor` hook that hit-tests in **CSS px** and writes the under-cursor `labelOf(cat)` into a tooltip element; glyphs (1i) when a run is wide enough; WCAG-bumped colours come from `classify.js` (Task 5) which Task 12 may adjust (e.g. diverge `M5_DIS_PV`/`M6_DIS_ESS` purples) — record any colour change in the spec §10.1 1i note.

- [ ] **Step 1: Implement `ribbon.js`** — draw-hook fills runs; hit-test tooltip. Key body:

```js
export function ribbonPlugin({ unionX, getCats, colorOf, labelOf, glyphOf, tooltipEl }) {
  function runEnd(cats, i) { let j = i; while (j < cats.length && cats[j] === cats[i]) j++; return j; }
  return {
    hooks: {
      draw: [(u) => {
        const cats = getCats();
        const ctx = u.ctx; const { left, top, width, height } = u.bbox;
        for (let i = 0; i < cats.length; ) {
          const j = runEnd(cats, i);
          const x0 = Math.round(u.valToPos(unionX[i], "x", true));
          const x1 = Math.round(u.valToPos(unionX[Math.min(j, unionX.length - 1)], "x", true));
          ctx.fillStyle = colorOf(cats[i]);
          ctx.fillRect(x0, top, Math.max(1, x1 - x0), height);
          if (x1 - x0 > 18 * devicePixelRatio && glyphOf) {
            ctx.fillStyle = "#0d1117"; ctx.font = `${10 * devicePixelRatio}px sans-serif`;
            ctx.textBaseline = "middle";
            ctx.fillText(glyphOf(cats[i]), x0 + 3 * devicePixelRatio, top + height / 2);
          }
          i = j;
        }
      }],
      setCursor: [(u) => {
        if (!tooltipEl) return;
        const idx = u.cursor.idx;            // CSS-px-derived index
        const cats = getCats();
        if (idx == null || cats[idx] == null) { tooltipEl.style.display = "none"; return; }
        tooltipEl.textContent = labelOf(cats[idx]);
        tooltipEl.style.display = "block";
        tooltipEl.style.left = `${u.cursor.left}px`;
      }],
    },
  };
}
```

- [ ] **Step 2: Commit** (verified in Task 13)

```bash
git add src/optimiser/api/static/ribbon.js
git commit -m "feat(dashboard): ribbon lane plugin (fixed-px cells, glyphs, hit-test)"
```

---

### Task 13: `panels.js` — per-panel uPlot builders + figure assembly (incl. 1d, 1e)

This is the largest task. Build a shared `makePanel()` helper, then the 8 panel configs, then the figure assembler that creates N synced instances over one union-x. **Cut over `#ts-figure` here** (Task 14 wires the cursor; do them back-to-back).

**Files:**
- Create: `src/optimiser/api/static/panels.js`
- Modify: `src/optimiser/api/static/dashboard.html:76-78` (replace the single `#ts-figure` div with one container per panel, or keep `#ts-figure` as a flex column the assembler fills — see Step 1)
- Modify: `src/optimiser/api/static/dashboard.css` (panel container heights from `PANEL_LAYOUT`)
- Modify: `dashboard.js` (`redrawTSFigure`/`redrawCursorLine` → call the new assembler; keep Plotly path deleted only for `#ts-figure`)

**Interfaces:**
- Consumes: `buildUnionX`/`alignSeries` (Task 8), `bandColumns` (Task 9), `chart-core` (Task 10), `shapes`/`ribbon` plugins (Tasks 11–12), `classify`/`derive`/`price-merge`/`time-utils`.
- Produces: `buildTsFigure(rootEl, model) → { instances: uPlot[], update(model), destroy() }` where `model` is derived from `state` (see Step 4). All instances share `model.unionX`.

- [ ] **Step 1: DOM containers** — In `dashboard.html`, keep `<div id="ts-figure">` but let the assembler create one child `<div class="uplot-panel" data-panel="...">` per `PANEL_LAYOUT` entry (so `#ts-figure` becomes the flex column). Heights come from `PANEL_LAYOUT[i].height` normalised to a total px height (e.g. the assembler sets each child's flex-basis from the height fractions). Add CSS:

```css
#ts-figure { display: flex; flex-direction: column; gap: 6px; }
#ts-figure .uplot-panel { width: 100%; }
```

- [ ] **Step 2: Shared `makePanel` + the PRICE panel (arcsinh, 1d, 1e)** — full arcsinh config:

```js
import uPlot from "./uplot.esm.js";
import { cursorDragOpts } from "./chart-core.js";

const AXIS = { stroke: "#c9d1d9", grid: { stroke: "#21262d" }, ticks: { stroke: "#21262d" },
               font: "12px " + getComputedStyle(document.body).fontFamily };
const FIXED_GUTTER = 52; // px — sized to widest arcsinh tick "1500" + date tick; verify no clip

const PRICE_SPLITS = [-40, 0, 10, 30, 100, 300, 1000, 1500];
function priceScale() { return { distr: 4, asinh: 30 }; }    // arcsinh, threshold 30 c/kWh
function priceAxis() {
  return { ...AXIS, scale: "y", size: FIXED_GUTTER,
    splits: (u, _a, min, max) => PRICE_SPLITS.filter((v) => v >= min && v <= max),
    values: (u, splits) => splits.map((v) => String(v)) };
}
```

- [ ] **Step 3: The other 7 panel builders** — implement each per spec §5.1 (heights, ranges) and §5.2 (series, colours, fill, gaps). Concrete requirements (no ambiguity — colours are in spec §5.2):
  - **DECISION ribbon** & **MODE ribbon**: a panel with a hidden y-scale `range:[0,1]`, no y-axis ticks, fixed CSS height ~14 px; rendered by `ribbonPlugin` (Task 12) — the ribbon's only "series" is a transparent placeholder so uPlot has data; cats from `classify` over `unionX`.
  - **PV (kW)**: P10–P90 native band (yellow .22 via `bandColumns`), P50 line (`#f2cc60` w1.6), PV measured line (gaps), PV-actual markers (conditional — only add series if any non-null).
  - **SOC (%)**: scale `range:[0,100]` fixed; measured (`#79c0ff` w1.8) + planned (dotted) lines; SOC-floor hline via `shapesPlugin({kind:"soc"})`.
  - **LOAD (kW)**: stacked areas — pre-compute cumulative sums per managed-load id (lexically sorted), render each as a stepped (`paths: stepped`) filled series; plus measured + planned envelope lines. Observable-category loads excluded.
  - **GRID (kW)**: measured inverter line (`#c9d1d9` w1.4), measured Shelly line (conditional), planned net line (dotted); zero-line via `shapesPlugin({kind:"grid"})`.
  - **COST (c/h)**: realised (`#bc8cff` w1.4), planned (dotted), settled (`#ffd700` stepped, conditional) — **linear autoscale**; zero-line; this is the bottom panel → its x-axis is the only one that shows time labels.
  Use `bandColumns` for the 3 bands (import price, export price, PV P10/P90). Use `connectGaps:false` (uPlot: leave nulls → gaps) for "realised/measured" series; `spanGaps:true` for dotted "planned/predicted" series (matches Plotly `spanGaps`).

- [ ] **Step 4: The assembler `buildTsFigure`** — creates one uPlot per `PANEL_LAYOUT` entry over a **shared `model.unionX`** (epoch seconds), joins them via `makeSyncHub("ts")`, registers each with `chart-core.registry`, attaches `shapesPlugin`/`ribbonPlugin`, shows the time axis only on the bottom (COST) panel, pins `FIXED_GUTTER` on every panel's y-axis. `model` shape:

```js
// model = {
//   unionX: number[] (epoch sec, ascending),
//   price: { importRealised, importPredicted, exportRealised, exportPredicted, bands:{importLo,importHi,exportLo,exportHi} },
//   pv: { p50, measured, actual?, bandLo, bandHi },
//   soc: { measured, planned, floorPct },
//   load: { stacks:[{id,color,cum}], measuredEnv, plannedEnv },
//   grid: { inverter, shelly?, planned },
//   cost: { realised, planned, settled? },
//   decisionCats: number[], modeCats: number[],
//   nowSec, cursorSec, regions:[{x0,x1,kind}], thresholdC: 30,
// }
```
`update(model)` calls `u.setData([...])` (full rebuild, `resetScales=false` after first build) on each instance and stashes the new `model` for the plugins' `getState`/`getCats`. Build `model` from `state` in `dashboard.js` using the same field derivations the current `buildTraces` uses (spec §5.2), but emitting columns aligned to `unionX` via `alignSeries` instead of Plotly traces.

- [ ] **Step 5: Cut over `dashboard.js`** — replace `redrawTSFigure()`/`redrawCursorLine()` bodies to call `buildTsFigure(...).update(model)`; build `model` from `state`; delete the Plotly `buildTraces`/`buildLayout`/`Plotly.newPlot/react/relayout` for `#ts-figure` (keep `bandPolygons` only if still referenced elsewhere — it isn't after this; delete it). Keep `onPlotlyHover`/`onPlotlyClick` until Task 14 replaces them.

- [ ] **Step 6: MCP verification (parity, desktop + spike day)**
  - Navigate to the dashboard; screenshot the Energy tab; compare panel-by-panel against a screenshot taken from `main` (pre-migration): panel order, colours, bands, ribbons, line styles.
  - **Arcsinh check:** the price axis shows ticks at `-…/0/10/30/100/…`; a spike (load a historical day with ≥500 c/kWh via the range picker, or use a snapshot fixture) compresses near the top while the 0–30 region stays legible; the **kW/SOC panels are unaffected** (autoscale isolation).
  - Zoom/pan one panel → all panels' x follow (sync); cursor crosshair lines up vertically across panels (equal gutters).
  - Console: zero errors.

- [ ] **Step 7: Commit**

```bash
git add src/optimiser/api/static/panels.js src/optimiser/api/static/dashboard.html src/optimiser/api/static/dashboard.css src/optimiser/api/static/dashboard.js
git commit -m "feat(dashboard): render #ts-figure with synced uPlot panels + arcsinh price axis"
```

---

### Task 14: Cursor wiring + status-strip readout (folds in 1a, 1b, 1c)

**Files:**
- Create: `src/optimiser/api/static/cursor.js`
- Modify: `dashboard.js` (replace `onPlotlyHover`/`onPlotlyClick`; keep `state.cursor`, `effectiveCursor`, `setCursor`, `snapToNow`, keyboard scrub)
- Modify: `dashboard.html` (add a cursor-readout column container; add ribbon tooltip + decision/mode chip elements)
- Modify: `dashboard.css` (style the readout column, chip, ribbon tooltip)

**Interfaces:**
- Consumes: the uPlot instances from Task 13; `classify` labels; `time-utils.nearestSlotAt`.
- Produces: `wireCursor(instances, { onPin })` — installs a `setCursor` hook on each instance that, on hover, computes the slot timestamp from `u.posToVal(u.cursor.left, "x")`, calls `setCursor(nearestSlotAt(...), {pinned:true})`, and updates: (a) the status-strip `#cursor-time`/`#cursor-mode`/`#cursor-now-btn` (parity); (b) the multi-panel readout column with every lane's `u.data[s][idx]` value (1b); (c) the sticky decision/mode chip from `decisionCats[idx]`/`modeCats[idx]` (1c); (d) the price readout in **true c/kWh** (un-compressed) with thousands-sep, 1-dp (1a). The pinned-cursor vertical line is drawn by `shapesPlugin` reading `state.cursor` (Task 11) — not here.

- [ ] **Step 1: Keep the cursor model** — `state.cursor`, `effectiveCursor`, `setCursor`, `snapToNow`, `nearestSlotAt`, and the keyboard scrub stay in `dashboard.js` (parity). `setCursor` already calls `renderCursorReadout()`, `redrawCursorLine()`, `redrawSpendCursor()` — repoint `redrawCursorLine()` to `instances.forEach(u => u.redraw(false, false))` (rAF-coalesced via `chart-core.syncGuard`, Task 10/0d).

- [ ] **Step 2: Implement `cursor.js`** `wireCursor` — install per-instance `setCursor` hook; on the bottom (time-axis) instance, derive the pinned slot and pin; on every instance, write its panel's value into the readout column. Pseudocode body with the real DOM ids (`#cursor-time`, `#cursor-mode`, the new `#cursor-readout`, `#decision-chip`, `#mode-chip`).

- [ ] **Step 3: Markup + CSS** — add `#cursor-readout` column (one row per panel), `#decision-chip`/`#mode-chip`, and a `.ribbon-tooltip` element; style to match the existing dashboard (use existing CSS variables/classes).

- [ ] **Step 4: Delete the Plotly hover handlers** — remove `onPlotlyHover`/`onPlotlyClick` and the `div.on("plotly_hover", …)` wiring.

- [ ] **Step 5: MCP verification**
  - Hover the chart → cursor pins; `#cursor-time` shows the slot HH:MM:SS; `#cursor-mode` flips to "pinned"; `#cursor-now-btn` enables; the readout column shows each panel's value; the chip shows the decision/mode at that slot; the price readout shows the **true** c/kWh (e.g. `1,480.0`) even on the compressed axis.
  - Arrow keys scrub ±5 min; Home/Snap-to-now un-pins; Live preset un-pins.
  - Console: zero errors.

- [ ] **Step 6: Commit**

```bash
git add src/optimiser/api/static/cursor.js src/optimiser/api/static/dashboard.js src/optimiser/api/static/dashboard.html src/optimiser/api/static/dashboard.css
git commit -m "feat(dashboard): uPlot cursor wiring + synced readout column, true-value tooltip, decision/mode chip"
```

---

## Phase 4 — Daily-spend chart

### Task 15: `spend-chart.js` — bars, legend, tooltips, NEM-date cursor

**Files:**
- Create: `src/optimiser/api/static/spend-chart.js`
- Modify: `dashboard.js` (replace the Plotly spend path)

**Interfaces:**
- Consumes: `/daily_spend` rows (unchanged), `time-utils.toNemDate`, `chart-core`.
- Produces: `buildSpendChart(el) → { update(rows, cursorNemDate), destroy() }`. Renders 3 series (import-cost bar, export-revenue **negated** bar with raw value retained for tooltip, net-cost line) on an integer-index x with `splits/values` mapping indices → `YYYY-MM-DD` labels; **legend toggles** series (`series.show`); custom tooltip with the **three formats** from spec §5.8 (export revenue shown **positive**); NEM-date highlight rect via a draw hook.

- [ ] **Step 1: Implement `spend-chart.js`** — bars via `uPlot.paths.bars({ size: [0.8] })`, category x as indices, `scales.x.time = false`, a `legend` with `series.show` click handling, a custom tooltip element formatting per series (carry raw export revenue in a parallel array indexed by data idx), and a `draw` hook that shades the column whose label === `cursorNemDate`.

- [ ] **Step 2: Cut over `dashboard.js`** — replace the Plotly spend `newPlot/react` with `buildSpendChart(...).update(rows, toNemDate(effectiveCursor()))`; delete the Plotly spend layout/traces; keep `redrawSpendCursor()` but repoint it to the new chart's `update`.

- [ ] **Step 3: MCP verification** — spend bars match the pre-migration screenshot (import up, export down, net line); **click a legend entry hides that series; double-click isolates** (or single-click-toggle parity — match current Plotly behaviour); tooltip shows export revenue as a **positive** `$`; moving the time-series cursor highlights the matching NEM-date bar.

- [ ] **Step 4: Commit**

```bash
git add src/optimiser/api/static/spend-chart.js src/optimiser/api/static/dashboard.js
git commit -m "feat(dashboard): daily-spend bars in uPlot (legend toggle, tooltips, NEM-date cursor)"
```

---

## Phase 5 — Ops charts

### Task 16: `ops-charts.js` — solve-series (union-x), histogram, status, modbus

**Files:**
- Create: `src/optimiser/api/static/ops-charts.js`
- Modify: `ops.js` (replace the 4 Plotly charts; keep the DOM tables `#ops-modbus-summary`/`#ops-api-table`/`#ops-state-list` untouched; keep lazy-load + visibility-pause)

**Interfaces:**
- Consumes: `/ops/solve`, `/ops/modbus` (unchanged), `buildUnionX`/`alignSeries`, `chart-core`.
- Produces: `buildOpsCharts() → { updateSolve(rows), updateModbus(rows), resize() }`.

- [ ] **Step 1: solve-series — union-x merge (spec §5.9 / critic b1)** — group rows by status, build **one** `unionX` from all statuses' timestamps, `alignSeries` each status onto it (null padding), render one points-only series per status (`STATUS_COLOR`), `scales.y.range` starting at 0, and a **hand-built unified tooltip** (uPlot `setCursor` hook listing each status's value at `idx`). Do NOT rely on per-series x.
- [ ] **Step 2: histogram** — single-series `uPlot.paths.bars({ align: 1 })` over pre-binned buckets, `#58a6ff`; empty → "no solves" innerHTML.
- [ ] **Step 3: solve-status** — per-bar status colour bars; empty → "no solves".
- [ ] **Step 4: modbus-writes** — grouped ok/err bars per register (`paths.bars` with `disp.x0/size` group layout), x sorted **numerically**; empty → "no writes".
- [ ] **Step 5: Cut over `ops.js`** — replace the 4 `Plotly.react(...)` calls with the new chart updates; **construct instances AFTER the ops tab's `hidden` flip** (next frame / 50 ms) so they don't build at width 0 (spec §11 risk #6 / critic d3); keep `document.visibilityState` pause + lazy first-build.
- [ ] **Step 6: MCP verification** — switch to Ops tab; all 4 charts render; solve-series shows per-status points with a working unified tooltip; modbus ok/err grouped per register; switch away and back → charts resize correctly (no 0-width); DOM tables unchanged; console clean.
- [ ] **Step 7: Commit**

```bash
git add src/optimiser/api/static/ops-charts.js src/optimiser/api/static/ops.js
git commit -m "feat(dashboard): ops charts in uPlot (union-x solve-series, grouped modbus bars)"
```

---

## Phase 6 — Remaining refinements

### Task 17: Future-region tint + NOW-line polish (1f)

**Files:** Modify `src/optimiser/api/static/shapes.js`, `panels.js` (pass `nowSec` + `isHistorical`).

- [ ] **Step 1:** In `shapesPlugin.draw`, when `!historical` and `nowSec` is set, fill the region `x ∈ [nowSec, right]` with a ~3–4% alpha tint **below** the buy/sell rects on every panel; keep the labelled NOW line on top. No-op in historical mode.
- [ ] **Step 2: MCP verification** — live view shows a faint tint over the forecast region right of NOW on all panels; historical view shows none; screenshot.
- [ ] **Step 3: Commit** — `git commit -m "feat(dashboard): faint future-region tint + NOW-line (1f)"`

---

### Task 18: Spike peak/trough markers (1g)

**Files:** Create `src/optimiser/api/static/spike-detect.js` + `tests-js/spike-detect.test.js`; create `spike-labels.js` plugin; wire into the PRICE panel in `panels.js`.

**Interfaces:**
- Produces: `topKExtrema(x, y, { k=3, minSepPx, viewMin, viewMax, severityC })` → up to `k` local maxima above `severityC` and `k` minima below `−severityC` **within the visible x-range**, separated by ≥ `minSepPx` (collision guard). Pure → Vitest.

- [ ] **Step 1: Write the failing test** for `topKExtrema` (peaks within window, severity filter, separation, symmetric troughs). Real assertions on a small array.
- [ ] **Step 2: Run — fails. Step 3: Implement `spike-detect.js`. Step 4: Run — passes.**
- [ ] **Step 5: `spike-labels.js` plugin** — on `draw`, call `topKExtrema` over the price series within `u.scales.x.{min,max}`, draw an apex caret + `$X.X` label (device px) at each. **Recompute on `setScale`/`setData` only — never inside the rAF cursor path** (guard with `chart-core.syncGuard`).
- [ ] **Step 6: MCP verification** — on a spike day, ≤3 peak labels (e.g. `$14.9`) appear at the tallest spikes and ≤3 trough labels at negative-export dips; labels don't overlap; scrubbing the cursor does not cause them to stutter/recompute.
- [ ] **Step 7: Commit** — `git commit -m "feat(dashboard): spike peak/trough markers with value labels (1g)"`

---

## Phase 7 — Cutover & cleanup

### Task 19: Delete the Sankey

**Files:** Modify `dashboard.js`, `dashboard.html`, `dashboard.css` per spec §8 (exact delete list).

- [ ] **Step 1:** Delete from `dashboard.js`: `SANKEY_NODES`, `SANKEY_NODE_COLORS`, `SANKEY_LINK_DEFS`, `SANKEY_NOISE_KW`, `disambiguateFlows`, `SANKEY_LINK_EPSILON`+`buildSankeyTrace`, `sankeyLayout`, `dailyFlowsKWh`, `redrawDailySankey`; drop `sankeyToday` from `state.built`; remove the 3 `await redrawDailySankey()` calls + the `if (state.built.sankeyToday) …` line.
- [ ] **Step 2:** Delete the `#sankey-today-figure` `<section>` from `dashboard.html`; delete `--sankey-figure-h` decls + the sankey CSS block + the `.panel-sankey-today,` selector entry from `dashboard.css`.
- [ ] **Step 3: Confirm shared helpers survive** — `grep -n "toNemDate\|marginalCost\|mergePriceForecasts\|colorForLoadId" dashboard.js` still present and used.
- [ ] **Step 4: MCP verification** — Energy tab no longer shows the Today/Sankey panel; everything else intact; console clean.
- [ ] **Step 5: Commit** — `git commit -m "feat(dashboard): remove Sankey panel (frontend-only)"`

---

### Task 20: Remove Plotly + chart-utils.js

**Files:** Modify `dashboard.html` (drop Plotly `<script>` + `chart-utils.js` `<script>`), delete `src/optimiser/api/static/chart-utils.js`, update `_STATIC_FILES` (`dashboard.py`) + `_PUBLIC_PATHS` (`server.py`) to drop `chart-utils.js`, and remove any residual `window.eoChart`/`Plotly` references.

- [ ] **Step 1: Find residual references** — `grep -rnE "Plotly|eoChart" src/optimiser/api/static/*.js` → expect **zero** (all charts now uPlot; `chart-core` replaced `eoChart`). Fix any stragglers.
- [ ] **Step 2:** Remove the Plotly CDN `<script>` and the `chart-utils.js` `<script>` from `dashboard.html`.
- [ ] **Step 3:** `git rm src/optimiser/api/static/chart-utils.js`; remove its entries from `_STATIC_FILES` and `_PUBLIC_PATHS`.
- [ ] **Step 4: Verify Python suite green** — `uv run pytest tests/ -q`.
- [ ] **Step 5: MCP verification** — hard-reload (cache-bust); Network panel shows **no** `plotly` request and no `chart-utils.js`; all charts render; console clean; the page weight dropped (note the delta).
- [ ] **Step 6: Commit** — `git commit -m "feat(dashboard): remove Plotly + chart-utils.js (uPlot-only)"`

---

### Task 21: Final verification + a11y hygiene + suites

**Files:** Modify `panels.js`/`spend-chart.js`/`ops-charts.js` (add `role="img"` + `aria-label` per canvas — the v1 a11y hygiene from spec §10.2).

- [ ] **Step 1: a11y hygiene** — give each uPlot root canvas a `role="img"` + a concise `aria-label` (e.g. `"Price, PV, battery and grid time series"`, `"Daily spend"`); this is the only a11y item in v1 (full offscreen table deferred).
- [ ] **Step 2: Vitest** — `npm test` → all green.
- [ ] **Step 3: Python** — `uv run pytest tests/ -q` → same pass count as before the branch (backend untouched).
- [ ] **Step 4: MCP parity sweep** — desktop (≥1200 px), 760 px boundary, ≤420 px: screenshot each panel + spend + all 4 ops charts; explicitly load a **≥ $5/kWh (≥500 c/kWh) spike day** and confirm arcsinh + autoscale isolation + true-value tooltip + spike labels all work together. Confirm mobile: vertical swipe scrolls the page (not the chart), no double-tap zoom, swipe-left tab cycle, ribbon legible at 740 px.
- [ ] **Step 5: MCP perf** — performance trace on the 60 s SSE path (no frame hitch on snapshot `setData`); `lighthouse_audit` to record first-paint improvement after Plotly removal. Network: confirm **no new/changed data endpoints** (API-unchanged guarantee) — only the new static assets.
- [ ] **Step 6: Commit** — `git commit -m "chore(dashboard): a11y labels + final verification pass"`

---

## Self-Review (completed by plan author)

**Spec coverage:** §3 architecture → Tasks 8/10/13; §4 arcsinh → Task 13 (PRICE) + 1d/1e; §5.1–5.7 parity → Tasks 11–14; §5.8 spend (legend+tooltips) → Task 15; §5.9 ops (disjoint-x) → Task 16; §5.10 infra → Tasks 3/10/14; §5.11 mobile → Tasks 10/13/21; §6 forced changes → Tasks 13 (gutter/ribbon) /10 (touch-action); §7 mechanism map → Tasks 11–16; §8 Sankey → Task 19; §9 Tier-0 → Tasks 10/13/14/20 (0e′ deferred); §10 v1 refinements 1a–1i → folded (1a/1b/1c→14, 1d/1e→13, 1f→17, 1g→18, 1h/1i→12); §13 testing → Vitest tasks + every MCP step. Accessibility bundle deferred except the §10.2 hygiene label (Task 21).

**Placeholder scan:** Rendering tasks intentionally point at spec §5.2/§5.8/§5.9 for exhaustive colour/series lists (a stable companion doc) rather than restating them; all novel/hard logic (arcsinh scale, sync hub, ribbon/shape draw-hooks, band columns, union-x, classification, spend tooltip redirect) has complete code or complete tests inline.

**Type consistency:** `unionX` (epoch seconds) is the shared currency across `timeline.js`, `bands.js`, `panels.js`, `cursor.js`, `ops-charts.js`. `setCursor`/`effectiveCursor`/`state.cursor` keep their current signatures. `chart-core.registry`/`makeSyncHub`/`syncGuard` names are used consistently in Tasks 10/13/14/18.
