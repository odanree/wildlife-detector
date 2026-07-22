/**
 * Typed client for /status?camera=<id>. Mirrors the shape Stats.snapshot()
 * emits in src/web/preview.py. Fields are optional where the backend may
 * omit them (e.g. resources: {available: false}, vlm_cost absent on
 * pre-cost-tracker builds) so this stays forward-compatible.
 */

export interface VlmCost {
  tokens_input: number;
  tokens_cache_read: number;
  tokens_cache_create: number;
  tokens_output: number;
  cost_usd: number;
  cache_hit_rate: number;
}

export interface GateFunnel {
  motion_velocity_rejected?: number;
  motion_persistence_rejected?: number;
  motion_events: number;
  zone_events: number;
  baseline_filtered: number;
  vlm_calls: number;
  vlm_rejected: number;
  vlm_insect?: number;
  vlm_confirmed: number;
}

export interface Resources {
  available: boolean;
  cpu_pct?: number;
  cpu_peak_pct?: number;
  num_cpus?: number;
  rss_mb?: number;
  rss_peak_mb?: number;
  threads?: number;
}

export interface LastAlert {
  species: string;
  confidence: number;
  description: string;
  ts: number;
  snapshot?: string | null;
  camera_id?: string;
}

export interface StatusSnapshot {
  fps: number;
  alerts_total: number;
  uptime_seconds: number;
  backend: string;
  camera: string;
  camera_id: string;
  zone_key: string;
  detection_size: [number, number];
  last_alert: LastAlert | null;
  gate_funnel: GateFunnel;
  resources: Resources;
  vlm_cost?: VlmCost;
}

export async function fetchStatus(camera?: string, signal?: AbortSignal): Promise<StatusSnapshot> {
  const qs = camera ? `?camera=${encodeURIComponent(camera)}` : "";
  const r = await fetch(`/status${qs}`, { signal });
  if (!r.ok) throw new Error(`/status ${r.status}`);
  return (await r.json()) as StatusSnapshot;
}
