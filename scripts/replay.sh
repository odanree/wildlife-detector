#!/usr/bin/env bash
# Replay a clip through an ephemeral detector container without polluting
# the production snapshots/ dir or the alerts DB.
#
# Bounded-context pattern: this is a test bulkhead — /tmp/eph inside the
# container isolates writes so real snapshots and the alert log stay
# untouched. Rebind SNAPSHOT_DIR + STATE_DB_PATH per-run.
#
# Usage:
#   ./scripts/replay.sh [--service <name>] <clip-path-inside-container> [extra env vars...]
#
# Default service is detector-rooftop (historical); override with --service
# to run any detector's config (matters when a clip's camera has camera-
# specific env like ZONE_KEY, MIN_MOTION_BBOX_PX, or VLM prompts).
#
# Examples (from Git Bash on Windows):
#   MSYS_NO_PATHCONV=1 ./scripts/replay.sh /app/clips/rooftop_raccoon_0009_tight.mp4 \
#     -e MOTION_VAR_THRESHOLD=10 -e BASELINE_DIFF_THRESHOLD=0.06
#
#   MSYS_NO_PATHCONV=1 ./scripts/replay.sh --service detector-under-adu-ptz \
#     /app/clips/under_adu_ptz_replay_16-46-50_to_16-47-40_PT.mp4
#
# Post-run: inspect ephemeral snapshots at ./snapshots/_ephemeral/replay_last/
# (mounted from the container's /tmp/eph).

set -euo pipefail

# 2026-09-26: added --service flag after a "-- detector-under-adu-ptz"
# invocation surfaced how the old positional-only shape swallowed the
# service name as a container command. The old shape hard-coded
# detector-rooftop; keeping that as the default preserves back-compat
# for scripts calling this without --service.
SERVICE="detector-rooftop"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --service)
            SERVICE="$2"; shift 2 ;;
        --)
            shift; break ;;
        -*)
            # Unknown flag — assume it's a docker -e or -v the user wants
            # passed through. Stop parsing so "$@" hands them to compose.
            break ;;
        *)
            break ;;
    esac
done

if [ $# -lt 1 ]; then
    echo "usage: $0 [--service <name>] <clip-path-inside-container> [extra docker env flags...]" >&2
    exit 1
fi

CLIP="$1"
shift

EPH_HOST="./snapshots/_ephemeral/replay_last"
mkdir -p "$EPH_HOST"

# Purge previous ephemeral run so the folder only shows the current test.
rm -f "$EPH_HOST"/*.jpg 2>/dev/null || true

echo "▶ Replay clip: $CLIP"
echo "▶ Detector service: $SERVICE"
echo "▶ Ephemeral snapshots → $EPH_HOST (host) / /tmp/eph (container)"
echo "▶ State DB          → /tmp/replay.db (container, discarded on exit)"

exec docker compose run --rm --no-deps \
    -e VIDEO_PATH="$CLIP" \
    -e SNAPSHOT_DIR=/tmp/eph \
    -e STATE_DB_PATH=/tmp/replay.db \
    -e STATE_DRY_RUN=1 \
    -e VLM_INTERVAL_S=0 \
    -v "$(pwd)/snapshots/_ephemeral/replay_last:/tmp/eph" \
    "$@" \
    "$SERVICE"

# STATE_DRY_RUN=1 is the load-bearing bulkhead against Postgres. Without
# it, the detector inherits DATABASE_URL from .env and writes replay
# events to the live alerts table (this bit us Aug 7 — see the deleted
# replay-contamination rows). STATE_DB_PATH is kept for back-compat with
# any code that still checks it, but the postgres path is what actually
# needs to be silenced.
