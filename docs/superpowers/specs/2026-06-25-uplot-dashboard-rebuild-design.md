# Design: Plotly → uPlot Dashboard Rebuild (with arcsinh price scaling)

- **Date:** 2026-06-25
- **Status:** Approved design — ready for implementation plan
- **Topic:** Replace the operator dashboard's Plotly charting with vendored uPlot; add arcsinh spike compression to the price axis; keep the backend API unchanged.

---

## 1. Goals

1. **Replace Plotly with uPlot** across the entire operator dashboard (`src/optimiser/api/static/`). Plotly is removed from the repo.
2. **Handle price-spike events** by compressing the price axis with **arcsinh** (uPlot-native `distr: 4`, `asinh: 30`), so a 1500 c/kWh spike and a −40 c/kWh export price are both legible on the same lane as normal 0–30 c/kWh prices.
3. **Keep the backend data API unchanged.** This is a frontend-only rebuild. No handler, route, or response shape under `src/optimiser/api/` changes.
4. **Strict behavioural parity** with the current dashboard, plus a curated set of value-add refinements that uPlot makes cheap.

### Non-goals

- No backend/API changes (explicitly out — the contract is locked).
- No Sankey diagram (removed — see §9).
- No accessibility overhaul in v1 (deferred to v1.1 — see §10.3). Keyboard scrub is preserved as **parity** (it exists today); only the new a11y *extras* are deferred.

---

## 2. Locked decisions

| Decision | Choice |
|---|---|
| Charting library | uPlot, vendored/self-hosted (no CDN) |
| Plotly | Removed entirely |
| Sankey panel | Removed (frontend-only delete; no backend route exists for it) |
| Price spike handling | arcsinh, always-on, `distr: 4`, `asinh: 30`, **price axis only** |
| Layout engine | **N synced uPlot instances** (one per panel) via `uPlot.sync()` — not a single multi-scale instance |
| Backend API | Unchanged |
| Fidelity | Strict parity + Tier-0 correctness/perf + refinement bundles 1–3 |
| Refinements deferred | Accessibility bundle (1j/1k-extras/1l/1m) → v1.1 |
| JS tests | Add **Vitest**, scoped to pure logic only (union-timeline merge, band run-building, decision/mode classification, `nearestSlotAt`/`toNemDate`, spike top-K) |

---

## 3. Architecture

### 3.1 N synced instances (not single multi-scale)

uPlot has **no native subplots**. The "one instance, many y-scales" trick cannot give independent vertical panel domains, per-panel gridlines, the SOC `[0,100]` clamp, or — critically — **per-panel autoscale isolation**. We use **one uPlot instance per panel**, joined by `uPlot.sync(key)` (the documented `sync-cursor` idiom).

Why this is the right call:

- **Autoscale isolation is a hard requirement.** A 1500 c/kWh price spike must rescale **only** the PRICE lane, never flatten the kW/SOC/COST lanes. Independent instances give this for free; a single multi-scale instance couples scale recompute and makes a COST-lane blow-out trivially easy.
- It is the only structure that can express the selected refinements (multi-panel cursor readout, fixed-px ribbon lanes) and future v1.1 items (collapse/expand, per-panel a11y figure).

**Costs we accept and must manage:**

- **x-zoom/pan is NOT auto-synced** — only the cursor crosshair syncs (by *value*: `cursor.sync.scales: ['x', null]`). We add a guarded `setScale` hook on each instance that propagates `{min,max}` to peers, protected by a single shared in-progress flag against feedback loops (shared with the cursor-redraw path — see §12 d4).
- **Equal left-gutter alignment is a correctness requirement.** Every panel pins an identical y-axis `axis.size` (sized to the widest arcsinh tick label, e.g. "1500"); the shared time axis renders only on the bottom panel; others are padded. Without this the crosshair lands at a different screen-x per panel and the synced readout is wrong.

### 3.2 The union timeline (keystone — build first)

uPlot requires **one ascending x array with null-padded, aligned y columns**. The dashboard merges multiple cadences and epochs onto one timeline:

- 5-min + 30-min prices (5-min wins where present — preserve the `pickPriceAt`/`_price_at` linear-scan semantics);
- past (telemetry) + future (forecast/plan) seam;
- telemetry that arrives **paged** (`fetchTablePaged`, `since` advanced by +1 ms) and must be concatenated/deduped before merge.

