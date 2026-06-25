// Marginal cost + colour helpers, extracted from dashboard.js.

export const LOAD_PALETTE = ["#7ee787", "#79c0ff", "#ffa657", "#ff7b72", "#bc8cff"];

export function colorForLoadId(loadId) {
  let h = 0;
  for (let i = 0; i < loadId.length; i++) h = (h * 31 + loadId.charCodeAt(i)) | 0;
  return LOAD_PALETTE[Math.abs(h) % LOAD_PALETTE.length];
}

export function hexToRgba(hex, alpha) {
  const h = hex.replace(/^#/, "");
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

export function marginalCost(ip, ep, grid) {
  if (ip == null || ep == null || grid == null) return null;
  // grid: + import, − export. Cost = ip * import_kw − ep * export_kw.
  const imp = Math.max(0,  grid);
  const exp = Math.max(0, -grid);
  return ip * imp - ep * exp;
}
