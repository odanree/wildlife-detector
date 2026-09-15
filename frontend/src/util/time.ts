/**
 * Timestamp formatters — parity with the fmtTs / fmtAgo / fmtRelative
 * helpers in preview.py's vanilla-JS template so alerts read identical
 * across old and new pages during the strangler-fig migration.
 */

export function fmtTs(unixSec: number): string {
  const d = new Date(unixSec * 1000);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  const mon = d.toLocaleString(undefined, { month: "short" });
  return `${hh}:${mm}:${ss} ${mon} ${d.getDate()}`;
}

export function fmtAgo(sec: number): string {
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h`;
  return `${Math.floor(sec / 86400)}d`;
}

export function fmtRelative(unixSec: number): string {
  const ago = Math.max(0, Math.floor(Date.now() / 1000 - unixSec));
  return `${fmtAgo(ago)} ago`;
}

/** Relative time for an ISO-8601 string (TIMESTAMPTZ columns like
 *  rats.first_seen). Returns "—" for unparseable input rather than
 *  "NaN ago". */
export function fmtRelativeIso(iso: string | null | undefined): string {
  if (!iso) return "—";
  const sec = Date.parse(iso) / 1000;
  return Number.isFinite(sec) ? fmtRelative(sec) : "—";
}

/** Absolute timestamp for an ISO-8601 string, same layout as fmtTs. */
export function fmtTsIso(iso: string | null | undefined): string {
  if (!iso) return "—";
  const sec = Date.parse(iso) / 1000;
  return Number.isFinite(sec) ? fmtTs(sec) : "—";
}
