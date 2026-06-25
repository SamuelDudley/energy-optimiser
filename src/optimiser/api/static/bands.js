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
