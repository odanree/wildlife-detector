/**
 * Typed client for the rat catalog (rat re-id Phase 2b/2c).
 *
 * `rats.*` timestamps arrive as ISO-8601 strings (TIMESTAMPTZ), unlike
 * alert rows whose `ts` is epoch seconds — `isoToSec` bridges the two so
 * the shared fmtRelative helper works on both. The display name lives in
 * `rats.notes` server-side (surfaced here as `name`); null → "Rat #<id>".
 */
import type { AlertRow } from "./alerts";

export interface RatSummary {
  id: number;
  name: string | null;
  first_seen: string;
  last_seen: string;
  alert_count: number;
  primary_camera: string | null;
  retired_at: string | null;
  /** Server returns a ready-to-use `/snapshots/<rel>` URL, not a relpath
   *  (2b contract). Use `sampleSnapshotRel` before handing it to SnapshotImg. */
  sample_snapshot: string | null;
  window_alert_count: number;
  window_last_ts: number | null;
}

export interface RatsResponse {
  rats: RatSummary[];
  camera: string | null;
  since_hours: number | null;
  error?: string;
}

export interface RatDetail {
  id: number;
  name: string | null;
  first_seen: string;
  last_seen: string;
  alert_count: number;
  primary_camera: string | null;
  retired_at: string | null;
  /** camera_id → percent of ALL this rat's alerts (0-100, 1 decimal). */
  camera_frequency: Record<string, number>;
  /** Newest first, capped server-side (default 500). Same shape as
   *  /api/alerts rows so AlertLightbox renders them unchanged. */
  alerts: AlertRow[];
  alerts_total: number;
  alerts_capped: boolean;
}

export interface RatsQuery {
  camera?: string;
  include_retired?: boolean;
  since_hours?: number;
}

export function ratDisplayName(r: { id: number; name: string | null }): string {
  return r.name?.trim() || `Rat #${r.id}`;
}

/** Strip the `/snapshots/` prefix the list endpoint bakes in so the
 *  path can go through SnapshotImg (retry + thumb variant). */
export function sampleSnapshotRel(url: string | null): string | null {
  if (!url) return null;
  return url.replace(/^\/snapshots\//, "");
}

export function isoToSec(iso: string): number {
  return Date.parse(iso) / 1000;
}

async function readError(r: Response, fallback: string): Promise<Error> {
  try {
    const j = (await r.json()) as { error?: string };
    if (j.error) return new Error(j.error);
  } catch {
    /* non-JSON body */
  }
  return new Error(`${fallback} ${r.status}`);
}

export async function fetchRats(q: RatsQuery = {}, signal?: AbortSignal): Promise<RatsResponse> {
  const p = new URLSearchParams();
  if (q.camera) p.set("camera", q.camera);
  if (q.include_retired) p.set("include_retired", "1");
  if (q.since_hours != null) p.set("since_hours", String(q.since_hours));
  const qs = p.toString();
  const r = await fetch(`/api/rats${qs ? `?${qs}` : ""}`, { signal });
  if (!r.ok) throw await readError(r, "/api/rats");
  return (await r.json()) as RatsResponse;
}

export async function fetchRat(id: number, signal?: AbortSignal): Promise<RatDetail> {
  const r = await fetch(`/api/rats/${id}`, { signal });
  if (!r.ok) throw await readError(r, `/api/rats/${id}`);
  return (await r.json()) as RatDetail;
}

export interface RatPatchResult {
  id: number;
  name: string | null;
  first_seen: string;
  last_seen: string;
  alert_count: number;
  primary_camera: string | null;
  retired_at: string | null;
}

export async function renameRat(id: number, name: string | null): Promise<RatPatchResult> {
  const r = await fetch(`/api/rats/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!r.ok) throw await readError(r, `PATCH /api/rats/${id}`);
  return (await r.json()) as RatPatchResult;
}

export interface MergeResult {
  ok: true;
  source_rat_id: number;
  target_rat_id: number;
  moved: number;
  target: RatPatchResult | null;
}

export async function mergeRat(sourceId: number, targetId: number): Promise<MergeResult> {
  const r = await fetch(`/api/rats/${sourceId}/merge`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target_rat_id: targetId }),
  });
  if (!r.ok) throw await readError(r, `POST /api/rats/${sourceId}/merge`);
  return (await r.json()) as MergeResult;
}
