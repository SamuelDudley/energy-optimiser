/**
 * spike-detect.js — pure windowed top-K extrema selector for price series.
 *
 * topKExtrema(xs, ys, opts) → { peaks: Extremum[], troughs: Extremum[] }
 *
 * Extremum = { idx: number, x: number, y: number }
 *
 * Algorithm:
 *   1. Find all LOCAL maxima (ys[i] > ys[i-1] AND ys[i] >= ys[i+1], with null
 *      neighbours treated as non-blocking — i.e. a non-null neighbour must be
 *      strictly lower) where:
 *        - xs[i] is within [viewMin, viewMax]
 *        - ys[i] > severityC
 *   2. Sort by |y| descending; greedily pick up to k, skipping any candidate
 *      within minSepPx of an already-picked extremum of the same sign.
 *   3. Symmetric process for troughs: local minima where ys[i] < -severityC.
 *
 * Separation:
 *   - If valToPx is provided: separation is |valToPx(candidate.x) - valToPx(picked.x)|
 *   - Otherwise: separation is |candidate.x - picked.x|  (x-value space)
 *   In both cases the threshold is minSepPx. If minSepPx is null/undefined,
 *   no separation guard is applied.
 *
 * @param {number[]} xs       - x values (epoch sec or any monotone sequence)
 * @param {(number|null)[]} ys - y values aligned to xs; null = gap (skip)
 * @param {{
 *   k?: number,
 *   minSepPx?: number|null,
 *   viewMin: number,
 *   viewMax: number,
 *   severityC: number,
 *   valToPx?: ((x: number) => number) | null,
 * }} opts
 * @returns {{ peaks: {idx:number,x:number,y:number}[], troughs: {idx:number,x:number,y:number}[] }}
 */
export function topKExtrema(xs, ys, opts) {
  const {
    k = 3,
    minSepPx = null,
    viewMin,
    viewMax,
    severityC,
    valToPx = null,
  } = opts;

  const n = xs.length;

  // Collect local maxima candidates (peaks).
  const peakCandidates = [];
  // Collect local minima candidates (troughs).
  const troughCandidates = [];

  for (let i = 0; i < n; i++) {
    const x = xs[i];
    const y = ys[i];

    // Skip nulls and out-of-window points.
    if (y == null) continue;
    if (x < viewMin || x > viewMax) continue;

    // Determine left and right non-null neighbours.
    // We use a "local extremum" definition that treats null neighbours as
    // non-constraining — i.e. if the left neighbour is null we only require
    // that the right (if non-null) is strictly lower, and vice versa.
    // This avoids losing genuine extrema at the edge of null gaps.
    let leftNonNull = null;
    for (let l = i - 1; l >= 0; l--) {
      if (ys[l] != null) { leftNonNull = ys[l]; break; }
    }
    let rightNonNull = null;
    for (let r = i + 1; r < n; r++) {
      if (ys[r] != null) { rightNonNull = ys[r]; break; }
    }

    // Peak: y strictly greater than both non-null neighbours (or no such neighbour).
    const isPeak =
      (leftNonNull == null || y > leftNonNull) &&
      (rightNonNull == null || y >= rightNonNull);

    // Trough: y strictly less than both non-null neighbours.
    const isTrough =
      (leftNonNull == null || y < leftNonNull) &&
      (rightNonNull == null || y <= rightNonNull);

    if (isPeak && y > severityC) {
      peakCandidates.push({ idx: i, x, y });
    }
    if (isTrough && y < -severityC) {
      troughCandidates.push({ idx: i, x, y });
    }
  }

  /**
   * Greedy top-K picker with separation guard.
   * @param {{ idx:number, x:number, y:number }[]} candidates
   * @param {boolean} isPositive - true for peaks, false for troughs (affects sort)
   */
  function pick(candidates, isPositive) {
    // Sort by |y| descending.
    candidates.sort((a, b) => Math.abs(b.y) - Math.abs(a.y));

    const picked = [];
    for (const cand of candidates) {
      if (picked.length >= k) break;

      // Separation guard.
      if (minSepPx != null && picked.length > 0) {
        const candPos = valToPx ? valToPx(cand.x) : cand.x;
        let tooClose = false;
        for (const p of picked) {
          const pPos = valToPx ? valToPx(p.x) : p.x;
          if (Math.abs(candPos - pPos) < minSepPx) {
            tooClose = true;
            break;
          }
        }
        if (tooClose) continue;
      }

      picked.push(cand);
    }
    return picked;
  }

  return {
    peaks: pick(peakCandidates, true),
    troughs: pick(troughCandidates, false),
  };
}
