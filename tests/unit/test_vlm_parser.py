"""Unit tests for the VLM output parser + normalizer."""
from __future__ import annotations

from src.vlm.analyzer import _normalize, _parse_json


class TestParseJson:
    def test_plain_json(self):
        assert _parse_json('{"species": "rat"}') == {"species": "rat"}

    def test_strips_code_fence(self):
        assert _parse_json('```json\n{"species": "rat"}\n```') == {"species": "rat"}

    def test_greedy_brace_match(self):
        text = 'sure, here is your answer: {"species": "mouse", "confidence": 0.8} — hope that helps'
        assert _parse_json(text) == {"species": "mouse", "confidence": 0.8}

    def test_unparseable_falls_back(self):
        result = _parse_json("not json at all")
        assert result["wildlife_detected"] is False
        assert result["species"] == "none"


class TestNormalize:
    def test_rodent_positive(self):
        r = _normalize({"wildlife_detected": True, "species": "rat", "is_rodent": True,
                        "confidence": 0.9, "description": "small elongated body"})
        assert r["wildlife_detected"] is True
        assert r["species"] == "rat"
        assert r["is_rodent"] is True
        assert r["confidence"] == 0.9

    def test_species_inferred_when_missing(self):
        # No is_rodent flag → infer from species
        r = _normalize({"species": "MOUSE", "confidence": 0.8})
        assert r["species"] == "mouse"
        assert r["is_rodent"] is True

    def test_non_rodent_wildlife(self):
        r = _normalize({"species": "raccoon", "confidence": 0.85})
        assert r["is_rodent"] is False
        # wildlife_detected default follows is_rodent when absent
        assert r["wildlife_detected"] is False

    def test_unknown_species_becomes_other(self):
        r = _normalize({"species": "elephant", "confidence": 0.7})
        assert r["species"] == "other"

    def test_confidence_clamped(self):
        assert _normalize({"confidence": 2.5})["confidence"] == 1.0
        assert _normalize({"confidence": -0.3})["confidence"] == 0.0

    def test_bad_confidence_becomes_zero(self):
        assert _normalize({"confidence": "high"})["confidence"] == 0.0

    def test_description_truncated(self):
        r = _normalize({"description": "x" * 1000})
        assert len(r["description"]) == 500
