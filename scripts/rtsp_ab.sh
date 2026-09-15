#!/usr/bin/env bash
# rtsp_ab.sh — A/B compare a detector's RTSP feed health before/after a config change.
#
# Metrics harvested from the detector container's own logs
# (src/stream/rtsp_handler.py already emits everything we need):
#   * reader-cadence fps      — mean & p95, higher/steadier = healthy
#   * consumer-pickup-age p95 — median across the samples, lower = queue draining fast
#   * "Stream read failed" hits — session died; fewer = healthier
#   * external ffprobe 503 rate — camera session-cap contention proxy
#
# Usage:
#   ./scripts/rtsp_ab.sh capture <label> <detector-container> <probe-rtsp-url> <duration-min>
#     - Captures live logs for <duration-min> and probes the URL periodically.
#     - Writes results to /tmp/rtsp_ab/<label>.log and /tmp/rtsp_ab/<label>_probe.log.
#
#   ./scripts/rtsp_ab.sh compare <baseline-label> <post-label>
#     - Prints a side-by-side table.
#
# Typical A/B flow:
#   1. ./scripts/rtsp_ab.sh capture baseline wildlife-detector-crawlspace \
#        "rtsp://admin:PW@192.168.1.105:554/1/stream0" 30
#   2. Change RTSP_URL in docker-compose.yml, `docker compose up -d detector-crawlspace`.
#      Wait 5 min for the new feed to settle.
#   3. ./scripts/rtsp_ab.sh capture post wildlife-detector-crawlspace \
#        "rtsp://admin:PW@192.168.1.105:554/1/stream0" 30
#   4. ./scripts/rtsp_ab.sh compare baseline post
#
# Probe URL is deliberately the DIRECT camera URL in both phases — that way you
# also measure the session-cap 503 rate from an OUTSIDE observer. If the switch
# reduced camera load, the probe should start succeeding.

set -euo pipefail

OUT_DIR="/tmp/rtsp_ab"
mkdir -p "$OUT_DIR"

usage() {
  sed -n '1,/^set -euo/p' "$0" | grep '^#' | sed 's/^# \{0,1\}//' >&2
  exit 1
}

cmd="${1:-}"; shift || usage

