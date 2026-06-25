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
