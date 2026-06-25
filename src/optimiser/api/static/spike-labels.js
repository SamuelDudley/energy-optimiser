/**
 * spike-labels.js — uPlot plugin that draws spike peak/trough markers on the
 * PRICE panel (refinement 1g).
 *
 * For each tick redraw the plugin draws:
 *   - Up to k=3 LOCAL MAXIMA where import price > PEAK_SEVERITY_C (100 c/kWh):
 *     an upward-pointing apex caret + a "$X.X" dollar label above.
 *   - Up to k=3 LOCAL MINIMA where export price < -TROUGH_SEVERITY_C (-20 c/kWh):
 *     a downward-pointing apex caret + a "$-X.X" label below.
 *
 * c/kWh → $/kWh conversion: divide by 100, round to 1 dp.
 *   e.g. 1480 c/kWh → "$14.8"
 *        -25 c/kWh  → "$-0.3" (troughs)
 *
 * PERF (critic c3):
 *   The cursor path calls u.redraw(false,false) on every mouse move. We must
 *   NOT recompute topKExtrema on every redraw or it will thrash during scrub.
 *
 *   Cache-invalidation strategy — dirty-flag approach:
 *     1. `setScale` hook (fires when x-zoom/pan changes) sets _dirty=true.
 *     2. `setData` hook (fires when data is replaced) sets _dirty=true.
 *     3. In the `draw` hook, if _dirty: recompute + clear flag. Otherwise reuse
 *        the cached _extrema.
 *
 *   setScale is also called by peer-sync on cursor moves, BUT the sync hub
 *   in chart-core.js guards re-entry via syncGuard.busy so setScale hooks do
 *   NOT fire during cursor-only redraws. The only path that calls redraw(false,
 *   false) is the cursor/now-line refresh (shapes.js + setShapes in panels.js);
 *   that path does NOT call setScale, so _dirty stays false and the draw hook
 *   reuses the cache. ✓
 *
 * Series indices in the price panel (after the x prepend at index 0):
 *   data[0] = unionX (epoch sec)
 *   data[1] = importLo band bound   — series local idx 0
 *   data[2] = importHi band bound   — series local idx 1
 *   data[3] = exportLo band bound   — series local idx 2
 *   data[4] = exportHi band bound   — series local idx 3
 *   data[5] = importRealised        — series local idx 4  → used for PEAKS
 *   data[6] = importPredicted       — series local idx 5
 *   data[7] = exportRealised        — series local idx 6  → used for TROUGHS
 *   data[8] = exportPredicted       — series local idx 7
 *
 * @param {{
 *   seriesIdxImport?: number,   // data[] index for import realised (default 5)
 *   seriesIdxExport?: number,   // data[] index for export realised (default 7)
 *   k?: number,                 // max markers per side (default 3)
 *   peakSeverityC?: number,     // import threshold in c/kWh (default 100)
 *   troughSeverityC?: number,   // export threshold magnitude in c/kWh (default 20)
 *   minSepPx?: number,          // min pixel separation between same-sign markers (default 60)
 * }} opts
 * @returns {object} uPlot plugin
 */
import { topKExtrema } from "./spike-detect.js";

// Defaults — documented in the JSDoc above.
const PEAK_SEVERITY_C   = 100;  // c/kWh: must exceed for a peak marker
const TROUGH_SEVERITY_C = 20;   // c/kWh: export must be below -20 c/kWh for a trough

// Visual constants.
const CARET_H  = 7;   // device-px height of the apex triangle
const CARET_W  = 5;   // device-px half-width at the base
const LABEL_PAD_Y = 4; // device-px gap between caret tip and label baseline
const CORNER_R  = 3;  // device-px corner radius for the label pill
const LABEL_PAD_X = 4; // horizontal padding inside pill
const LABEL_PAD_YPill = 2; // vertical padding inside pill

