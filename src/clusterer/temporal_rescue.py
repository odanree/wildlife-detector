"""Noise recovery via a spatiotemporal-locality prior.

HDBSCAN on CLIP-ViT-B/32 unit vectors is conservative on small dwells:
5-10 alerts on the same camera within a few minutes routinely land as
label -1 (noise) because their pairwise cosines fall just under the
density threshold. Appearance says "maybe", but co-occurrence on one
camera within a visit window says "same animal, high confidence".

`rescue_bursts` promotes those noise runs into synthetic clusters so the
downstream linker (centroid + member vote) gets a shot at attaching them
to an existing rat. If the appearance still doesn't match a known
centroid, the linker spawns a new rat — which is the correct outcome for
a genuinely new visitor. Either way, a coherent visit stops being lost.

Contract:

* Pure numpy; no DB, no hdbscan.
* Deterministic: same (labels, cameras, ts) → same output.
* Additive: existing HDBSCAN clusters are never renumbered or split. New
  cluster ids are handed out strictly above the current max.
* Only same-camera runs coalesce. Cross-camera transit is a separate
  problem (a rat moving from rooftop → backyard within 90 s is real, but
  needs a route model, not a clustering hack).
"""
from __future__ import annotations

from typing import Sequence

import numpy as np

DEFAULT_BURST_WINDOW_S = 300.0   # 5 minutes — one dwell on a 3 s-debounced camera
DEFAULT_BURST_MIN_SIZE = 3       # sub-min_cluster_size on purpose: this is the recall net


def rescue_bursts(
    labels: np.ndarray,
    camera_ids: Sequence[str],
    ts: np.ndarray,
    burst_window_s: float = DEFAULT_BURST_WINDOW_S,
    burst_min_size: int = DEFAULT_BURST_MIN_SIZE,
) -> tuple[np.ndarray, int]:
    """Return (new_labels, n_rescued_alerts).

    Scan noise-labelled alerts (label == -1) grouped by camera_id, sorted
    by ts. Consecutive same-camera noise alerts within `burst_window_s`
    of each other form a run; runs of length >= `burst_min_size` get a
    fresh positive cluster label (max existing label + 1, +2, …).

    All other labels — real HDBSCAN clusters AND noise alerts that did
    not join a burst — are preserved unchanged.
    """
    labels = np.asarray(labels, dtype=np.int64).copy()
    n = labels.shape[0]
    if n == 0 or burst_min_size < 2:
        return labels, 0

    ts = np.asarray(ts, dtype=np.float64)
    if len(camera_ids) != n or ts.shape[0] != n:
        raise ValueError("labels, camera_ids, ts must all have length N")

    next_label = int(labels.max()) + 1 if (labels >= 0).any() else 0

    # Group noise indices by camera; within each camera, sort by ts.
    noise_by_cam: dict[str, list[int]] = {}
    for i in range(n):
        if labels[i] == -1:
            noise_by_cam.setdefault(camera_ids[i], []).append(i)

    n_rescued = 0
    for cam in sorted(noise_by_cam):  # sorted → determinism across dict orders
        idxs = sorted(noise_by_cam[cam], key=lambda j: (ts[j], j))
        run: list[int] = []
        for j in idxs:
            if not run or (ts[j] - ts[run[-1]]) <= burst_window_s:
                run.append(j)
                continue
            # Gap too wide — close the previous run, start a new one.
            if len(run) >= burst_min_size:
                labels[run] = next_label
                next_label += 1
                n_rescued += len(run)
            run = [j]
        if len(run) >= burst_min_size:
            labels[run] = next_label
            next_label += 1
            n_rescued += len(run)

    return labels, n_rescued
