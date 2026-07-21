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

export type BaselineMode = "day" | "night";

export interface CaptureResult {
  ok: boolean;
  captured_mode?: BaselineMode;
  error?: string;
}

/**
 * Capture the current frame as a baseline for the given mode. Mode is
 * explicit — the backend has an auto-picker keyed on brightness that
 * misclassifies IR-lit night as "day" on the overhead camera, so
 * exposing an explicit-mode capture from the UI is the reliable path.
 */
export async function captureBaseline(
  camera: string,
  mode: BaselineMode,
  signal?: AbortSignal,
): Promise<CaptureResult> {
  const params = new URLSearchParams({ camera, mode });
  const r = await fetch(`/api/baseline/capture?${params.toString()}`, {
    method: "POST",
    signal,
  });
  const body = (await r.json().catch(() => ({}))) as CaptureResult;
  if (!r.ok) {
    throw new Error(body.error ?? `/api/baseline/capture ${r.status}`);
  }
  return body;
}

/**
 * Delete the baseline for the given mode. Reversible (just re-capture).
 * Confirming is left to the caller — the button component owns UX policy.
 */
export async function clearBaseline(
  camera: string,
  mode: BaselineMode,
  signal?: AbortSignal,
): Promise<void> {
  const params = new URLSearchParams({ camera, mode });
  const r = await fetch(`/api/baseline/clear?${params.toString()}`, {
    method: "POST",
    signal,
  });
  if (!r.ok) {
    const body = (await r.json().catch(() => ({}))) as { error?: string };
    throw new Error(body.error ?? `/api/baseline/clear ${r.status}`);
  }
}
