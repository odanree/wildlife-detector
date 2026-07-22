#!/usr/bin/env bash
# Replay a clip through an ephemeral detector container without polluting
# the production snapshots/ dir or the alerts DB.
#
# Bounded-context pattern: this is a test bulkhead — /tmp/eph inside the
# container isolates writes so real snapshots and the alert log stay
# untouched. Rebind SNAPSHOT_DIR + STATE_DB_PATH per-run.
#
# Usage:
#   ./scripts/replay.sh <clip-path-inside-container> [extra env vars...]
#
# Example (from Git Bash on Windows):
#   MSYS_NO_PATHCONV=1 ./scripts/replay.sh /app/clips/rooftop_raccoon_0009_tight.mp4 \
#     -e MOTION_VAR_THRESHOLD=10 -e BASELINE_DIFF_THRESHOLD=0.06
#
# Post-run: inspect ephemeral snapshots at ./snapshots/_ephemeral/replay_last/
# (mounted from the container's /tmp/eph).

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "usage: $0 <clip-path-inside-container> [extra docker env flags...]" >&2
    exit 1
fi

CLIP="$1"
shift

EPH_HOST="./snapshots/_ephemeral/replay_last"
mkdir -p "$EPH_HOST"

# Purge previous ephemeral run so the folder only shows the current test.
rm -f "$EPH_HOST"/*.jpg 2>/dev/null || true

echo "▶ Replay clip: $CLIP"
echo "▶ Ephemeral snapshots → $EPH_HOST (host) / /tmp/eph (container)"
echo "▶ State DB          → /tmp/replay.db (container, discarded on exit)"

exec docker compose run --rm --no-deps \
    -e VIDEO_PATH="$CLIP" \
    -e SNAPSHOT_DIR=/tmp/eph \
    -e STATE_DB_PATH=/tmp/replay.db \
    -e VLM_INTERVAL_S=0 \
    -v "$(pwd)/snapshots/_ephemeral/replay_last:/tmp/eph" \
    "$@" \
    detector-rooftop
