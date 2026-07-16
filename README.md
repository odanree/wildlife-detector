# Wildlife Detector

Real-time yard/property wildlife detection using YOLO object tracking, MOG2 motion supplementation, and a vision-language model (Claude or Ollama) for species classification. Primary target is rodents (rats, mice) — other visible wildlife (raccoon, opossum, cat, dog, squirrel, bird) is reported as baseline data but does not trigger alerts.

On a positive rodent event, a secondary Amcrest/Dahua PTZ camera slews to the zone containing the detection via a **zone → preset lookup** — the operator saves N presets in the NVR UI and maps them to N primary-FOV polygons in `config/detection.yaml`.

## How it works

```
RTSP / video file
      │
      ▼
  YOLO (COCO cat/dog/bird)  ←  advisory only — COCO has no rat/mouse
      +
  MOG2 motion supplement    ←  load-bearing signal for small movers
      │
      ▼
  Zone filter (yard polygon)
      │
      ▼
  Rate-limited VLM classify  ←  "is this a rodent? what species?"
      │
      ├──  if rodent → Notifier (snapshot + HA / generic webhook)
      │               + slew secondary PTZ to zone preset
      │
      └──  else → nothing (no cooldown state, no dedup — headless MVP)
```

The **motion-gated VLM classification** pattern reuses the primary VLM for rate-limited per-track species calls (default `VLM_INTERVAL_S=2.0`). The **slew-to-zone dispatcher** carries per-event lockout (10 s default) so the secondary camera doesn't thrash while a rodent lingers in view, and rolls back the lockout on PTZ failure so a transient error doesn't drop the next event.

## Quick start

```bash
python -m venv .venv && source .venv/Scripts/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # fill in ANTHROPIC_API_KEY and RTSP_URL
python -m src.main
```

Replay a saved clip instead of a live camera:

```bash
python -m src.main --video path/to/clip.mp4
```

## Docker

```bash
docker compose up -d
```

## Config

- `config/detection.yaml` — YOLO / motion / zones / slew_presets
- `config/alerts.yaml` — cooldowns + Home Assistant / generic webhook wiring
- `config/botsort.yaml` / `bytetrack.yaml` — YOLO tracker configs

## Environment

See [`.env.example`](.env.example) for the full list. The load-bearing ones:

| Var | Purpose |
|---|---|
| `RTSP_URL` | Primary camera stream |
| `VLM_BACKEND` | `claude` \| `ollama` \| `mock` |
| `ANTHROPIC_API_KEY` | Required for `VLM_BACKEND=claude` |
| `SLEW_ENABLED` | Master kill-switch for the secondary PTZ camera |
| `SECONDARY_CAMERA_ID` | 0-based PTZ_HOST_{n} / PTZ_CHANNEL_{n} slot |
| `SLEW_LOCKOUT_SECONDS` | Per-event PTZ debounce (default 10s) |

## Stack

- **Detection:** YOLOv8 (Ultralytics) + ByteTrack; MOG2 motion supplement
- **VLM:** Claude via Anthropic API, or any Ollama multimodal model
- **PTZ:** Amcrest/Dahua HTTP CGI
- **Alerts:** Home Assistant webhook + generic HTTP POST + local JPEG snapshots
- **Runtime:** headless Python + docker compose (no dashboard in the MVP)

## Origin

Extracted from [`parking-enforcement-detector`](https://github.com/odanree/parking-enforcement-detector) — the parent repo carries the same RTSP + YOLO + VLM + PTZ scaffolding but is scoped to street-parking enforcement (chalking, sweeper, PE-vehicle detection). See [`docs/adr/001-fork-from-parking-detector.md`](docs/adr/001-fork-from-parking-detector.md) for the extract-a-bounded-context rationale — the two sites diverge in every meaningful pipeline stage, so a shared strategy pattern was earning less than it cost.
