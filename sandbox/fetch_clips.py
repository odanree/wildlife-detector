"""Fetch ±5s MP4 clips from the NVR playback stream around each alert
timestamp, and write a ground-truth JSON that pairs clips with their
label verdict.

Runs on the HOST (uses local ffmpeg + reads the bind-mounted state.db
directly). No container needed.

Usage:
    python sandbox/fetch_clips.py --tps                     # all 16 rooftop TPs
    python sandbox/fetch_clips.py --sample-fps 30           # 30 random rooftop FPs
    python sandbox/fetch_clips.py --tps --sample-fps 30     # both

Requires:
    - NVR (Dahua/Amcrest) reachable at $AMCREST_HOST (default 192.168.1.148)
    - ffmpeg on PATH

The NVR playback endpoint is:
    rtsp://<user>:<pass>@<host>:554/cam/playback?channel=<N>&starttime=<T>&endtime=<T>

starttime/endtime in NVR-local time YYYY_MM_DD_hh_mm_ss format. Channel
6 = rooftop (per the NVR channel mapping). Recording gaps at NVR file
boundaries can cause short clips; we skip anything under 100KB and flag
it in the manifest.

Pattern: **batch offline capture** — cheap read-only pull, mirrors the
'download-once-analyze-many' shape of ML dataset building.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Resolve paths relative to this script so it doesn't matter where you
# run it from.
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DB_PATH = REPO_ROOT / "data" / "state.db"
CLIPS_DIR = SCRIPT_DIR / "clips"
MANIFEST = SCRIPT_DIR / "ground_truth.json"

# NVR credentials — pull from repo .env if present so we don't hardcode
# them. .env format is simple KEY=VALUE lines.
def _load_env_from_dotenv() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())

_load_env_from_dotenv()

NVR_HOST = os.getenv("AMCREST_HOST", "192.168.1.148")
NVR_USER = os.getenv("AMCREST_USER", "admin")
NVR_PASS = os.getenv("AMCREST_PASSWORD", "windows98")
NVR_CHANNEL_BY_CAMERA = {
    # channel numbers per the NVR mapping — verify against RTSP_URL_* in .env
    "rooftop": 6,
    "yard": 8,
}

# Window around each alert timestamp: -PRE_S seconds .. +POST_S seconds.
# Rationale: motion detector needs a few frames of settled background
# before the target enters, plus enough post-event frames for the
# persistence gate to fire. 5s each side at 15fps = 150 frames of
# context, generous but cheap.
PRE_S = 5
POST_S = 5

# NVR local timezone — the /cam/playback endpoint interprets starttime
# in the NVR's configured local time, not UTC. Set once here to avoid
# UTC/local drift; adjust for your NVR's TZ.
NVR_TZ = timezone(timedelta(hours=-7))  # America/Los_Angeles PDT; use -8 for PST


def _nvr_playback_url(camera: str, start: datetime, end: datetime) -> str:
    """Build the /cam/playback URL for a time window on one camera."""
    channel = NVR_CHANNEL_BY_CAMERA.get(camera)
    if channel is None:
        raise ValueError(f"Unknown camera {camera!r}; add to NVR_CHANNEL_BY_CAMERA")
    def fmt(t: datetime) -> str:
        local = t.astimezone(NVR_TZ)
        return local.strftime("%Y_%m_%d_%H_%M_%S")
    return (
        f"rtsp://{NVR_USER}:{NVR_PASS}@{NVR_HOST}:554/cam/playback"
        f"?channel={channel}&starttime={fmt(start)}&endtime={fmt(end)}"
    )


def _fetch_one(alert_id: int, ts_epoch: float, camera: str, out_path: Path) -> tuple[bool, str]:
    """Pull a single clip. Returns (success, detail_msg)."""
    center = datetime.fromtimestamp(ts_epoch, tz=timezone.utc)
    start = center - timedelta(seconds=PRE_S)
    end = center + timedelta(seconds=POST_S)
    url = _nvr_playback_url(camera, start, end)

    # -c:v libx264: NVR playback stream sometimes has odd SPS/PPS that
    # break `-c copy`. Re-encode with ultrafast preset — small quality
    # loss, but the alternative is corrupted MP4 outputs.
    #
    # -vf scale=1280:720: match production's detection input size (from
    # config/detection.yaml `input_width`/`input_height`). NVR playback
    # returns 4096×1860 raw; motion detector thresholds (min_area,
    # max_area) are calibrated for 1280×720 so pixel counts must match.
    #
    # -an: audio not needed for detection replay.
    cmd = [
        "ffmpeg", "-y",
        "-rtsp_transport", "tcp",
        "-i", url,
        "-t", str(PRE_S + POST_S + 1),  # +1s slack for RTSP-side jitter
        "-vf", "scale=1280:720",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "23",
        "-an",
        str(out_path),
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=60,  # single-clip timeout; NVR playback should be fast
        )
        if r.returncode != 0:
            tail = r.stderr.strip().splitlines()[-3:]
            return False, f"ffmpeg exit {r.returncode}: {' | '.join(tail)}"
    except subprocess.TimeoutExpired:
        return False, "ffmpeg timeout (60s)"

    if not out_path.exists() or out_path.stat().st_size < 100_000:
        # Under 100KB usually means "recording gap crossed" — NVR
        # /cam/playback can't span file boundaries.
        return False, f"clip too small ({out_path.stat().st_size if out_path.exists() else 0} bytes)"
    return True, "ok"


def _load_alerts(only_tps: bool, sample_fps: int, camera: str = "rooftop") -> list[dict]:
    """Pull the alert set to fetch clips for."""
    conn = sqlite3.connect(str(DB_PATH))
    out: list[dict] = []

    if only_tps:
        rows = conn.execute("""
            SELECT id, ts, species, camera_id, label_verdict
            FROM alerts
            WHERE camera_id = ? AND label_verdict = 'correct'
              AND snapshot IS NOT NULL
            ORDER BY ts
        """, (camera,)).fetchall()
        for r in rows:
            out.append({"id": r[0], "ts": r[1], "species": r[2],
                        "camera": r[3], "verdict": "TP"})

    if sample_fps > 0:
        rows = conn.execute("""
            SELECT id, ts, species, camera_id, label_verdict
            FROM alerts
            WHERE camera_id = ? AND label_verdict = 'incorrect'
              AND snapshot IS NOT NULL
            ORDER BY RANDOM()
            LIMIT ?
        """, (camera, sample_fps)).fetchall()
        for r in rows:
            out.append({"id": r[0], "ts": r[1], "species": r[2],
                        "camera": r[3], "verdict": "FP"})

    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tps", action="store_true", help="fetch clips for all TPs")
    ap.add_argument("--sample-fps", type=int, default=0,
                    help="also fetch N random FPs (0 = none)")
    ap.add_argument("--camera", default="rooftop",
                    help="camera to pull clips for (default: rooftop)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print planned fetches, don't call ffmpeg")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap total clips (0 = unlimited); useful for smoke test")
    args = ap.parse_args()

    if not args.tps and args.sample_fps == 0:
        ap.error("nothing to do — pass --tps and/or --sample-fps N")

    if not shutil.which("ffmpeg"):
        print("ERROR: ffmpeg not found on PATH.", file=sys.stderr)
        return 2

    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}", file=sys.stderr)
        return 2

    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    alerts = _load_alerts(only_tps=args.tps, sample_fps=args.sample_fps, camera=args.camera)
    if args.limit > 0:
        alerts = alerts[-args.limit:]  # keep most recent (highest chance NVR still has it)
    print(f"Planned: {len(alerts)} clips ({sum(1 for a in alerts if a['verdict']=='TP')} TP, "
          f"{sum(1 for a in alerts if a['verdict']=='FP')} FP)")

    if args.dry_run:
        for a in alerts:
            print(f"  {a['verdict']:2} id={a['id']:>6} camera={a['camera']} "
                  f"ts={a['ts']} species={a['species']}")
        return 0

    manifest: list[dict] = []
    ok_count = fail_count = 0
    for i, a in enumerate(alerts, 1):
        fname = f"{a['camera']}_{a['verdict'].lower()}_{a['id']}.mp4"
        out_path = CLIPS_DIR / fname
        if out_path.exists() and out_path.stat().st_size > 100_000:
            print(f"  [{i:>3}/{len(alerts)}] SKIP (exists) {fname}")
            manifest.append({**a, "clip": fname, "status": "exists"})
            ok_count += 1
            continue

        ok, detail = _fetch_one(a["id"], a["ts"], a["camera"], out_path)
        status = "ok" if ok else "fail"
        print(f"  [{i:>3}/{len(alerts)}] {status.upper():4} {fname} — {detail}")
        manifest.append({**a, "clip": fname if ok else None, "status": status, "detail": detail})
        if ok:
            ok_count += 1
        else:
            fail_count += 1
            # Failed clips leave junk behind; clean up so file-size checks
            # don't misread the retry state on re-run.
            if out_path.exists():
                out_path.unlink()

    MANIFEST.write_text(json.dumps(manifest, indent=2))
    print(f"\nDone: {ok_count} ok, {fail_count} fail")
    print(f"Manifest: {MANIFEST}")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