const PEAK_COLOR  = "#f0883e";   // orange — matches import series stroke
const TROUGH_COLOR = "#56d364";  // green  — matches export series stroke

/**
 * Convert c/kWh to a "$X.X" string.
 * Troughs are negative; we keep the minus sign.
 * @param {number} cents
 * @returns {string}
 */
function centsToLabel(cents) {
  const dollars = cents / 100;
  return "$" + dollars.toFixed(1);
}

export function spikeLabelsPlugin({
  seriesIdxImport = 5,
  seriesIdxExport = 7,
  k = 3,
  peakSeverityC  = PEAK_SEVERITY_C,
  troughSeverityC = TROUGH_SEVERITY_C,
  minSepPx = 60,
} = {}) {
  // Mutable plugin state — shared across hook calls via closure.
  let _dirty = true;
  let _extrema = { peaks: [], troughs: [] };

  /** Recompute extrema using the current uPlot state. */
  function _recompute(u) {
    const xs = u.data[0];
    const importYs = u.data[seriesIdxImport];
    const exportYs = u.data[seriesIdxExport];

    if (!xs || !importYs || !exportYs) {
      _extrema = { peaks: [], troughs: [] };
      return;
    }

    const viewMin = u.scales.x.min;
    const viewMax = u.scales.x.max;

    // valToPx converts an x-value (epoch sec) to device pixels using uPlot's
    // valToPos with canvasPx=true. Used for the separation guard.
    const valToPx = (x) => u.valToPos(x, "x", true);

    // Peaks: from import realised.
    const peakResult = topKExtrema(xs, importYs, {
      k, viewMin, viewMax, severityC: peakSeverityC, minSepPx, valToPx,
    });

    // Troughs: from export realised. topKExtrema expects severity as a positive
    // threshold and returns y values below -severityC.
    const troughResult = topKExtrema(xs, exportYs, {
      k, viewMin, viewMax, severityC: troughSeverityC, minSepPx, valToPx,
    });

    _extrema = {
      peaks:   peakResult.peaks,
      troughs: troughResult.troughs,
    };
    _dirty = false;
  }

  /**
   * Draw a single apex caret + pill label.
   *
   * @param {CanvasRenderingContext2D} ctx
   * @param {number} cx         - centre x in device px
   * @param {number} tipY       - tip y in device px (apex of the triangle)
   * @param {boolean} pointUp   - true=peak (↑), false=trough (↓)
   * @param {string} color
   * @param {string} label      - e.g. "$14.8"
   * @param {{ left:number, top:number, width:number, height:number }} bbox
   * @param {number} dpr        - device pixel ratio
   */
  function _drawMarker(ctx, cx, tipY, pointUp, color, label, bbox, dpr) {
    // ── caret (filled triangle) ──────────────────────────────────────────────
    ctx.beginPath();
    if (pointUp) {
      // Upward triangle: tip at tipY, base BELOW.
      ctx.moveTo(cx, tipY);
      ctx.lineTo(cx - CARET_W * dpr, tipY + CARET_H * dpr);
      ctx.lineTo(cx + CARET_W * dpr, tipY + CARET_H * dpr);
    } else {
      // Downward triangle: tip at tipY, base ABOVE.
      ctx.moveTo(cx, tipY);
      ctx.lineTo(cx - CARET_W * dpr, tipY - CARET_H * dpr);
      ctx.lineTo(cx + CARET_W * dpr, tipY - CARET_H * dpr);
    }
    ctx.closePath();
    ctx.fillStyle = color;
    ctx.fill();

    // ── pill label ───────────────────────────────────────────────────────────
    const fontSize = 10 * dpr;
    ctx.font = `bold ${fontSize}px sans-serif`;
    const textW = ctx.measureText(label).width;
    const pillW = textW + LABEL_PAD_X * 2 * dpr;
    const pillH = fontSize + LABEL_PAD_YPill * 2 * dpr;

    // Centre the pill horizontally on cx; clamp to stay inside bbox.
    let pillX = cx - pillW / 2;
    const bboxRight = bbox.left + bbox.width;
    if (pillX < bbox.left) pillX = bbox.left;
    if (pillX + pillW > bboxRight) pillX = bboxRight - pillW;

    // Place the pill above the caret tip (peaks) or below (troughs).
    let pillY;
    if (pointUp) {
      pillY = tipY - CARET_H * dpr - LABEL_PAD_Y * dpr - pillH;
    } else {
      pillY = tipY + CARET_H * dpr + LABEL_PAD_Y * dpr;
    }

    // Pill background.
    const r = CORNER_R * dpr;
    ctx.beginPath();
    ctx.moveTo(pillX + r, pillY);
    ctx.lineTo(pillX + pillW - r, pillY);
    ctx.quadraticCurveTo(pillX + pillW, pillY, pillX + pillW, pillY + r);
    ctx.lineTo(pillX + pillW, pillY + pillH - r);
    ctx.quadraticCurveTo(pillX + pillW, pillY + pillH, pillX + pillW - r, pillY + pillH);
    ctx.lineTo(pillX + r, pillY + pillH);
    ctx.quadraticCurveTo(pillX, pillY + pillH, pillX, pillY + pillH - r);
    ctx.lineTo(pillX, pillY + r);
    ctx.quadraticCurveTo(pillX, pillY, pillX + r, pillY);
    ctx.closePath();
    ctx.fillStyle = "rgba(13,17,23,0.85)";   // near-black, semi-transparent
    ctx.fill();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1 * dpr;
    ctx.stroke();

    // Label text.
    ctx.fillStyle = color;
    ctx.textBaseline = "middle";
    ctx.textAlign = "left";
    ctx.fillText(label, pillX + LABEL_PAD_X * dpr, pillY + pillH / 2);
  }

  return {
    hooks: {
      /**
       * Mark dirty when x-scale changes (zoom/pan).
       * NOTE: cursor-sync setScale propagation is guarded by syncGuard.busy
       * in chart-core.js, so this does NOT fire during cursor-move redraws.
       */
      setScale: [
        (u, scaleKey) => {
          if (scaleKey === "x") _dirty = true;
        },
      ],

      /**
       * Mark dirty when new data is loaded.
       */
      setData: [
        (_u) => {
          _dirty = true;
        },
      ],

      /**
       * Draw markers. Recomputes only when _dirty (scale or data changed).
       * Cursor-only redraws (redraw(false,false)) do NOT set _dirty, so this
       * reuses the cached _extrema — O(1) per cursor-move frame. ✓
       */
      draw: [
        (u) => {
          if (_dirty) _recompute(u);

          const { peaks, troughs } = _extrema;
          if (peaks.length === 0 && troughs.length === 0) return;

          const ctx = u.ctx;
          const { left, top, width, height } = u.bbox;
          const dpr = (typeof devicePixelRatio !== "undefined" ? devicePixelRatio : 1);

          ctx.save();
          // Clip to plot area so labels don't bleed into the gutter.
          ctx.beginPath();
          ctx.rect(left, top, width, height);
          ctx.clip();

          for (const peak of peaks) {
            const cx  = Math.round(u.valToPos(peak.x, "x", true));
            const tipY = Math.round(u.valToPos(peak.y, "y", true));
            _drawMarker(ctx, cx, tipY, true, PEAK_COLOR, centsToLabel(peak.y), u.bbox, dpr);
          }

          for (const trough of troughs) {
            const cx  = Math.round(u.valToPos(trough.x, "x", true));
            const tipY = Math.round(u.valToPos(trough.y, "y", true));
            _drawMarker(ctx, cx, tipY, false, TROUGH_COLOR, centsToLabel(trough.y), u.bbox, dpr);
          }

          ctx.restore();
        },
      ],
    },
  };
}
