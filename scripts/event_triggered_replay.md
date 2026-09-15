# event_triggered_replay.py

Use existing alerts as **temporal priors** to pull clips from a different
(new / repositioned) camera, so an operator can eyeball whether the new
angle catches the same events without grinding 24h of footage blind.

Pattern names for the interview reps: **cross-camera replay driven by
temporal priors**, with a **debounce coalescer** over alert timestamps so a
burst of alerts becomes one clip. The playback URL routing is reused from
`src/stream/playback_url.py` — the same **two-NVR routing** (Dahua vs
Hikvision family, NVR-local wallclock) the archiver uses. Nothing is
reimplemented.

## Runtime

Run it inside the `archiver` image: that image has ffmpeg, psycopg3, the
cv2-free `playback_url.py`, the `clips/` bind mount, and the `NVR_*` env.
(`web` has no ffmpeg; detectors carry a full YOLO/cv2 stack you don't
need.) `scripts/` isn't baked into the image, so bind-mount it:

```bash
MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps \
    -v "$(pwd)/scripts:/app/scripts:ro" archiver \
    python scripts/event_triggered_replay.py \
        --target-camera annke:7 --target-label side_path \
        --lookback-hours 6 --limit 10 \
        --out-dir /app/clips/event_triggered/side_path_smoke
```

Host-side alternative (needs `DATABASE_URL` pointing at
`127.0.0.1:$POSTGRES_HOST_PORT`, plus `ANNKE_*`/`AMCREST_*`/`NVR_*` in the
shell env and ffmpeg on PATH):

```bash
python scripts/event_triggered_replay.py --target-camera yard --dry-run
```

## Targets

| `--target-camera` | routes via | notes |
|---|---|---|
| `yard` `rooftop` `backyard` `crawlspace` `crawlspace_inside` | `NVR_CHANNEL_<CAM>` + `NVR_HOST/FAMILY_<CAM>` env from compose | same as the archiver |
| `annke:N` | Annke .130, Hikvision family, channel N | seeds `NVR_*_ANNKE_N` env from `ANNKE_*` |
| `amcrest:N` | Amcrest .148, Dahua family, channel N | fleet `AMCREST_*` defaults |

`--target-label` sets the dir/filename prefix (default derived from the
spec, e.g. `annke_7`).

## Windowing

- Each alert contributes `[ts - pre_roll, ts + post_roll]` (30s / 30s).
- Consecutive alerts within `--dedupe-window-seconds` (60) merge into one
  window spanning `[first - pre, last + post]`.
- `--max-window-seconds` (120) closes a cluster early. This is not
  arbitrary: `build_nvr_playback_url` hard-codes `endtime = start + 2min`,
  so a longer window would be silently truncated by the NVR.
- `--min-age-seconds` (90) skips windows that end inside the NVR's live
  recording tail — those return 400 on Annke because the segment isn't
  seekable yet.
- `--limit N` keeps the **newest** N windows (most likely still on disk
  at the NVR), then writes them in chronological order.

## Pull

Serial ffmpeg, `--sleep-seconds` (3) between pulls — the NVR drops
packets past 2–3 concurrent RTSP sessions.

```
ffmpeg -rtsp_transport tcp -timeout 15000000 -y -i <url> -t <dur> \
       -c:v copy -c:a aac -b:a 64k -movflags +faststart out.mp4
```

If the first attempt fails with an audio-flavoured error, it retries once
with `-an` and records `audio_dropped: true`. NVR 4xx errors are not
retried (dropping audio won't fix them).

Idempotent: the filename is derived from the window
(`<label>_<startZ>_<dur>s_n<alerts>.mp4`), so a re-run skips clips that
already exist and just refreshes the manifest.

## Manifest

`<out-dir>/manifest.json`, rewritten atomically after every pull:

- `args` echo (minus `DATABASE_URL`), `target` resolution
- `window_selection`: alerts fetched → windows coalesced → skipped (tail) → selected
- `clips[]`: window (ts + ISO), file, `source_alert_ids`, `dedupe_count`,
  per-source-camera + per-species counts, redacted `playback_url`,
  `pull_status` (`ok`/`fail`/`dry-run`), `failure_reason`, ffprobe
  `probe` (duration/codec/WxH), `size_bytes`, every ffmpeg attempt's rc +
  stderr tail
- `aggregate`: `total_windows`, `total_pulled_ok`, `total_failed`,
  `total_duration_seconds`, `total_size_bytes`, `total_source_alerts`

## Debug flags

- `--dry-run` — query + coalesce + print the manifest to stdout; no NVR
  traffic, no files written.
- `--print-cmd` — print each ffmpeg command **with credentials** (for
  pasting into a shell). Combine with `--dry-run` to preview a run.
- `-v` — also surface `playback_url`'s INFO lines.

## Non-goals (this MVP)

No detection pass on the pulled clips, no label diffing, no UI. Next
iteration: `--run-detection` to push each clip through
`scripts/replay.sh` with `STATE_DRY_RUN=1` and diff the target camera's
detections against `source_alert_ids`.
