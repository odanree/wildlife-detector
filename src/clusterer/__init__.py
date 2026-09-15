"""Rat re-identification Phase 2b — clusterer + rat_id lifecycle.

Consumes `alert_embeddings` (Phase 2a) and populates `alerts.rat_id`,
`rats`, `rat_cluster_runs`. See src/clusterer/clusterer.py for the design.
"""