**Invariant:** *all* panel instances are built on the **identical** shared x column. Conditional series (see §5.2) must never tempt per-panel x trimming, or `u.cursor.idx` diverges across panels and `u.data[s][idx]` reads the wrong row.

`union-timeline.js` is a pure module, unit-tested with Vitest, and **lands before any panel/ribbon/band/ops work** (§12).

### 3.3 Module / file layout

```
static/
  vendor/uplot.iife.min.js, uPlot.min.css     # vendored, version-pinned, self-hosted
  chart-core.js        # replaces window.eoChart: isNarrow, breakpoint hub, instance
                       #   registry, resizeAll() via explicit setSize, sync-hub factory,
                       #   x-zoom propagation glue, mobile cursor/drag flags
  union-timeline.js    # merge past+future+multi-cadence → one ascending x + null cols;
                       #   full rebuild (150s path) + incremental seam-splice (deferred 0e′)
  plugins/
    ribbon.js          # categorical draw-hook lanes (DECISION + MODE) + hit-test tooltip
    bands.js           # confidence-band setup: sort/dedup, <2-run skip, null-both-bounds
    shapes.js          # now-line, pinned-cursor vline, buy/sell rects, floor/zero hlines,
                       #   asinh-threshold line — all draw / drawClear hooks
    spike-labels.js    # (refinement 1g) windowed top-K peak/trough markers
  panels/
    price.js soc.js pv.js load.js grid.js cost.js   # per-panel uPlot opts builders
    spend.js           # daily-spend bars + interactive legend + NEM-date highlight
    ops-charts.js      # solve-series / histogram / status / modbus-writes
  cursor.js            # state.cursor model, effectiveCursor, setCursor, snapToNow,
                       #   nearestSlotAt, keyboard scrub, status-strip + readout DOM
  dashboard.js         # orchestration: state, SSE, range bar, tabs, applySnapshot
  ops.js               # ops pollers/DOM tables (mostly unchanged) + ops-charts wiring
```

- `chart-core.js` keeps the public surface shape of `eoChart` (`isNarrow`, `registerPlot`, `resizeAll`, `onBreakpointChange`) so rewiring `dashboard.js`/`ops.js` is mechanical; internals swap Plotly resize for explicit `setSize` (uPlot has **no `responsive: true`** — every resize is explicit).
- Each `panels/*.js` exports `build(container, sharedX, syncHub) → uPlot`.

### 3.4 Vendoring

Vendor uPlot (~50 KB) under the existing bearer-auth-free static path. **No CDN** — the service runs host-network in a container with no guaranteed internet. Pin the exact version, add the two files to the static whitelist (`server.py` `_STATIC_FILES` + the `dashboard_static` content-type map), verify content-type, and preserve load order `chart-core → dashboard → ops`.

---

## 4. arcsinh price scaling

- Price panel scale: `{ distr: 4, asinh: 30 }`. Linear within ≈±30 c/kWh, logarithmic beyond, smooth and sign-symmetric (handles negative export prices). Deterministic across ticks; no UI toggle.
- **Explicit named ticks** (refinement 1d) via custom `splits` in data space: `[-40, 0, 10, 30, 100, 300, 1000, 1500]`, filtered to the current `[min,max]` so flat days still show `0/10/30`. uPlot's auto-`distr:4` ticks are decades and miss `0`/`30` and negative-region resolution.
- `axis.size` for the price gutter is sized from this tick set (widest label) and reused as the fixed gutter for **all** panels (§3.1).
- COST panel stays **linear-autoscale** (parity); arcsinh on COST is explicitly out of v1 (§10.3 3c).

---

## 5. Faithful-port parity inventory (the parity floor)

Everything in this section must be reproduced. `[RISK]` = needs custom uPlot work or is a behaviour-sensitive port.

### 5.1 Main figure (`#ts-figure`) — 8 stacked panels, one shared x

Driven by the `PANEL_LAYOUT` array + `panelDomains()` — **port off that array, not Plotly axis IDs** (axes are non-contiguous `y,y2,y3,y4,y5,y6,y7,y9`; no y8; screen order ≠ axis order). `PANEL_GAP = 0.03`. Axis styling: grid `#21262d`, tick font 12 / `#c9d1d9`, no zeroline by default.

