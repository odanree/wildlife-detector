# Prototype-to-Production Blueprint

**Purpose**: Distilled playbook from the wildlife-detector journey — the concrete steps that took this repo from "headless Python script that mostly worked" to "containerized multi-service system with typed frontend, cost telemetry, CI gates, and PR discipline." Reusable as the default arc for every new project in this portfolio.

Each phase names the **pattern** being applied (interview vocabulary) alongside the **mechanic** (what you actually change). Order matters — earlier phases unblock later ones; skipping ahead creates rework.

---

## Phase 0 — Starting state (what "prototype" looks like)

A single Python entrypoint. All state in-memory. All config hardcoded or read from `.env`. One camera, one model, no observability past `print()`. Works on the author's laptop, breaks on restart, no visibility into cost or failure modes. This is fine for validating the idea — do NOT skip prototype and jump straight to phase 1. But it's the ceiling until you decompose.

---

## Phase 1 — Containerize (bulkhead pattern)

**Pattern**: **Bulkhead** per bounded context. Every failure domain gets its own container so a crash in detection doesn't take down the UI, and a memory leak in the web layer doesn't OOM the OpenCV process.

**Mechanic**:
- Split the monolith into services with clear responsibilities: detector (heavy CPU/GPU, ephemeral), web sidecar (light, restart-safe), any secondary workers (metrics, cascade primary, etc.)
- Write an ADR naming the split before writing code. `docs/ADR-002-three-plane-architecture.md` was the anchor for wildlife-detector.
- Multi-stage Dockerfiles per service. Runtime image contains ONLY what that service needs (`docker/web/Dockerfile` skips OpenCV entirely — 60 MB vs 2 GB).
- `docker-compose.yml` for local; healthchecks per service; `restart: unless-stopped` for prod services.
- Bind mounts for data that survives restart (state.db, snapshots, config, logs). Named volumes for cache-only data (models).
- Explicit trust boundaries: internal APIs on loopback + bearer token; only user-facing surfaces on `0.0.0.0`.

**Signal you're done**: `docker compose up -d` runs the whole thing from a clean clone; killing one container doesn't cascade.

---

## Phase 2 — Multi-tenancy (env-driven per-instance)

**Pattern**: **Strangler-fig for scale** — add a second tenant next to the first without breaking anything.

**Mechanic**:
- Every hardcoded config becomes an env var with a sensible default from YAML/config file.
- Compose services get per-instance env overrides layered on top of `.env` shared defaults. Explicit `CAMERA_ID: yard` / `CAMERA_ID: rooftop` — never inferred.
- State schema gets the tenant column (`camera_id`) with a backward-compat ALTER migration.
- API endpoints take `?camera=<id>` — routing at the API layer, not at compose.
- YAML config supports per-tenant sections (`osd_masks.yard`, `osd_masks.rooftop`) with a legacy-flat-list migration path.

**Signal you're done**: adding a third tenant is a 5-line compose block + a UI dropdown entry. Zero pipeline code changes.

---

## Phase 3 — Observability from day one

**Pattern**: **SLI/SLO thinking** — count everything at every stage, expose it live, don't wait for prod to notice.

**Mechanic**:
- Health check endpoints per service (`/health`, `/status`) — not just for docker's `HEALTHCHECK`, for humans.
- **Gate funnel counters** at every filter stage (`motion → zone → baseline → vlm → hit`). The ratio between stages tells you which layer is doing the filtering work.
- Resource self-monitoring: `psutil` sampled on `/status` (cpu_pct, rss_mb, threads, cpu_peak). Peak tracking so a settled reading still lets an operator see prior spikes.
- Structured logging of *decisions*, not just events: `DECISION track=N species=X rodent=Y conf=Z bbox=(...)`.
- **Cost telemetry** for anything with per-call pricing (LLMs, cloud APIs). Per-model rate card as a constant; session-lifetime `cost_usd` accumulator; `cache_hit_rate` computed live.
- UI header chips for the metrics that matter: `cpu 346% / peak 631% · vlm $0.0672 · cache 94%`.

