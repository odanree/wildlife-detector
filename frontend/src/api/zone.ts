/**
 * Typed client for /api/zone. The polygon is a list of [x, y] pairs
 * in DETECTOR PIXEL coordinates (baseW × baseH — matches
 * status.detection_size). Backend handles normalized ↔ pixel
 * conversion for the on-disk yaml storage.
 */

export type Point = [number, number];

export interface ZoneMeta {
  polygon: Point[];
  version: number;
}

export async function fetchZone(camera: string, signal?: AbortSignal): Promise<ZoneMeta> {
  const params = new URLSearchParams({ camera });
  const r = await fetch(`/api/zone?${params.toString()}`, { signal });
  if (!r.ok) throw new Error(`/api/zone ${r.status}`);
  return (await r.json()) as ZoneMeta;
}

export interface SaveZoneResult {
  ok: boolean;
  version?: number;
  error?: string;
}

export async function saveZone(
  camera: string,
  polygon: Point[],
  signal?: AbortSignal,
): Promise<SaveZoneResult> {
  const params = new URLSearchParams({ camera });
  const r = await fetch(`/api/zone?${params.toString()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ polygon }),
    signal,
  });
  const body = (await r.json().catch(() => ({}))) as SaveZoneResult;
  if (!r.ok) {
    throw new Error(body.error ?? `/api/zone ${r.status}`);
  }
  return body;
}
