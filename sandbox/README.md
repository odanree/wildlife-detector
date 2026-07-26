# Sandbox — replay + config-sweep harness

Reproducible detection-pipeline eval against recorded clips. Purpose: try
config knobs (persistence gate, area thresholds, velocity gates, zone
polygon) offline against a fixed corpus of TP + FP clips and measure the
tradeoff without touching production detectors.

## Why it exists

Static-frame analysis of the ~4,700 rooftop FPs showed they are visually
indistinguishable from the 16 rooftop TPs — same tiny bboxes, same corner
positions, same VLM confidence (0.85). The separating signal has to be
**temporal** (motion behavior across frames), which means we need to
replay actual video sequences through the pipeline with different configs
and compare. Per-alert single-snapshot inspection cannot answer the
question.

See the labeling-vs-signal analysis in `docs/adr/*` (TODO once written) or
the earlier session transcript for background.

## Design

**Motion-first, VLM-mocked.** The primary gate we're tuning is the motion
detector's persistence / area / velocity thresholds. If motion doesn't
emit a detection, VLM never sees the crop and no alert fires. So we
replay clips through `MotionDetector` (plus zone filter + optional
`ObjectDetector` for the YOLO-person-exclusion path) and log whether a
detection would have made it to the VLM stage. Real VLM calls are
skipped — saves cost and lets sweeps run in seconds.

**Non-invasive.** Reads from `src/` but never writes; production
detectors are unaffected. Runs inside the `web` container to reuse the
detection deps (opencv, ultralytics, torch).

**Config-parameterized.** Every knob under test is a Python dict or env
override on the harness side — the motion detector's module-level
constants (`_MIN_TRACK_AGE`, `_MAX_VELOCITY_PX`) get monkey-patched
between runs. Zone polygons swap in as function args.

## Directory layout

```
sandbox/
├── README.md              this file
├── fetch_clips.py         pull ±5s MP4 clips from NVR around alert timestamps
├── replay.py              run one clip through MotionDetector with a config
├── sweep.py               grid-search configs, tabulate TP/FP tradeoff
├── ground_truth.json      map clip filename → alert_id, verdict, camera
├── clips/                 recorded MP4 clips (gitignored — 100MB+)
└── results/               sweep outputs (gitignored)
```

## Workflow

1. **Collect clips** (once):
   ```
   docker compose exec web python /app/sandbox/fetch_clips.py --tps --sample-fps 30
   ```
   Fetches all 16 rooftop TPs + 30 sampled FPs from the NVR playback
   stream (channel 6). Requires the NVR (192.168.1.148) reachable from
   the container.

2. **Verify baseline reproduces current alerts**:
   ```
   docker compose exec web python /app/sandbox/sweep.py --config baseline
   ```
   Should recover roughly the same alerts the production detector would
   have fired at those timestamps. If not, the sandbox is not
   representative and we can't trust its config-sweep numbers.

3. **Grid-search configs**:
   ```
   docker compose exec web python /app/sandbox/sweep.py --grid
   ```
   Iterates over MIN_TRACK_AGE ∈ {2, 3, 4, 5, 6}, area thresholds,
   velocity limits. Outputs a table of TP-retention × FP-rejection per
   config to `results/sweep_<timestamp>.csv`.

4. **Pick a config**, apply to production, monitor.

## Non-goals

- **Not testing VLM prompt changes.** VLM is mocked — assumed to always
  say "yes rodent 0.85" as it does in production today. Prompt-level
  eval is a separate harness (needs recorded VLM inputs + human
  verdicts).
- **Not testing per-frame pixel filtering.** Motion detector is the
  narrowest useful boundary; frame-level filters (baseline diff, OSD
  masks) are already applied inside MotionDetector and we test the
  aggregate.
- **Not a substitute for live A/B testing.** Sandbox validates the
  hypothesis on a fixed corpus; live traffic can still surface
  regressions the corpus didn't cover.

## Patterns

- **Shadow-model evaluation** — same pipeline binary, replayable input,
  no side effects on production. Same shape as reranker / prompt eval
  in ML workflows.
- **Monkey-patch-module-constants for config sweep** — motion detector's
  env-read constants (`_MIN_TRACK_AGE`, `_MAX_VELOCITY_PX`) are frozen
  at import; the sweep mutates them directly between runs. Not
  production-safe but perfectly fine in an isolated harness process.
- **Ground-truth as separate JSON** — alert verdicts (TP/FP) live
  alongside clips, not inside the pipeline. Lets us re-label clips
  without re-collecting them.