**Signal you're done**: any operator can answer "why isn't this alerting?" without SSH. Cost surprises are impossible; you see them accumulating in real time.

---

## Phase 4 — LLM cost engineering (if the project uses an LLM)

**Patterns**: **Cache-breakpoint placement**, **rate-limit identity chain**, **cascade with confirm-stage escalation**.

**Mechanic**:
- **Prompt caching**: verify the cache actually hits with `usage.cache_read_input_tokens > 0`. Anthropic silently ignores `cache_control` on system prompts below 1024 tokens — a footgun that cost this project real money before we logged it. Move static bulk (rules, few-shot, decision trees) INTO the cached system block; keep per-call-dynamic in user content.
- **Model-tier eval**: run the same workload through Haiku / Sonnet / Opus on the SAME test clip; log per-call cost, hit rate, FP rate. Ship the tier that wins on cost-per-correct-detection, not the tier with the best marketing.
- **Cascade** (Ollama primary + Claude confirm) when you can — cost bounded by positive rate, not call rate. Falls back to pure hosted when the local model can't do the task (e.g., overhead silhouettes).
- **Rate-limit identity chain** for debouncers: per-track VLM interval + fallback spatial FP suppression when track IDs are ephemeral (each blink of a light gets a fresh track_id — per-track debounce is useless).
- **Volume caps**: `VLM_INTERVAL_S`, `MIN_MOTION_BBOX_PX`, baseline diff thresholds. Every knob has an env override so ops can tune without a rebuild.

**Signal you're done**: monthly cost is predictable; a 10x traffic spike doesn't 10x the bill (cache absorbs the delta).

---

## Phase 5 — Precision engineering (two-layer defense)

**Pattern**: **Fail-fast at trust boundary + graceful degradation on runtime path**.

**Mechanic**:
- **Structural filters first, LLM second.** Kill obvious noise (small bboxes, edge-of-frame, non-wildlife COCO classes, stationary-FP centers) BEFORE the expensive stage.
- **Hard rejects in the prompt** for known failure modes. `HUMAN HARD REJECT` overrides all other patterns. `ALL THREE traits required to reject as insect` prevents over-rejection.
- **Whitelist over blacklist** for classification (`_WILDLIFE_COCO = {"cat","dog","bird",...}`) — new noisy classes can't sneak past.
- **Mask what the model shouldn't see** (`osd_masks` blanks burnt-in timestamps in BOTH baseline diff and VLM crops so Sonnet doesn't hallucinate "the only diff is the timestamp").
- **Stationary-FP suppression**: rolling window of recently-rejected bbox centers; new bboxes within radius of a known-bad spot get dropped pre-VLM.
- **Species-level policy in one place** (`_ALERTABLE_SPECIES` set) — includes `other` because "wildlife detected but species uncertain" is still alertable.

**Signal you're done**: known false-positive modes (blinking LEDs, moth wing-flash, humans, brush motion) are named, filtered, and covered by regression evidence in commit messages.

---

## Phase 6 — Surface silent failures

**Pattern**: **Observability contract at process boundaries**.

**Mechanic**:
- `try/except Exception as e: log.exception(...)` BEFORE any `os._exit()` or `sys.exit()`. Force-exit skips Python's default traceback printing; without explicit logging, crashes are silent.
- **Log the request shape** at the API boundary when debugging cache/auth/model issues (`VLM req: sys_blocks=2 sys_chars=[1170,13153] has_cache_control=True`). Remove after verification — diagnostic logs should be surgical.
- **Assertions at trust boundaries**: verify container env matches expectations at startup, not on first call. If `CLAUDE_MODEL` is unset, log a warning at init not a KeyError on request 47.
- **Compare adjacent state** in logs so anomalies are visible (`Baseline switched to day slot (v=1)` next to `daytime-rodent low-conf: 0.45 < 0.95`).

