/**
 * Typed client for /api/masks. Same pattern as zone.ts — masks are
 * axis-aligned rectangles stored in DETECTOR PIXEL coordinates
 * (baseW × baseH). Backend handles normalized-yaml ↔ pixel
 * conversion + per-camera storage keying.
 */

export type Rect = [number, number, number, number]; // [x1, y1, x2, y2]

export interface MasksMeta {
  masks: Rect[];
  version: number;
}

export async function fetchMasks(camera: string, signal?: AbortSignal): Promise<MasksMeta> {
  const params = new URLSearchParams({ camera });
  const r = await fetch(`/api/masks?${params.toString()}`, { signal });
  if (!r.ok) throw new Error(`/api/masks ${r.status}`);
  return (await r.json()) as MasksMeta;
}

export interface SaveMasksResult {
  ok: boolean;
  version?: number;
  error?: string;
}

export async function saveMasks(
  camera: string,
  masks: Rect[],
  signal?: AbortSignal,
): Promise<SaveMasksResult> {
  const params = new URLSearchParams({ camera });
  const r = await fetch(`/api/masks?${params.toString()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ masks }),
    signal,
  });
  const body = (await r.json().catch(() => ({}))) as SaveMasksResult;
  if (!r.ok) {
    throw new Error(body.error ?? `/api/masks ${r.status}`);
  }
  return body;
}
