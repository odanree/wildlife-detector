/**
 * Presentation-layer formatters used by the header chips. Kept
 * separate from src/util/time.ts so components can pull only what
 * they need without pulling every helper in the util namespace.
 */

export function fmtDuration(sec: number): string {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

export function fmtPct(n: number, digits = 0): string {
  return `${n.toFixed(digits)}%`;
}

export function fmtMB(n: number): string {
  return `${n.toFixed(0)}MB`;
}
