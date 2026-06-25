export function priceLogToInterval(r) {
  return {
    start: r.interval_start, end: r.interval_end,
    import_per_kwh: r.per_kwh, export_per_kwh: r.export_per_kwh,
    forecast_predicted: r.forecast_predicted,
    forecast_low: r.forecast_low, forecast_high: r.forecast_high,
    export_forecast_predicted: r.export_forecast_predicted,
    export_forecast_low: r.export_forecast_low,
    export_forecast_high: r.export_forecast_high,
  };
}

export function mergePriceForecasts(pastRows, futureIntervals) {
  // Past rows from /price_forecast_log → PriceInterval-shape, with the
  // future side appended. Drop past entries that overlap the future
  // (snapshot's current+future supersedes the forecast log for those).
  //
  // The snapshot's `price_forecast` is the LP's `prices_planning`
  // list — 5-min intervals first (covering ~current + 30 min) then
  // 30-min intervals interleaved for the rest of the horizon. The
  // first 30-min entry (e.g. 23:00) is emitted *after* the last 5-min
  // entry (e.g. 23:55), which makes the array non-monotonic in
  // `start`. The LP doesn't mind — its `_price_at` linear scan picks
  // the first match, so 5-min wins where both are present. The
  // dashboard's polygon builder DOES mind: a non-monotonic forward
  // path crosses itself and renders the band as a self-intersecting
  // shape. Sort by start and dedupe-by-start (stable sort preserves
  // 5-min first when both share a start).
  const futureStartMs = futureIntervals.length
    ? Math.min(...futureIntervals.map((p) => +new Date(p.start)))
    : Infinity;
  const past = pastRows
    .map(priceLogToInterval)
    .filter((p) => +new Date(p.start) < futureStartMs);
  const merged = [...past, ...futureIntervals]
    .slice()
    .sort((a, b) => +new Date(a.start) - +new Date(b.start));
  const seen = new Set();
  const dedup = [];
  for (const p of merged) {
    if (seen.has(p.start)) continue;
    seen.add(p.start);
    dedup.push(p);
  }
  return dedup;
}

export function mergePVForecasts(pastRows, futureIntervals) {
  // Past rows have only period_end; synthesise start = period_end - 30 min
  // (Solcast 30-min cadence). Drop past entries that overlap the future
  // window so the line doesn't double-back.
  const futureStartMs = futureIntervals.length
    ? +new Date(futureIntervals[0].start) : Infinity;
  const past = pastRows
    .map((r) => ({
      start: new Date(+new Date(r.period_end) - 30 * 60_000).toISOString(),
      end: r.period_end,
      pv_estimate_kw: r.pv_estimate_kw,
      pv_estimate10_kw: r.pv_estimate10_kw,
      pv_estimate90_kw: r.pv_estimate90_kw,
    }))
    .filter((p) => +new Date(p.start) < futureStartMs);
  return [...past, ...futureIntervals];
}

export function pickPriceAt(priceList, t, side) {
  // Linear scan — fine at this size (≲ 500 entries). Returns the price
  // whose [start,end) interval contains t. Null if none.
  const ts = +new Date(t);
  for (const p of priceList) {
    const s = +new Date(p.start), e = +new Date(p.end);
    if (s <= ts && ts < e) {
      if (side === "import") return coalesce(p.forecast_predicted, p.import_per_kwh);
      return coalesce(p.export_forecast_predicted, p.export_per_kwh);
    }
  }
  return null;
}

export function coalesce(...vals) {
  for (const v of vals) if (v != null) return v;
  return null;
}
