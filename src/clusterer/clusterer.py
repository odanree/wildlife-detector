"""RatClusterer — HDBSCAN over a time window of alert_embeddings, linked
to a persistent `rats` catalog, written back as `alerts.rat_id`.

## One run, end to end

    load_window(start, end)      alerts ⋈ alert_embeddings, TP-labelled
                                 live rodent rows with ts in [start, end)
        │
    cluster(X)                   HDBSCAN(min_cluster_size, min_samples,
        │                        cluster_selection_epsilon) on unit vectors
        │                        (euclidean on unit vectors ranks identically
        │                        to cosine; see below)
        │
    summarize_clusters           label → (members, unit centroid); noise
        │                        (-1) dropped — never gets a rat_id
        │
    link_clusters                centroid cosine vs. ACTIVE rats.centroid
        │                        (>= link_threshold → same rat, else spawn)
        │
    ── one transaction ─────────────────────────────────────────────────
    INSERT rats (spawns)         provisional → real BIGSERIAL ids
    UPDATE alerts SET rat_id     per resolved rat, member alert ids
    refresh_rat_stats            first/last_seen, alert_count,
                                 primary_camera, centroid RECOMPUTED from
                                 the member rows (derived state, never
                                 incremented → idempotent re-runs)
    retire                       active rats with last_seen older than
                                 window_end - retire_days → retired_at
    INSERT rat_cluster_runs      audit row with params + notes
    ────────────────────────────────────────────────────────────────────

## Why euclidean on unit vectors, not metric='cosine'

The `hdbscan` package accepts metric='cosine' only through the slow
generic path (full pairwise matrix, O(n²) memory — 9k rows ≈ 650 MB of
float64). For unit vectors ‖a-b‖² = 2 - 2·cos(a,b), a strictly monotone
function of cosine, so HDBSCAN's density ordering is IDENTICAL under
euclidean and we get the KD-tree code path (O(n) memory). Phase 2a
stores L2-normalised vectors; we re-normalise defensively on load.

`cluster_selection_epsilon` is therefore expressed in euclidean units.
The helper `cosine_to_euclid(0.85) ≈ 0.548` converts a cosine floor
into that space so the knob reads in the same units as link_threshold.

## Parameter calibration (measured 2026-09-15 on 9,322 TP-labelled alerts)

CLIP ViT-B/32 on 30-80 px IR crops is only weakly identity-discriminative:

    pairwise cosine, same camera, > 1 day apart      median 0.870
    pairwise cosine, same camera, < 2 min apart      median 0.893   (≈ same animal)
    pairwise cosine, different cameras               median 0.841
    1-NN cosine                                      median 0.958

A 0.02 gap between "certainly the same rat" and "any two rats on this
camera" means the embedding space is a dense continuum dominated by
camera / background / pose. Consequences for the knobs:

* `cluster_selection_method='leaf'`, not 'eom'. Excess-of-mass picks the
  root split on a continuum → ONE cluster holding 98% of the window
  (measured: 5085/5091), i.e. no identity at all. Leaf selection returns
  the finest stable density peaks: single-camera (purity 0.99-1.00),
  recurring across 5-24 separate visits, intra-cluster cosine ≈ 0.95.
  Those are the best "individual" proxy this embedding offers.
* `min_cluster_size=5, min_samples=2`. min_samples decoupled from
  min_cluster_size (hdbscan's default ties them) — 2 keeps the density
  estimate from being so conservative that 93% of the window is noise
  (min_samples=5) while avoiding the single-linkage chaining of 1.
  Five alerts ≈ one dwell on a 3 s-debounced camera; a rat seen once
  or twice stays noise, which is the brief's intent for "3".
* `link_threshold=0.93`, NOT the 0.85 the Phase 2b brief guessed.
  Centroid-vs-centroid cosine between DISTINCT same-camera leaf clusters
  has median 0.91-0.94; at 0.85 the linker merges 74-92% of all cluster
  pairs and the catalog collapses to one rat per camera. 0.93 sits at
  that same-camera median: pairs tighter than a typical
  neighbouring-cluster pair merge, looser ones stay separate.
* `cluster_selection_epsilon=0` (off). Any epsilon ≥ cosine 0.80 re-
  connects the whole manifold and reproduces the eom blob.

Honest limit: nothing above is validated against ground-truth identity
(there is none). The catalog is "recurring appearance modes per camera",
which is what /api/rats can promise until the embedding is fine-tuned
— track_id gives free positive pairs for that (Phase 3 candidate).

## Noise

HDBSCAN label -1 = noise. Those alerts are NOT assigned a rat_id: a rat
seen once or twice in a window is either a cross-track fragment of a
catalogued rat we couldn't attach with confidence, or a genuine one-off.
Either way a rat_id would be a guess. They keep whatever rat_id they had
(NULL for a fresh row; an earlier window's assignment in an overlap).

## Idempotency

Re-running a window with identical inputs yields identical rat_ids:
HDBSCAN is deterministic on identical input order (rows sorted by
alert_id), the linker is deterministic, and clusters re-match their own
prior rats at cosine ≈ 1.0. rats.* are recomputed from truth each run,
never incremented, so counts don't drift.

## Neighbour diagnostics (the HNSW index's job)

For every SPAWN decision we ask the HNSW index which already-assigned
alerts sit nearest the new centroid (`nearest_assigned`). If the top
hits belong to one existing rat at high similarity, the linker just
declined to merge something that the per-alert neighbourhood says is
the same animal — that is the "spillover" the backfill report grades
each window boundary on. Stored in rat_cluster_runs.notes as JSON so a
bad window is auditable after the fact.
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.clusterer.linker import (
    DEFAULT_LINK_THRESHOLD,
    ClusterSummary,
    KnownRat,
    LinkResult,
    link_clusters,
    resolve_new_ids,
    summarize_clusters,
)
from src.clusterer.schema import EMBEDDING_DIM
from src.clusterer.temporal_rescue import (
    DEFAULT_BURST_MIN_SIZE,
    DEFAULT_BURST_WINDOW_S,
    rescue_bursts,
)

logger = logging.getLogger(__name__)

ALGO = "hdbscan+centroid-link"
DEFAULT_MODEL_VERSION = "openai/clip-vit-base-patch32"


def _to_np(v) -> np.ndarray:
    """pgvector's psycopg adapter returns numpy arrays in 0.3/0.4 and
    `pgvector.Vector` objects (with .to_numpy()) from 0.5. Accept both."""
    if hasattr(v, "to_numpy"):
        v = v.to_numpy()
    elif hasattr(v, "to_list"):
        v = v.to_list()
    return np.asarray(v, dtype=np.float32)


def cosine_to_euclid(cos: float) -> float:
    """Euclidean distance between two unit vectors with the given cosine."""
    return math.sqrt(max(0.0, 2.0 - 2.0 * cos))


def euclid_to_cosine(d: float) -> float:
    return 1.0 - (d * d) / 2.0


# Above this many rows the O(n²) precomputed distance matrix (float64:
# 8000² × 8 B = 512 MB) would crowd the 2 GB bulkhead; fall back to the
# KD-tree euclidean path, which yields IDENTICAL labels (verified ARI=1.0
# on a 5,142-row window) at ~10x the wall time (17 s vs 1.5 s).
PRECOMPUTED_MAX_N = 8000


@dataclass
class ClusterParams:
    # Defaults are the calibrated values from the module docstring, not
    # hdbscan's — see "Parameter calibration".
    min_cluster_size: int = 5
    min_samples: Optional[int] = 2          # None → hdbscan ties it to min_cluster_size
    cluster_selection_epsilon: float = 0.0  # euclidean units on unit vectors
    cluster_selection_method: str = "leaf"
    link_threshold: float = DEFAULT_LINK_THRESHOLD
    retire_days: int = 30
    labeled_only: bool = True               # label_verdict = 'correct' only
    model_version: str = DEFAULT_MODEL_VERSION
    precomputed_max_n: int = PRECOMPUTED_MAX_N
    # Hybrid density-locality: promote same-camera noise bursts into
    # synthetic clusters so the linker sees them. 0 disables the rescue.
    burst_window_s: float = DEFAULT_BURST_WINDOW_S
    burst_min_size: int = DEFAULT_BURST_MIN_SIZE

    def to_json(self) -> dict:
        d = asdict(self)
        d["algo"] = ALGO
        d["epsilon_as_cosine"] = (
            euclid_to_cosine(self.cluster_selection_epsilon) if self.cluster_selection_epsilon else None
        )
        return d


@dataclass
class WindowData:
    alert_ids: np.ndarray           # (N,) int64, sorted ascending
    camera_ids: list[str]
    ts: np.ndarray                  # (N,) float64 epoch seconds
    vectors: np.ndarray             # (N, 512) float32 unit-norm
    prior_rat_ids: list[Optional[int]]  # rat_id before this run (overlap carry-over)

    @property
    def n(self) -> int:
        return int(self.alert_ids.shape[0])


@dataclass
class RunResult:
    window_start: datetime
    window_end: datetime
    n_alerts: int = 0
    n_clusters: int = 0
    n_noise: int = 0
    n_temporal_rescued: int = 0    # alerts recovered from HDBSCAN noise via burst rescue
    n_assigned: int = 0
    rats_new: int = 0
    rats_linked: int = 0
    rats_linked_by_vote: int = 0
    rats_merged_in_run: int = 0
    rats_retired: int = 0
    rats_orphaned: int = 0
    rats_active: int = 0
    run_id: Optional[int] = None
    dry_run: bool = False
    elapsed_s: float = 0.0
    spillover: list[dict] = field(default_factory=list)
    assignments: dict[int, list[int]] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"window {self.window_start.date()}→{self.window_end.date()} "
            f"alerts={self.n_alerts} clusters={self.n_clusters} noise={self.n_noise} "
            f"rescued={self.n_temporal_rescued} "
            f"assigned={self.n_assigned} rats_new={self.rats_new} linked={self.rats_linked} "
            f"(by_vote={self.rats_linked_by_vote}) merged_in_run={self.rats_merged_in_run} "
            f"retired={self.rats_retired} orphaned={self.rats_orphaned} "
            f"active={self.rats_active} spillover_flags={sum(1 for s in self.spillover if s['flag'])} "
            f"{'DRY-RUN ' if self.dry_run else ''}run_id={self.run_id} {self.elapsed_s:.1f}s"
        )


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class RatClusterer:
    def __init__(self, dsn: str, params: ClusterParams | None = None) -> None:
        self.dsn = dsn
        self.params = params or ClusterParams()

    # ── DB plumbing ───────────────────────────────────────────────────────

    def _connect(self) -> psycopg.Connection:
        conn = psycopg.connect(self.dsn, row_factory=dict_row)
        register_vector(conn)
        with conn.cursor() as cur:
            # HNSW query-time knobs (session-scoped). See schema.py.
            cur.execute("SET hnsw.ef_search = 100")
            try:
                cur.execute("SET hnsw.iterative_scan = relaxed_order")
            except psycopg.Error:
                # pgvector < 0.8 — filtered ANN may under-fill LIMIT; degrade.
                conn.rollback()
                logger.warning("hnsw.iterative_scan unsupported on this pgvector; filtered ANN may under-fill")
        conn.commit()
        return conn

    # ── Load ──────────────────────────────────────────────────────────────

    def load_window(self, conn: psycopg.Connection, start: datetime, end: datetime) -> WindowData:
        """TP-labelled live rodent alerts with an embedding for the
        configured model, ts in [start, end). Sorted by alert_id so the
        HDBSCAN input order (and therefore its output) is reproducible."""
        where = [
            "a.is_rodent", "NOT a.historical", "a.snapshot IS NOT NULL",
            "a.ts >= %s", "a.ts < %s", "e.model_version = %s",
        ]
        params: list = [_utc(start).timestamp(), _utc(end).timestamp(), self.params.model_version]
        if self.params.labeled_only:
            where.append("a.label_verdict = 'correct'")
        else:
            # Unlabelled rows ride along; explicit FP labels never do.
            where.append("(a.label_verdict IS NULL OR a.label_verdict = 'correct')")
        sql = f"""
            SELECT a.id, a.camera_id, a.ts, a.rat_id, e.embedding
            FROM alerts a
            JOIN alert_embeddings e ON e.alert_id = a.id
            WHERE {' AND '.join(where)}
            ORDER BY a.id
        """  # noqa: S608 -- where-clauses are compile-time constants
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        if not rows:
            return WindowData(
                alert_ids=np.zeros(0, dtype=np.int64), camera_ids=[], ts=np.zeros(0),
                vectors=np.zeros((0, EMBEDDING_DIM), dtype=np.float32), prior_rat_ids=[],
            )
        X = np.stack([_to_np(r["embedding"]) for r in rows])
        norms = np.linalg.norm(X, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        X = X / norms
        return WindowData(
            alert_ids=np.asarray([int(r["id"]) for r in rows], dtype=np.int64),
            camera_ids=[r["camera_id"] for r in rows],
            ts=np.asarray([float(r["ts"]) for r in rows], dtype=np.float64),
            vectors=X,
            prior_rat_ids=[r["rat_id"] for r in rows],
        )

    def load_active_rats(self, conn: psycopg.Connection) -> list[KnownRat]:
        with conn.cursor() as cur:
            cur.execute("SELECT id, centroid FROM rats WHERE retired_at IS NULL ORDER BY id")
            return [KnownRat(id=int(r["id"]), centroid=_to_np(r["centroid"])) for r in cur.fetchall()]

    # ── Cluster ───────────────────────────────────────────────────────────

    def cluster(self, X: np.ndarray) -> np.ndarray:
        """HDBSCAN labels for unit vectors X. -1 = noise. Imported lazily
        so the linker + schema stay importable without the C extension."""
        if X.shape[0] == 0:
            return np.zeros(0, dtype=np.int64)
        if X.shape[0] < self.params.min_cluster_size:
            return np.full(X.shape[0], -1, dtype=np.int64)
        import hdbscan  # noqa: WPS433 -- heavy optional dep

        p = self.params
        common = dict(
            min_cluster_size=p.min_cluster_size,
            min_samples=p.min_samples,
            cluster_selection_epsilon=p.cluster_selection_epsilon,
            cluster_selection_method=p.cluster_selection_method,
            approx_min_span_tree=False,   # exact MST → reproducible labels
        )
        X64 = X.astype(np.float64)
        if X.shape[0] <= p.precomputed_max_n:
            # Fast path: one BLAS matmul gives every pairwise cosine; the
            # euclidean distance on unit vectors is sqrt(2 - 2cos). Exact,
            # same MST as the KD-tree path, ~10x faster at 512 dims.
            G = X64 @ X64.T
            D = np.sqrt(np.clip(2.0 - 2.0 * G, 0.0, None))
            np.fill_diagonal(D, 0.0)
            model = hdbscan.HDBSCAN(metric="precomputed", **common)
            return model.fit_predict(D).astype(np.int64)
        model = hdbscan.HDBSCAN(metric="euclidean", core_dist_n_jobs=1, **common)
        return model.fit_predict(X64).astype(np.int64)

    # ── Neighbour diagnostics (HNSW) ──────────────────────────────────────

    def nearest_assigned(
        self, conn: psycopg.Connection, centroid: np.ndarray, exclude_alert_ids: list[int], k: int = 10
    ) -> list[dict]:
        """k nearest ALREADY-ASSIGNED alerts to `centroid`, via the HNSW
        index. Returns [{rat_id, alert_id, sim}] sorted by sim desc."""
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.rat_id, a.id AS alert_id, 1 - (e.embedding <=> %(c)s) AS sim
                FROM alert_embeddings e
                JOIN alerts a ON a.id = e.alert_id
                WHERE a.rat_id IS NOT NULL AND NOT (a.id = ANY(%(ex)s))
                ORDER BY e.embedding <=> %(c)s
                LIMIT %(k)s
                """,
                {"c": centroid, "ex": exclude_alert_ids, "k": int(k)},
            )
            return [{"rat_id": int(r["rat_id"]), "alert_id": int(r["alert_id"]), "sim": float(r["sim"])}
                    for r in cur.fetchall()]

    def _spillover_for_spawn(
        self, conn: psycopg.Connection, cl: ClusterSummary, threshold: float
    ) -> dict:
        """Grade one spawn: does the neighbourhood say this is really an
        existing rat? `flag` = a majority of the k nearest assigned
        alerts belong to one rat AND the top similarity clears the link
        threshold. That's the linker saying "new" while the data says
        "same" — a window-boundary spillover candidate."""
        nn = self.nearest_assigned(conn, cl.centroid, cl.member_ids, k=10)
        if not nn:
            return {"cluster": cl.label, "size": cl.size, "nn_rat": None, "nn_votes": 0,
                    "nn_top_sim": None, "flag": False}
        votes: dict[int, int] = {}
        for r in nn:
            votes[r["rat_id"]] = votes.get(r["rat_id"], 0) + 1
        top_rat, top_votes = max(votes.items(), key=lambda kv: (kv[1], -kv[0]))
        top_sim = max(r["sim"] for r in nn if r["rat_id"] == top_rat)
        return {
            "cluster": cl.label, "size": cl.size, "nn_rat": top_rat, "nn_votes": top_votes,
            "nn_k": len(nn), "nn_top_sim": round(top_sim, 4),
            "flag": bool(top_votes * 2 > len(nn) and top_sim >= threshold),
        }

    # ── Write ─────────────────────────────────────────────────────────────

    @staticmethod
    def _spawn_rat(conn: psycopg.Connection, cl: ClusterSummary, ts_by_alert: dict[int, float],
                   cam_by_alert: dict[int, str]) -> int:
        """INSERT a rats row for a spawn decision. Stats are provisional
        (refresh_rat_stats overwrites them from truth before commit)."""
        member_ts = [ts_by_alert[a] for a in cl.member_ids]
        cams = [cam_by_alert[a] for a in cl.member_ids]
        primary = max(sorted(set(cams)), key=cams.count)
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO rats (first_seen, last_seen, alert_count, primary_camera, centroid)
                   VALUES (to_timestamp(%s), to_timestamp(%s), %s, %s, %s) RETURNING id""",
                (min(member_ts), max(member_ts), len(cl.member_ids), primary, cl.centroid),
            )
            return int(cur.fetchone()["id"])

    @staticmethod
    def refresh_rat_stats(
        conn: psycopg.Connection, rat_ids: list[int], model_version: str, as_of: datetime
    ) -> int:
        """Recompute rats.* from member alerts. Derived state, never
        incremented — the idempotency anchor for the whole run.

        Returns the number of rats ORPHANED: touched rats left with zero
        members (all re-assigned elsewhere this run) are retired on the
        spot with a note, so the catalog never carries an identity that
        no alert points at."""
        if not rat_ids:
            return 0
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH agg AS (
                    SELECT a.rat_id,
                           MIN(a.ts) AS first_ts, MAX(a.ts) AS last_ts, COUNT(*) AS n,
                           MODE() WITHIN GROUP (ORDER BY a.camera_id) AS primary_camera,
                           AVG(e.embedding) AS mean_vec
                    FROM alerts a
                    JOIN alert_embeddings e ON e.alert_id = a.id AND e.model_version = %s
                    WHERE a.rat_id = ANY(%s)
                    GROUP BY a.rat_id
                ),
                -- Body-size proxy: median bbox_area/frame_area over
                -- primary-camera members. Cross-camera bbox area is not
                -- comparable (distance-to-lens dominates), so the median
                -- is scoped to the rat's primary_camera; other cameras'
                -- members feed centroid & counts but not size.
                size AS (
                    SELECT a.rat_id,
                           percentile_cont(0.5) WITHIN GROUP (
                             ORDER BY (
                               ((e.crop_bbox->>'x2')::int - (e.crop_bbox->>'x1')::int)::double precision *
                               ((e.crop_bbox->>'y2')::int - (e.crop_bbox->>'y1')::int)::double precision
                             ) / NULLIF(
                               (e.crop_bbox->>'frame_w')::int::double precision *
                               (e.crop_bbox->>'frame_h')::int::double precision, 0)
                           ) AS med_frac
                    FROM alerts a
                    JOIN alert_embeddings e ON e.alert_id = a.id AND e.model_version = %s
                    JOIN agg ON agg.rat_id = a.rat_id
                    WHERE a.rat_id = ANY(%s)
                      AND e.crop_bbox IS NOT NULL
                      AND a.camera_id = agg.primary_camera
                    GROUP BY a.rat_id
                )
                UPDATE rats r SET
                    first_seen             = to_timestamp(agg.first_ts),
                    last_seen              = to_timestamp(agg.last_ts),
                    alert_count            = agg.n,
                    primary_camera         = agg.primary_camera,
                    centroid               = l2_normalize(agg.mean_vec),
                    body_size_frac_median  = size.med_frac,
                    updated_at             = now()
                FROM agg
                LEFT JOIN size ON size.rat_id = agg.rat_id
                WHERE agg.rat_id = r.id
                """,
                (model_version, rat_ids, model_version, rat_ids),
            )
            cur.execute(
                """
                UPDATE rats r SET
                    alert_count = 0,
                    retired_at  = COALESCE(r.retired_at, %s),
                    notes       = CONCAT_WS(' | ', r.notes, 'orphaned: all members reassigned'),
                    updated_at  = now()
                WHERE r.id = ANY(%s)
                  AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.rat_id = r.id)
                """,
                (as_of, rat_ids),
            )
            return cur.rowcount

    @staticmethod
    def retire_stale(conn: psycopg.Connection, as_of: datetime, retire_days: int) -> int:
        """Soft-retire active rats with no member alert in the last
        retire_days before `as_of` (window_end — so a backfill retires
        relative to the window it is replaying, not wall-clock now)."""
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE rats SET retired_at = %s, updated_at = now()
                   WHERE retired_at IS NULL AND last_seen < %s - make_interval(days => %s)""",
                (as_of, as_of, int(retire_days)),
            )
            return cur.rowcount

    # ── Orchestration ─────────────────────────────────────────────────────

    def run_window(
        self,
        window_start: datetime,
        window_end: datetime,
        dry_run: bool = False,
        notes: str | None = None,
    ) -> RunResult:
        t0 = time.perf_counter()
        window_start, window_end = _utc(window_start), _utc(window_end)
        res = RunResult(window_start=window_start, window_end=window_end, dry_run=dry_run)
        p = self.params

        with self._connect() as conn:
            data = self.load_window(conn, window_start, window_end)
            res.n_alerts = data.n
            labels = self.cluster(data.vectors)
            # Noise-recovery via spatiotemporal-locality prior. Runs
            # BEFORE summarize so rescued bursts get real centroids and
            # feed the linker like any other cluster.
            if p.burst_min_size >= 2 and p.burst_window_s > 0 and data.n:
                labels, res.n_temporal_rescued = rescue_bursts(
                    labels, data.camera_ids, data.ts,
                    burst_window_s=p.burst_window_s, burst_min_size=p.burst_min_size,
                )
            res.n_noise = int((labels == -1).sum())
            clusters = summarize_clusters(labels, data.alert_ids, data.vectors)
            res.n_clusters = len(clusters)

            known = self.load_active_rats(conn)
            # Prior assignments (overlap region / re-run) feed the member
            # vote — see linker.py step 0.
            prior = {int(a): int(r) for a, r in zip(data.alert_ids, data.prior_rat_ids) if r is not None}
            link: LinkResult = link_clusters(clusters, known, p.link_threshold, prior=prior)
            res.rats_new = link.n_new
            res.rats_linked = link.n_linked
            res.rats_linked_by_vote = link.n_linked_by_vote
            res.rats_merged_in_run = link.n_merged_in_run

            # Spillover diagnostics BEFORE we write, so the neighbourhood
            # reflects only prior runs' assignments.
            for d in link.decisions:
                if d.spawned:
                    res.spillover.append(self._spillover_for_spawn(conn, d.cluster, p.link_threshold))

            ts_by_alert = {int(a): float(t) for a, t in zip(data.alert_ids, data.ts)}
            cam_by_alert = {int(a): c for a, c in zip(data.alert_ids, data.camera_ids)}

            if dry_run:
                # Resolve with fake ids so the caller can still inspect membership.
                counter = {"n": 0}

                def _fake(_cl: ClusterSummary) -> int:
                    counter["n"] -= 1
                    return counter["n"]

                res.assignments = resolve_new_ids(link, _fake)
                res.n_assigned = sum(len(v) for v in res.assignments.values())
                conn.rollback()
                res.elapsed_s = time.perf_counter() - t0
                logger.info(res.summary())
                return res

            # ── Single transaction: spawn → assign → refresh → retire → audit ──
            assignments = resolve_new_ids(
                link, lambda cl: self._spawn_rat(conn, cl, ts_by_alert, cam_by_alert)
            )
            with conn.cursor() as cur:
                for rid, members in assignments.items():
                    cur.execute("UPDATE alerts SET rat_id = %s WHERE id = ANY(%s)", (rid, members))
            res.assignments = assignments
            res.n_assigned = sum(len(v) for v in assignments.values())

            # Refresh every rat touched this run PLUS any rat whose members
            # we just re-assigned away (their counts shrank).
            touched = set(assignments)
            touched.update(int(r) for r in data.prior_rat_ids if r is not None)
            res.rats_orphaned = self.refresh_rat_stats(conn, sorted(touched), p.model_version, window_end)

            res.rats_retired = self.retire_stale(conn, window_end, p.retire_days)
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM rats WHERE retired_at IS NULL")
                res.rats_active = int(cur.fetchone()["n"])
                cur.execute(
                    """INSERT INTO rat_cluster_runs
                         (algo, params, window_start, window_end, rats_active, rats_new, rats_retired, notes)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                    (
                        ALGO, Jsonb(p.to_json()), window_start, window_end,
                        res.rats_active, res.rats_new, res.rats_retired,
                        json.dumps({
                            "note": notes,
                            "n_alerts": res.n_alerts, "n_clusters": res.n_clusters,
                            "n_noise": res.n_noise, "n_temporal_rescued": res.n_temporal_rescued,
                            "n_assigned": res.n_assigned,
                            "rats_linked": res.rats_linked, "rats_linked_by_vote": res.rats_linked_by_vote,
                            "rats_merged_in_run": res.rats_merged_in_run, "rats_orphaned": res.rats_orphaned,
                            "spillover": res.spillover,
                        }, default=str),
                    ),
                )
                res.run_id = int(cur.fetchone()["id"])
            conn.commit()

        res.elapsed_s = time.perf_counter() - t0
        logger.info(res.summary())
        return res

    # ── Convenience ───────────────────────────────────────────────────────

    def run_trailing(self, window_days: int, now: datetime | None = None, **kw) -> RunResult:
        """The nightly shape: cluster the last `window_days` up to now."""
        end = _utc(now or datetime.now(timezone.utc))
        return self.run_window(end - timedelta(days=window_days), end, **kw)
