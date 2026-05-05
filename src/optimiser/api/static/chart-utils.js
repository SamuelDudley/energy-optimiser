// Shared mobile/touch chart behaviour.
//
// Loaded BEFORE dashboard.js and ops.js. Exposes `window.eoChart` with the
// helpers each chart needs to behave correctly on touch devices:
//
//   - `isNarrow()`             — viewport check at the project breakpoint
//   - `mobileLayoutFragment()` — Plotly layout bits that flip on narrow
//                                viewports (notably `dragmode: false`, so
//                                vertical touch-scroll over the chart
//                                scrolls the page instead of panning the
//                                axis — Plotly's default `dragmode: "zoom"`
//                                eats the gesture entirely)
//   - `mobileConfig()`         — Plotly config defaults: kills scroll-zoom
//                                and the modebar buttons we never want;
//                                stays `responsive: true`
//   - `onBreakpointChange(fn)` — register a redraw to fire when the
//                                viewport crosses the breakpoint. Plotly's
//                                built-in `responsive: true` re-sizes but
//                                does NOT re-evaluate dragmode/margins, so
//                                every chart that wants to respond to
//                                rotation/resize must redraw itself.
//
// **Every new chart in the dashboard must use these helpers.** If you
// `Plotly.newPlot(div, traces, layout, config)` without merging in
// `mobileLayoutFragment()` and `mobileConfig()`, mobile users won't be
// able to scroll past your chart.
(function () {
  "use strict";

  const MOBILE_BREAKPOINT_PX = 760;

  function isNarrow() {
    return (
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT_PX}px)`).matches
    );
  }

  // `desktopDrag` — Plotly dragmode used on wide viewports. "pan" is the
  // most common choice (the time-series chart). For category-axis bar
  // charts (daily spend, ops modbus-writes) "zoom" is fine — there's no
  // pan along a categorical axis; we just want it disabled on narrow.
  function mobileLayoutFragment(opts) {
    const o = opts || {};
    const desktopDrag = o.desktopDrag != null ? o.desktopDrag : "pan";
    return { dragmode: isNarrow() ? false : desktopDrag };
  }

  function mobileConfig(extra) {
    return Object.assign(
      {
        responsive: true,
        displaylogo: false,
        scrollZoom: false,
        // On touch devices a single tap on a trace can be interpreted
        // as a double-click (300ms-window pattern), triggering Plotly's
        // default `doubleClick: "reset+autosize"` which rewrites the
        // axis ranges and re-ticks the x-axis. Disable it so a tap
        // never reflows the chart.
        doubleClick: false,
        modeBarButtonsToRemove: [
          "select2d",
          "lasso2d",
          "autoScale2d",
          "zoom2d",
        ],
      },
      extra || {},
    );
  }

  const watchers = [];
  function fireWatchers() {
    for (const fn of watchers) {
      try { fn(); } catch (e) { console.warn("eoChart redraw failed:", e); }
    }
  }
  if (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function"
  ) {
    const mq = window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT_PX}px)`);
    if (mq.addEventListener) mq.addEventListener("change", fireWatchers);
    else if (mq.addListener) mq.addListener(fireWatchers);
  }

  function onBreakpointChange(fn) {
    if (typeof fn === "function") watchers.push(fn);
  }

  // Plotly chart registry. Each chart module calls `registerPlot(id)`
  // once it has done its first `Plotly.newPlot`. `resizeAll()` then
  // sweeps the registry and forces each visible chart to re-fit its
  // parent's current dimensions — needed because Plotly's `responsive:
  // true` only listens to `window.resize`, and a chart that was hidden
  // (display:none on its tab panel) during a resize keeps its stale
  // inline width forever afterwards. Manifest: chart's outer div is
  // wider than the viewport, page gets a horizontal scrollbar.
  //
  // Call `resizeAll()` on every tab-show and after any layout change
  // that may have shrunk a parent (e.g. closing a side panel). Cheap —
  // we skip any div that's not in the DOM or has zero width.
  const plotIds = new Set();
  function registerPlot(id) { if (id) plotIds.add(id); }
  function resizeAll() {
    if (typeof Plotly === "undefined") return;
    for (const id of plotIds) {
      const el = document.getElementById(id);
      if (!el || !el.offsetParent) continue;        // not in DOM / hidden
      if (el.clientWidth === 0) continue;
      try { Plotly.Plots.resize(el); } catch (e) { /* not a Plotly div yet */ }
    }
  }

  // Wire breakpoint change to also re-fit existing plots. The redraw
  // callbacks registered via onBreakpointChange call Plotly.react with
  // a new layout, which doesn't always re-fit the SVG to a shrunken
  // parent — calling Plots.resize after gives a deterministic fit.
  onBreakpointChange(() => setTimeout(resizeAll, 50));

  window.eoChart = {
    MOBILE_BREAKPOINT_PX,
    isNarrow,
    mobileLayoutFragment,
    mobileConfig,
    onBreakpointChange,
    registerPlot,
    resizeAll,
  };
})();