case "$cmd" in
  capture)
    label="${1:-}"; container="${2:-}"; probe_url="${3:-}"; duration_min="${4:-30}"
    [[ -z "$label" || -z "$container" || -z "$probe_url" ]] && usage
    log_file="$OUT_DIR/${label}.log"
    probe_file="$OUT_DIR/${label}_probe.log"

    echo "==> A/B capture '$label' — container=$container duration=${duration_min}m"
    echo "    log:   $log_file"
    echo "    probe: $probe_file"

    # Docker logs — since <duration_min> ago-to-now. Poll at end rather than
    # tail live so we capture a clean window matching the probe duration.
    end_ts=$(( $(date +%s) + duration_min * 60 ))

    # Background probe: hit the external URL every 30s. --timeout 3s so a session-cap
    # 503 lands fast without dragging the loop.
    (
      while [[ $(date +%s) -lt $end_ts ]]; do
        ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        # Prints the ffprobe stderr line (contains 401/503/OK) plus timestamp.
        result=$(ffprobe -v error -rtsp_transport tcp -timeout 3000000 \
          -show_entries stream=codec_name \
          "$probe_url" 2>&1 | head -1)
        echo "$ts $result"
        sleep 30
      done
    ) > "$probe_file" &
    probe_pid=$!

    # Sleep for the window, then dump logs. `--since <N>m` grabs the last N minutes
    # from the container's logs (works whether it was running or restarted).
    sleep $(( duration_min * 60 ))
    kill $probe_pid 2>/dev/null || true

    docker logs --since "${duration_min}m" "$container" > "$log_file" 2>&1

    echo "==> capture done: $(wc -l < "$log_file") log lines, $(wc -l < "$probe_file") probe samples"
    ;;

  compare)
    a_label="${1:-}"; b_label="${2:-}"
    [[ -z "$a_label" || -z "$b_label" ]] && usage

    a_log="$OUT_DIR/${a_label}.log"; a_probe="$OUT_DIR/${a_label}_probe.log"
    b_log="$OUT_DIR/${b_label}.log"; b_probe="$OUT_DIR/${b_label}_probe.log"

    for f in "$a_log" "$a_probe" "$b_log" "$b_probe"; do
      [[ -f "$f" ]] || { echo "missing: $f" >&2; exit 2; }
    done

    # Compact awk to compute metric-set for one log+probe pair.
    compute() {
      local log="$1" probe="$2"
      local mean_fps p95_fps mean_pickup p95_pickup reconnects probes_total probes_503 probes_ok
      # Mean + p95 fps from reader-cadence lines.
      mean_fps=$(grep -oE 'reader-cadence: fps=[0-9.]+' "$log" | grep -oE '[0-9.]+' \
        | awk 'BEGIN{s=0;n=0}{s+=$1;n++}END{if(n>0)printf "%.1f",s/n; else printf "n/a"}')
      p95_fps=$(grep -oE 'reader-cadence: fps=[0-9.]+' "$log" | grep -oE '[0-9.]+' | sort -n \
        | awk 'BEGIN{n=0}{a[n++]=$1}END{if(n>0)printf "%.1f",a[int(n*0.05)]; else printf "n/a"}')
      # Reconnects — literal string count.
      reconnects=$(grep -c 'Stream read failed' "$log" 2>/dev/null || echo 0)
      # p95 pickup age (of the p95 field emitted per 100-sample bucket).
      mean_pickup=$(grep -oE 'consumer-pickup-age.*p95=[0-9]+ms' "$log" | grep -oE 'p95=[0-9]+' | grep -oE '[0-9]+' \
        | awk 'BEGIN{s=0;n=0}{s+=$1;n++}END{if(n>0)printf "%.0f",s/n; else printf "n/a"}')
      p95_pickup=$(grep -oE 'consumer-pickup-age.*max=[0-9]+ms' "$log" | grep -oE 'max=[0-9]+' | grep -oE '[0-9]+' | sort -n \
        | awk 'BEGIN{n=0}{a[n++]=$1}END{if(n>0)printf "%.0f",a[int(n*0.95)]; else printf "n/a"}')
      # Probe outcomes.
      probes_total=$(wc -l < "$probe" | tr -d ' ')
      probes_503=$(grep -c '503' "$probe" 2>/dev/null || echo 0)
      probes_ok=$(grep -c 'codec_name=' "$probe" 2>/dev/null || echo 0)

      # emit as tab-sep for consumption below
      printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "$mean_fps" "$p95_fps" "$mean_pickup" "$p95_pickup" \
        "$reconnects" "$probes_total" "$probes_503" "$probes_ok"
    }

    IFS=$'\t' read -r a_mfps a_pfps a_mpu a_ppu a_rec a_pt a_p503 a_pok < <(compute "$a_log" "$a_probe")
    IFS=$'\t' read -r b_mfps b_pfps b_mpu b_ppu b_rec b_pt b_p503 b_pok < <(compute "$b_log" "$b_probe")

    printf "\nA/B: %s vs %s\n\n" "$a_label" "$b_label"
    printf "%-32s  %-14s  %-14s  %s\n" "Metric" "$a_label" "$b_label" "Δ (post-baseline)"
    printf "%-32s  %-14s  %-14s  %s\n" "--------------------------------" "--------------" "--------------" "-----------------"

    d() { awk -v a="$1" -v b="$2" 'BEGIN{if(a=="n/a"||b=="n/a")print "n/a"; else printf "%+.1f", b-a}'; }
    di() { awk -v a="$1" -v b="$2" 'BEGIN{if(a=="n/a"||b=="n/a")print "n/a"; else printf "%+d", b-a}'; }

    printf "%-32s  %-14s  %-14s  %s (higher=better)\n" "reader mean fps"      "$a_mfps"  "$b_mfps"  "$(d "$a_mfps" "$b_mfps")"
    printf "%-32s  %-14s  %-14s  %s (higher=better)\n" "reader p5 fps"        "$a_pfps"  "$b_pfps"  "$(d "$a_pfps" "$b_pfps")"
    printf "%-32s  %-14s  %-14s  %s (lower=better)\n"  "pickup mean p95 (ms)" "$a_mpu"   "$b_mpu"   "$(di "$a_mpu" "$b_mpu")"
    printf "%-32s  %-14s  %-14s  %s (lower=better)\n"  "pickup p95 max (ms)"  "$a_ppu"   "$b_ppu"   "$(di "$a_ppu" "$b_ppu")"
    printf "%-32s  %-14s  %-14s  %s (lower=better)\n"  "reconnect events"     "$a_rec"   "$b_rec"   "$(di "$a_rec" "$b_rec")"
    printf "%-32s  %-14s  %-14s  %s (lower=better)\n"  "external 503 probes"  "$a_p503"  "$b_p503"  "$(di "$a_p503" "$b_p503")"
    printf "%-32s  %-14s  %-14s  %s (higher=better)\n" "external OK probes"   "$a_pok"   "$b_pok"   "$(di "$a_pok" "$b_pok")"
    printf "%-32s  %-14s  %-14s\n" "external probes total"                    "$a_pt"    "$b_pt"
    echo
    ;;

  *)
    usage
    ;;
esac