**Signal you're done**: no `Detector service exiting` log lines without a traceback preceding them. No "why did this stop?" mysteries.

---

## Phase 7 — Frontend as bounded context

**Pattern**: **Bounded context split** + **strangler-fig migration** + **two-plane deployment topology** (build vs runtime).

**Mechanic**:
- Separate `frontend/` directory with its own build config, deps, tsconfig, gitignore. Coupled to the backend ONLY via a typed API contract file (`src/api/status.ts` mirrors `Stats.snapshot()`).
- Modern toolchain: **Bun + Vite + React + strict TypeScript + Biome**. One Rust binary (Biome) replaces two Node processes (ESLint + Prettier). Bun replaces npm.
- **Multi-stage Dockerfile**: `oven/bun:1-alpine AS frontend-build` → runtime image copies `dist/` in. No node process at runtime.
- **Prefixed base path** (`base: "/react/"` in Vite, matching Flask serving prefix). Same discipline as `/api/v1/` — the URL prefix IS the version boundary.
- **Dev proxy**: Vite dev server on :5173 forwards `/status /api /snapshots` to Flask :8100. Frontend hot-reloads against the real backend.
- **Strangler migration**: new `/react/` route runs in PARALLEL with old vanilla-JS templates. Per-page cutover in subsequent PRs. Zero-downtime, reversible.
- **Typed hooks over ad-hoc fetch**: `useStatus(camera, intervalMs)` with `AbortController` cleanup. Callers can't leak fetches on unmount.

**Signal you're done**: `docker compose build web` produces a self-contained artifact. Frontend engineers can work without touching Python. Backend engineers can rename UI templates without breaking anything.

---

## Phase 8 — CI/CD gates

**Pattern**: **Fail-fast at CI trust boundary** — enforce contracts at PR review, not after they leak past into main.

**Mechanic**:
- `.github/workflows/<subsystem>.yml` per bounded context. `paths:` filter so unrelated PRs don't run irrelevant jobs.
- **Parallel jobs**: `lint`, `typecheck`, `build`, `docker-build` run independently. A lint failure MUST NOT hide a typecheck failure — reviewer needs the full contract-violation surface in one run.
- **Concurrency group** with `cancel-in-progress: true` — force-push to a PR branch cancels the superseded run. Keeps queue tight, saves CI minutes.
- **Docker multi-stage build in CI**, not just locally. "Works on my machine, breaks in prod" is exactly what this catches.
- **Cache aggressively** (GHA cache for Docker layers, node_modules restore, etc.) — otherwise CI feedback loop is too slow and people stop reading failures.
- **Local pre-commit is the primary workflow; CI is the backstop.** Run `bun run lint && bun run typecheck` before pushing; CI enforces it for reviewers who don't.

**Signal you're done**: PRs cannot merge red. Every failure mode CI catches is a mode that would have shipped before.

---

## Phase 9 — PR discipline

**Pattern**: **Small, reversible, self-explanatory changes**.

