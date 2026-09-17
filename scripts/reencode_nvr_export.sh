#!/usr/bin/env bash
# Re-encode a USB-exported Annke NVR clip to clean, smooth H.264.
#
# The Annke NVR's USB-export files carry the on-disk HEVC verbatim,
# including whatever frame-drop damage the muxer suffered under iowait
# spikes. They also stamp a bogus `r_frame_rate=100/1` in the container.
# ffmpeg -c copy chokes on the malformed refs, and naive re-encodes at
# the wrong fps throw away real content. This wrapper applies the
# procedure that actually works for these files:
#
#   * -fflags +genpts+discardcorrupt      regenerate PTS from scratch,
#                                          skip frames without valid refs
#   * -err_detect ignore_err              don't bail on POC/RPS errors
#   * -ss <offset>                        trim from the start of the file
#   * -vf fps=20                          honor the camera's real capture
#                                          rate; never -r 15 (drops real
#                                          frames the encoder needed)
#   * -c:v libx264 -crf 20 -preset veryfast   near-lossless, fast
#   * -c:a aac -b:a 96k                   pcm_mulaw doesn't fit in mp4
#   * -movflags +faststart                web-playable
#
# Usage:
#   scripts/reencode_nvr_export.sh <src.mp4> <start> <duration_s> <label>
#
# `start` is one of:
#   HH:MM:SS   Pacific clock time — script computes the offset from the
#              file's embedded start timestamp. Preferred for hourly exports.
#   Ns        raw seconds from the start of the file (e.g. "11s" or just "11")
#
# Example — hourly export workflow (recommended for bulk):
#   1. Export 21:00 → 22:00 from NVR UI once → K:/NVR/D06_20260915210000.mp4
#   2. Extract each event with:
#      scripts/reencode_nvr_export.sh K:/NVR/D06_20260915210000.mp4 21:39:30 60 rat_investigating_vent
#      scripts/reencode_nvr_export.sh K:/NVR/D06_20260915210000.mp4 21:54:30 30 vent_prison_break
#      scripts/reencode_nvr_export.sh K:/NVR/D06_20260915210000.mp4 22:19:30 60 checks_vent_hole
#
# Or raw offset if you prefer:
#   scripts/reencode_nvr_export.sh K:/NVR/D06_20260915213919.mp4 11 60 rat_investigating_vent
#
# The output filename is derived from the SRC filename's embedded timestamp
# plus the trim offset, so the label reads naturally alongside other clips.
# Files land under clips/highlights/.

set -euo pipefail

if [ $# -lt 4 ]; then
    echo "usage: $0 <src.mp4> <start> <duration_s> <label>"
    echo "  start: HH:MM:SS Pacific clock time OR raw seconds (Ns or N)"
    echo "example: $0 K:/NVR/D06_20260915210000.mp4 21:39:30 60 rat_investigating_vent"
    exit 1
fi

SRC="$1"
START="$2"
DUR="$3"
LABEL="$4"

if [ ! -f "$SRC" ]; then
    echo "src not found: $SRC" >&2
    exit 1
fi

# Extract the channel + timestamp from the source filename. Annke exports
# name files like D06_20260915213919.mp4 (D<channel>_<YYYYMMDDHHMMSS>).
BASE=$(basename "$SRC" .mp4)
CH=$(echo "$BASE" | sed -n 's|^D\([0-9]\+\)_.*|\1|p')
TS=$(echo "$BASE" | sed -n 's|^D[0-9]\+_\(.*\)|\1|p')

if [ -z "$TS" ] || [ ${#TS} -ne 14 ]; then
    echo "could not parse timestamp from filename: $BASE" >&2
    echo "expected D<ch>_<YYYYMMDDHHMMSS>.mp4" >&2
    exit 1
fi

YEAR=${TS:0:4}; MONTH=${TS:4:2}; DAY=${TS:6:2}
HOUR=${TS:8:2}; MINUTE=${TS:10:2}; SECOND=${TS:12:2}
SRC_EPOCH=$(date -d "$YEAR-$MONTH-$DAY $HOUR:$MINUTE:$SECOND" +%s)

# Resolve `start` to a numeric ffmpeg -ss offset (seconds from src start).
# Two accepted forms: HH:MM:SS clock time OR N/Ns raw seconds.
if [[ "$START" =~ ^[0-9]{1,2}:[0-9]{2}:[0-9]{2}$ ]]; then
    # Clock time: interpret in the SAME date as the file's start. If the
    # clock time is EARLIER than the file's start (e.g. file starts 23:45,
    # clock is 00:15 for next day), roll forward one day.
    TGT_EPOCH=$(date -d "$YEAR-$MONTH-$DAY $START" +%s)
    if [ "$TGT_EPOCH" -lt "$SRC_EPOCH" ]; then
        TGT_EPOCH=$((TGT_EPOCH + 86400))
    fi
    TRIM=$((TGT_EPOCH - SRC_EPOCH))
    if [ "$TRIM" -lt 0 ]; then
        echo "trim resolved to negative offset — clock time before file start" >&2
        exit 1
    fi
else
    TRIM=${START%s}
    if ! [[ "$TRIM" =~ ^[0-9]+$ ]]; then
        echo "start must be HH:MM:SS or a number of seconds, got: $START" >&2
        exit 1
    fi
fi

EVENT_EPOCH=$((SRC_EPOCH + TRIM))
EVENT_STAMP=$(date -d "@$EVENT_EPOCH" +"%Y%m%dT%H%M%S")

# Annke camera-name lookup so the output filename tells you what you're
# looking at, not just a channel number. Update if channel assignments change.
CH_NUM=$((10#$CH))
case "$CH_NUM" in
    1) CAMNAME="crawlspace_int" ;;
    2) CAMNAME="rooftop" ;;
    3) CAMNAME="crawlspace_ext" ;;
    4) CAMNAME="backyard" ;;
    5) CAMNAME="frontyard" ;;
    6) CAMNAME="sideyard" ;;
    7) CAMNAME="corner" ;;
    8) CAMNAME="plant_pathway" ;;
    *) CAMNAME="ch${CH_NUM}" ;;
esac

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$REPO_ROOT/clips/highlights"
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/${LABEL}_${CAMNAME}_${EVENT_STAMP}_${DUR}s.mp4"

echo "  src   : $SRC"
echo "  trim  : +${TRIM}s for ${DUR}s"
echo "  event : $EVENT_STAMP Pacific"
echo "  out   : $OUT"

ffmpeg -hide_banner -loglevel warning \
    -fflags +genpts+discardcorrupt -err_detect ignore_err \
    -ss "$TRIM" -i "$SRC" \
    -vf "fps=20" \
    -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p \
    -c:a aac -b:a 96k -movflags +faststart \
    -t "$DUR" -y "$OUT"

SIZE=$(stat -c%s "$OUT" 2>/dev/null || stat -f%z "$OUT" 2>/dev/null)
DURATION=$(ffprobe -hide_banner -v error -show_entries format=duration -of default=noprint_wrappers=1 "$OUT" | cut -d= -f2)
printf "  done  : %.1f MB, %.2fs\n" "$(awk "BEGIN{print $SIZE/1048576}")" "$DURATION"
