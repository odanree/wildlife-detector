/**
 * Typed client for the pre-VLM drops labeling endpoints (PR #153).
 * `drop_id` is the crop's relative path under logs/pre_vlm_drops
 * (same as the JSONL row's `snapshot` field).
 */

export type DropLabel = "moth" | "real_animal" | "unclear";

export interface DropRow {
  drop_id: string;
  camera_id: string | null;
  ts: number | null;
  bbox: [number, number, number, number] | null;
  bbox_w: number | null;
  bbox_h: number | null;
  area: number | null;
  mean: number | null;
  max: number | null;
  ar: number | null;
  wide_mean: number | null;
  wide_max: number | null;
  trigger: string | null;
  baseline_mode: string | null;
  crop_url: string; // /drops/<path>
  label: DropLabel | null;
  label_species: string | null;
  labeled_at: number | null;
}

export interface DropsResponse {
  total: number;
  items: DropRow[];
}

export type DropsFilter = "unlabeled" | "labeled" | "all";

export interface DropsQuery {
  camera?: string;
  filter?: DropsFilter;
  boundary?: boolean;
  mean_min?: number;
  mean_max?: number;
  limit?: number;
  offset?: number;
}

export async function fetchDrops(q: DropsQuery = {}, signal?: AbortSignal): Promise<DropsResponse> {
  const p = new URLSearchParams();
  if (q.camera) p.set("camera", q.camera);
  if (q.filter) p.set("filter", q.filter);
  if (q.boundary != null) p.set("boundary", q.boundary ? "1" : "0");
  if (q.mean_min != null) p.set("mean_min", String(q.mean_min));
  if (q.mean_max != null) p.set("mean_max", String(q.mean_max));
  if (q.limit != null) p.set("limit", String(q.limit));
  if (q.offset != null) p.set("offset", String(q.offset));
  const r = await fetch(`/api/drops?${p.toString()}`, { signal });
  if (!r.ok) throw new Error(`/api/drops ${r.status}`);
  return r.json();
}

export async function setDropLabel(
  drop_id: string,
  label: DropLabel | null,
  opts: { label_species?: string; notes?: string } = {},
  signal?: AbortSignal,
): Promise<void> {
  const body: Record<string, unknown> = { drop_id, label };
  if (opts.label_species) body.label_species = opts.label_species;
  if (opts.notes) body.notes = opts.notes;
  const r = await fetch("/api/drops/label", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try {
      const j = (await r.json()) as { error?: string };
      if (j.error) msg = j.error;
    } catch {
      /* ignore */
    }
    throw new Error(msg);
  }
}

export async function fetchDropCounts(signal?: AbortSignal): Promise<Record<string, number>> {
  const r = await fetch("/api/drops/counts", { signal });
  if (!r.ok) throw new Error(`/api/drops/counts ${r.status}`);
  return r.json();
}
