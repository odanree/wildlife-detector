# ADR 002 — Split monolith into ingest / detector / web planes (shared-nothing processes)

**Status:** Accepted
**Date:** 2026-07-17
**Supersedes:** the "single-process, multi-threaded" runtime shape implied by [ADR 001](./001-fork-from-parking-detector.md#what-we-kept-from-the-parking-scaffolding).

## Context

The wildlife-detector currently runs as **one Python process** with three concurrent threads:

- `RTSPHandler` — daemon thread reading frames from `cv2.VideoCapture`, pushing to a `queue.Queue(maxsize=1)` that evicts stale frames on write.
- Main loop — YOLO + motion + zone filter + VLM job submission + VLM harvest + notifier + preview frame publish, all sequential in one iteration.
- Flask preview server — daemon thread served by werkzeug's built-in thread pool.

Plus a `ThreadPoolExecutor(max_workers=2)` for VLM calls that the main loop submits work to and harvests non-blockingly via `fut.done()`.

**This worked for the MVP.** Empirically during development:
- 15 fps sustained at 1088×612 while Ollama cold-loaded a 6 GB model in the background (60-180 s).
- Flask kept responding to `/status` polls during VLM inference.
- The `queue.Queue(maxsize=1)` semantic meant VLM latency couldn't drop frames — it just replaced stale ones.

So the naive "the VLM blocks the loop" claim isn't what happens today. But three real limits emerge as the deployment matures:

1. **GIL contention under sustained load** — CPython threads share one interpreter lock. NumPy/OpenCV/PyTorch release the GIL for C-level ops (which is why YOLO doesn't starve Flask), but pure-Python work (JSON parse, `_normalize`, werkzeug routing, our alert-log manipulation) contends. Under sustained load this manifests as latency jitter — the 95th-percentile per-frame time creeps up, not a hard lock.

2. **Blast radius = 1 process** — an unhandled exception in werkzeug's request path, a segfault in the anthropic SDK, a bad JPEG from the RTSP stream that crashes `cv2.imdecode` — any of these takes down the whole detector. The **bulkhead pattern** would isolate each concern into its own process so a fault in one plane doesn't cascade.

3. **Restart granularity is monolithic** — changing a VLM prompt requires restarting the whole process, which:
   - Drops the RTSP TCP connection (2-4 s reconnect + first-frame delay)
   - Blows away the `AlertLog` ring buffer (in-memory state)
   - Resets uptime counters, baseline holder, all the derived state
   - Kills the Flask preview thread mid-request

   The user has hit "stale zombie process" bugs in this session because kill/restart of a monolithic process is inherently coarse.

4. **Multi-camera scaling is blocked by the shape.** Adding a second camera to the current design either means running two full detector processes (2× VLM cost, 2× state, no shared alerts) or refactoring the main loop to iterate over N cameras (unbounded per-frame time, one camera's slow VLM starves the others). **Neither is production-shaped.**

## Decision

Split the runtime into **three shared-nothing processes**, communicating via three distinct IPC channels:

```
┌─────────────────┐          ┌──────────────────────┐          ┌─────────────────┐
│  INGEST (N)     │──frames──▶  DETECTOR (1)        │──state──▶│  WEB / CONTROL  │
│                 │  shmem   │                      │  sqlite  │                 │
│  RTSP reader    │          │  YOLO + motion       │          │  Flask preview  │
│  per camera     │          │  zone filter         │          │  read-mostly    │
│  writes latest  │          │  VLM pool            │          │                 │
│  frame per cam  │          │  Notifier            │          │                 │
└─────────────────┘          └──────────────────────┘          └───────┬─────────┘
       ▲                                ▲                              │
       │                                │  ─────────commands───────────┘
       │                                │  HTTP :127.0.0.1:8101
       └────── restart independently ───┘  bearer-token
```

### IPC boundaries

| Boundary | Direction | Transport | Rationale |
|---|---|---|---|
| **Ingest → Detector** | Frames (BGR ndarray, ~500 KB @ 960×540 × N cameras × ~15 fps) | `multiprocessing.shared_memory` ring buffer, one slot per camera, single-writer / single-reader | Zero-copy on same machine. Python 3.8+ stdlib, works on Windows without third-party deps. Overwrites stale frame (backpressure via "drop old, keep newest") — the current `queue.Queue(maxsize=1)` semantic preserved across processes. |
| **Detector → Web** | State: alerts, snapshot metadata, stats, baseline info | **SQLite with WAL mode**, one file `data/state.db` | Persistent (also solves the in-session pain of "AlertLog dies on restart"). Multi-reader / single-writer, WAL mode makes read concurrency cheap. No daemon to run, no serialization protocol to maintain. Web NEVER writes to alerts/stats — writes go through the command channel below. |
| **Web → Detector** | Commands: capture baseline, save zone polygon, save OSD masks, clear baseline | **HTTP on `127.0.0.1:8101`** with a shared-secret bearer token from `INTERNAL_API_TOKEN` env | Request-response semantics — validation errors return 400, saved-successfully returns 200 with new version. Reuses HTTP idioms the web already speaks. Bearer token gates access; loopback-only bind prevents external reach. Low command rate (~10/min max) means the per-request overhead is irrelevant. |

### Patterns

- **Bulkhead pattern** — three failure domains, not one. A werkzeug bug can't crash the detector. An Ollama wedge can't freeze the UI. An RTSP disconnect can't lose alert history.
- **Sidecar (control-plane extraction)** — web is a sidecar to the detector. Detector doesn't `import flask`. The dashboard is a separately-deployable, separately-restartable read-mostly view over state the detector owns.
- **CQRS-lite** — detector is the single writer to `state.db`; web is a read-only consumer of that store. Commands flow through a distinct channel (HTTP), not by web mutating the state store directly. This is what makes the SQLite concurrency model actually work under real load.
- **Shared-nothing per plane** — no shared Python objects across processes. Every cross-plane operation is a serialization boundary (frames via shmem, state via SQLite rows, commands via JSON over HTTP). This kills GIL contention and makes each plane independently profilable.
- **Producer-consumer with slot semantics** — ingest publishes "latest frame per camera" into a fixed-size shmem slot; detector reads the current slot. There is no queue that grows under load — old frames are overwritten. Same shape ROS 2 / MediaPipe / gstreamer's `queue leaky=downstream` use.
- **Fault-tolerant restart** — each plane can restart without the others noticing beyond a short reconnect. Ingest restart → detector sees a stale frame for ~2 s then resumes. Web restart → user's browser reconnects the MJPEG stream. Detector restart → ingest keeps ring-buffering (no backpressure); web shows "detector offline" for the outage window.

## Alternatives considered

### A. Do nothing (rely on current threading)

**Rejected** because it doesn't answer the multi-camera scaling question. The current design was fine for the MVP; it's structurally blocked from N-camera without a rewrite anyway.

### B. Keep one process, use `multiprocessing.Pool` for the VLM

Only extracts the VLM plane into worker processes; keeps Flask + ingest + orchestration in one process. **Rejected** because it doesn't isolate the werkzeug blast radius, doesn't buy anything for multi-camera ingest, and adds serialization cost (pickling frames to worker processes) for a problem the ThreadPoolExecutor already solves adequately (VLM cost is dominated by Ollama HTTP round-trip, not Python thread overhead).

### C. ZeroMQ / RabbitMQ / Redis pubsub for IPC

Battle-tested message brokers with excellent multi-language support. **Rejected for now** because:
- ZeroMQ / RabbitMQ adds a broker to run and monitor — extra failure surface for a home deployment.
- Redis is nice but is genuinely overkill for our data volumes (single-digit MB/s frame throughput, single-digit alerts/hour).
- `multiprocessing.shared_memory` is stdlib and gives us the same zero-copy frame handoff without deploying an extra service.
- If we ever go multi-machine (detector on a separate GPU box from ingest), revisit ZeroMQ. Documented in "When to reconsider" below.

### D. gRPC between planes

Language-neutral, streaming, well-typed. **Rejected** because we're all-Python — the language-neutrality benefit is zero, and gRPC adds a protobuf compilation step. HTTP + JSON is enough for our command rate.

### E. Full container per plane (`docker-compose up`)

Genuinely appealing for the deployment story. **Deferred, not rejected** — Phase 3 (see below) ends with a codebase that's *ready* to be dockerized per-plane, since each plane already has its own `python -m src.<service>` entrypoint. The docker-compose file is a follow-up ADR when we're ready.

### F. Structured log-tail for state instead of SQLite

Detector appends JSON lines to `state.jsonl`; web reads with `tail -f` semantics. **Rejected** because query patterns like "show me alerts filtered by species from the last hour" become O(N) file scans. SQLite gives us an index and a WHERE clause for free.

## Phase plan

Each phase ships independently, each solves a real problem, each is <1 day of work. Failure of a later phase doesn't undo an earlier one.

### Phase 1 — SQLite state store (single-process, no split yet)

Extract `AlertLog`, `Stats`, `Baseline metadata` from in-memory holders into SQLite. Same process, no new IPC — just move the source of truth from Python objects to a database file. Benefits **before** any process split:

- Alert history survives restarts (removes the in-session "AlertLog dies on restart" pain)
- `/alerts` becomes a `SELECT` with a `species` filter and `LIMIT`, not a linear scan of a `deque`
- Snapshot backfill from `snapshots/YYYY-MM-DD/` becomes an idempotent `INSERT OR IGNORE`
- Web reading from SQLite is trivially thread-safe; single-writer discipline preserved

**Deliverable:** `data/state.db` with tables `alerts`, `snapshots`, `stats_snapshots`, `baselines_meta`. The `preview.AlertLog` class becomes a thin façade over SQL.

### Phase 2 — Extract web to a sidecar process

Move `src/web/preview.py` to a standalone service. Detector no longer imports Flask. Detector exposes a minimal internal HTTP on `127.0.0.1:8101` that only accepts `POST /api/{zone,masks,baseline}` with a bearer token. Web reads all state from `state.db`; writes commands via HTTP.

Benefits:
- **Bulkhead** — werkzeug bug can no longer crash detector; anthropic SDK bug can no longer freeze UI.
- **Restart granularity** — prompt tuning cycle stops touching the RTSP connection or the ingest pipeline.
- **Sidecar pattern** shipped — dashboard becomes a separately-deployable view.

**Deliverable:** two `python -m src.<service>` invocations, a small `scripts/start.ps1` that launches both.

### Phase 3 — Extract ingest to a service

Move `RTSPHandler` into `src/ingest_service.py`. One process per camera. Frames flow through a `multiprocessing.shared_memory` ring buffer, one slot per camera. Detector reads latest frame per camera from the ring.

Benefits:
- **Multi-camera unlocked** — add a second camera by starting a second `ingest_service` instance with a different camera ID. Detector's main loop iterates over camera slots.
- **RTSP restart independence** — a camera reconnect no longer disturbs YOLO/VLM state.

**Deliverable:** N-camera support with per-camera ingest processes and a detector that treats cameras uniformly.

## Consequences

### Positive

- **Bulkhead** — three failure domains, not one.
- **Persistent alert history** — Phase 1 solves the in-memory-ring-buffer problem.
- **Multi-camera path** — Phase 3 makes N-camera a matter of `docker compose scale ingest=N`, not a refactor.
- **Interview defense** — bulkhead + sidecar + CQRS-lite + shared-nothing are all Staff-vocabulary patterns the code will directly embody.
- **Testability** — each plane is now a black box with well-defined inputs/outputs. Detector tests can inject frames via shmem and assert on `alerts` table rows. Web tests can prime `state.db` and assert on HTTP responses.

### Negative

- **More processes to launch / monitor.** `python -m src.main` becomes `python -m src.detector`, `python -m src.web_service`, `python -m src.ingest_service --camera 0` — three invocations. `docker-compose up` normalizes this but adds a container-per-plane baseline.
- **`multiprocessing.shared_memory` on Windows has quirks** — the shmem block name must be globally unique across processes, and if a process crashes without calling `.close()` the resource leaks until reboot. Mitigation: name blocks with the camera ID + a startup UUID, and the ingest service's cleanup handler unlinks on exit.
- **SQLite WAL under contention** — a slow writer can starve readers if we ever have long-running transactions. Our writes are per-alert (small, fast), reads are per-request (indexed, fast), so this shouldn't bite. Documented as a "known gap — if it becomes a problem, switch to Postgres."
- **Command channel adds an HTTP hop** — web POSTs to detector's internal API, adding ~1-2 ms of localhost HTTP round-trip. Irrelevant for our command rate (single-digit commands/minute).
- **Debugging one bug can now touch three processes** — trace-id / correlation-id discipline becomes important. Phase 2 will add a `request_id` field to state.db alert rows so cross-plane traces work.

## When to reconsider

- **If we go multi-machine** (detector on a GPU rig separate from the ingest box) — revisit ZeroMQ or gRPC for the frame channel. `multiprocessing.shared_memory` is same-machine only.
- **If SQLite WAL contention becomes measurable** — switch state store to Postgres. Same pattern (single-writer detector, read-only web), different backend.
- **If we ever have more than 2-3 planes** — consider a message broker (Redis pubsub minimum) for orchestrating them instead of hand-rolled IPC.

## References

- Command channel choice: HTTP on 127.0.0.1 with bearer token (see [portfolio-audit report for wildlife-detector, 2026-07-17](../../../.claude/skills/portfolio-audit/history/wildlife-detector/) — the three high-severity findings all trace to the current Flask 0.0.0.0-bind design; this refactor addresses them by construction).
- State-store choice: SQLite WAL is the same pattern used by the [portfolio-drift-mcp state persistence layer](../../portfolio-drift-mcp/) and the [beacon materialized-view cache](../../job-search-pipeline/beacon/).
- Shared-memory ring buffer: same shape as [ROS 2 zero-copy shared-memory transport](https://design.ros2.org/articles/zero_copy.html) and [gstreamer's `queue leaky=downstream`](https://gstreamer.freedesktop.org/documentation/coreelements/queue.html), on a smaller scale.
