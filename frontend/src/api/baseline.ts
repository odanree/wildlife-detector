/**
 * Typed client for /api/baseline. Meta shape mirrors _baseline_meta in
 * src/web_service.py — day + night slots each report whether the JPEG
 * exists, its mtime (unix seconds), and byte size.
 */

export interface BaselineSlot {
  exists: boolean;
  ts: number;
  bytes: number;
}

export interface BaselineMeta {
  exists: boolean;
  version: number;
  day: BaselineSlot;
  night: BaselineSlot;
}

export async function fetchBaselineMeta(
  camera: string,
  signal?: AbortSignal,
): Promise<BaselineMeta> {
  const r = await fetch(`/api/baseline?camera=${encodeURIComponent(camera)}`, { signal });
  if (!r.ok) throw new Error(`/api/baseline ${r.status}`);
  return (await r.json()) as BaselineMeta;
}

/**
 * Baseline JPEG URL. Appends the meta version as a cache-buster so a
 * fresh capture re-fetches immediately (browser caches the previous
 * image aggressively otherwise).
 */
export function baselineImageUrl(camera: string, mode: "day" | "night", version: number): string {
  const params = new URLSearchParams({ camera, mode, v: String(version) });
  return `/api/baseline.jpg?${params.toString()}`;
}
