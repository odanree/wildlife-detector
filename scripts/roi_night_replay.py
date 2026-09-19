"""ROI night replay — re-run detection over a camera's stored NVR footage
across a time range, then cross-reference against production alerts to
surface only the events LIVE detection missed (the delta).

## Why

When 6 detectors run concurrently, they compete for the same 8 Ollama
VLM slots. Peak motion cascades push queue depth past `VLM_MAX_ALERT_AGE_S`
and real events get stale-dropped. There's no way to reconstruct those
in real time, but the recordings still sit on the NVR. A bulkheaded
single-detector replay against the same footage has:

  * Full Ollama concurrency for that one process (no fan-out contention)
  * No time pressure (`VLM_MAX_ALERT_AGE_S=600` in the isolation env)
  * Same YOLO + zone + VLM stack as live — the ONLY thing that changed
    is who else was competing

If the replay catches something live didn't, that IS the miss caused by
overload. Cross-reference on `(camera_id, ts ± window, bbox IoU)` and
report only the batch-only delta so the operator eyeballs "what live
missed" rather than the whole night.

Pattern: **time-window backfill + set-difference reconciliation**. The
sibling `event_triggered_replay.py` uses source-camera alerts as triggers
(cross-camera validation on new cameras); this uses a manual time range
(single-camera catch-up on missed events).

## Reuses from event_triggered_replay.py

  * `DETECTOR_ENV_FIXED` — the test-bulkhead env (STATE_DRY_RUN, VIDEO_LOOP,
    REPLAY_EXIT_ON_EOF, etc). Same isolation semantics; no reason to
    diverge.
  * `preflight_detector` — fail fast if cv2/ultralytics/src.pipeline
    aren't importable (running from archiver image, not detector).
  * `parse_env_overrides` — --detector-env KEY=VAL passthrough.
  * `_run_detection_subprocess` structure — Popen with fixed env, JSON
    sidecar via `REPLAY_REPORT_PATH`.

## What it does

1. Enumerate chunks: [start, start+chunk_s], [start+chunk_s, start+2*chunk_s],
   ..., stopping at `end`. Chunks are aligned to `chunk_s` (default 120s
   = the NVR playback URL max), no overlap.
2. For each chunk:
   a. Build the NVR playback URL via `src.stream.playback_url.build_nvr_playback_url`.
   b. `ffmpeg -c copy` pull to `out_dir/chunks/<ts>.mp4`.
   c. Run detector as a subprocess with `--video <chunk>` and the isolation env.
      Sidecar JSON reports every DECISION with (ts_offset_s, species, bbox,
      confidence, description).
   d. Load the sidecar; annotate each verdict with its absolute UTC ts
      (chunk start + ts_offset_s).
3. After all chunks: query `alerts` for prod events on the same camera
   in the same time range.
4. Cross-reference batch verdicts vs prod alerts:
   * Match rule: same camera, `|batch_ts - alert_ts| <= --delta-time-window-s`,
     and bbox IoU >= `--delta-iou-threshold` (defaults 8s / 0.3).
   * Bucket into `matched` (both saw it → drop) and `batch_only` (delta).
5. Write `night_replay_report.json` with the full manifest + delta summary.

## Running

The detector image is the right runtime (cv2 + YOLO + Ollama reach +
ffmpeg + psycopg). `scripts/` isn't baked in, so bind-mount it. Pick the
detector service whose zone_key and tuning match the target camera:

    MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps \\
        -v "$(pwd)/scripts:/app/scripts:ro" detector-sideyard \\
        python scripts/roi_night_replay.py \\
            --camera sideyard \\
            --start 2026-09-16T22:00:00-07:00 \\
            --end 2026-09-17T05:00:00-07:00 \\
            --out-dir /app/clips/night_replay/2026-09-17_sideyard

For two cameras in one run, repeat --camera. Each gets its own subdir.

    --camera sideyard --camera crawlspace_ext ...

Add `--dry-run` to enumerate chunks + print the manifest without pulling.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from src.stream.playback_url import build_nvr_playback_url  # noqa: E402

# Reuse the sibling replay's isolation env + helpers exactly. Divergence
# would fragment the test-bulkhead contract — one env dict, two entry
# points. If a knob needs to change for one and not the other, put it in
# --detector-env, not a fork of DETECTOR_ENV_FIXED.
from scripts.event_triggered_replay import (  # noqa: E402
    DETECTOR_ENV_FIXED,
    parse_env_overrides,
    preflight_detector,
)

logger = logging.getLogger("roi_night_replay")

# NVR playback URLs are capped by `build_nvr_playback_url` at endtime =
# start + 2min; anything longer is silently truncated. So chunks default
# to 120s and never exceed it.
CHUNK_SECONDS_MAX = 120


@dataclass
class ChunkResult:
    """One 2-min chunk pulled + replayed. Records the pull path and the
    detector verdicts that came out of it."""

    chunk_start_iso: str
    chunk_start_epoch: float
    chunk_seconds: int
    playback_url_redacted: str  # creds stripped for logging
    clip_path: Optional[str] = None
    clip_bytes: int = 0
    pull_seconds: float = 0.0
    detection_seconds: float = 0.0
    verdicts: list[dict] = field(default_factory=list)  # from replay sidecar
    n_verdicts: int = 0
    error: Optional[str] = None


@dataclass
class DeltaEvent:
    """A batch-only verdict that had no matching live alert. The interesting
    output of the whole tool — 'this is what live missed'."""

    camera: str
    batch_ts_epoch: float
    batch_ts_iso: str
    species: str
    confidence: float
    bbox: list[int]  # x1,y1,x2,y2
    description: str
    clip_source: str  # path to the chunk containing this verdict
    clip_offset_s: float  # seconds into the chunk


@dataclass
class CameraReport:
    camera: str
    start_iso: str
    end_iso: str
    chunk_seconds: int
    chunks: list[ChunkResult] = field(default_factory=list)
    n_batch_verdicts: int = 0
    n_live_alerts: int = 0
    matched: list[dict] = field(default_factory=list)      # {batch_ts, live_ts, iou, delta_s}
    batch_only: list[DeltaEvent] = field(default_factory=list)
    live_only: list[dict] = field(default_factory=list)    # live alerts that batch didn't reproduce


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    """Accepts YYYY-MM-DDTHH:MM:SS with optional TZ offset (default UTC)."""
    # datetime.fromisoformat accepts ...+HH:MM or ...Z (3.11+).
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _redact_url(url: str) -> str:
    """Strip creds from an rtsp:// URL for safe logging."""
    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    _, host = rest.split("@", 1)
    return f"{scheme}://***@{host}"


