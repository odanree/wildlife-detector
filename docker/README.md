# Docker deployment — Phase A + B

Containerizes the three-plane architecture from
[ADR-002](../docs/adr/002-three-plane-process-split.md):

```
┌─────────────────────────┐         ┌─────────────────────────┐
│  detector container     │────────►│  web container          │
│  ─────────────────      │  bearer │  ─────────────────      │
│  • RTSP + YOLO + MOG2   │  bearer │  • Flask UI (port 8100) │
│  • Cascade VLM          │  ◄───── │  • Reads state.db + FS  │
│  • Internal HTTP :8101  │   HTTP  │  • Proxies MJPEG        │
└──────┬──────────────────┘         └─────────────────────────┘
       │                                        ▲
       │  RTSP (LAN)                            │
       ▼                                        │  browser
   camera                                       │
```

**Volumes** shared between the two containers (SQLite WAL is safe under
single-writer / multi-reader):

- `state` → `state.db` + WAL
- `snapshots` → alert JPEGs
- `logs` → rotating log files
- `models` → cached YOLO weights (survives rebuilds)

**Cross-container trust boundary** is unchanged from the process-split path:
web writes to detector go over HTTP with a bearer token from
`INTERNAL_API_TOKEN` in `.env`. `detector:8101` is exposed only on the
compose network, never published to the LAN.

## Bring up

From the repo root:

```bash
docker compose up -d --build
docker compose logs -f            # follow both services
docker compose logs -f detector   # just one
```

UI at `http://localhost:8100`. First run pulls the YOLO weights from
Ultralytics — takes ~30 s once, then cached in the `models` volume.

## Bring down

```bash
docker compose down          # keeps volumes (state, snapshots, models)
docker compose down -v       # nukes everything
```

## Windows-specific notes

**Docker Desktop for Windows** does not support `network_mode: host`. This
compose file uses bridge networking with explicit port publishing (`8100`)
and `host.docker.internal` for the Ollama host address. Two things to
verify on first run:

1. **RTSP camera reachable from container** — Docker Desktop's bridge
   network usually routes to the LAN via the host's default gateway. Test
   from inside the running detector container:

   ```bash
   docker exec -it wildlife-detector ping 192.168.1.86
   ```

   If ping fails, either the corporate/router firewall is blocking, or
   Docker Desktop needs "WSL 2 integration" enabled with your LAN
   interface. Fallback: set `RTSP_URL` to point at the NVR (192.168.1.148)
   instead of the direct camera — NVRs typically re-expose channel streams.

2. **Ollama on the Windows host** — the compose `extra_hosts` block maps
   `host.docker.internal` to the host gateway. If Ollama binds only to
   `127.0.0.1` on the host (default), the container can't reach it. Fix:

   ```powershell
   $env:OLLAMA_HOST = "0.0.0.0:11434"
   ollama serve
   ```

   Then restart the detector container. Verify from inside:

   ```bash
   docker exec -it wildlife-detector curl -f http://host.docker.internal:11434/api/tags
   ```

3. **Line endings** — if you cloned with `core.autocrlf=true`, Python
   scripts may have CRLF endings that break in Linux containers. Guard
   with a `.gitattributes`:

   ```
   *.py text eol=lf
   *.sh text eol=lf
   ```

## GPU passthrough (optional)

The default detector image runs YOLO on CPU. If you want CUDA acceleration
on Windows via WSL 2:

1. Install NVIDIA Container Toolkit inside WSL 2
2. Add to the `detector` service in `docker-compose.yml`:

   ```yaml
   deploy:
     resources:
       reservations:
         devices:
           - driver: nvidia
             count: all
             capabilities: [gpu]
   ```

3. Swap the base image in `docker/detector/Dockerfile`:

   ```dockerfile
   FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04
   ```

   Then install Python 3.11 + the same requirements. YOLO auto-detects
   CUDA if `torch` finds it.

Not required — CPU inference at 15 fps is fine for a single yard camera.

## Deploying to the Hetzner VPS

The same `docker-compose.yml` works on the Hetzner box:

```bash
scp -r wildlife-detector/ hetzner:/opt/
ssh hetzner "cd /opt/wildlife-detector && docker compose up -d --build"
```

Two adjustments for that environment:

- `network_mode: host` **does** work on Linux; you can drop the
  `extra_hosts` block if you want the detector to hit Ollama on
  `127.0.0.1:11434` directly.
- Publish `8100` behind Caddy at a subdomain
  (`wildlife.<yourdomain>`) rather than exposing it directly.

## Comparing to the process-split path

| Concern | `scripts/start-split.ps1` | `docker compose` |
|---|---|---|
| Setup | Windows PS + Python + Ollama on host | Docker Desktop + Ollama on host |
| Restart granularity | `Ctrl+C` in one terminal | `docker compose restart web` |
| Portability | Single Windows dev box | Any host with Docker |
| Resource limits | None | `deploy.resources.limits` per service |
| State isolation | Files in repo dirs | Named volumes (survives image rebuild) |
| Interview legibility | "Process split from ADR-002" | "Multi-service compose from ADR-002, same trust boundary" |

Both paths are supported — use whichever fits the environment.
