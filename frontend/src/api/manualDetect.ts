/**
 * Typed client for POST /api/manual-detect — operator-drawn bbox for
 * manual detection. Wildlife-web routes to the correct detector via
 * DetectorRegistry based on the `?camera=` query param. Response
 * contains the reserved track_id (>= MANUAL_TRACK_ID_BASE = 900_000)
 * that will show up on the alert row.
 *
 * Bbox is in **detection-frame** coords (same coord space the zone
 * editor + preview MJPEG use), NOT source-frame or displayed CSS
 * pixels. Overlay component handles the CSS→detection conversion via
 * its SVG viewBox.
 */

export interface ManualDetectResponse {
  ok?: boolean;
  track_id?: number;
  error?: string;
}

export async function postManualDetect(
  camera: string,
  bbox: [number, number, number, number],
  signal?: AbortSignal,
): Promise<ManualDetectResponse> {
  const params = new URLSearchParams({ camera });
  const r = await fetch(`/api/manual-detect?${params.toString()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ bbox }),
    signal,
  });
  let body: ManualDetectResponse = {};
  try {
    body = await r.json();
  } catch {
    body = { error: `non-JSON response (HTTP ${r.status})` };
  }
  if (!r.ok && !body.error) body.error = `HTTP ${r.status}`;
  return body;
}