def _pull_chunk(url: str, out_path: Path, timeout_s: int, ffmpeg: str) -> tuple[bool, str]:
    """`ffmpeg -c copy` pull. Returns (ok, stderr_tail)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "warning",
        "-rtsp_transport", "tcp",
        "-probesize", "20M", "-analyzeduration", "3M",
        "-i", url,
        "-c:v", "copy", "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        # Stop just BEFORE the URL's endtime, not after. Hikvision playback
        # sessions don't send a clean EOS at the window edge; if ffmpeg keeps
        # reading past endtime it sits waiting until our subprocess timeout
        # fires (~180s per chunk, kills the whole backfill). Dahua doesn't
        # care either way. -2s gives a small margin for encoder GOP variance.
        "-t", str(CHUNK_SECONDS_MAX - 2),
        "-y", str(out_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as e:
        return False, f"ffmpeg timeout after {timeout_s}s: {e}"
    if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size < 1024:
        return False, (proc.stderr or "")[-400:]
    return True, ""


def _run_detection(
    clip_path: Path,
    camera: str,
    env_overrides: dict[str, str],
    detector_python: str,
    timeout_s: int,
    log_dir: Path,
) -> tuple[list[dict], str]:
    """Invoke `python -m src.main --video <clip>` with the test-bulkhead env
    + camera-specific config. Reads back the JSON sidecar the pipeline
    writes at REPLAY_REPORT_PATH. Returns (verdicts, log_tail)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    report_path = log_dir / f"{clip_path.stem}_report.json"
    log_path = log_dir / f"{clip_path.stem}.log"

    env = os.environ.copy()
    env.update(DETECTOR_ENV_FIXED)
    env.update({
        "CAMERA_ID": camera,
        "ZONE_KEY": f"{camera}_zone",
        "BASELINE_PATH": f"data/baseline_{camera}.jpg",
        "RTSP_URL": "",
        "REPLAY_REPORT_PATH": str(report_path),
    })
    env.update(env_overrides)  # operator-supplied last-word

    cmd = [detector_python, "-m", "src.main", "--video", str(clip_path)]
    with log_path.open("w", encoding="utf-8") as logf:
        proc = subprocess.Popen(
            cmd, cwd=str(_REPO_ROOT), env=env, stdout=logf, stderr=subprocess.STDOUT
        )
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            return [], f"detector timed out after {timeout_s}s"

    if not report_path.exists():
        return [], f"no sidecar report at {report_path}"
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return [], f"malformed sidecar report: {e}"

    verdicts = data.get("verdicts") or data.get("decisions") or []
    return verdicts, ""


