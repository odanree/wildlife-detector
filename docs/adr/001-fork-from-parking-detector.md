# ADR 001 — Extract wildlife detection into its own repo

**Status:** Accepted
**Date:** 2026-07-16

## Context

We had a working parking-enforcement detector (`odanree/parking-enforcement-detector`) — RTSP + YOLOv8 + ChalkingAnalyzer + wand-gate + pose-priors + two-stage VLM + ChromaDB RAG + FastAPI dashboard + React Kanban. Sound stack for street PE work.

A new deployment appeared: same operator, different site, **rodent detection** with a dual-camera slew-to-zone rig (primary sees the yard, secondary Amcrest/Dahua PTZ close-ups on the zone where the rodent appears).

The first design pass introduced a **thin strategy pattern** inside the parking repo:

- `DetectionStrategy` protocol
- `ParkingEnforcementStrategy` (existing behaviour, `on_positive` was a Null Object)
- `RodentStrategy` (rodent VLM prompts, `on_positive` slewed the secondary camera)
- Pipeline branched on `strategy.use_parking_gates` to skip the chalking/wand/pose/classifier stack

That code was merged onto a feature branch and opened as PR #11.

## Decision

Reversed course: **close PR #11 and extract the rodent context into a separate repo** (`odanree/wildlife-detector`).

The strategy pattern earns its keep when two variants live in one process. The moment they diverge into separate deployments, the indirection becomes a **tax**, not a savings:

- The parking pipeline gains nothing from carrying a `DetectionStrategy` protocol whose only-ever caller is `ParkingEnforcementStrategy`.
- The rodent pipeline gains nothing from carrying `use_parking_gates: bool` guards on every stage it doesn't use.
- Bugs, tests, and CI stop being independent — a wand-gate bug fix has to be regression-tested against the rodent site.

**YAGNI reverses direction** here. The strategy pattern was a valid design exercise (documented in the parking repo's closed PR), but it's not the right runtime shape once the sites are real.

## What we kept from the parking scaffolding

Verbatim (pure plumbing, mode-agnostic):

- `src/stream/rtsp_handler.py`, `video_file_handler.py`, `ptz.py`
- `src/detection/motion_detector.py`, `zone_filter.py`
- `config/botsort.yaml`, `bytetrack.yaml`

Lifted and lightly adapted:

- `src/detection/object_detector.py` — swapped hardcoded parking classes (`person, truck, motorcycle, car, chalker`) for wildlife-relevant COCO classes (`cat, dog, bird`); zeroed `_STATIONARY_EXEMPT` and `_POSITION_SUPPRESS_CLASSES` (small-mover-friendly)

Fresh writes (purpose-built, no strategy indirection):

- `src/vlm/analyzer.py` — wildlife-focused prompt returning `wildlife_detected` + `species` + `is_rodent`; three backends (claude/ollama/mock); ~230 lines vs the parking repo's 720
- `src/alerts/notifier.py` — headless: snapshot + HA/generic webhook + cooldown; no dashboard integration
- `src/stream/slew.py` — zone→preset dispatcher with per-event lockout + failure rollback (originally written for parking PR #11, no strategy adapter needed here)
- `src/pipeline.py` — lean loop: frame → YOLO + motion → zone filter → rate-limited VLM → slew + alert. No ChalkingAnalyzer, no wand gate, no pose priors, no person classifier, no RAG, no two-stage. ~200 lines vs the parking repo's 1367.

## What we intentionally dropped

- **Dashboard, FastAPI, WebSocket, React frontend, Kanban, ChromaDB RAG, session trust/suppress, hi-res re-localize, dedup vector store, action classifier, pose estimator, wand detector, chalking analyzer, person-type classifier, two-stage VLM, RAG auto-reject, langfuse tracing.** All of these are earning their keep on a busy street feed. None of them are earning it on a yard camera pointed at (usually) empty ground.
- **`DetectionStrategy` protocol, mode env var, `use_parking_gates` guards.** No branching, no future-proof indirection. If a third context appears, we extract again — that's the whole "extract-a-bounded-context" pattern.

## Consequences

**Positive:**
- Rodent pipeline is ~200 lines instead of ~1400 — audit-able in one sitting.
- Independent versioning, CI, docker image, ADRs.
- Bug fix in parking can't accidentally regress wildlife detection and vice versa.
- Naming reflects reality — `wildlife-detector` describes the code, `parking-enforcement-detector` describes the parking code.

**Negative:**
- Shared plumbing (RTSP handler, PTZ CGI, YOLO wrapper, motion detector, zone filter) is now duplicated across two repos. If we ever need to fix e.g. a stream reconnect bug, we fix it twice. **Copy-paste tax.**
- No shared release process. Ops has to remember which repo controls which deployment.

**When to reconsider — extract a shared library:**

If a third detection context appears (bird deterrent? delivery driver detection? garage entry?), the duplicated plumbing across three repos becomes worth extracting into a `ped-core` package that all three consume. That's a future ADR. For two contexts, the duplication is cheaper than the coupling.

## Alternatives considered

1. **Merge PR #11 as-is.** Parking permanently carries a `DetectionStrategy` protocol it never exercises. Rejected — YAGNI.
2. **Fork the parking repo (`git clone` + strip).** Initial commit would be the parking codebase with parking-specific files deleted — misleading provenance for a new repo. Rejected — chose fresh commit for clean history.
3. **Monorepo split with a shared `libs/` folder.** Rarely worth it for two services from one operator. Rejected — one shared library between two repos is where this becomes worthwhile, not before.

## References

- Closed PR: [parking-enforcement-detector#11](https://github.com/odanree/parking-enforcement-detector/pull/11)
- Parent repo: [parking-enforcement-detector](https://github.com/odanree/parking-enforcement-detector)