**Mechanic**:
- **Feature branches per phase** (`frontend/phase-1-scaffold`, `frontend/phase-2-alerts`). No mega-PRs.
- **PR descriptions with an `## Architecture calls` section** naming the patterns applied — this is the artifact reviewers screenshot in future interviews.
- **Commit messages explain the WHY**, not the WHAT. Include failure-mode receipts: "Haiku: 6 hits, 4 moth FPs — hallucinates species on ambiguous crops even with the Pattern 3 insect rail."
- **Bundle related-but-small changes** with a paragraph per change; DON'T split into 5 trivial PRs that all need review.
- **Author identity in commits matches the identity that will show on GitHub** (personal email if that's the public identity, not corp email).
- **No `git add .`** — stage explicitly. Prevents accidental secret commits, prevents scope creep.

**Signal you're done**: 6 months later you can read the git log and reconstruct the design decisions without hunting through Slack.

---

## Anti-patterns from this session (don't repeat)

- **Silent `os._exit(0)` in `finally`** — swallows all tracebacks. Always log unhandled exceptions FIRST.
- **Prompt caching without token-count verification** — `cache_control` on a small system prompt silently no-ops. Log `usage.cache_read_input_tokens` from day one.
- **Compose env var overridden by `.env`** without notice — `CASCADE_CONFIRM_MODEL` in `.env` overrode our `CLAUDE_MODEL` in compose. Container ran Haiku while we thought it was on Sonnet for hours. Fix: explicit per-service env overrides that shadow `.env`, and diagnostic logging of the actual model at init.
- **Overlays that a smart model reads literally** — the motion-contour overlay we drew on VLM crops made Opus say "there is a green overlay mask, not an animal." Cost: 0 confirms across an entire replay. Fix: default the overlay OFF; only re-enable with prompt language teaching the model to interpret it.
- **Debounce identity that doesn't cover the failure mode** — per-track_id debounce is useless when the FP source generates fresh track_ids per event (blinking lights). Need spatial identity as fallback.
- **Assuming CI will catch it** — CI is a backstop. Run lint/typecheck locally BEFORE pushing.

---

## Pattern glossary (used in commit messages + PR descriptions)

| Pattern | When to name it |
|---|---|
| Bulkhead per bounded context | Container splits, service boundaries |
| Strangler-fig migration | Parallel new-vs-old routes; per-piece cutover |
| Two-plane deployment | Build-time vs runtime, multi-stage Docker |
| Prefixed base path as URL contract | Versioned prefixes, `/api/v1/`, `/react/` |
| Bounded context split | Separate build target coupled only via API contract |
| Cache-breakpoint placement | Where the stable prefix boundary sits in an LLM prompt |
| Rate-limit identity chain | Debounce keys + fallback identity (per-track + spatial) |
| Cascade with confirm-stage escalation | Cheap primary + strict confirm; cost bounded by positive rate |
| Fail-fast at trust boundary | Structural filters before the expensive stage |
| Graceful degradation on runtime path | LLM as belt-and-suspenders after structural filters |
| Observability contract at process boundaries | Log the shape at API/exit points |
| SLI/SLO thinking | Counters at every filter stage |
| Fail-fast at CI trust boundary | Gates at PR review, parallel jobs |

---

## Per-project checklist (copy this into new repos as `docs/PRODUCTION-CHECKLIST.md`)

- [ ] ADR written for the service split (Phase 1)
- [ ] `docker-compose.yml` runs the whole thing from a clean clone
- [ ] Healthchecks per service
- [ ] `.env.example` committed; `.env` gitignored
- [ ] Multi-tenancy plumbed even if only one tenant today (Phase 2)
- [ ] `/status` endpoint with resource metrics + funnel counters (Phase 3)
- [ ] Cost telemetry if any per-call-billed API (Phase 3)
- [ ] Prompt cache verified with `usage` logs if using Anthropic (Phase 4)
- [ ] Hard rejects for known FP modes in code AND prompt (Phase 5)
- [ ] Two-layer defense: structural filter + LLM (Phase 5)
- [ ] `log.exception` before any force-exit (Phase 6)
- [ ] Frontend in separate directory with typed API contract (Phase 7)
- [ ] Bun + Vite + React + strict TS + Biome (Phase 7)
- [ ] `.github/workflows/` gates for every bounded context (Phase 8)
- [ ] `paths:` filter, `concurrency:` group, parallel jobs (Phase 8)
- [ ] Feature branches per phase; PR descriptions with `## Architecture calls` (Phase 9)
- [ ] Commit messages name the patterns applied

Follow the phases in order. Skip a phase, come back to it, and it'll cost 3x.
