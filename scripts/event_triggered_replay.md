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

**Pull only** — run it inside the `archiver` image: that image has ffmpeg,
psycopg3, the cv2-free `playback_url.py`, the `clips/` bind mount, and the
`NVR_*` env. (`web` has no ffmpeg.) `scripts/` isn't baked into the image,
so bind-mount it:

```bash
MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps \
    -v "$(pwd)/scripts:/app/scripts:ro" archiver \
    python scripts/event_triggered_replay.py \
        --target-camera annke:7 --target-label side_path \
        --lookback-hours 6 --limit 10 \
        --out-dir /app/clips/event_triggered/side_path_smoke
```

**With `--run-detection`** — run it inside a `detector-*` image instead.
Detection needs cv2 + YOLO weights (the `models` volume) + Ollama reach;
the detector image also carries ffmpeg and psycopg, so the pull path is
unchanged. The archiver has no docker socket and cannot spawn detector
containers, so the honest answer is "change runtime", not docker-in-docker.
Pick the service whose tuning is closest to the target camera — its env
(`INPUT_WIDTH/HEIGHT`, `MOTION_*`, `VLM_*`, insect thresholds) is what the
replay inherits. `detector-backyard` is a Hikvision-family 4K side angle,
the nearest match for the Annke side-path cam:

```bash
MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps \
    -v "$(pwd)/scripts:/app/scripts:ro" \
    -v "$(pwd)/src:/app/src:ro" \
    detector-backyard \
    python scripts/event_triggered_replay.py \
        --target-camera annke:7 --target-label side_path \
        --lookback-hours 6 --limit 3 --run-detection \
        --out-dir /app/clips/event_triggered/side_path_smoke
```

The `-v src` mount is only needed until the detector image has been
rebuilt with the `REPLAY_*` pipeline hook (see below); it is read-only and
does not touch the running production containers. The script fails fast
if `cv2`/`ultralytics`/`src.pipeline` aren't importable, with this
instruction in the error.

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
- `--until-iso <utc>` anchors "newest" at a fixed time instead of now. A
  re-run 40 minutes later otherwise selects a different newest-N set as
  fresh alerts arrive (observed in the smoke); pass the previous run's
  `run_started_utc` to reproduce it. Idempotent clip pulls + cached
  detection reports then make the re-run a no-op except for whatever
  failed the first time.

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

## Detection pass (`--run-detection`)

The pull answers "what did the target camera record?". Detection answers
the actual validation question: **did the TARGET camera see the event the
SOURCE cameras alerted on?** For each pulled clip the script runs, inline
and serialized:

```
python -m src.main --video <clip>          # cwd = repo root, same config/
```

inside a **test bulkhead** — the same isolation `scripts/replay.sh` uses,
made non-overridable here:

