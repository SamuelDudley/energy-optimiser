export const SLOT_MINUTES = 5;
export const SLOT_MS = SLOT_MINUTES * 60 * 1000;

export function nearestSlotAt(time) {
  const t = time instanceof Date ? time.getTime() : +new Date(time);
  return new Date(Math.floor(t / SLOT_MS) * SLOT_MS);
}

export function toNemDate(d) {
  if (d == null) return null;
  const t = d instanceof Date ? +d : +new Date(d);
  if (!Number.isFinite(t)) return null;
  return new Date(t + 10 * 3_600_000).toISOString().slice(0, 10);
}

export function toEpochSec(d) {
  if (d == null) return null;
  const t = d instanceof Date ? +d : +new Date(d);
  if (!Number.isFinite(t)) return null;
  return Math.round(t / 1000);
}
