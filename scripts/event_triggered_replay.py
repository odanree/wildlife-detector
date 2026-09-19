"""Event-triggered replay — use existing alerts as temporal priors to pull
clips from a *different* (new / repositioned) camera.

## Why

Validating a new camera angle by grinding 24h of its footage through the
detector is expensive and mostly empty. The alerts table already holds
500+/day localized rodent events across the fleet; each one is a strong
prior that "something moved at time T in this yard". So instead of
scanning blind, pull ±30s from the TARGET camera around each source
alert's ts and let the operator eyeball whether the new angle caught the
same event.

Pattern: **temporal-prior sampling / cross-camera replay** — the source
cameras act as the trigger, the target camera is the payload. Overlapping
windows are coalesced (a **debounce coalescer** over alert ts) so a
burst of 5 alerts in 20s yields one clip, not five copies of the same
60s of footage.

## What it does

1. Query `alerts` for non-historical rows on the source cameras within
   the lookback horizon, filtered by species.
2. Cluster by ts: consecutive alerts within `--dedupe-window-seconds`
   of each other merge into one window `[first_ts - pre, last_ts + post]`,
   capped at `--max-window-seconds` so one window never exceeds what the
   NVR playback URL can serve (`build_nvr_playback_url` hard-codes a
   2-minute endtime).
3. For each window build the TARGET camera's playback URL via
   `src.stream.playback_url.build_nvr_playback_url` — the same routing
   the archiver uses (Dahua vs Hikvision family, NVR-local wallclock,
   fake-Z convention). Nothing is reimplemented here; raw `annke:N` /
   `amcrest:N` targets are expressed by seeding the same `NVR_*_<CAM>`
   env keys that module already reads.
4. Pull serially with ffmpeg (stream-copy video, AAC audio, 3s gap) —
   the NVR tolerates only a couple of concurrent RTSP sessions.
5. Write `manifest.json` (rewritten after every pull so a crash still
   leaves a usable partial record).

6. Optional `--run-detection`: push each pulled clip through the real
   detector (`python -m src.main --video <clip>`) in a **test bulkhead**
   — `STATE_DRY_RUN=1` (no Postgres rows, no HA/webhooks), snapshots
   redirected under the out-dir, PTZ slew disabled — and collect the
   VLM verdicts via the pipeline's opt-in `REPLAY_REPORT_PATH` sidecar.
   Each window's manifest entry gains `detection` (gate-funnel counters
   + per-verdict `{ts_offset_s, species, confidence, bbox}`) and a
   `verdict` (`caught_event`, `n_source_alerts`); a top-level `summary`
   carries `total_windows / caught_event_count / catch_rate`. That is
   the actual validation signal: did the TARGET camera see what the
   source cameras alerted on?

## Running

Clip pull only: the `archiver` image is the right runtime (ffmpeg +
psycopg + the cv2-free playback_url + clips mount + NVR_* env); `web`
has no ffmpeg. `scripts/` isn't baked into that image, so bind-mount it:

    MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps \\
        -v "$(pwd)/scripts:/app/scripts:ro" archiver \\
        python scripts/event_triggered_replay.py \\
            --target-camera annke:7 --target-label side_path \\
            --lookback-hours 6 --limit 10 \\
            --out-dir /app/clips/event_triggered/side_path_smoke

With `--run-detection` the runtime must be a `detector-*` image instead
(cv2 + YOLO weights + Ollama reach; it also carries ffmpeg and psycopg,
so the pull path works unchanged). The archiver image has no docker
socket, so it cannot spawn detector containers — run the whole script in
the detector image rather than pretending otherwise:

    MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps \\
        -v "$(pwd)/scripts:/app/scripts:ro" detector-backyard \\
        python scripts/event_triggered_replay.py \\
            --target-camera annke:7 --target-label side_path \\
            --lookback-hours 6 --limit 3 --run-detection \\
            --out-dir /app/clips/event_triggered/side_path_smoke

Pick the detector service whose tuning is closest to the target camera
(backyard = Hikvision-family 4K side angle); its env is the baseline the
replay inherits. Add `--detector-env KEY=VAL` to override tuning knobs.

Add `--dry-run` to print the manifest without pulling, `--print-cmd` to
see the ffmpeg invocations (credentials included — for debugging only).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Make `src.*` importable whether we're invoked from /app (container) or
# the repo root on the host.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from src.stream.playback_url import build_nvr_playback_url  # noqa: E402

logger = logging.getLogger("event_triggered_replay")

FLEET_CAMERAS = ["yard", "rooftop", "backyard", "crawlspace", "crawlspace_inside"]

# build_nvr_playback_url hard-codes endtime = start + 2min. A window longer
# than that would be silently truncated by the NVR, so the coalescer caps
# cluster length here rather than pretending we can serve it.
PLAYBACK_URL_MAX_SECONDS = 120

_CRED_RE = re.compile(r"://[^:/@]+:[^@]+@")


def _redact(url: str) -> str:
    return _CRED_RE.sub("://****:****@", url)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _iso_compact(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ── Target resolution ──────────────────────────────────────────────────


@dataclass
class Target:
    spec: str          # what the operator typed
    label: str         # filesystem-safe label for dirs/filenames
    camera_id: str     # key fed to build_nvr_playback_url (drives NVR_*_<CAM> lookup)
    channel: int
    family: str        # informational only; playback_url reads env itself
    host: str          # informational only (redacted in manifest? no — host isn't a secret)


def resolve_target(spec: str, label: Optional[str]) -> Target:
    """Map --target-camera to a (camera_id, channel) pair that
    build_nvr_playback_url will route correctly.

    Named fleet cameras rely on the NVR_CHANNEL_<CAM> / NVR_HOST_<CAM> /
    NVR_FAMILY_<CAM> env the compose file already sets. Raw `annke:N` /
    `amcrest:N` specs seed a synthetic camera key (`annke_7`) with the
    same env shape so the builder needs no special-casing.
    """
    spec = spec.strip()
    m = re.fullmatch(r"(annke|amcrest):(\d+)", spec, re.I)
    if m:
        vendor, ch = m.group(1).lower(), int(m.group(2))
        cam_key = f"{vendor}_{ch}"
        env_cam = cam_key.upper()
        if vendor == "annke":
            host = os.getenv("ANNKE_HOST", "")
            if not host:
                raise SystemExit("annke:N target needs ANNKE_HOST/ANNKE_USER/ANNKE_PASS in env")
            os.environ.setdefault(f"NVR_HOST_{env_cam}", host)
            os.environ.setdefault(f"NVR_USER_{env_cam}", os.getenv("ANNKE_USER", "admin"))
            os.environ.setdefault(f"NVR_PASS_{env_cam}", os.getenv("ANNKE_PASS", ""))
            os.environ.setdefault(f"NVR_FAMILY_{env_cam}", "hikvision")
            family = "hikvision"
        else:
            host = os.getenv("AMCREST_HOST", "")
            if not host:
                raise SystemExit("amcrest:N target needs AMCREST_HOST/AMCREST_USER/AMCREST_PASS in env")
            # Fleet defaults (AMCREST_*) are the builder's fallback; nothing to seed.
            family = "dahua"
        return Target(spec=spec, label=label or cam_key, camera_id=cam_key,
                      channel=ch, family=family, host=host)

    cam = spec.lower()
    if cam not in FLEET_CAMERAS:
        raise SystemExit(
            f"--target-camera must be one of {FLEET_CAMERAS} or annke:N / amcrest:N, got {spec!r}"
        )
    ch_env = os.getenv(f"NVR_CHANNEL_{cam.upper()}")
    if not ch_env:
        raise SystemExit(f"NVR_CHANNEL_{cam.upper()} not set in env — can't route target {cam}")
    family = (os.getenv(f"NVR_FAMILY_{cam.upper()}") or "dahua").lower()
    host = os.getenv(f"NVR_HOST_{cam.upper()}") or os.getenv("AMCREST_HOST", "")
    return Target(spec=spec, label=label or cam, camera_id=cam,
                  channel=int(ch_env), family=family, host=host)


# ── Alert query ────────────────────────────────────────────────────────


def fetch_alerts(dsn: str, cameras: list[str], species: list[str],
                 since_ts: float, until_ts: float) -> list[dict]:
    sql = """
        SELECT id, ts, camera_id, species, confidence, is_rodent, snapshot, track_id
        FROM alerts
        WHERE camera_id = ANY(%s)
          AND species   = ANY(%s)
          AND historical = false
          AND ts >= %s AND ts <= %s
        ORDER BY ts ASC
    """
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(sql, (cameras, species, since_ts, until_ts))
        return cur.fetchall()


# ── Coalescer ──────────────────────────────────────────────────────────


@dataclass
class Window:
    start_ts: float
    end_ts: float
    first_event_ts: float
    last_event_ts: float
    alert_ids: list[int] = field(default_factory=list)
    source_cameras: dict[str, int] = field(default_factory=dict)
    species: dict[str, int] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end_ts - self.start_ts


def coalesce(alerts: list[dict], pre: int, post: int, gap: int, max_window: int) -> list[Window]:
    """Debounce coalescer over alert ts.

    Consecutive alerts whose ts are within `gap` seconds join the same
    cluster; the cluster's clip window is [first - pre, last + post].
    A cluster is closed early if extending it would push the window past
    `max_window` — long rodent parties become several clips instead of
    one the NVR would truncate.
    """
    windows: list[Window] = []
    cur: Optional[Window] = None
    for a in alerts:
        ts = float(a["ts"])
        if cur is not None:
            fits_gap = (ts - cur.last_event_ts) <= gap
            fits_len = (ts + post) - cur.start_ts <= max_window
            if fits_gap and fits_len:
                cur.last_event_ts = ts
                cur.end_ts = ts + post
                cur.alert_ids.append(int(a["id"]))
                cur.source_cameras[a["camera_id"]] = cur.source_cameras.get(a["camera_id"], 0) + 1
                cur.species[a["species"]] = cur.species.get(a["species"], 0) + 1
                continue
            windows.append(cur)
        cur = Window(start_ts=ts - pre, end_ts=ts + post, first_event_ts=ts, last_event_ts=ts,
                     alert_ids=[int(a["id"])],
                     source_cameras={a["camera_id"]: 1},
                     species={a["species"]: 1})
    if cur is not None:
        windows.append(cur)
    return windows


# ── Pull ───────────────────────────────────────────────────────────────


def build_ffmpeg_cmd(url: str, duration: int, out_path: Path, ffmpeg: str,
                     drop_audio: bool = False) -> list[str]:
    cmd = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "warning",
        "-rtsp_transport", "tcp",
        # socket I/O timeout in microseconds — 15s. Without it a dead NVR
        # session hangs ffmpeg until our subprocess timeout fires.
        "-timeout", "15000000",
        "-y",
        "-i", url,
        "-t", str(duration),
        "-c:v", "copy",
    ]
    if drop_audio:
        cmd += ["-an"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "64k"]
    cmd += ["-movflags", "+faststart", str(out_path)]
    return cmd


def ffprobe(path: Path, ffprobe_bin: str) -> dict:
    """Return {duration, codec, width, height} or {} on failure."""
    try:
        out = subprocess.run(
            [ffprobe_bin, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(out.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        dur = data.get("format", {}).get("duration")
        return {
            "duration": round(float(dur), 2) if dur else None,
            "codec": stream.get("codec_name"),
            "width": stream.get("width"),
            "height": stream.get("height"),
        }
    except Exception as e:  # pragma: no cover - diagnostics only
        return {"error": str(e)}


def _tail(s: str, n: int = 600) -> str:
    s = s.strip()
    return s[-n:] if len(s) > n else s


def pull_window(w: Window, target: Target, out_path: Path, args) -> dict:
    """Run ffmpeg for one window. Returns a manifest entry fragment."""
    duration = int(round(w.duration))
    # pre_roll_seconds=0 + timestamp=start_ts → URL starttime == window
    # start; endtime is the builder's +2min, which ≥ our capped duration,
    # and -t bounds the actual pull.
    url = build_nvr_playback_url(
        timestamp=w.start_ts, base_rtsp_url="", pre_roll_seconds=0,
        nvr_channel=target.channel, speed=1, camera_id=target.camera_id,
    )
    entry: dict = {"playback_url": _redact(url), "ffmpeg_attempts": []}

    attempts = [False] if args.no_audio else [False, True]
    for drop_audio in attempts:
        cmd = build_ffmpeg_cmd(url, duration, out_path, args.ffmpeg, drop_audio=drop_audio)
        if args.print_cmd:
            print(" ".join(cmd))
        if args.dry_run:
            entry["pull_status"] = "dry-run"
            return entry
        t0 = time.monotonic()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=duration * 3 + 30)
            rc, err = proc.returncode, _tail(proc.stderr)
        except subprocess.TimeoutExpired as e:
            rc, err = -1, f"timeout after {duration * 3 + 30}s: {_tail(str(e.stderr or ''))}"
        elapsed = round(time.monotonic() - t0, 1)
        size = out_path.stat().st_size if out_path.exists() else 0
        attempt = {"drop_audio": drop_audio, "rc": rc, "elapsed_s": elapsed,
                   "size_bytes": size, "stderr_tail": err}
        entry["ffmpeg_attempts"].append(attempt)
        if rc == 0 and size > 0:
            probe = ffprobe(out_path, args.ffprobe)
            entry.update({
                "pull_status": "ok",
                "size_bytes": size,
                "probe": probe,
                "audio_dropped": drop_audio,
            })
            return entry
        # Failed — clean up any zero/partial file before a retry / exit.
        if out_path.exists():
            try:
                out_path.unlink()
            except OSError:
                pass
        # Only fall through to the -an retry when the failure smells
        # like an audio-encode problem; a 4xx from the NVR won't be fixed
        # by dropping audio, so don't hammer it twice.
        if not drop_audio and not re.search(r"aac|audio|pcm_|Audio", err or ""):
            break
    entry["pull_status"] = "fail"
    last = entry["ffmpeg_attempts"][-1] if entry["ffmpeg_attempts"] else {}
    entry["failure_reason"] = _classify_failure(last.get("stderr_tail", ""), last.get("rc"))
    entry["size_bytes"] = 0
    return entry


def _classify_failure(stderr: str, rc: Optional[int]) -> str:
    s = stderr or ""
    if rc == -1:
        return "timeout"
    if "400" in s or "Bad Request" in s:
        return "nvr_400_bad_request (window likely past recording tail or not recorded)"
    if "404" in s or "Not Found" in s:
        return "nvr_404_not_found"
    if "401" in s or "Unauthorized" in s:
        return "nvr_401_unauthorized"
    if "453" in s or "Not Enough Bandwidth" in s:
        return "nvr_453_not_enough_bandwidth (too many concurrent sessions)"
    if "Connection refused" in s or "No route" in s:
        return "nvr_unreachable"
    if "Output file is empty" in s or "does not contain any stream" in s:
        return "empty_stream"
    return _tail(s, 200) or f"ffmpeg_rc_{rc}"


# ── Detection (opt-in) ─────────────────────────────────────────────────

# Species the VLM returns for "nothing alertable here". A verdict with one
# of these is never a catch, even if the model mislabels detected=true.
NON_CATCH_SPECIES = {"insect", "none", "unknown", ""}

# Env the replay subprocess ALWAYS gets. Isolation first (test bulkhead:
# no DB rows, no pages, no PTZ moves), then "make the replay complete":
# single pass, no stale-drop (freshness deadlines are meaningless offline
# — we want to know whether the target saw it, not whether an alert would
# have been actionable), EOF exit + JSON sidecar.
DETECTOR_ENV_FIXED = {
    "STATE_DRY_RUN": "1",
    "VIDEO_LOOP": "false",
    "VIDEO_SPEED": "1.0",
    "PREVIEW_ENABLED": "false",
    "SLEW_ENABLED": "false",
    "SELF_SLEW_ENABLED": "false",
    "HA_WEBHOOK_URL": "",
    "ALERT_WEBHOOK_URL": "",
    "REPLAY_EXIT_ON_EOF": "1",
    "VLM_MAX_ALERT_AGE_S": "600",
    # Override the operator pause file-sentinel so the sandbox never
    # inherits a UI pause from the live detector. Live detectors and the
    # backfill sandbox share ./config: without this, pausing "crawlspace"
    # in the UI would silently zero out every backfill run's detection
    # (motion.detect() never runs → 0 motion_events → 0 verdicts).
    # /dev/null-style unreachable path — os.path.exists() is False → gate
    # never fires. Backfill is orthogonal to live pause state.
    "OPERATOR_PAUSE_FLAG_PATH": "/tmp/__replay_never_pause__",
    # Saturate the Ollama VLM pool during backfill. Live detectors are
    # tuned to VLM_MAX_INFLIGHT=4 to leave room for peers on the shared
    # Ollama instance; a backfill sandbox has no such neighbor obligation
    # — 8 matches Ollama's NUM_PARALLEL cap and cuts detection wall-clock
    # ~30-40% on VLM-bound chunks. Any live detector still running shares
    # the lanes fairly (Ollama round-robins requests).
    "VLM_MAX_INFLIGHT": "8",
}
# Env seeded per target unless the operator overrides via --detector-env.
# CAMERA_ID/ZONE_KEY keyed on the target label mean: no OSD mask, an
# empty zone polygon (→ full-frame detection until someone draws one),
# and a baseline path that doesn't exist (→ no pixel-diff pre-filter
# tuned for a different camera silently eating the target's motion).
def _detector_env_defaults(target: Target) -> dict[str, str]:
    return {
        "CAMERA_ID": target.label,
        "ZONE_KEY": f"{target.label}_zone",
        "BASELINE_PATH": f"data/baseline_{target.label}.jpg",
        "RTSP_URL": "",   # belt-and-braces: --video wins, but never fall back to a live cam
    }


def parse_env_overrides(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"--detector-env expects KEY=VAL, got {item!r}")
        k, v = item.split("=", 1)
        out[k.strip()] = v
    return out


def preflight_detector(python: str) -> None:
    """Fail fast at the trust boundary: --run-detection needs the detector
    stack (cv2 + ultralytics + src.pipeline importable). The archiver image
    has none of that — say so instead of failing 3 clips in."""
    probe = subprocess.run(
        [python, "-c", "import cv2, ultralytics; import src.pipeline"],
        capture_output=True, text=True, cwd=str(_REPO_ROOT), timeout=120,
    )
    if probe.returncode != 0:
        raise SystemExit(
            "--run-detection needs the detector image (cv2 + YOLO + Ollama reach). "
            "Re-run via `docker compose run --rm --no-deps -v \"$(pwd)/scripts:/app/scripts:ro\" "
            "detector-backyard python scripts/event_triggered_replay.py ...`.\n"
            f"probe stderr: {_tail(probe.stderr, 400)}"
        )


DETECTOR_KILL_GRACE_S = 45


def run_detection(clip_path: Path, detect_dir: Path, target: Target, args,
                  clip_duration_s: float = 0.0) -> dict:
    """Replay one clip through `python -m src.main --video` in a dry-run
    bulkhead and return the `detection` manifest block.

    Serialized by construction (called inline per window). Idempotent:
    an existing report for this clip is reused unless --force-detection.
    """
    stem = clip_path.stem
    report_path = detect_dir / f"{stem}.report.json"
    log_path = detect_dir / f"{stem}.detector.log"
    snap_dir = detect_dir / f"{stem}_snapshots"
    detect_dir.mkdir(parents=True, exist_ok=True)

    if report_path.exists() and not args.force_detection:
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            block = _detection_block_from_report(report, report_path, log_path, snap_dir)
            block["skipped_existing"] = True
            return block
        except Exception as e:  # corrupt/partial report → re-run
            logger.warning("existing report %s unreadable (%s) — re-running detection", report_path, e)

    env = os.environ.copy()
    env.update(_detector_env_defaults(target))
    env.update(parse_env_overrides(args.detector_env))
    env.update(DETECTOR_ENV_FIXED)          # isolation keys are not overridable
    env["SNAPSHOT_DIR"] = str(snap_dir)
    env["REPLAY_REPORT_PATH"] = str(report_path)
    env["REPLAY_DRAIN_TIMEOUT_S"] = str(args.detection_drain_seconds)
    if report_path.exists():
        report_path.unlink()

    # Timeout budget: CPU decode of 4K HEVC + YOLO + a shared Ollama ran at
    # ~9-10x realtime in the smoke, so scale with clip length (0 = auto).
    timeout_s = args.detection_timeout_seconds or max(900, int(clip_duration_s * 15) + 300)

    cmd = [args.detector_python, "-m", "src.main", "--video", str(clip_path)]
    if args.print_cmd:
        print(" ".join(cmd))
    t0 = time.monotonic()
    timed_out = False
    with log_path.open("w", encoding="utf-8") as logf:
        proc = subprocess.Popen(cmd, cwd=str(_REPO_ROOT), env=env, stdout=logf,
                                stderr=subprocess.STDOUT)
        try:
            rc = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            # Graceful first: SIGINT trips src.main's shutdown event, the
            # loop exits, and the pipeline's `finally` writes the partial
            # report. Hard-kill only if that doesn't happen in time.
            timed_out = True
            logger.warning("    detector exceeded %ds — sending SIGINT, %ds grace for the partial report",
                           timeout_s, DETECTOR_KILL_GRACE_S)
            try:
                proc.send_signal(signal.SIGINT)
                rc = proc.wait(timeout=DETECTOR_KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = proc.wait()
    elapsed = round(time.monotonic() - t0, 1)

    block: dict = {"elapsed_s": elapsed, "rc": rc, "timeout_s": timeout_s, "log": str(log_path)}
    if not report_path.exists():
        block.update({
            "status": "timeout" if timed_out else "no_report",
            "error": (f"detector exceeded {timeout_s}s and wrote no report" if timed_out
                      else f"detector exited rc={rc} without writing {report_path.name}; see log"),
        })
        return block
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as e:
        block.update({"status": "bad_report", "error": str(e)})
        return block
    block.update(_detection_block_from_report(report, report_path, log_path, snap_dir))
    if timed_out:
        # Report exists (written in finally after SIGINT) but the clip was
        # not fully processed — verdicts cover only the frames seen.
        block["status"] = "timeout_partial"
    return block


def _detection_block_from_report(report: dict, report_path: Path, log_path: Path,
                                 snap_dir: Path) -> dict:
    funnel = report.get("gate_funnel") or {}
    verdicts = report.get("verdicts") or []
    alerts = report.get("alerts") or []
    detections = [{
        "ts_offset_s": v.get("clip_pos_s"),
        "species": v.get("species"),
        "confidence": v.get("confidence"),
        "bbox": v.get("bbox"),
        "wildlife_detected": v.get("wildlife_detected"),
        "is_rodent": v.get("is_rodent"),
        "track_id": v.get("track_id"),
        "vlm_queue_age_s": v.get("vlm_queue_age_s"),
        "description": v.get("description"),
    } for v in verdicts]
    return {
        "status": "ok" if report.get("exit_reason") in ("eof", "eof_drain_timeout") else report.get("exit_reason", "unknown"),
        "exit_reason": report.get("exit_reason"),
        "backend": report.get("backend"),
        "frames_processed": report.get("frames_processed"),
        "n_motion_events": funnel.get("motion_events", 0),
        "n_zone_events": funnel.get("zone_events", 0),
        "n_baseline_filtered": funnel.get("baseline_filtered", 0),
        "n_vlm_calls": funnel.get("vlm_calls", 0),
        "n_vlm_rejected": funnel.get("vlm_rejected", 0),
        "n_vlm_insect": funnel.get("vlm_insect", 0),
        "n_vlm_positive": sum(1 for d in detections if d["wildlife_detected"]
                              and str(d["species"] or "").lower() not in NON_CATCH_SPECIES),
        "n_vlm_verdicts_harvested": len(detections),
        "n_vlm_pending_at_exit": report.get("pending_vlm_jobs", 0),
        "n_alerts": len(alerts),
        "detections": detections,
        "alerts": alerts,
        "report": str(report_path),
        "log": str(log_path),
        "snapshot_dir": str(snap_dir),
    }


def compute_verdict(entry: dict) -> dict:
    """Per-window verdict: did the TARGET camera catch the event the SOURCE
    cameras alerted on? `caught_event` = at least one VLM-positive verdict
    with a non-insect species inside the window."""
    det = entry.get("detection") or {}
    target_species: dict[str, int] = {}
    caught = False
    if det.get("status") in ("ok", "timeout_partial"):
        for d in det.get("detections", []):
            sp = str(d.get("species") or "").lower()
            if d.get("wildlife_detected") and sp not in NON_CATCH_SPECIES:
                caught = True
                target_species[sp] = target_species.get(sp, 0) + 1
    return {
        "caught_event": caught,
        "n_source_alerts": entry.get("dedupe_count", 0),
        "source_species": entry.get("species", {}),
        "target_species": target_species,
        "detection_status": det.get("status"),
    }


# ── Main ───────────────────────────────────────────────────────────────


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Pull target-camera clips around source-camera alert timestamps.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--source-cameras", default=",".join(FLEET_CAMERAS),
                    help="comma-separated alerts.camera_id values (default: all 5)")
    ap.add_argument("--target-camera", required=True,
                    help="yard|rooftop|backyard|crawlspace|crawlspace_inside or annke:N / amcrest:N")
    ap.add_argument("--target-label", default=None,
                    help="filesystem label for the target (default: derived from --target-camera)")
    ap.add_argument("--lookback-hours", type=float, default=24.0)
    ap.add_argument("--until-iso", default=None,
                    help="anchor the lookback horizon at this UTC time instead of now (e.g. the previous "
                         "run's run_started_utc) so a re-run selects the same windows with --limit")
    ap.add_argument("--pre-roll-seconds", type=int, default=30)
    ap.add_argument("--post-roll-seconds", type=int, default=30)
    ap.add_argument("--dedupe-window-seconds", type=int, default=60,
                    help="alerts within this many seconds of each other share one clip")
    ap.add_argument("--max-window-seconds", type=int, default=PLAYBACK_URL_MAX_SECONDS,
                    help=f"hard cap on one clip's length (NVR playback URL serves <={PLAYBACK_URL_MAX_SECONDS}s)")
    ap.add_argument("--min-age-seconds", type=int, default=90,
                    help="skip windows ending closer to now than this — the NVR's live recording tail isn't seekable yet")
    ap.add_argument("--species-filter", default="rat,mouse,other")
    ap.add_argument("--out-dir", default=None,
                    help="default: $CLIPS_DIR/event_triggered/<label>_<utc_iso>/")
    ap.add_argument("--limit", type=int, default=0,
                    help="max windows to pull, newest first (0 = all)")
    ap.add_argument("--sleep-seconds", type=float, default=3.0, help="gap between pulls")
    ap.add_argument("--no-audio", action="store_true", help="-an instead of aac transcode")
    ap.add_argument("--ffmpeg", default=os.getenv("FFMPEG_BINARY", "ffmpeg"))
    ap.add_argument("--ffprobe", default=os.getenv("FFPROBE_BINARY", "ffprobe"))
    ap.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    ap.add_argument("--dry-run", action="store_true", help="query + coalesce + print manifest; no pulls")
    ap.add_argument("--print-cmd", action="store_true", help="print each ffmpeg command (creds included)")
    det = ap.add_argument_group("detection (opt-in; needs the detector image)")
    det.add_argument("--run-detection", action="store_true",
                     help="after each pull, replay the clip through src.main in STATE_DRY_RUN and record verdicts")
    det.add_argument("--detector-python", default=sys.executable,
                     help="interpreter for `-m src.main` (default: this one)")
    det.add_argument("--detector-env", action="append", default=[], metavar="KEY=VAL",
                     help="extra env for the detector subprocess (tuning knobs); repeatable. Isolation keys are not overridable")
    det.add_argument("--detection-timeout-seconds", type=int, default=0,
                     help="budget for one clip's detection run; 0 = auto: max(900, 15*clip_len + 300). "
                          "On expiry SIGINT first (partial report), hard kill after 45s")
    det.add_argument("--detection-drain-seconds", type=int, default=90,
                     help="after EOF, how long to wait for in-flight VLM verdicts before exiting")
    det.add_argument("--force-detection", action="store_true",
                     help="re-run detection even when a report for the clip already exists")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # playback_url logs the full URL at INFO — keep that below our floor
    # unless the operator asked for verbose.
    if not args.verbose:
        logging.getLogger("src.stream.playback_url").setLevel(logging.WARNING)

    if not args.database_url:
        raise SystemExit("DATABASE_URL not set (or pass --database-url)")
    if args.max_window_seconds > PLAYBACK_URL_MAX_SECONDS:
        logger.warning("--max-window-seconds %d exceeds the playback URL's %ds endtime; clips will be truncated by the NVR",
                       args.max_window_seconds, PLAYBACK_URL_MAX_SECONDS)
    if args.pre_roll_seconds + args.post_roll_seconds > args.max_window_seconds:
        raise SystemExit("pre + post roll exceeds --max-window-seconds; a single alert wouldn't fit")

    target = resolve_target(args.target_camera, args.target_label)
    run_started = time.time()
    now_ts = run_started
    if args.until_iso:
        # Reproducible window selection: `--limit N` keeps the newest N
        # windows *before the anchor*, so a re-run with the first run's
        # run_started_utc sees the same set regardless of new alerts.
        try:
            _until = datetime.fromisoformat(args.until_iso.replace("Z", "+00:00"))
        except ValueError:
            raise SystemExit(f"--until-iso must be ISO-8601, got {args.until_iso!r}")
        if _until.tzinfo is None:
            _until = _until.replace(tzinfo=timezone.utc)
        now_ts = min(_until.timestamp(), run_started)
    since_ts = now_ts - args.lookback_hours * 3600.0
    sources = [c.strip() for c in args.source_cameras.split(",") if c.strip()]
    species = [s.strip() for s in args.species_filter.split(",") if s.strip()]

    clips_root = Path(os.getenv("CLIPS_DIR", "clips"))
    out_dir = Path(args.out_dir) if args.out_dir else (
        clips_root / "event_triggered" / f"{target.label}_{_iso_compact(run_started)}"
    )

    logger.info("target=%s (camera_id=%s ch=%d family=%s host=%s)",
                target.spec, target.camera_id, target.channel, target.family, target.host)
    logger.info("sources=%s species=%s lookback=%.1fh (%s → %s)",
                sources, species, args.lookback_hours, _iso(since_ts), _iso(now_ts))

    alerts = fetch_alerts(args.database_url, sources, species, since_ts, now_ts)
    logger.info("fetched %d alerts", len(alerts))

    windows = coalesce(alerts, args.pre_roll_seconds, args.post_roll_seconds,
                       args.dedupe_window_seconds, args.max_window_seconds)
    total_before_filters = len(windows)
    too_fresh = [w for w in windows if w.end_ts > now_ts - args.min_age_seconds]
    windows = [w for w in windows if w.end_ts <= now_ts - args.min_age_seconds]
    logger.info("coalesced → %d windows (%d skipped: inside %ds recording tail)",
                total_before_filters, len(too_fresh), args.min_age_seconds)

    # Newest first — most likely still on the NVR — then re-sort ascending
    # for a tidy manifest.
    windows.sort(key=lambda w: w.start_ts, reverse=True)
    if args.limit > 0:
        windows = windows[: args.limit]
    windows.sort(key=lambda w: w.start_ts)

    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        for binary in (args.ffmpeg, args.ffprobe):
            if shutil.which(binary) is None:
                raise SystemExit(f"{binary!r} not on PATH — run inside the archiver image or pass --ffmpeg/--ffprobe")
        if args.run_detection:
            preflight_detector(args.detector_python)
    detect_dir = out_dir / "detect"

    manifest: dict = {
        "schema": "event_triggered_replay/v2" if args.run_detection else "event_triggered_replay/v1",
        # `summary` first so the validation answer is the first thing in the file.
        "summary": {},
        "run_started_utc": _iso(run_started),
        "args": {k: v for k, v in vars(args).items() if k not in ("database_url",)},
        "target": asdict(target),
        "window_selection": {
            "alerts_fetched": len(alerts),
            "windows_coalesced": total_before_filters,
            "windows_skipped_recording_tail": len(too_fresh),
            "windows_selected": len(windows),
        },
        "clips": [],
        "aggregate": {},
    }
    manifest_path = out_dir / "manifest.json"

    def _flush() -> None:
        ok = [c for c in manifest["clips"] if c["pull_status"] == "ok"]
        failed = [c for c in manifest["clips"] if c["pull_status"] == "fail"]
        manifest["aggregate"] = {
            "total_windows": len(windows),
            "total_pulled_ok": len(ok),
            "total_failed": len(failed),
            "total_duration_seconds": round(sum(
                (c.get("probe") or {}).get("duration") or 0.0 for c in ok), 1),
            "total_size_bytes": sum(c.get("size_bytes", 0) for c in ok),
            "total_source_alerts": sum(c["dedupe_count"] for c in manifest["clips"]),
            "run_finished_utc": _iso(time.time()),
        }
        detected = [c for c in manifest["clips"]
                    if (c.get("detection") or {}).get("status") in ("ok", "timeout_partial")]
        caught = [c for c in detected if (c.get("verdict") or {}).get("caught_event")]
        manifest["summary"] = {
            "total_windows": len(windows),
            "windows_detected": len(detected),
            "caught_event_count": len(caught),
            # Denominator is windows where detection actually ran — a failed
            # pull is not a miss. `total_windows` is alongside for context.
            "catch_rate": round(len(caught) / len(detected), 3) if detected else None,
            "run_detection": bool(args.run_detection),
        }
        if not args.dry_run:
            tmp = manifest_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(manifest, indent=2, default=str))
            tmp.replace(manifest_path)

    for i, w in enumerate(windows, 1):
        fname = f"{target.label}_{_iso_compact(w.start_ts)}_{int(round(w.duration))}s_n{len(w.alert_ids)}.mp4"
        out_path = out_dir / fname
        entry = {
            "index": i,
            "window": {
                "start_ts": round(w.start_ts, 3),
                "end_ts": round(w.end_ts, 3),
                "start_iso": _iso(w.start_ts),
                "end_iso": _iso(w.end_ts),
                "first_event_iso": _iso(w.first_event_ts),
                "last_event_iso": _iso(w.last_event_ts),
                "duration_seconds": round(w.duration, 1),
            },
            "file": str(out_path),
            "source_alert_ids": w.alert_ids,
            "dedupe_count": len(w.alert_ids),
            "source_cameras": w.source_cameras,
            "species": w.species,
        }
        def _detect(entry: dict) -> None:
            """Serialized detection pass on a pulled clip; flushes the manifest
            before (so a crash mid-detection leaves the pull recorded) and after."""
            if not (args.run_detection and not args.dry_run and entry.get("pull_status") == "ok"):
                return
            entry["detection"] = {"status": "running"}
            _flush()
            logger.info("    detect %s …", out_path.name)
            entry["detection"] = run_detection(
                out_path, detect_dir, target, args,
                clip_duration_s=(entry.get("probe") or {}).get("duration") or w.duration)
            entry["verdict"] = compute_verdict(entry)
            d, v = entry["detection"], entry["verdict"]
            if d.get("status") in ("ok", "timeout_partial"):
                logger.info("    %s  motion=%d zone=%d vlm=%d pos=%d insect=%d alerts=%d  (%.0fs%s)  source=%s target=%s",
                            "CAUGHT" if v["caught_event"] else "missed",
                            d["n_motion_events"], d["n_zone_events"], d["n_vlm_calls"],
                            d["n_vlm_positive"], d["n_vlm_insect"], d["n_alerts"],
                            d.get("elapsed_s") or 0.0, ", cached" if d.get("skipped_existing") else "",
                            v["source_species"], v["target_species"] or "{}")
            else:
                logger.warning("    detection %s — %s", d.get("status"), d.get("error", ""))

        if out_path.exists() and out_path.stat().st_size > 0 and not args.dry_run:
            # Idempotency: same window → same filename; skip re-pull.
            entry.update({"pull_status": "ok", "size_bytes": out_path.stat().st_size,
                          "probe": ffprobe(out_path, args.ffprobe), "skipped_existing": True})
            manifest["clips"].append(entry)
            _flush()
            logger.info("[%d/%d] exists, skipping pull of %s", i, len(windows), fname)
            _detect(entry)
            _flush()
            continue

        logger.info("[%d/%d] %s → %s  (%d alerts: %s)", i, len(windows),
                    entry["window"]["start_iso"], entry["window"]["end_iso"],
                    len(w.alert_ids), w.source_cameras)
        entry.update(pull_window(w, target, out_path, args))
        manifest["clips"].append(entry)
        _flush()
        if entry["pull_status"] == "ok":
            p = entry.get("probe") or {}
            logger.info("    ok  %s  %s %sx%s  %.1fs  %d bytes",
                        fname, p.get("codec"), p.get("width"), p.get("height"),
                        p.get("duration") or 0.0, entry["size_bytes"])
        elif entry["pull_status"] == "fail":
            logger.warning("    FAIL %s — %s", fname, entry.get("failure_reason"))
        _detect(entry)
        _flush()
        if not args.dry_run and i < len(windows):
            time.sleep(args.sleep_seconds)

    _flush()
    if args.dry_run:
        print(json.dumps(manifest, indent=2, default=str))
    else:
        agg = manifest["aggregate"]
        logger.info("done: pulled %d/%d ok, %d failed, %.1fs of footage → %s",
                    agg["total_pulled_ok"], agg["total_windows"], agg["total_failed"],
                    agg["total_duration_seconds"], manifest_path)
        if args.run_detection:
            s = manifest["summary"]
            logger.info("detection: %d/%d windows caught the source event (catch_rate=%s)",
                        s["caught_event_count"], s["windows_detected"], s["catch_rate"])
    return 0 if not windows or manifest["aggregate"].get("total_pulled_ok", 0) > 0 or args.dry_run else 1


if __name__ == "__main__":
    sys.exit(main())
