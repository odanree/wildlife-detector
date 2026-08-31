/**
 * Typed client for /api/zone. The polygon is a list of [x, y] pairs
 * in DETECTOR PIXEL coordinates (baseW × baseH — matches
 * status.detection_size). Backend handles normalized ↔ pixel
 * conversion for the on-disk yaml storage.
 *
 * Day/night polygons: `mode='night'` (default) is the required base
 * polygon and doubles as the daytime fallback. `mode='day'` is an
 * optional override that supersedes the base while _is_daytime is
 * true. `has_day_polygon` in the response is the "does a day override
 * exist?" flag so the editor can distinguish 'inherits night' from
 * 'day polygon actually set'.
 */

export type Point = [number, number];
export type ZoneMode = "day" | "night";

export interface ZoneMeta {
  polygon: Point[];
  version: number;
  mode: ZoneMode;
  has_day_polygon: boolean;
}

export async function fetchZone(
  camera: string,
  mode: ZoneMode = "night",
  signal?: AbortSignal,
): Promise<ZoneMeta> {
  const params = new URLSearchParams({ camera, mode });
  const r = await fetch(`/api/zone?${params.toString()}`, { signal });
  if (!r.ok) throw new Error(`/api/zone ${r.status}`);
  return (await r.json()) as ZoneMeta;
}

export interface SaveZoneResult {
  ok: boolean;
  version?: number;
  mode?: ZoneMode;
  cleared?: boolean;
  error?: string;
}

export async function saveZone(
  camera: string,
  polygon: Point[],
  mode: ZoneMode = "night",
  signal?: AbortSignal,
): Promise<SaveZoneResult> {
  const params = new URLSearchParams({ camera });
  const r = await fetch(`/api/zone?${params.toString()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ polygon, mode }),
    signal,
  });
  const body = (await r.json().catch(() => ({}))) as SaveZoneResult;
  if (!r.ok) {
    throw new Error(body.error ?? `/api/zone ${r.status}`);
  }
  return body;
}

/** Delete the day polygon override so day inherits the night polygon.
 *  Only valid for mode='day' — night is the required base. */
export async function clearDayZone(camera: string, signal?: AbortSignal): Promise<SaveZoneResult> {
  const params = new URLSearchParams({ camera });
  const r = await fetch(`/api/zone?${params.toString()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode: "day", clear: true }),
    signal,
  });
  const body = (await r.json().catch(() => ({}))) as SaveZoneResult;
  if (!r.ok) {
    throw new Error(body.error ?? `/api/zone ${r.status}`);
  }
  return body;
}
