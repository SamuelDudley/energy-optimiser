/**
 * ribbonPlugin — categorical colour-lane uPlot plugin.
 *
 * Paints a fixed-height row of filled rectangles, one per contiguous equal-category run.
 * Drawing uses device px (valToPos(..., true) + u.bbox).
 * Cursor hit-testing uses CSS-px-derived u.cursor.idx to look up the category at that
 * data index and writes labelOf(cat) into a caller-supplied tooltip element.
 *
 * All category data + colour/label/glyph mappings arrive via opts — this module has
 * no knowledge of DECISION or MODE categories.
 *
 * @param {object}   opts
 * @param {number[]} opts.unionX     - x-axis timestamps (one per data slot)
 * @param {function} opts.getCats    - () => number[]  category index per slot (called each draw)
 * @param {function} opts.colorOf    - (cat) => string  CSS colour string
 * @param {function} opts.labelOf    - (cat) => string  human-readable label
 * @param {function} [opts.glyphOf]  - (cat) => string  optional single glyph / short text
 * @param {Element}  [opts.tooltipEl]- DOM element to write cursor label into
 * @returns {object} uPlot plugin ({ hooks: { draw, setCursor } })
 */
export function ribbonPlugin({ unionX, getCats, colorOf, labelOf, glyphOf, tooltipEl }) {
  /** Returns the index just past the end of the run starting at i. */
  function runEnd(cats, i) {
    let j = i;
    while (j < cats.length && cats[j] === cats[i]) j++;
    return j;
  }

  return {
    hooks: {
      draw: [(u) => {
        const cats = getCats();
        if (!cats || cats.length === 0) return;

        const ctx = u.ctx;
        const { top, height } = u.bbox;

        ctx.save();
        try {
          for (let i = 0; i < cats.length; ) {
            const j = runEnd(cats, i);
            const x0 = Math.round(u.valToPos(unionX[i], "x", true));
            const x1 = Math.round(u.valToPos(unionX[Math.min(j, unionX.length - 1)], "x", true));

            ctx.fillStyle = colorOf(cats[i]);
            ctx.fillRect(x0, top, Math.max(1, x1 - x0), height);

            if (x1 - x0 > 18 * devicePixelRatio && glyphOf) {
              ctx.fillStyle = "#0d1117";
              ctx.font = `${10 * devicePixelRatio}px sans-serif`;
              ctx.textBaseline = "middle";
              ctx.fillText(glyphOf(cats[i]), x0 + 3 * devicePixelRatio, top + height / 2);
            }

            i = j;
          }
        } finally {
          ctx.restore();
        }
      }],

      setCursor: [(u) => {
        if (!tooltipEl) return;
        const idx = u.cursor.idx;          // CSS-px-derived index
        const cats = getCats();
        if (idx == null || !cats || cats[idx] == null) {
          tooltipEl.style.display = "none";
          return;
        }
        tooltipEl.textContent = labelOf(cats[idx]);
        tooltipEl.style.display = "block";
        tooltipEl.style.left = `${u.cursor.left}px`;
      }],
    },
  };
}