| env | why |
|---|---|
| `STATE_DRY_RUN=1` | no Postgres rows, no HA / generic webhooks (load-bearing — see replay.sh's Aug 7 note) |
| `SNAPSHOT_DIR=<out-dir>/detect/<clip>_snapshots` | JPEGs stay out of prod `snapshots/` (pipeline now honors this env for the notifier too — previously only reject-crops did) |
| `SLEW_ENABLED=false`, `SELF_SLEW_ENABLED=false` | the inherited detector env may drive a real PTZ; replay must not move cameras |
| `VIDEO_LOOP=false`, `REPLAY_EXIT_ON_EOF=1` | single pass, exit at EOF instead of idling forever |
| `VLM_MAX_ALERT_AGE_S=600` | disable the freshness stale-drop — offline we want "did it see it", not "would the alert have been actionable" |
| `REPLAY_REPORT_PATH=<out-dir>/detect/<clip>.report.json` | structured verdicts instead of grepping `DECISION` lines |

Per-target defaults (overridable with `--detector-env KEY=VAL`, repeatable):
`CAMERA_ID=<label>`, `ZONE_KEY=<label>_zone` (no polygon drawn → empty →
**full-frame detection**), `BASELINE_PATH=data/baseline_<label>.jpg`
(absent → no pixel-diff pre-filter tuned for a different camera), `RTSP_URL=`
(never fall back to a live feed). Everything else — motion thresholds,
insect brightness gates, VLM backend/model, temporal frames — is inherited
from the detector service you ran the script in.

### Pipeline hook (`src/pipeline.py`, `src/stream/video_file_handler.py`)

Two opt-in env flags, no-ops when unset:

- `REPLAY_REPORT_PATH` — wraps `notifier.send` (the single choke point every
  fired alert passes through) and records every harvested VLM verdict at the
  `DECISION` point with the clip position captured at VLM submit. On exit
  writes `{gate_funnel, verdicts[], alerts[], exit_reason, frames_processed,
  pending_vlm_jobs}` atomically.
- `REPLAY_EXIT_ON_EOF=1` — `VideoFileHandler` now exposes `finished`; when a
  single-pass file is exhausted the loop drains in-flight VLM jobs (by
  re-feeding the last frame so the harvest block runs — a repeated identical
  frame produces no MOG2 foreground, so no new detections are minted) for up
  to `REPLAY_DRAIN_TIMEOUT_S` (`--detection-drain-seconds`, 90) and returns.
  Without this the tail of every clip lost its verdicts: the pipeline's
  `frame is None → continue` skips the harvest block.

Detection runs are idempotent — an existing `<clip>.report.json` is reused
(`skipped_existing: true`) unless `--force-detection`. One clip is hard-
killed after `--detection-timeout-seconds` (900); a report written by the
pipeline's `finally` before the kill is kept and flagged `timeout_partial`.

Expect it to be slow: YOLO load + CPU decode of 4K HEVC + Ollama per
motion track. A 60–120s clip took 2–6 minutes in the smoke run.

## Manifest

`<out-dir>/manifest.json`, rewritten atomically after every pull **and**
after every detection pass (a crash mid-detection leaves the pull recorded
with `detection.status: running`):

- `summary` (first key): `total_windows`, `windows_detected`,
  `caught_event_count`, `catch_rate` (= caught / windows_detected — a failed
  pull is not a miss; `null` without `--run-detection`)
- `args` echo (minus `DATABASE_URL`), `target` resolution
- `window_selection`: alerts fetched → windows coalesced → skipped (tail) → selected
- `clips[]`: window (ts + ISO), file, `source_alert_ids`, `dedupe_count`,
  per-source-camera + per-species counts, redacted `playback_url`,
  `pull_status` (`ok`/`fail`/`dry-run`), `failure_reason`, ffprobe
  `probe` (duration/codec/WxH), `size_bytes`, every ffmpeg attempt's rc +
  stderr tail
- `aggregate`: `total_windows`, `total_pulled_ok`, `total_failed`,
  `total_duration_seconds`, `total_size_bytes`, `total_source_alerts`
- with `--run-detection`, per clip:
  - `detection`: `status` (`ok` / `timeout` / `timeout_partial` / `no_report`
    / `bad_report`), gate-funnel counters `n_motion_events`, `n_zone_events`,
    `n_baseline_filtered`, `n_vlm_calls`, `n_vlm_rejected`, `n_vlm_insect`,
    `n_vlm_positive` (VLM `wildlife_detected` with species not in
    `insect/none/unknown`), `n_vlm_pending_at_exit`, `n_alerts`;
    `detections[]` = every VLM verdict `{ts_offset_s, species, confidence,
    bbox, wildlife_detected, is_rodent, track_id, vlm_queue_age_s,
    description}`; `alerts[]` = every `notifier.send` call (pre-cooldown, i.e.
    what would have hit the alerts table); paths to `report`, `log`,
    `snapshot_dir`; `elapsed_s`, `rc`
  - `verdict`: `caught_event` (≥1 positive, non-insect verdict in the
    window), `n_source_alerts`, `source_species`, `target_species`,
    `detection_status`

`ts_offset_s` is the clip position when the track was submitted to the VLM
(≤ 8 frames ahead of the processed frame). Add it to `window.start_ts` to
get the wall-clock of the target sighting and compare against the source
alerts' `ts`.

## Debug flags

- `--dry-run` — query + coalesce + print the manifest to stdout; no NVR
  traffic, no files written.
- `--print-cmd` — print each ffmpeg command **with credentials** (for
  pasting into a shell). Combine with `--dry-run` to preview a run.
- `-v` — also surface `playback_url`'s INFO lines.

- `--detector-env KEY=VAL` — tuning overrides for the detection subprocess
  (e.g. `MOTION_VAR_THRESHOLD=10`, `MIN_MOTION_BBOX_PX=22`). Isolation keys
  (`STATE_DRY_RUN`, slew, loop, EOF, report path) cannot be overridden.
- `--force-detection` — ignore cached `<clip>.report.json`.

## Reading the result

`catch_rate` is the wiring signal. Two caveats before trusting a low number:

1. The default `--species-filter rat,mouse,other` and all-five-camera source
   set include `other` alerts from indoor cameras (`crawlspace*`) — priors an
   outdoor side-path camera cannot possibly confirm. Narrow with
   `--source-cameras yard,backyard --species-filter rat,mouse` when the
   question is "does the new angle see the same yard rodents".
2. Detection runs full-frame with no baseline and the *host* detector's
   thresholds. A miss can be "camera angle doesn't cover it" or "thresholds
   tuned for another camera"; `n_motion_events` vs `n_vlm_calls` vs
   `n_vlm_positive` tells you which stage dropped it.

## Non-goals

No label diffing beyond the per-window verdict, no UI, no parallelism (one
clip at a time — the NVR tolerates 2–3 RTSP sessions and Ollama is shared
with five live detectors).
