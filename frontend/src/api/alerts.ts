/**
 * Typed client for /api/alerts. Row shape mirrors StateDB._row_to_dict
 * in src/storage/state_db.py — camera_id, is_rodent, historical are
 * always present; optional fields (snapshot, description, track_id,
 * yolo_conf) may be null on legacy rows backfilled from disk before
 * schema v1.
 */

export interface AlertRow {
  id: number;
  ts: number;
  created_at?: number;
  camera_id: string;
  species: string;
  confidence: number | null;
  is_rodent: boolean;
  historical: boolean;
  description?: string | null;
  snapshot?: string | null;
  track_id?: number | null;
  yolo_conf?: number | null;
}

export interface AlertsResponse {
  total: number;
  items: AlertRow[];
}

export interface AlertsQuery {
  limit?: number;
  species?: string;
  camera?: string;
}

export async function fetchAlerts(
  query: AlertsQuery = {},
  signal?: AbortSignal,
): Promise<AlertsResponse> {
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 200));
  if (query.species) params.set("species", query.species);
  if (query.camera) params.set("camera", query.camera);
  const r = await fetch(`/api/alerts?${params.toString()}`, { signal });
  if (!r.ok) throw new Error(`/api/alerts ${r.status}`);
  return (await r.json()) as AlertsResponse;
}
