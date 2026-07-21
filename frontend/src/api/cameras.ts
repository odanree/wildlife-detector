/**
 * Typed client for /api/cameras — the DetectorRegistry roster the operator
 * UI needs to render camera filters. Kept identical shape to the vanilla-JS
 * fetch site in preview.py so we can drop-in swap.
 */

export interface CamerasResponse {
  cameras: string[];
  default: string;
}

export async function fetchCameras(signal?: AbortSignal): Promise<CamerasResponse> {
  const r = await fetch("/api/cameras", { signal });
  if (!r.ok) throw new Error(`/api/cameras ${r.status}`);
  return (await r.json()) as CamerasResponse;
}
