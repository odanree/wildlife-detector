"""Centroid-matching linker — deterministic input → deterministic rat_id.

Pure numpy; no Postgres, no hdbscan. The DB write path is exercised by
scripts/backfill_rat_ids.py against the compose stack.
"""
from __future__ import annotations

import numpy as np
import pytest

from src.clusterer.linker import (
    ClusterSummary,
    KnownRat,
    link_clusters,
    resolve_new_ids,
    summarize_clusters,
    unit_centroid,
)

D = 8


def _unit(seed: int, dim: int = D) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.normal(size=dim).astype(np.float32)
    return v / np.linalg.norm(v)


def _near(base: np.ndarray, noise: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = base + noise * rng.normal(size=base.shape).astype(np.float32)
    return (v / np.linalg.norm(v)).astype(np.float32)


def _cluster(label: int, members: list[int], centroid: np.ndarray) -> ClusterSummary:
    return ClusterSummary(label=label, member_ids=members, centroid=centroid.astype(np.float32))


# ── unit_centroid / summarize_clusters ────────────────────────────────


def test_unit_centroid_is_unit_norm_and_direction_of_mean():
    a, b = _unit(1), _unit(2)
    c = unit_centroid(np.stack([a, b]))
    assert pytest.approx(1.0, abs=1e-6) == float(np.linalg.norm(c))
    m = (a + b) / 2
    assert np.allclose(c, m / np.linalg.norm(m), atol=1e-6)


def test_unit_centroid_rejects_empty():
    with pytest.raises(ValueError):
        unit_centroid(np.zeros((0, D), dtype=np.float32))


def test_summarize_drops_noise_and_sorts_members():
    base = _unit(7)
    vecs = np.stack([_near(base, 0.05, i) for i in range(5)])
    labels = np.array([0, -1, 0, 1, 1])
    ids = np.array([50, 10, 30, 20, 40])
    out = summarize_clusters(labels, ids, vecs)
    assert [c.label for c in out] == [0, 1]
    assert out[0].member_ids == [30, 50]      # sorted, noise id 10 absent
    assert out[1].member_ids == [20, 40]
    assert all(pytest.approx(1.0, abs=1e-5) == float(np.linalg.norm(c.centroid)) for c in out)


# ── link_clusters ─────────────────────────────────────────────────────


def test_matches_existing_rat_above_threshold():
    base = _unit(11)
    known = [KnownRat(id=5, centroid=base)]
    cl = _cluster(0, [1, 2, 3], _near(base, 0.05, 1))
    res = link_clusters([cl], known, threshold=0.85)
    d = res.decisions[0]
    assert d.rat_id == 5 and not d.spawned
    assert d.similarity >= 0.85
    assert res.n_linked == 1 and res.n_new == 0


def test_spawns_when_below_threshold():
    known = [KnownRat(id=5, centroid=_unit(11))]
    cl = _cluster(0, [1, 2, 3], _unit(99))  # unrelated direction
    res = link_clusters([cl], known, threshold=0.85)
    d = res.decisions[0]
    assert d.rat_id is None and d.spawned
    assert d.matched_rat_id == -1  # provisional
    assert res.n_new == 1 and res.n_linked == 0


def test_no_known_rats_first_spawn_has_nan_similarity_later_ones_compare_to_it():
    res = link_clusters([_cluster(0, [1, 2, 3], _unit(1)), _cluster(1, [4, 5, 6], _unit(2))], [], 0.85)
    assert res.n_new == 2
    first, second = res.decisions
    assert np.isnan(first.similarity)            # nothing to compare against
    assert not np.isnan(second.similarity)       # compared to the provisional rat -1
    assert second.similarity < 0.85 and second.spawned


def test_fragment_attaches_to_rat_spawned_earlier_in_same_run():
    base = _unit(3)
    big = _cluster(0, [1, 2, 3, 4, 5], base)
    frag = _cluster(1, [6, 7, 8], _near(base, 0.05, 2))
    res = link_clusters([frag, big], [], threshold=0.85)  # input order irrelevant
    # largest-first: `big` spawns -1, `frag` links to -1
    assert [d.cluster.label for d in res.decisions] == [0, 1]
    assert res.decisions[0].spawned and res.decisions[0].matched_rat_id == -1
    assert not res.decisions[1].spawned and res.decisions[1].matched_rat_id == -1
    assert res.n_new == 1 and res.n_merged_in_run == 1
    # resolve → one real rat, all 8 alerts
    ids = iter([101])
    assigned = resolve_new_ids(res, lambda _cl: next(ids))
    assert assigned == {101: [1, 2, 3, 4, 5, 6, 7, 8]}


def test_many_to_one_two_clusters_may_link_same_existing_rat():
    base = _unit(5)
    known = [KnownRat(id=9, centroid=base)]
    a = _cluster(0, [1, 2, 3], _near(base, 0.05, 1))
    b = _cluster(1, [4, 5, 6], _near(base, 0.05, 2))
    res = link_clusters([a, b], known, 0.85)
    assert [d.rat_id for d in res.decisions] == [9, 9]
    assigned = resolve_new_ids(res, lambda _cl: pytest.fail("no spawn expected"))
    assert assigned == {9: [1, 2, 3, 4, 5, 6]}


def test_ties_resolve_to_lowest_rat_id():
    base = _unit(21)
    known = [KnownRat(id=40, centroid=base.copy()), KnownRat(id=12, centroid=base.copy())]
    res = link_clusters([_cluster(0, [1, 2, 3], base)], known, 0.85)
    assert res.decisions[0].rat_id == 12


def test_deterministic_across_calls_and_input_order():
    rng = np.random.default_rng(0)
    bases = [_unit(s) for s in range(4)]
    known = [KnownRat(id=i + 1, centroid=bases[i]) for i in range(2)]
    clusters = []
    for label in range(6):
        b = bases[label % 4]
        members = sorted(int(x) for x in rng.choice(1000, size=3 + label, replace=False))
        clusters.append(_cluster(label, members, _near(b, 0.05, 100 + label)))

    def run(order):
        res = link_clusters([clusters[i] for i in order], known, 0.85)
        counter = iter(range(1000, 2000))
        return resolve_new_ids(res, lambda _cl: next(counter))

    a = run(range(6))
    b = run(reversed(range(6)))
    c = run([3, 0, 5, 1, 4, 2])
    assert a == b == c
    # rats 1,2 reused; bases[2], bases[3] spawn exactly two new ids
    assert set(a) == {1, 2, 1000, 1001}


def test_threshold_validation():
    with pytest.raises(ValueError):
        link_clusters([], [], threshold=1.5)


# ── member vote (step 0) ──────────────────────────────────────────────


def test_member_vote_beats_drifted_centroid():
    """Re-run after centroid drift: the rat's stored centroid has moved
    far from this cluster, but the cluster's alerts already carry its
    id → inherit via vote, never spawn a phantom."""
    drifted = _unit(77)                                    # unrelated direction
    known = [KnownRat(id=5, centroid=drifted)]
    cl = _cluster(0, [1, 2, 3, 4], _unit(78))
    prior = {1: 5, 2: 5, 3: 5, 4: 5}
    res = link_clusters([cl], known, 0.93, prior=prior)
    d = res.decisions[0]
    assert d.rat_id == 5 and d.via == "vote" and d.votes == 4 and not d.spawned
    assert d.similarity < 0.93                              # centroid alone would have spawned
    assert res.n_linked_by_vote == 1 and res.n_new == 0


def test_member_vote_majority_and_ties_to_lowest_id():
    known = [KnownRat(id=3, centroid=_unit(1)), KnownRat(id=9, centroid=_unit(2))]
    cl = _cluster(0, [1, 2, 3, 4, 5, 6], _unit(3))
    res = link_clusters([cl], known, 0.93, prior={1: 9, 2: 9, 3: 3, 4: 3, 5: 3})
    assert res.decisions[0].rat_id == 3                     # 3 votes vs 2
    res = link_clusters([cl], known, 0.93, prior={1: 9, 2: 9, 3: 3, 4: 3})
    assert res.decisions[0].rat_id == 3                     # 2-2 tie → lowest id


def test_member_vote_floor_falls_through_to_centroid():
    base = _unit(4)
    known = [KnownRat(id=5, centroid=_unit(77)), KnownRat(id=8, centroid=base)]
    cl = _cluster(0, list(range(1, 13)), _near(base, 0.05, 1))  # 12 members
    # One stray prior vote (< MIN_PRIOR_VOTES) → ignored; centroid links to rat 8.
    res = link_clusters([cl], known, 0.85, prior={1: 5})
    assert res.decisions[0].rat_id == 8 and res.decisions[0].via == "centroid"
    # Two votes but < 25% of 12 members → still ignored.
    res = link_clusters([cl], known, 0.85, prior={1: 5, 2: 5})
    assert res.decisions[0].rat_id == 8 and res.decisions[0].via == "centroid"
    # Three votes = 25% → vote wins.
    res = link_clusters([cl], known, 0.85, prior={1: 5, 2: 5, 3: 5})
    assert res.decisions[0].rat_id == 5 and res.decisions[0].via == "vote"


def test_member_vote_preserves_identity_of_retired_rat():
    """Re-running an OLD window after a later one retired the rat: its
    alerts still say 'rat 42' → keep 42 (not in `known` = retired), do
    not mint a phantom. Regression for the 2026-09 rooftop case."""
    cl = _cluster(0, [1, 2, 3, 4], _unit(9))
    res = link_clusters([cl], [], 0.93, prior={1: 42, 2: 42, 3: 42, 4: 42})
    d = res.decisions[0]
    assert d.rat_id == 42 and d.via == "vote" and not d.spawned
    assert np.isnan(d.similarity)  # no centroid available for a retired rat
    assert res.n_new == 0


def test_idempotent_rerun_reattaches_to_own_rats():
    """Simulates window re-run: rats spawned in run 1 (with centroid ==
    cluster centroid) must be re-matched, not re-spawned, in run 2."""
    clusters = [_cluster(i, [i * 10 + j for j in range(4)], _unit(50 + i)) for i in range(3)]
    run1 = link_clusters(clusters, [], 0.85)
    counter = iter([1, 2, 3])
    assigned1 = resolve_new_ids(run1, lambda _cl: next(counter))
    known = [KnownRat(id=rid, centroid=next(c.centroid for c in clusters if c.member_ids == members))
             for rid, members in assigned1.items()]
    run2 = link_clusters(clusters, known, 0.85)
    assert run2.n_new == 0
    assigned2 = resolve_new_ids(run2, lambda _cl: pytest.fail("re-run must not spawn"))
    assert assigned2 == assigned1
