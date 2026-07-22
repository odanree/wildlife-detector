/**
 * Typed client for /api/alerts. Row shape mirrors StateDB._row_to_dict
 * in src/storage/state_db.py — camera_id, is_rodent, historical are
 * always present; optional fields (snapshot, description, track_id,
 * yolo_conf) may be null on legacy rows backfilled from disk before
 * schema v1.
 */

export type LabelVerdict = "correct" | "incorrect" | "unclear" | null;

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
  // Human-in-the-loop label fields — supervised training data.
  // label_verdict null = unlabeled; otherwise 'correct' | 'incorrect' | 'unclear'.
  // label_species is the fine-grained tag (real_mouse, FP:insect, ...) applied
  // via the species-picker popover — quick-verdict rows leave it null.
  label_verdict?: LabelVerdict;
  label_species?: string | null;
  label_notes?: string | null;
  label_ts?: number | null;
}

export async function setAlertLabel(
  alertId: number,
  verdict: LabelVerdict,
  species?: string | null,
  notes?: string | null,
): Promise<void> {
  const r = await fetch(`/api/alerts/${alertId}/label`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ verdict, species: species ?? null, notes: notes ?? null }),
  });
  if (!r.ok) throw new Error(`/api/alerts/${alertId}/label ${r.status}`);
}

export async function setAlertLabelsBulk(
  alertIds: number[],
  verdict: LabelVerdict,
  species?: string | null,
  notes?: string | null,
): Promise<number> {
  const r = await fetch("/api/alerts/label-bulk", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      alert_ids: alertIds,
      verdict,
      species: species ?? null,
      notes: notes ?? null,
    }),
  });
  if (!r.ok) throw new Error(`/api/alerts/label-bulk ${r.status}`);
  const j = (await r.json()) as { updated: number };
  return j.updated;
}

export interface AlertsResponse {
  total: number;
  items: AlertRow[];
}

export type AlertsScope = "historical" | "live" | "all";
export type AlertsLabelFilter = "unlabeled" | "labeled" | "all";

export interface AlertsQuery {
  limit?: number;
  species?: string;
  camera?: string;
  /** 'historical' → only backfilled/pre-tuning rows (labeling workflow default),
   *  'live' → only fresh VLM-fired rows, 'all' → both. */
  scope?: AlertsScope;
  /** 'unlabeled' → hide rows already voted on (sifting flow),
   *  'labeled'   → only show rows you've labeled,
   *  'all'       → both. */
  label_filter?: AlertsLabelFilter;
}

export async function fetchAlerts(
  query: AlertsQuery = {},
  signal?: AbortSignal,
): Promise<AlertsResponse> {
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 200));
  if (query.species) params.set("species", query.species);
  if (query.camera) params.set("camera", query.camera);
  if (query.scope) params.set("scope", query.scope);
  if (query.label_filter) params.set("label_filter", query.label_filter);
  const r = await fetch(`/api/alerts?${params.toString()}`, { signal });
  if (!r.ok) throw new Error(`/api/alerts ${r.status}`);
  return (await r.json()) as AlertsResponse;
}
