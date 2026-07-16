"""Vision-language model client for wildlife detection.

Two backends:
  • claude — Anthropic claude-haiku-4-5-20251001 via the Messages API.
             System prompt is cache-controlled so repeated calls are cheaper.
  • ollama — Local LLaVA-style multimodal model via Ollama REST API.
             Use a 4-bit or 8-bit quantised 7B for < 2 s/frame.
  • mock   — Always returns wildlife_detected=True. Use to verify the alert
             chain without a real VLM or credits.

Returns a dict:
    {
        "wildlife_detected": bool,
        "species":           str,     # best-guess species (rat/mouse/raccoon/opossum/cat/dog/unknown)
        "is_rodent":         bool,
        "confidence":        float,   # 0.0–1.0
        "description":       str,
    }

Rodent-focused prompt with open-vocab species reporting — asks primarily
"is this a rodent?" but names other wildlife when it appears, so the yard
site accumulates a species breakdown over time without needing a broader
prompt from day one.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re

import anthropic
import httpx

logger = logging.getLogger(__name__)

_OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "120"))


_SYSTEM_PROMPT = (
    "You are a vision analyst examining short-range yard-surveillance frames. "
    "Your primary job is to detect rodents (rats, mice). Report other wildlife "
    "if visible so the deployment accumulates baseline data on what moves "
    "through the yard, but the wildlife_detected flag is set only for rodents. "
    "Always respond with valid JSON only, no markdown, no commentary."
)

_USER_PROMPT = (
    "Analyze the frames for a rodent (rat, mouse) or other visible wildlife.\n"
    "Return JSON with these exact keys:\n"
    "{\n"
    '  "wildlife_detected": <bool>,   // set true ONLY for a live rodent (rat, mouse)\n'
    '  "species":           <string>, // best guess: "rat", "mouse", "raccoon", "opossum", "cat", "dog", "squirrel", "bird", "other", "none"\n'
    '  "is_rodent":         <bool>,   // true for rat OR mouse; false otherwise\n'
    '  "confidence":        <0..1>,   // your visual certainty in species\n'
    '  "description":       <one short sentence: location, motion, distinguishing feature>\n'
    "}\n"
    "Notes:\n"
    "- Cats, squirrels, birds, insects, raccoons, opossums are NOT rodents — report species but set wildlife_detected: false.\n"
    "- Shadows, leaves, drifting debris, and moving fabric are NOT wildlife — return species: none.\n"
    "- A small elongated body with a visible tail is the strongest rodent signal.\n"
    "- If ambiguous, prefer wildlife_detected: false with a description explaining the ambiguity.\n"
)


_FALLBACK = {
    "wildlife_detected": False,
    "species":           "none",
    "is_rodent":         False,
    "confidence":        0.0,
    "description":       "VLM analysis failed",
}

_MOCK_RESULT = {
    "wildlife_detected": True,
    "species":           "rat",
    "is_rodent":         True,
    "confidence":        0.86,
    "description":       "Small elongated body with visible tail moving low along the ground.",
}


class VLMAnalyzer:
    def __init__(
        self,
        backend: str = "claude",
        claude_model: str = "claude-haiku-4-5-20251001",
        ollama_url: str = "http://localhost:11434",
        ollama_model: str = "llava:7b-v1.6-mistral-q4_K_M",
        user_prompt: str | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self._backend = backend
        self._ollama_url = ollama_url.rstrip("/")
        self._ollama_model = ollama_model
        self._user_prompt = user_prompt or _USER_PROMPT
        self._system_prompt = system_prompt or _SYSTEM_PROMPT

        if backend == "claude":
            self._claude = anthropic.Anthropic()
            self._claude_model = claude_model
            logger.info("VLM backend: Claude (%s)", claude_model)
        elif backend == "mock":
            self._claude = None
            logger.warning("VLM backend: MOCK — every call returns a positive rodent result")
        else:
            self._claude = None
            logger.info("VLM backend: Ollama (%s @ %s)", ollama_model, ollama_url)

    @property
    def model_name(self) -> str:
        if self._backend == "claude":
            return self._claude_model
        if self._backend == "mock":
            return "mock"
        return self._ollama_model

    def analyze(self, image_bytes: bytes | list[bytes]) -> dict:
        """Send one or more JPEG frames to the VLM and return the parsed result."""
        if self._backend == "mock":
            return dict(_MOCK_RESULT)

        frames = image_bytes if isinstance(image_bytes, list) else [image_bytes]
        try:
            if self._backend == "claude":
                raw = self._analyze_claude(frames)
            else:
                raw = self._analyze_ollama(frames)
            return _normalize(raw)
        except httpx.TimeoutException:
            logger.warning("VLM timeout (%s): >120 s", self.model_name)
            fb = _FALLBACK.copy()
            fb["description"] = f"VLM timeout — {self.model_name} took >120 s"
            return fb
        except httpx.ConnectError:
            logger.warning("VLM unreachable (%s @ %s)", self.model_name, self._ollama_url)
            fb = _FALLBACK.copy()
            fb["description"] = f"VLM unreachable — {self._ollama_url}"
            return fb
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.warning("VLM model not found — run: ollama pull %s", self._ollama_model)
                fb = _FALLBACK.copy()
                fb["description"] = f"Model not found — run: ollama pull {self._ollama_model}"
                return fb
            logger.exception("VLM HTTP error (%s)", self.model_name)
            fb = _FALLBACK.copy()
            fb["description"] = f"VLM HTTP {exc.response.status_code} — {self.model_name}"
            return fb
        except Exception as exc:
            logger.exception("VLM analysis error (%s)", self.model_name)
            fb = _FALLBACK.copy()
            fb["description"] = f"VLM error — {type(exc).__name__}: {exc}"
            return fb

    def _analyze_claude(self, frames: list[bytes]) -> dict:
        content: list[dict] = []
        for fb in frames:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.standard_b64encode(fb).decode(),
                },
            })
        content.append({"type": "text", "text": self._user_prompt})
        response = self._claude.messages.create(
            model=self._claude_model,
            max_tokens=512,
            system=[
                {"type": "text", "text": self._system_prompt, "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": content}],
        )
        return _parse_json(response.content[0].text)

    def _analyze_ollama(self, frames: list[bytes]) -> dict:
        payload = {
            "model": self._ollama_model,
            "prompt": self._user_prompt,
            "system": self._system_prompt,
            "images": [base64.standard_b64encode(f).decode() for f in frames],
            "stream": False,
            "format": "json",
        }
        with httpx.Client(timeout=_OLLAMA_TIMEOUT) as c:
            r = c.post(f"{self._ollama_url}/api/generate", json=payload)
            r.raise_for_status()
            data = r.json()
        return _parse_json(data.get("response", "{}"))


def _parse_json(text: str) -> dict:
    """Extract the first JSON object from a VLM response, or return _FALLBACK."""
    text = text.strip()
    # Strip common code-fence noise
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to a greedy brace-match
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    logger.warning("VLM returned unparseable JSON: %r", text[:200])
    return _FALLBACK.copy()


_VALID_SPECIES = {"rat", "mouse", "raccoon", "opossum", "cat", "dog", "squirrel", "bird", "other", "none"}
_RODENT_SPECIES = {"rat", "mouse"}


def _normalize(data: dict) -> dict:
    """Coerce arbitrary VLM output into the canonical shape."""
    species = str(data.get("species", "none")).lower().strip()
    if species not in _VALID_SPECIES:
        species = "other"
    is_rodent = bool(data.get("is_rodent", species in _RODENT_SPECIES))
    detected = bool(data.get("wildlife_detected", is_rodent))
    conf = data.get("confidence", 0.0)
    try:
        conf_f = max(0.0, min(1.0, float(conf)))
    except (TypeError, ValueError):
        conf_f = 0.0
    return {
        "wildlife_detected": detected,
        "species":           species,
        "is_rodent":         is_rodent,
        "confidence":        conf_f,
        "description":       str(data.get("description", ""))[:500],
    }
