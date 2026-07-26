"""Per-day snapshot audit tool for the intermittent camera-attribution
bug — helps find the timestamp when stream attribution stabilized.

For each day in the requested window, pulls N random alerts per camera
and copies their snapshots into:
    sandbox/investigation/YYYY-MM-DD/rooftop/<id>_<species>.jpg
    sandbox/investigation/YYYY-MM-DD/yard/<id>_<species>.jpg

Open each day folder in Explorer with thumbnail view → instant visual
grid. Days where the "rooftop" folder shows yard scenes (or vice
versa) reveal the intermittent misattribution period. The last day
with any mismatch is the cutoff — trust the DB from the day after.

Usage:
    python sandbox/investigate_attribution.py --days 21 --per-day 6

Pattern: **bisect via visual audit** — same shape as git bisect but
the "test" is a human eyeballing a folder of thumbnails.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DB_PATH = REPO_ROOT / "data" / "state.db"
SNAPSHOTS_DIR = REPO_ROOT / "snapshots"
INVEST_DIR = SCRIPT_DIR / "investigation"

# PDT — same convention as fetch_clips.py. Adjust for PST if the clocks
# have rolled over between runs.
LOCAL_TZ = timezone(timedelta(hours=-7))


def _copy_samples_for_day(conn: sqlite3.Connection, day: datetime, camera: str,
                          per_day: int, out_dir: Path) -> int:
    """Sample `per_day` random alerts for (day, camera), copy their
    snapshots into out_dir. Returns count copied."""
    # Day window in local time; convert to epoch bounds for the query.
    start = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=LOCAL_TZ)
    end = start + timedelta(days=1)
    rows = conn.execute(
        """
        SELECT id, species, snapshot
        FROM alerts
        WHERE camera_id = ? AND ts >= ? AND ts < ?
          AND snapshot IS NOT NULL
        ORDER BY RANDOM()
        LIMIT ?
        """,
        (camera, start.timestamp(), end.timestamp(), per_day),
    ).fetchall()
    out_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for id_, species, snap in rows:
        src = SNAPSHOTS_DIR / snap.replace("/", "\\")
        # Also try forward-slash form since manifest paths mix.
        if not src.exists():
            src = SNAPSHOTS_DIR / snap
        if not src.exists():
            print(f"  MISS {id_}: snapshot not on disk ({snap})", file=sys.stderr)
            continue
        dst = out_dir / f"{id_}_{species or 'unknown'}.jpg"
        shutil.copy(src, dst)
        copied += 1
    return copied


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=21,
                    help="how many days back from today to audit (default 21)")
    ap.add_argument("--per-day", type=int, default=6,
                    help="random snapshots per camera per day (default 6)")
    ap.add_argument("--cameras", nargs="+", default=["rooftop", "yard"],
                    help="camera_ids to audit")
    ap.add_argument("--fresh", action="store_true",
                    help="wipe investigation/ before starting")
    args = ap.parse_args()

    if not DB_PATH.exists():
        raise SystemExit(f"DB not found at {DB_PATH}")

    if args.fresh and INVEST_DIR.exists():
        shutil.rmtree(INVEST_DIR)

    INVEST_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))

    today = datetime.now(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    print(f"Auditing {args.days} days back from {today.date()}, "
          f"{args.per_day} random snapshots per camera per day")
    print(f"Output: {INVEST_DIR}")
    print()

    total = 0
    for i in range(args.days):
        day = today - timedelta(days=args.days - 1 - i)
        day_str = day.strftime("%Y-%m-%d")
        print(f"{day_str}:", end=" ")
        parts = []
        for cam in args.cameras:
            out = INVEST_DIR / day_str / cam
            n = _copy_samples_for_day(conn, day, cam, args.per_day, out)
            parts.append(f"{cam}={n}")
            total += n
        print(" ".join(parts))

    print(f"\nDone: {total} snapshots copied")
    print(f"\nOpen {INVEST_DIR} in Explorer, browse each day folder in thumbnail")
    print("view. The last day where 'rooftop/' shows yard scenes (or vice versa)")
    print("is the cutoff — trust the DB from the day after.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
