"""Cluster → rat_id linker. Pure numpy, no DB, fully deterministic.

## The problem

HDBSCAN returns integer labels that mean nothing across runs: cluster 3
tonight can be cluster 7 tomorrow, or split in two. A `rat_id` that
reshuffles nightly is useless to the operator, so identity has to be
carried by something that DOES persist — the cluster's centroid.

## The rule

For each cluster this run (largest first — the biggest cluster is the
best estimate of an identity, fragments attach to it):

  0. member vote — if enough of the cluster's alerts ALREADY carry a
     rat_id (a re-run of this window, or the overlap region between two
     backfill windows), inherit the majority rat. This is direct
     evidence ("these very alerts were rat 7 last time") and beats any
     centroid heuristic. Floor: ≥ 2 votes and ≥ 25% of members, so one
     stray alert can't drag a cluster onto the wrong identity.
  1. centroid = unit-normalised mean of the members' unit vectors
  2. best     = argmax cosine(centroid, known_rat.centroid) over the
                candidate set
  3. cosine >= threshold  →  same rat (reuse its id)
     otherwise           →  spawn a new rat, and ADD it to the candidate
                            set for the remaining clusters of this run

Step 0 is what makes re-runs idempotent in practice. Two things move
between runs that a pure centroid rule is blind to:

* **retirement** — a later window retires a rat (30 days idle); re-
  running an EARLIER window re-clusters that rat's alerts, finds it
  absent from the active candidate set, and mints a phantom while the
  retired row is left orphaned. Observed on the first forced re-run of
  the 2026-09 backfill (rooftop rat, 5 alerts: 1 phantom + 1 orphan out
  of 21). The vote therefore counts prior ids that point at RETIRED rats
  too — identity is preserved, retirement status is untouched.
* **centroid drift** — rats.centroid is recomputed from ALL members
  after every run (derived state), so once a later window links more
  alerts into a rat its centroid moves and an unchanged earlier cluster
  can fall under the threshold. Not observed yet at 0.93; the vote
  closes it anyway.

With the vote, re-running a window never re-decides identities the
alerts already carry.

Step 3's "add to candidates" is deliberate: if HDBSCAN over-splits one
rat into clusters A and B whose centroids are >= threshold apart-ness,
they merge to one id in the same run — exactly as they would have if
they'd shown up in consecutive runs. Identity semantics must not depend
on which run a fragment happens to land in.

Many clusters may link to ONE rat within a run (many-to-one). We do not
enforce one-to-one because the failure mode we care about is over-
splitting (one animal → several clusters), not two animals collapsing
into one — CLIP on 30-80 px IR crops splits far more readily than it
merges. One-to-one would turn every split fragment into a phantom rat.

## Determinism

Same clusters + same known rats + same threshold → same decisions, byte
for byte. Ordering is (-size, label); ties in similarity resolve to the
lowest known-rat id. New ids are handed out by the caller in decision
order, so the caller's id allocation is deterministic too.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import numpy as np

# 0.93, not the 0.85 the Phase 2b brief proposed — calibrated against the
# measured centroid-vs-centroid cosine between DISTINCT same-camera
# clusters (median 0.91-0.94). See clusterer.py "Parameter calibration".
DEFAULT_LINK_THRESHOLD = 0.93

# Member-vote floor: at least this many already-assigned members AND at
# least this fraction of the cluster must agree before identity is
# inherited without a centroid check.
MIN_PRIOR_VOTES = 2
MIN_PRIOR_FRAC = 0.25


@dataclass(frozen=True)
class KnownRat:
    """A rat already in the catalog. `centroid` is unit-norm float32."""

    id: int
    centroid: np.ndarray


@dataclass
class ClusterSummary:
    """One HDBSCAN cluster from this run."""

    label: int
    member_ids: list[int]
    centroid: np.ndarray

    @property
    def size(self) -> int:
        return len(self.member_ids)


@dataclass
class LinkDecision:
    cluster: ClusterSummary
    rat_id: int | None          # existing catalog id (>0), or None when the
                                # target is a rat spawned in THIS run
    matched_rat_id: int | None  # best candidate id; negative = provisional
                                # (spawned this run), None = no candidates
    similarity: float           # cosine to the best candidate (nan if none)
    spawned: bool = False       # True only for the decision that CREATED
                                # the provisional id; fragments that attach
                                # to it later in the run are not "new"
    via: str = "centroid"       # "vote" | "centroid" | "spawn"
    votes: int = 0              # member votes behind a "vote" decision

    @property
    def is_new(self) -> bool:
        return self.spawned


@dataclass
class LinkResult:
    decisions: list[LinkDecision] = field(default_factory=list)

    @property
    def n_new(self) -> int:
        """Distinct rats spawned this run."""
        return sum(1 for d in self.decisions if d.spawned)

    @property
    def n_linked(self) -> int:
        """Clusters that attached to a rat that existed BEFORE this run."""
        return sum(1 for d in self.decisions if d.rat_id is not None)

    @property
    def n_merged_in_run(self) -> int:
        """Clusters that attached to a rat spawned earlier in this run
        (HDBSCAN over-split, healed by the linker)."""
        return sum(1 for d in self.decisions if d.rat_id is None and not d.spawned)

    @property
    def n_linked_by_vote(self) -> int:
        """Clusters that inherited identity from their already-assigned members."""
        return sum(1 for d in self.decisions if d.via == "vote")


def unit_centroid(vectors: np.ndarray) -> np.ndarray:
    """Unit-normalised mean of (N, D) row vectors. Mean of unit vectors
    is NOT unit-length; renormalising keeps cosine comparisons honest."""
    v = np.asarray(vectors, dtype=np.float32)
    if v.ndim != 2 or v.shape[0] == 0:
        raise ValueError("unit_centroid needs a non-empty (N, D) array")
    c = v.mean(axis=0)
    n = float(np.linalg.norm(c))
    if n == 0.0:
        # Degenerate (antipodal members). Fall back to the first member
        # so the caller still gets a valid direction.
        c = v[0].copy()
        n = float(np.linalg.norm(c)) or 1.0
    return (c / n).astype(np.float32)


def summarize_clusters(
    labels: np.ndarray, alert_ids: np.ndarray, vectors: np.ndarray
) -> list[ClusterSummary]:
    """Group HDBSCAN output into ClusterSummary objects. Noise (label -1)
    is dropped here — those alerts keep whatever rat_id they had (NULL
    for a fresh row). member_ids are sorted so the summary is a pure
    function of the (label, alert_id) set, not of row order."""
    labels = np.asarray(labels)
    alert_ids = np.asarray(alert_ids)
    out: list[ClusterSummary] = []
    for label in sorted(int(lb) for lb in set(labels.tolist()) if lb >= 0):
        mask = labels == label
        members = sorted(int(a) for a in alert_ids[mask])
        out.append(ClusterSummary(label=label, member_ids=members, centroid=unit_centroid(vectors[mask])))
    return out


def _next_new_id(known: list[KnownRat]) -> int:
    """Provisional negative ids for rats spawned within this run so they
    can be candidates for later clusters. The DB layer maps them to real
    BIGSERIAL ids in decision order (see clusterer.py)."""
    return -1 - sum(1 for k in known if k.id < 0)


def _vote(cl: ClusterSummary, prior: dict[int, int]) -> tuple[int, int] | None:
    """(rat_id, votes) if the cluster's already-assigned members agree
    strongly enough; else None. Ties → lowest rat id. Retired rats count:
    the alerts ARE that rat, whatever its activity status."""
    votes = Counter(prior[a] for a in cl.member_ids if a in prior)
    total = sum(votes.values())
    if total < MIN_PRIOR_VOTES or total < MIN_PRIOR_FRAC * cl.size:
        return None
    rat_id, n = min(votes.items(), key=lambda kv: (-kv[1], kv[0]))
    return rat_id, n


def link_clusters(
    clusters: list[ClusterSummary],
    known: list[KnownRat],
    threshold: float = DEFAULT_LINK_THRESHOLD,
    prior: dict[int, int] | None = None,
) -> LinkResult:
    """Assign each cluster to an existing rat (member vote, else centroid
    cosine >= threshold), to a rat spawned earlier in this run
    (provisional NEGATIVE id, -1, -2, ...), or spawn a new one.

      decision.rat_id          → existing catalog id (>0), else None
      decision.matched_rat_id  → best candidate id (negative = provisional)
      decision.similarity      → cosine to that candidate
      decision.spawned         → this decision created the provisional id
      decision.via             → "vote" | "centroid" | "spawn"

    `prior` maps alert_id → rat_id as stored BEFORE this run. Ids that
    point at rats NOT in `known` (retired) still count — see the module
    docstring. `resolve_new_ids` turns provisional ids into real ones in
    decision order, so the caller's id allocation is deterministic too.
    """
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"threshold must be in [0, 1], got {threshold}")
    candidates: list[KnownRat] = sorted(known, key=lambda k: k.id)
    by_id = {k.id: k for k in candidates}
    prior = prior or {}
    result = LinkResult()
    # Largest first; label breaks ties so the order is total.
    for cl in sorted(clusters, key=lambda c: (-c.size, c.label)):
        voted = _vote(cl, prior) if prior else None
        if voted is not None:
            rid, n = voted
            k = by_id.get(rid)
            sim = float(k.centroid.astype(np.float32) @ cl.centroid.astype(np.float32)) if k else float("nan")
            result.decisions.append(
                LinkDecision(cluster=cl, rat_id=rid, matched_rat_id=rid, similarity=sim, via="vote", votes=n)
            )
            continue
        if candidates:
            mat = np.stack([k.centroid for k in candidates]).astype(np.float32)
            sims = mat @ cl.centroid.astype(np.float32)
            # argmax returns the FIRST max → lowest id wins ties (sorted above).
            best_i = int(np.argmax(sims))
            best_sim = float(sims[best_i])
            best_id = candidates[best_i].id
        else:
            best_sim, best_id = float("nan"), None

        if best_id is not None and best_sim >= threshold:
            result.decisions.append(
                LinkDecision(cluster=cl, rat_id=best_id if best_id > 0 else None,
                             matched_rat_id=best_id, similarity=best_sim, via="centroid")
            )
            continue

        new_id = _next_new_id(candidates)
        candidates.append(KnownRat(id=new_id, centroid=cl.centroid))
        candidates.sort(key=lambda k: k.id)
        result.decisions.append(
            LinkDecision(cluster=cl, rat_id=None, matched_rat_id=new_id,
                         similarity=best_sim, spawned=True, via="spawn")
        )
    return result


def resolve_new_ids(result: LinkResult, allocate: "callable") -> dict[int, list[int]]:
    """Turn a LinkResult into {rat_id: [alert_ids]} with real ids.

    `allocate(cluster)` is called once per distinct provisional id, in
    decision order, and must return the real (positive) id — e.g. an
    INSERT ... RETURNING id. Clusters that linked to a provisional id
    (a rat spawned earlier in this run) resolve through the same mapping,
    so a fragment attaches to the rat its sibling created.
    """
    provisional: dict[int, int] = {}
    assignments: dict[int, list[int]] = {}
    for d in result.decisions:
        if d.rat_id is not None:
            rid = d.rat_id
        else:
            pid = d.matched_rat_id
            assert pid is not None and pid < 0, "spawn decision must carry a provisional id"
            if pid not in provisional:
                provisional[pid] = int(allocate(d.cluster))
            rid = provisional[pid]
        assignments.setdefault(rid, []).extend(d.cluster.member_ids)
    for rid in assignments:
        assignments[rid] = sorted(set(assignments[rid]))
    return assignments
