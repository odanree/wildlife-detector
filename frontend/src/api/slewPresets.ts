/**
 * Typed client for /api/slew/presets and /api/ptz/preset.
 *
 * Polygons are transported in DETECTOR PIXEL coordinates (baseW × baseH,
 * matches status.detection_size) — same convention as /api/zone. The
 * backend normalizes to (0-1) before persisting to yaml so config is
 * resolution-independent.
 */

export type Point = [number, number];

export interface SlewPresetEntry {
  name: string;
  preset: number;
  polygon: Point[];
}

export interface SlewPresetsResponse {
  camera_id: string;
  enabled: boolean;
  ptz_camera_id: number;
  home_preset: number;
  return_home_after_s: number;
  frame_width: number;
  frame_height: number;
  presets: SlewPresetEntry[];
}

export async function fetchSlewPresets(
  camera: string,
  signal?: AbortSignal,
): Promise<SlewPresetsResponse> {
  const params = new URLSearchParams({ camera });
  const r = await fetch(`/api/slew/presets?${params.toString()}`, { signal });
  if (!r.ok) throw new Error(`/api/slew/presets ${r.status}`);
  return (await r.json()) as SlewPresetsResponse;
}

export interface SaveSlewPresetsResult {
  ok: boolean;
  count?: number;
  error?: string;
}

/**
 * Persist all presets in one call. Send the FULL list — the backend
 * replaces slew.<camera>.presets wholesale, so any preset not in the
 * body is dropped. Optional scalar overrides (enabled, home_preset)
 * can be included and will overwrite the yaml scalars.
 */
export async function saveSlewPresets(
  camera: string,
  presets: SlewPresetEntry[],
  extras?: {
    enabled?: boolean;
    home_preset?: number;
    return_home_after_s?: number;
  },
  signal?: AbortSignal,
): Promise<SaveSlewPresetsResult> {
  const params = new URLSearchParams({ camera });
  const r = await fetch(`/api/slew/presets?${params.toString()}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ presets, ...(extras ?? {}) }),
    signal,
  });
  const body = (await r.json().catch(() => ({}))) as SaveSlewPresetsResult;
  if (!r.ok) throw new Error(body.error ?? `/api/slew/presets ${r.status}`);
  return body;
}

export interface PtzPresetResult {
  ok: boolean;
  camera_id?: number;
  preset?: number;
  error?: string;
}

/**
 * Fire a manual GotoPreset — used by the editor's "go here" button so
 * the operator can align the polygon with what the preset actually
 * views. Camera-id here is the FRONTEND camera name (yard/rooftop/
 * backyard); backend resolves the PTZ target from the slew config.
 */
export async function commandPtzPreset(
  camera: string,
  preset: number,
  signal?: AbortSignal,
): Promise<PtzPresetResult> {
  const params = new URLSearchParams({ camera });
  const r = await fetch(`/api/ptz/preset?${params.toString()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ preset }),
    signal,
  });
  const body = (await r.json().catch(() => ({}))) as PtzPresetResult;
  if (!r.ok) throw new Error(body.error ?? `/api/ptz/preset ${r.status}`);
  return body;
}