| # | Panel | h-frac | y-range | Sign | Notes |
|---|---|---|---|---|---|
| 1 | PRICE c/kWh | 0.22 | auto, neg-capable | ± | arcsinh `[RISK]` |
| 2 | DECISION ribbon | 0.035 | fixed [0,1], no ticks | — | draw-hook `[RISK]` |
| 3 | MODE ribbon | 0.035 | fixed [0,1], no ticks | — | draw-hook `[RISK]` |
| 4 | PV kW | 0.18 | auto ≥0 | + | band + 4 lines + markers |
| 5 | SOC % | 0.16 | **fixed [0,100]** | + | floor hline |
| 6 | LOAD kW | 0.22 | auto ≥0 | + | stacked areas `[RISK]` |
| 7 | GRID kW | 0.14 | auto | ± | zero-line |
| 8 | COST c/h | 0.16 | auto | ± | zero-line; bottom = x-axis anchor |

### 5.2 Traces (z-order back→front; "future" arrays empty in historical mode)

- **PRICE:** import band (`forecast_low/high`, orange .15) · export band (`export_forecast_low/high`, green .15) · import realised (`#f0883e` w1.6, gaps) · import predicted (dot w1.0, gaps) · export realised (`#56d364` w1.6, gaps) · export predicted (dot, gaps).
- **PV:** P10–P90 band (yellow .22) · P10 dot · P90 dot (both `hoverinfo:skip`) · P50 dot (`#f2cc60` w1.6, spanGaps) · PV measured (w1.6, gaps) · PV-actual markers (`circle-open` size4, **conditional** on `pvActualPast.some(v!=null)`).
- **SOC:** measured (`#79c0ff` w1.8, gaps) · planned (dot, spanGaps).
- **LOAD `[RISK]`:** per-load measured stack (`stackgroup:load-past`, `shape:hv`, fill .5) · measured envelope `max(house_load, Σmanaged)` (w1.6, gaps) · per-load planned stack (`stackgroup:load-future`, dot, fill .32) · planned envelope (dot). Per-load colour = `colorForLoadId` hash; **IDs sorted lexically** for deterministic stacking. Observable-category loads filtered out (they belong on GRID). uPlot has no auto-stacking → pre-compute cumulative sums + stepped paths (`paths.stepped`), `series.fill`.
- **GRID:** measured inverter (`#c9d1d9` w1.4, gaps) · measured Shelly (`#7ee787` w1.0, gaps, **conditional** on `hist.some(r.grid_kw_shelly!=null)`) · planned net `import+(−export)` (dot, gaps).
- **COST:** realised c/h = `marginalCost(ip,ep,grid_kw)` (`#bc8cff` w1.4, gaps) · planned c/h (dot) · settled c/h from `amber_usage ×12` (`#ffd700` w1.2, `shape:hv`, **conditional** on `settledCost.x.length>0`).

**Conditional-series handling under uPlot's fixed-columns model:** uPlot can't omit a series mid-stream the way Plotly omits a trace. Decide series membership at **instance-build time** (preferred) — if the conditional data is entirely absent, don't add the series; otherwise add it null-filled. Re-evaluate membership only on full rebuild, not on incremental `setData`.

### 5.3 Categorical ribbons (DECISION + MODE) `[RISK — highest]`

Currently Plotly single-row stepped-colorscale heatmaps. Port = draw-hook `fillRect` cells:

- x grid = `[...pastTs, ...slotTs]`; cell width = 5-min slot. Collapse equal-category adjacent slots into one rect.
- DECISION: 5 categories (`DECISION_COLORS`); past via `decisionFromTelemetry(planner_action)`, future via `decisionFor(s)` (signed `battery_kw` + grid/PV split; `MODE_SWITCH_HYSTERESIS_KW=0.05`, `DEADBAND_KW=0.1`).
- MODE: 7 categories (`MODE_COLORS`); past `modeFromTelemetry`, future `modeFromSlot` (mirrors `dispatch_from_slot`).
- Per-cell hover text `"${fmtTime} — ${LABEL}"`. **Single source of truth** — chips/readouts derive from these same arrays, never reclassify.
- **Device-px vs CSS-px (the #1 ribbon bug):** draw with `valToPos('x', true)` + `u.bbox` (device px); hit-test with `u.cursor.left/top` + `u.over` (CSS px). One shared px-bridge helper.

### 5.4 Confidence bands `[RISK]` — preserve `bandPolygons()` invariants

Three bands: import price, export price, PV P10/P90. Native uPlot `bands: [{ series: [loIdx, hiIdx], fill }]`, **but** native bands cannot express the per-run skip on their own. Required pre-processing pass on the bound columns:

- `mergePriceForecasts` **sorts by start + dedupes by start** (monotonic x) — preserve before band build.
- Wherever **either** bound is null at an index, **null both** bounds at that index (so uPlot doesn't bridge to baseline/adjacent).
- Discard any contiguous run of `< 2` points (degenerate vertical fill).
- Past=null/future=real is covered by null padding (band simply has no fill in the past).

### 5.5 Shapes & annotations `[RISK]` — draw-hook plugins / DOM overlays

1. **Buy/sell window rects** (per future slot): charge `grid_to_battery_kw > DEADBAND` amber `rgba(210,153,34,.10)`; export `grid_export_kw > DEADBAND` green `rgba(63,185,80,.10)`; span PRICE panel, below series. Live-only.
2. **SOC floor hline** on SOC panel, `#f85149` dashed, only if `config.battery.soc_floor_pct` finite.
3. **GRID zero-line** `#444c56` w0.6. 4. **COST zero-line** same.
5. **"Now" vline** green dotted `#56d364`, spans all panels, live + snapshot only.
6. **Pinned-cursor vline** blue `#58a6ff` w1.4, spans all panels, when `effectiveCursor()` non-null. **Reads `state.cursor`, NOT `u.cursor.idx`** (must survive `setData`).
7. **Panel-title overlays** (HTML, absolute-positioned): `<b>LABEL</b> units` top-left per panel; PRICE additionally appends live `imp X.X`/`exp X.X` from `currentLivePrices()`. **This live-price suffix is parity and must survive independently of refinement 1a.**

### 5.6 X-axis & time `[RISK]`

- Single shared date axis, `automargin` → replaced by fixed gutter (§7).
- `computeXRange()`: historical `[from,to]`; live `[now−24h, now+48h]` (`HISTORY_LOOKBACK_MS`/`FUTURE_HORIZON_MS`); `undefined` if no snapshot.
- **Drop the `toPlotlyTime` offset hack.** uPlot takes epoch **seconds** (÷1000) + native local formatting.
- `hovermode:"x unified"` → synced crosshair across the N instances.

### 5.7 Cursor model & status-strip

- `state.cursor = {time, pinned}`; `effectiveCursor()`; `setCursor`, `snapToNow`, `nearestSlotAt` (floor to 5-min `SLOT_MS`).
- DOM: `#cursor-time` (HH:MM:SS), `#cursor-mode` (live/pinned), `#cursor-now-btn` (disabled unless pinned), `.pinned` class on the status-block.
- Hover **pins** the cursor; click delegates to hover. **No unhover handler** — cursor stays pinned; only Live/Home/Snap un-pins.
- **Keyboard scrub (parity):** Arrow ±`SLOT_MS`, Home=snap, clamped in historical, ignored in INPUT/TEXTAREA.
- Live auto-advance: `applySnapshot` sets `cursor.time = nowFromSnapshot()` if `!pinned && !historical`.
- Cheap cursor-only redraw `redrawCursorLine()` → uPlot `u.redraw(false,false)`, rAF-coalesced (Tier-0 0d).
- Daily-spend cursor: `spendCursorShapes` maps `effectiveCursor()` → NEM date (`toNemDate`, UTC+10) → bar highlight.

### 5.8 Daily-spend bar (`#spend-figure`) `[RISK]`

- 3 series: import cost bar (orange), export revenue **negated** bar (green; **raw positive value carried separately for the tooltip**), net-cost line+markers (purple).
- `barmode:"relative"`, **x = category** (`YYYY-MM-DD`, re-sorted ASC) → uPlot integer-index x + `splits/values` label map, `time:false`, `uPlot.paths.bars`.
- **Interactive legend (parity — do not drop):** the spend chart has `showlegend:true` with Plotly's click-to-hide / double-click-to-isolate. Reproduce via uPlot legend `series.show` toggling (the main `#ts-figure` has `showlegend:false`, so this applies to spend only).
- **Tooltip formats (parity — do not drop):** three distinct formats — `import cost $%.2f` (from y), `export revenue $%.2f` **from the un-negated raw value** (not the drawn-negative y), `net $%.2f` (from y). The custom `setCursor` tooltip must replicate per-series formatting **and** the export-revenue value redirection.
- Cursor overlay: NEM-date band highlight (blue .12) via draw-hook.

### 5.9 Ops charts (`ops.js`) `[RISK]` — 4 charts, lazy (ops tab only)

- `ops-solve-series` `[RISK — under-rated]`: currently scattergl, **one group per status with its own disjoint x array** (`byStatus[s]={x:[],y:[]}`). uPlot's columnar model forbids per-series x → all status series go on **one union x-timeline with null padding** (same hazard as the main chart), and the `hovermode:"x unified"` across statuses becomes a **hand-built tooltip**. `rangemode:tozero` → y starts at 0. Rate this medium, not "scatter recolor."
- `ops-solve-histogram`: bar, categorical buckets, `#58a6ff`, pre-binned → single-series `paths.bars`, `align:1`.
- `ops-solve-status`: bar, per-bar status colour; empty → "no solves" innerHTML.
- `ops-modbus-writes`: 2 stacked bars (ok green / err red) per register, x categorical **sorted numerically** → `paths.bars` + grouped layout (`disp.x0/size`); empty → "no writes" innerHTML.
- DOM-only (keep as-is, not charts): `#ops-modbus-summary`, `#ops-api-table`, `#ops-state-list`.

### 5.10 Shared infra & live update

- `eoChart` → `chart-core.js`: `isNarrow()` (760 px breakpoint, single source of truth), breakpoint watchers + 50 ms `resizeAll`, `registerPlot`/`resizeAll` (skip `!offsetParent` / `clientWidth===0`). **uPlot resize is always explicit `setSize`.**
- SSE `/dashboard/stream` (fetch + ReadableStream, bearer header, backoff `[1,2,5,10,30]s`, `event:snapshot` → `applySnapshot`); `/plan/current` poll fallback (15 s, only if `!sseConnected`).
- `applySnapshot`: status strip, loads, cursor readout, ModesUI; charts redraw **live-only**.
- `refreshLiveHistory` (150 s, bulk 4-endpoint reload → full-rebuild `setData`); `renderTickAge` (1 s); visibilitychange force-refresh; `pollOnce` (15 s).
- Range bar: presets `live/today/yesterday/7d` (local-midnight, end-exclusive), custom date inputs, `updateRangeIndicator`, `applyRange` (wipe history, reload, redraw → `setScale('x')`).
- Tabs energy/ops via `hidden`; ops pollers start on show / stop on hide; 50 ms-after-`hidden`-flip `resizeAll`.

### 5.11 Mobile / touch (parity guarantees)

1. Vertical swipe scrolls **page** not chart → `cursor.drag:{x:false,y:false,setScale:false}` + `touch-action:pan-y`.
2. No scroll/pinch zoom (don't install wheel-zoom).
3. No double-click reset (inert once drag-zoom off).
4. No modebar (uPlot has none).
5. Breakpoint redraw re-applies margins + refits.
6. Narrow margins (energy `{28,6,22,36}` vs `{44,20,26,44}`; ops `{22,36,6,32}` vs `{26,48,12,36}`).
7. Hidden-chart resize on tab-show (50 ms).
8. Swipe-left tab cycle on `#status-strip` only, narrow-only, passive listeners (`MIN_DX=50, MAX_DY=40, RATIO=1.5, MAX_MS=600`, left-only, wraps).
9. `hovermode:"x unified"` on ts + spend + ops-solve-series.

---

## 6. Forced parity *changes* (3 — deliberate, documented)

1. **Fixed left-gutter `axis.size` replaces `automargin`.** Required for synced-crosshair alignment. This is a layout *change* (not free): `automargin` currently breathes with tick width. Size the gutter from the actual arcsinh tick set (§4) and the widest date tick; **verify no clipping** on an unexpectedly wide tick. `parity_safe: partly`.
2. **Ribbons become fixed ≈14 px lanes** instead of 0.035 domain-fractions (which collapse to ~near-invisible on a 740 px phone).
3. **`touch-action: pan-y`** for scroll-through instead of the Plotly `dragmode` hack — compositor-enforced, strictly better.

---

## 7. Feature → uPlot mechanism map

| Current feature | uPlot mechanism | Risk / notes |
|---|---|---|
| arcsinh price axis | `scale {distr:4, asinh:30}` + explicit `splits` + value formatter | Auto ticks are decades; named ticks need explicit splits |
| 8 stacked panels | N instances + `uPlot.sync`, `cursor.sync.scales:['x',null]` | x-zoom sync = manual guarded `setScale` |
| Equal alignment | fixed `axis.size` all panels; bottom-only time axis | Correctness req |
| DECISION/MODE ribbons | `draw` hook `fillRect` per slot; `setCursor` hit-test tooltip | device-vs-CSS px bridge; collapse equal-cat slots |
| Confidence bands ×3 | native `bands`, null-padded bounds | null-both-bounds + <2-run skip pre-pass |
| Now-line / cursor vline | `draw` hook vlines on every panel | cursor reads `state.cursor`, not `u.cursor.idx` |
| Buy/sell regions | `draw`/`drawClear` rect loop | live-only |
| floor / zero lines | `draw` hook hlines via `valToPos(v,scale,true)` | trivial |
| Stacked LOAD areas | cumulative sums + `paths.stepped` + `series.fill` | no auto-stacking |
| Daily-spend bars | `paths.bars`, integer-index x + `splits/values` | category x = indices, `time:false` |
| Spend legend toggle | uPlot legend `series.show` | parity — do not drop |
| Spend tooltips ×3 | custom `setCursor` tooltip + value redirect | export revenue shown positive |
| Ops grouped bars | `paths.bars` + `disp.x0/size` | numeric register sort |
| Ops histogram | single-series `paths.bars`, `align:1` | pre-binned |
| Ops solve-series | union-x + per-status series + hand-built unified hover | disjoint-x merge req |
| Cursor→status-strip | `setCursor` hook → `u.cursor.idx` → `u.data[s][idx]` → DOM | index aligns only if all panels share x |
| Range presets | DOM unchanged + `setScale('x')` | — |
| Tabs + resize | `hidden` + explicit `setSize` on show (50 ms) | uPlot never self-fits |
| SSE live update | `setData(data,false)` on full rebuild | incremental seam-splice deferred (0e′) |
| Mobile scroll | `cursor.drag` off + `touch-action:pan-y` | — |
| Panel titles + live price | HTML overlay divs | — |
| HiDPI | auto `pxRatio` + `matchMedia('(resolution)')` → `setPxRatio` | uPlot won't watch DPR |
| `legend.live` global readout | **not used** — built-in legend only covers its own instance | use custom synced readout (1b) |

---

## 8. Sankey removal — delete list

**Backend: nothing** (computed client-side from `state.history.rows` / `/telemetry`, which stays).

**`dashboard.js` — delete:** `SANKEY_NODES`, `SANKEY_NODE_COLORS`, `SANKEY_LINK_DEFS`, `SANKEY_NOISE_KW`; functions `disambiguateFlows`, `SANKEY_LINK_EPSILON`+`buildSankeyTrace`, `sankeyLayout`, `dailyFlowsKWh`, `redrawDailySankey`.

**`dashboard.js` — edit:** drop `sankeyToday:false` from `state.built`; remove the 3 `await redrawDailySankey()` calls + the `if (state.built.sankeyToday) redrawDailySankey()` line; update the header doc comment (cosmetic). **Leave** the MODE comment (refers to ribbon, not Sankey).

**`dashboard.html` — delete:** the `<section class="panel panel-sankey panel-sankey-today">` block (incl. `#sankey-today-figure`).

**`dashboard.css` — delete/edit:** `--sankey-figure-h` decls; the sankey comment+rules block; remove `.panel-sankey-today,` from the mobile selector list.

**Do NOT delete (shared):** `toNemDate`, `marginalCost`, `hexToRgba`, `colorForLoadId`, `mergePriceForecasts`, `bandPolygons`, `/telemetry`, `state.history.rows`, `registerPlot`/`resizeAll`.

---

## 9. Tier-0 — mandatory correctness/perf (ship by default, not optional)

| # | Item | parity_safe |
|---|---|---|
| 0a | Equal-gutter alignment (fixed `axis.size`, bottom-only time axis) | partly (see §6 item 1) |
| 0b | Per-panel autoscale isolation (COST stays linear) | yes |
| 0c | `touch-action:pan-y` + `cursor.drag.setScale:false` | yes |
| 0d | rAF-coalesced cursor/now-line redraw (one `u.redraw(false,false)`/frame) | yes |
| 0e | Full-rebuild `setData(data,false)` on snapshot/150 s reload | yes |
| 0f | Drop Plotly bundle, vendor uPlot | yes |
| 0g | HiDPI `setPxRatio` + visible-only `setData` | yes |
| 0h | Pause render on `document.hidden`, one coalesced catch-up on return | yes |

(0e′ incremental SSE seam-splice is **deferred to Tier-2** — minefield adjacent to paged telemetry, 150 s reload, pinned-cursor stability, and conditional-series membership. v1 ships full-rebuild `setData` only.)

---

## 10. Refinements

### 10.1 v1 — SHIP (selected bundles 1–3)

**Bundle 1 — Spike legibility + clarity:**
- **1a** TRUE c/kWh in tooltip/readout, never asinh-compressed (thousands-sep, 1-dp). The precise number is the only reliable read of spike magnitude on a compressed axis. Generalises `currentLivePrices()`.
- **1d** Explicit named price ticks `[-40,0,10,30,100,300,1000,1500]` filtered to range.
- **1e** Faint labelled threshold reference line at 30 c/kWh (the linear/log knee). Same plugin family as floor/zero lines.
- **1f** Labelled NOW line (parity) + faint (~3–4 % alpha) future-region tint right of now on every panel (below buy/sell rects; no-op in historical).
- **1g** Top-K (≤3) spike peak markers + `$` value labels, and symmetric negative-export troughs. **Effort med-high** (viewport-recompute on every `setScale`/`setData`, collision guard, device-px carets). Must **not** fire inside the rAF cursor path (0d) or it stutters scrub. Windowed top-K only.

**Bundle 2 — Scrubbable inspector:**
- **1b** Synced multi-panel cursor readout column: every lane's value + decoded DECISION/MODE at the cursor timestamp; render `—` on null index. (`parity_safe:no` — additive.)
- **1c** Sticky decision/mode colour chip (cursor-resolved, falls back to live slot-0). Derived from the same arrays as the ribbon (no reclassify).

**Bundle 3 — Ribbon legibility:**
- **1h** Fixed-px ribbon lanes (≈14 px) + tap/hover hit-test tooltip. Folds into the mandatory ribbon reimplementation.
- **1i** Non-colour-redundant ribbons (glyphs `G⁺`/`PV`/`DIS`/`IDL` + min-width guard) + WCAG contrast pass: bump faint band/forecast-line alphas to ≥3:1; diverge the two near-identical mode purples (`#bc8cff` vs `#8957e5`). Absorbs the band-stroke "don't vanish" need (replaces skipped 3b).

### 10.2 Deferred to v1.1 (Accessibility bundle + Tier-2)

- **1j** offscreen synced data-table + figure roles/aria-labels (core canvas-a11y mitigation). *In v1, each canvas DOES get a trivial `role="img"` + `aria-label` (near-zero cost, avoids a silent SVG→canvas regression); the full offscreen synced data-table is deferred.*
- **1k extras** focus-visible rings + skip-link + focusable chart wrapper (keyboard scrub itself is **parity**, kept in v1).
- **1l** prefers-reduced-motion freeze. **1m** ≥44 px tap targets.
- Tier-2: **0e′** incremental seam-splice · **2b** 7-day min/max decimation (measure first) · **2c** live ring-buffer · **2d** IntersectionObserver pause · **2e/2f** collapse/reorder panels · **2g** legend toggles (main chart) · **2h** locked y-ranges (price floor/ceil folds into 0b).

### 10.3 Skipped

- **3a** severity-scaled rect opacity (1g + 1a restore magnitude more honestly).
- **3b** band min-height floor (distorts; band stroke in 1i covers it).
- **3c** COST arcsinh (parity = linear; revisit only on operator report).

### 10.4 Out of scope (backend locked)

All server-side reduction candidates (marginal cost, flow disambiguation, decision/mode enums, forecast-merge seam, load envelope, modbus pivot) stay **client-side**.

---

## 11. Risks (ranked)

1. **Union-timeline merge correctness** (highest) — past+future seam + 5-min/30-min cadence + paged telemetry. Keep pure, snapshot the seam index, reuse `nearestSlotAt`, unit-test in isolation.
2. **Ribbon device-px vs CSS-px** — one shared px-bridge helper; visual-diff vs live.
3. **Equal-gutter + x-zoom sync glue** — fixed size to widest label; guard `setScale` propagation with a shared in-progress flag.
4. **Band sort/dedup/null-both/<2-run invariants** — port verbatim; test with partial-null fixtures.
5. **Pinned-cursor persistence across `setData`** — shapes plugin reads `state.cursor` only.
6. **Hidden-instance 0-width render** — explicit `setSize` on show/expand/breakpoint; **construct lazy ops instances *after* the `hidden` flip** (uPlot won't self-correct a cached 0-width canvas).
7. **Vendored-version / offline-LAN** — self-host pinned file, verify content-type + load order.
8. **Spend legend + tooltip-format regression** — the 3 formats + export-revenue-positive redirect + legend toggle are load-bearing parity.

---

## 12. Build order / sequencing

1. **`union-timeline.js` + Vitest** — lands and is green before anything consumes it. Establish the "all panels share one identical x" invariant.
2. **`chart-core.js`** (resize/setSize/sync hub) replaces `eoChart`. Sequence the **eoChart→chart-core swap and the Sankey deletion** so they don't both edit the same `dashboard.js` lines concurrently (do one, then the other).
3. **Plugins** (`shapes`, `bands`, `ribbon`) then **panels** then **spend** then **ops-charts**.
4. **`setScale` x-zoom propagation and the rAF cursor/spend-cursor coalescing share one feedback-loop guard** — design them together, not as two independent guards.
5. Vendoring + static-whitelist wiring can land early (independent).

---

## 13. Testing & verification

**Vitest (new, pure-logic only):** union-timeline merge; band run-building (sort/dedup/null-both/<2-skip); decision/mode classification; `nearestSlotAt`/`toNemDate`; spike top-K selector. **No** DOM/canvas assertions in the unit layer.

**chrome-devtools MCP (pixels + behaviour):**
- **Visual parity diff** live-Plotly vs uPlot, per panel, at desktop (≥1200 px), 760 px boundary, and ≤420 px. Explicitly capture a **price-spike day (≥$5/kWh)** — exercises arcsinh + autoscale isolation + spike labels + true-value tooltip together.
- **Console/network:** zero errors across first-paint, SSE snapshot, range switch, tab flip, breakpoint cross, visibility hide/return. Confirm SSE frames, `/plan/current` fallback only when SSE down, **no new/changed endpoints** (API-unchanged guarantee), uPlot correct content-type.
- **Interaction:** hover→pin, keyboard arrow-scrub smoothness, Home=snap, `cursor-now-btn` enable/disable, range presets + custom range, NEM-date spend highlight tracking, **spend legend toggle + the 3 tooltip formats**.
- **Mobile:** `emulate`/`resize_page` to a phone — vertical swipe scrolls page, no scroll/double-tap zoom, swipe-left tab cycle, ribbon legibility at 740 px.
- **Perf/memory:** performance trace on the 60 s SSE path (frame-hitch gone with full-rebuild `setData`; informs whether 0e′/2b are needed); `lighthouse_audit` for first-paint after Plotly removal.

---

## 14. Open questions / future

- v1.1 accessibility (offscreen table, focus rings, skip-link, reduced-motion, tap targets).
- Tier-2 perf (incremental seam-splice, 7-day decimation, ring-buffer) — gate on measured evidence.
- COST arcsinh — revisit only if operators report cost-spike illegibility.