def enumerate_chunks(start: datetime, end: datetime, chunk_s: int) -> list[tuple[datetime, int]]:
    """[(chunk_start_utc, chunk_seconds), ...] over [start, end)."""
    out = []
    t = start
    while t < end:
        remaining = int((end - t).total_seconds())
        length = min(chunk_s, remaining)
        out.append((t, length))
        t += timedelta(seconds=chunk_s)
    return out


def _bbox_iou(a: list[int], b: list[int]) -> float:
    """(x1,y1,x2,y2) IoU. Returns 0 if either bbox is degenerate."""
    if not a or not b:
        return 0.0
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    iw = max(0, ix2 - ix1)
    ih = max(0, iy2 - iy1)
    inter = iw * ih
    a_area = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    b_area = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = a_area + b_area - inter
    return inter / union if union > 0 else 0.0


def load_live_alerts(dsn: str, camera: str, start: datetime, end: datetime) -> list[dict]:
    """Prod alerts on this camera in [start, end). Bbox comes from
    alert_embeddings.crop_bbox (bboxes on alerts itself aren't populated
    for older rows — see the earlier session diagnostic on that column)."""
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.id, a.ts, a.species, a.confidence, a.label_species,
                   e.crop_bbox
            FROM alerts a
            LEFT JOIN alert_embeddings e ON e.alert_id = a.id
            WHERE a.camera_id = %s
              AND a.ts >= %s AND a.ts < %s
              AND NOT a.historical
            ORDER BY a.ts
            """,
            (camera, start.timestamp(), end.timestamp()),
        )
        return [dict(r) for r in cur.fetchall()]


def cross_reference(
    camera: str,
    batch_verdicts: list[tuple[float, dict, str, float]],   # (ts_epoch, verdict, clip_path, offset_s)
    live_alerts: list[dict],
    time_window_s: float,
    iou_threshold: float,
    det_w: int | None = None,
    det_h: int | None = None,
) -> tuple[list[dict], list[DeltaEvent], list[dict]]:
    """Bucket batch × live pairs into matched / batch_only / live_only.

    A pair matches when |batch_ts - alert_ts| <= time_window_s AND the
    bboxes overlap at IoU >= iou_threshold. Greedy assignment (each live
    alert matches at most one batch verdict and vice versa) — no attempt
    at a global optimal because the time+space windows are tight enough
    that ambiguous overlaps are rare, and the report is meant to be
    eyeballed anyway."""
    matched: list[dict] = []
    live_matched_ids: set[int] = set()
    batch_matched_idx: set[int] = set()

    # Bbox on batch verdicts: pipeline emits (x1,y1,x2,y2) in detection-
    # frame coords (INPUT_WIDTH × INPUT_HEIGHT). Live alerts store bbox in
    # ORIGINAL frame coords via crop_bbox {x1,x2,y1,y2,frame_w,frame_h}.
    # For IoU we need both in the same coord space; normalize each to
    # fraction-of-frame (0..1) so absolute size disagreements don't matter.
    # Batch verdict bboxes are in detection-frame coords (INPUT_WIDTH x
    # INPUT_HEIGHT — pipeline resizes source to that before YOLO/MOG).
    # Live alerts' crop_bbox carries its own frame_w/frame_h (source).
    # Normalize both to fraction-of-frame so absolute-size disagreements
    # between det-frame and source-frame don't matter.
    #
    # Explicit args win; env is the compose-default fallback; last-ditch
    # constants match the fleet's shared 2048x1152 downscale target.
    _det_w = det_w or int(os.getenv("INPUT_WIDTH", "2048"))
    _det_h = det_h or int(os.getenv("INPUT_HEIGHT", "1152"))

    def _norm_batch(v: dict) -> Optional[tuple[float, float, float, float]]:
        b = v.get("bbox") or []
        if len(b) != 4:
            return None
        return (b[0] / _det_w, b[1] / _det_h, b[2] / _det_w, b[3] / _det_h)

    def _norm_live(a: dict) -> Optional[tuple[float, float, float, float]]:
        cb = a.get("crop_bbox") or {}
        if not cb:
            return None
        fw = cb.get("frame_w") or 1
        fh = cb.get("frame_h") or 1
        return (cb["x1"] / fw, cb["y1"] / fh, cb["x2"] / fw, cb["y2"] / fh)

    for i, (batch_ts, verdict, clip_path, offset_s) in enumerate(batch_verdicts):
        b_norm = _norm_batch(verdict)
        best_alert = None
        best_iou = 0.0
        for alert in live_alerts:
            if alert["id"] in live_matched_ids:
                continue
            if abs(alert["ts"] - batch_ts) > time_window_s:
                continue
            a_norm = _norm_live(alert)
            if b_norm is None or a_norm is None:
                # Time-only match when either side lacks bbox metadata.
                iou = 1.0
            else:
                # Bbox IoU in normalized (0..1) space.
                aw = max(0.0, b_norm[2] - b_norm[0]) * max(0.0, b_norm[3] - b_norm[1])
                bw = max(0.0, a_norm[2] - a_norm[0]) * max(0.0, a_norm[3] - a_norm[1])
                ix1, iy1 = max(b_norm[0], a_norm[0]), max(b_norm[1], a_norm[1])
                ix2, iy2 = min(b_norm[2], a_norm[2]), min(b_norm[3], a_norm[3])
                inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
                union = aw + bw - inter
                iou = inter / union if union > 0 else 0.0
            if iou >= iou_threshold and iou > best_iou:
                best_iou = iou
                best_alert = alert
        if best_alert is not None:
            matched.append({
                "batch_ts": batch_ts,
                "live_ts": best_alert["ts"],
                "live_alert_id": best_alert["id"],
                "delta_seconds": batch_ts - best_alert["ts"],
                "iou": round(best_iou, 3),
                "batch_species": verdict.get("species"),
                "live_species": best_alert.get("species"),
            })
            live_matched_ids.add(best_alert["id"])
            batch_matched_idx.add(i)

    batch_only: list[DeltaEvent] = []
    for i, (batch_ts, verdict, clip_path, offset_s) in enumerate(batch_verdicts):
        if i in batch_matched_idx:
            continue
        # Skip verdicts that live would have skipped too — insects, empty
        # detections, VLM-none rejections. The delta is REAL missed events.
        species = (verdict.get("species") or "").lower()
        if species in {"insect", "none", "unknown", ""}:
            continue
        bbox = verdict.get("bbox") or [0, 0, 0, 0]
        batch_only.append(DeltaEvent(
            camera=camera,
            batch_ts_epoch=batch_ts,
            batch_ts_iso=datetime.fromtimestamp(batch_ts, tz=timezone.utc).isoformat(),
            species=verdict.get("species", ""),
            confidence=float(verdict.get("confidence", 0.0)),
            bbox=list(bbox),
            description=verdict.get("description", "")[:400],
            clip_source=clip_path,
            clip_offset_s=offset_s,
        ))

    live_only = [
        {"alert_id": a["id"], "ts": a["ts"], "species": a["species"], "label_species": a["label_species"]}
        for a in live_alerts if a["id"] not in live_matched_ids
    ]
    return matched, batch_only, live_only


def run_camera(
    camera: str,
    start: datetime,
    end: datetime,
    chunk_s: int,
    out_dir: Path,
    dsn: str,
    detector_python: str,
    ffmpeg: str,
    env_overrides: dict[str, str],
    dry_run: bool,
    pull_timeout_s: int,
    detection_timeout_s: int,
    delta_time_window_s: float,
    delta_iou_threshold: float,
    speed: int = 1,
) -> CameraReport:
    """Run the replay for one camera end-to-end. Called once per --camera
    on the CLI so the top-level loop stays flat."""
    cam_dir = out_dir / camera
    cam_dir.mkdir(parents=True, exist_ok=True)
    (cam_dir / "chunks").mkdir(exist_ok=True)
    (cam_dir / "logs").mkdir(exist_ok=True)

    report = CameraReport(
        camera=camera,
        start_iso=_iso(start),
        end_iso=_iso(end),
        chunk_seconds=chunk_s,
    )

    chunks = enumerate_chunks(start, end, chunk_s)
    logger.info("[%s] %d chunks over %s → %s", camera, len(chunks), _iso(start), _iso(end))

    batch_verdicts_flat: list[tuple[float, dict, str, float]] = []

    # Read NVR_CHANNEL_<CAM> once — build_nvr_playback_url takes the
    # channel as an arg, it doesn't consult that env itself. Fleet convention
    # is CAMERA_ID upper-cased into the env name.
    ch_env = f"NVR_CHANNEL_{camera.upper()}"
    nvr_channel_env = os.environ.get(ch_env)
    if nvr_channel_env is None:
        logger.warning("[%s] %s not set — build_nvr_playback_url will default to ch=1", camera, ch_env)

    for idx, (chunk_start, chunk_len) in enumerate(chunks, 1):
        chunk_start_epoch = chunk_start.timestamp()
        try:
            url = build_nvr_playback_url(
                timestamp=chunk_start_epoch,
                camera_id=camera,
                nvr_channel=int(nvr_channel_env) if nvr_channel_env else None,
                pre_roll_seconds=0,
                speed=speed,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("[%s %d/%d] URL build failed: %s", camera, idx, len(chunks), e)
            continue

        clip_path = cam_dir / "chunks" / f"{camera}_{chunk_start.strftime('%Y%m%dT%H%M%S')}.mp4"
        chunk = ChunkResult(
            chunk_start_iso=_iso(chunk_start),
            chunk_start_epoch=chunk_start_epoch,
            chunk_seconds=chunk_len,
            playback_url_redacted=_redact_url(url),
        )

        if dry_run:
            logger.info("[%s %d/%d] DRY %s → %s", camera, idx, len(chunks), chunk.chunk_start_iso, chunk.playback_url_redacted)
            report.chunks.append(chunk)
            continue

        # Pull
        t0 = time.monotonic()
        ok, err = _pull_chunk(url, clip_path, pull_timeout_s, ffmpeg)
        chunk.pull_seconds = round(time.monotonic() - t0, 2)
        if not ok:
            chunk.error = f"pull failed: {err}"
            logger.warning("[%s %d/%d] pull FAILED after %.1fs: %s",
                           camera, idx, len(chunks), chunk.pull_seconds, err[:120])
            report.chunks.append(chunk)
            continue
        chunk.clip_path = str(clip_path)
        chunk.clip_bytes = clip_path.stat().st_size

        # Detect
        t0 = time.monotonic()
        verdicts, det_err = _run_detection(
            clip_path=clip_path, camera=camera, env_overrides=env_overrides,
            detector_python=detector_python, timeout_s=detection_timeout_s,
            log_dir=cam_dir / "logs",
        )
        chunk.detection_seconds = round(time.monotonic() - t0, 2)
        chunk.verdicts = verdicts
        chunk.n_verdicts = len(verdicts)
        if det_err:
            chunk.error = det_err
            logger.warning("[%s %d/%d] detect ERR: %s", camera, idx, len(chunks), det_err[:200])
        else:
            logger.info("[%s %d/%d] pull=%.1fs detect=%.1fs verdicts=%d",
                        camera, idx, len(chunks), chunk.pull_seconds, chunk.detection_seconds, chunk.n_verdicts)

        for v in verdicts:
            offset = float(v.get("ts_offset_s", 0.0))
            batch_verdicts_flat.append((chunk_start_epoch + offset, v, str(clip_path), offset))

        report.chunks.append(chunk)

    report.n_batch_verdicts = len(batch_verdicts_flat)

    if dry_run:
        return report

    # Cross-reference against production alerts.
    live_alerts = load_live_alerts(dsn, camera, start, end)
    report.n_live_alerts = len(live_alerts)
    # Detection-frame dims from env if the container's got them; falls back
    # to fleet default inside cross_reference. Passing None here preserves
    # the "env-first, hardcoded-last" precedence for the runtime.
    matched, batch_only, live_only = cross_reference(
        camera=camera,
        batch_verdicts=batch_verdicts_flat,
        live_alerts=live_alerts,
        time_window_s=delta_time_window_s,
        iou_threshold=delta_iou_threshold,
        det_w=int(os.environ["INPUT_WIDTH"]) if os.environ.get("INPUT_WIDTH") else None,
        det_h=int(os.environ["INPUT_HEIGHT"]) if os.environ.get("INPUT_HEIGHT") else None,
    )
    report.matched = matched
    report.batch_only = batch_only
    report.live_only = live_only

    logger.info(
        "[%s] batch=%d live=%d matched=%d batch_only=%d live_only=%d",
        camera, report.n_batch_verdicts, report.n_live_alerts,
        len(matched), len(batch_only), len(live_only),
    )
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--camera", action="append", required=True,
                    help="camera_id to replay (repeatable for multi-camera runs)")
    ap.add_argument("--start", required=True, help="window start (ISO8601, TZ-aware)")
    ap.add_argument("--end", required=True, help="window end (ISO8601, TZ-aware)")
    ap.add_argument("--chunk-seconds", type=int, default=CHUNK_SECONDS_MAX,
                    help=f"chunk length (max {CHUNK_SECONDS_MAX}, NVR playback URL cap)")
    ap.add_argument("--speed", type=int, default=1, choices=[1, 2, 4, 8],
                    help="Dahua/Amcrest playback speed multiplier (1|2|4|8). "
                         "Hikvision ignores this — its RTSP has no speed knob.")
    ap.add_argument("--out-dir", required=True, help="output directory root")
    ap.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    ap.add_argument("--pull-timeout-seconds", type=int, default=180)
    ap.add_argument("--detection-timeout-seconds", type=int, default=300)
    ap.add_argument("--delta-time-window-seconds", type=float, default=8.0,
                    help="batch/live match window (default 8s)")
    ap.add_argument("--delta-iou-threshold", type=float, default=0.3,
                    help="batch/live bbox IoU threshold (default 0.3)")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--detector-python", default=sys.executable)
    ap.add_argument("--detector-env", action="append", default=[],
                    help="extra env for the detector subprocess, repeatable KEY=VAL")
    ap.add_argument("--dry-run", action="store_true",
                    help="enumerate chunks + print manifest; no pulls, no detection")
    ap.add_argument("--parallel", action="store_true",
                    help="fork one detector subprocess per --camera concurrently. "
                         "Wall-clock faster for sparse scenes; VLM contention on "
                         "Ollama when both fire simultaneously. Skip if the scenes "
                         "you're replaying are motion-dense.")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose >= 2 else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.chunk_seconds < 10 or args.chunk_seconds > CHUNK_SECONDS_MAX:
        ap.error(f"--chunk-seconds must be in [10, {CHUNK_SECONDS_MAX}]")
    if not args.dry_run and not args.database_url:
        ap.error("DATABASE_URL must be set (env or --database-url) for the cross-reference step")

    start = _parse_iso(args.start)
    end = _parse_iso(args.end)
    if end <= start:
        ap.error("--end must be after --start")

    if not args.dry_run:
        preflight_detector(args.detector_python)

    env_overrides = parse_env_overrides(args.detector_env)
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    def _run_one(cam: str) -> CameraReport:
        return run_camera(
            camera=cam, start=start, end=end, chunk_s=args.chunk_seconds,
            out_dir=out_dir, dsn=args.database_url or "",
            detector_python=args.detector_python, ffmpeg=args.ffmpeg,
            env_overrides=env_overrides, dry_run=args.dry_run,
            pull_timeout_s=args.pull_timeout_seconds,
            detection_timeout_s=args.detection_timeout_seconds,
            delta_time_window_s=args.delta_time_window_seconds,
            delta_iou_threshold=args.delta_iou_threshold,
            speed=args.speed,
        )

    all_reports: list[CameraReport] = []
    if args.parallel and len(args.camera) > 1:
        # Fan-out via a thread pool sized to the camera count. Each thread
        # spends most of its time waiting on `subprocess.wait` (ffmpeg
        # pull, detector run) or blocked on Ollama HTTP — so a GIL-bound
        # thread pool is fine here, no need for multiprocessing.
        logger.info("Parallel mode: %d cameras in flight simultaneously", len(args.camera))
        with ThreadPoolExecutor(max_workers=len(args.camera), thread_name_prefix="cam") as pool:
            futures = {pool.submit(_run_one, cam): cam for cam in args.camera}
            for fut in as_completed(futures):
                cam = futures[fut]
                try:
                    all_reports.append(fut.result())
                except Exception as e:  # noqa: BLE001
                    logger.exception("[%s] run_camera crashed: %s", cam, e)
        # Preserve --camera order in the manifest so operators reading the
        # JSON see cameras in the argv order they typed, not completion
        # order (which depends on which camera had more chunks / longer
        # VLM queues).
        cam_order = {cam: i for i, cam in enumerate(args.camera)}
        all_reports.sort(key=lambda r: cam_order.get(r.camera, 999))
    else:
        for cam in args.camera:
            all_reports.append(_run_one(cam))

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "start_iso": _iso(start),
        "end_iso": _iso(end),
        "chunk_seconds": args.chunk_seconds,
        "delta_time_window_s": args.delta_time_window_seconds,
        "delta_iou_threshold": args.delta_iou_threshold,
        "cameras": [
            {
                **{k: v for k, v in asdict(r).items() if k not in {"batch_only"}},
                "batch_only": [asdict(d) for d in r.batch_only],
            }
            for r in all_reports
        ],
        "summary": {
            r.camera: {
                "chunks": len(r.chunks),
                "batch_verdicts": r.n_batch_verdicts,
                "live_alerts": r.n_live_alerts,
                "matched": len(r.matched),
                "batch_only_delta": len(r.batch_only),
                "live_only": len(r.live_only),
            } for r in all_reports
        },
    }
    manifest_path = out_dir / "night_replay_report.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    logger.info("Manifest written → %s", manifest_path)
    logger.info("Delta summary:")
    for cam, s in manifest["summary"].items():
        logger.info("  %s: batch_only=%d (from %d batch verdicts vs %d live alerts, %d matched)",
                    cam, s["batch_only_delta"], s["batch_verdicts"], s["live_alerts"], s["matched"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
