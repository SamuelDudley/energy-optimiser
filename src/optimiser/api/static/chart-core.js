/**
 * chart-core.js — shared sync/resize hub for the uPlot dashboard.
 *
 * Replaces window.eoChart (Plotly era). Consumed by chart panels (Tasks 11–13)
 * and the cursor hook (Task 14). Browser-only at runtime; tested via Vitest
 * with a stubbed matchMedia (node environment).
 */
import uPlot from "./uplot.esm.js";

// ---------------------------------------------------------------------------
// Breakpoint
// ---------------------------------------------------------------------------

export const MOBILE_BREAKPOINT_PX = 760;

// Build the MediaQueryList once if matchMedia is available.
const _mqQuery = `(max-width: ${MOBILE_BREAKPOINT_PX}px)`;
const _mql =
  typeof matchMedia === "function"
    ? matchMedia(_mqQuery)
    : { matches: false, addEventListener() {}, removeEventListener() {} };

// isNarrow() re-reads matchMedia each call so that test stubs which mutate
// globalThis.__narrow (via matches on the returned object) or the mql itself
// are reflected correctly. The real browser MQL updates .matches in-place, so
// this is a live read in production too.
export function isNarrow() {
  // Re-query each call so test stubs that return a fresh object each time
  // (with matches: globalThis.__narrow) are observed correctly.
  if (typeof matchMedia === "function") {
    return matchMedia(_mqQuery).matches;
  }
  return false;
}

// ---------------------------------------------------------------------------
// Breakpoint change watchers
// ---------------------------------------------------------------------------

const _watchers = [];

/**
 * Register a callback that fires whenever the breakpoint crosses 760 px.
 * @param {() => void} fn
 */
export function onBreakpointChange(fn) {
  if (typeof fn === "function") _watchers.push(fn);
}

_mql.addEventListener("change", () => {
  for (const fn of _watchers) {
    try {
      fn();
    } catch (e) {
      console.warn("[chart-core] breakpoint watcher threw:", e);
    }
  }
});

// ---------------------------------------------------------------------------
// Instance registry
// ---------------------------------------------------------------------------

const _instances = new Map();

export const registry = {
  /**
   * Register a uPlot instance under a stable string id.
   * @param {string} id
   * @param {import("uplot").default} u
   */
  register(id, u) {
    if (id && u) _instances.set(id, u);
  },

  /**
   * Remove a previously registered instance.
   * @param {string} id
   */
  unregister(id) {
    _instances.delete(id);
  },

  /**
   * Resize every visible registered instance to match its container.
   * Hidden charts (no offsetParent or zero clientWidth) are skipped so they
   * resize lazily on next tab activation.
   * Size is read from the PARENT of u.root — uPlot pins u.root to its
   * build-time size, but the parent container flexes with the viewport.
   */
  resizeAll() {
    for (const [, u] of _instances) {
      const el = u.root;
      const box = el && el.parentElement; // the flexing container we mounted into
      if (!box || !box.offsetParent || box.clientWidth === 0) continue;
      u.setSize({ width: box.clientWidth, height: box.clientHeight });
    }
  },
};

// Resize all charts whenever the breakpoint changes (50 ms debounce to let
// the browser finish reflowing the layout).
onBreakpointChange(() => setTimeout(() => registry.resizeAll(), 50));

// Resize all charts on every window resize (debounced 100 ms), not just on
// breakpoint crossings.  Restores Plotly's `responsive:true` behaviour.
// Guarded so the module imports cleanly in node/Vitest environments.
if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
  let _resizeT;
  window.addEventListener("resize", () => {
    clearTimeout(_resizeT);
    _resizeT = setTimeout(() => registry.resizeAll(), 100);
  });
}

// ---------------------------------------------------------------------------
// Mobile drag options
// ---------------------------------------------------------------------------

/**
 * Returns cursor/drag options suitable for the current viewport.
 * On mobile (narrow) all drag interactions are disabled so the browser's
 * native scroll-through behaviour is preserved (spec §5.11.1).
 *
 * @returns {{ drag: { x: boolean, y: boolean, setScale: boolean } }}
 */
export function cursorDragOpts() {
  return {
    drag: { x: !isNarrow(), y: false, setScale: false },
  };
}

// ---------------------------------------------------------------------------
// Shared sync guard (also used by the cursor-redraw path in Task 14)
// ---------------------------------------------------------------------------

/**
 * Shared in-progress flag.  Set to true while propagating a setScale change
 * so that peer hooks skip re-entry, and also used by the cursor redraw path
 * (Task 14) for the same reason.
 */
export const syncGuard = { busy: false };

// ---------------------------------------------------------------------------
// Sync hub factory
// ---------------------------------------------------------------------------

/**
 * Create a named uPlot cursor-sync group and return helpers to join it.
 *
 * @param {string} key  — unique sync group key (e.g. "main-charts")
 * @returns {{ sync: object, instanceOpts: (peersGetter: () => uPlot[]) => object }}
 */
export function makeSyncHub(key) {
  const sync = uPlot.sync(key);

  return {
    sync,

    /**
     * Returns per-instance uPlot opts that join this sync group and
     * propagate x-axis zoom/pan to peers without feedback loops.
     *
     * @param {() => import("uplot").default[]} peersGetter
     *   Called on each setScale event; should return ALL instances that share
     *   this hub so the hook can skip `u` itself.
     */
    instanceOpts(peersGetter) {
      return {
        cursor: { sync: { key, scales: ["x", null] } },
        hooks: {
          setScale: [
            (u, scaleKey) => {
              if (scaleKey !== "x" || syncGuard.busy) return;
              syncGuard.busy = true;
              try {
                const { min, max } = u.scales.x;
                for (const p of peersGetter()) {
                  if (p !== u) p.setScale("x", { min, max });
                }
              } finally {
                syncGuard.busy = false;
              }
            },
          ],
        },
      };
    },
  };
}
