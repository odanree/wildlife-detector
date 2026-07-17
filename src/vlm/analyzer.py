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

_OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "180"))
# Ask Ollama to keep the model resident in VRAM for this long after each call.
# Default "5m" (5 minutes) means idle yards trigger cold-load reload every few
# minutes. Set to "1h" so mid-day gaps between rodent activity don't cost 90s
# of cold-load latency each time.
_OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "1h")


_SYSTEM_PROMPT = (
    "You are a skeptical vision analyst reviewing IR night footage of a yard/patio. "
    "Your job is to detect live rodents (rats, mice) while rejecting the many common "
    "false-positive sources this environment generates. Be conservative — the cost of "
    "a false positive is high (a false alarm every few minutes); the cost of a miss is "
    "low (the same rodent will pass again). Always respond with valid JSON only, no "
    "markdown, no commentary."
)

_USER_PROMPT = (
    "Look at the image and decide whether a LIVE RODENT is clearly visible. Return JSON "
    "with these exact keys:\n"
    "{\n"
    '  "wildlife_detected": <bool>,   // TRUE only if you can see a live rat or mouse with confidence ≥ 0.75\n'
    '  "species":           <string>, // "rat" | "mouse" | "raccoon" | "opossum" | "cat" | "dog" | "squirrel" | "bird" | "other" | "none"\n'
    '  "is_rodent":         <bool>,   // true only for rat or mouse\n'
    '  "confidence":        <0..1>,   // honest visual certainty; use < 0.5 for anything blurry, small, or ambiguous\n'
    '  "description":       <one short sentence naming what you see and why you chose your answer>\n'
    "}\n"
    "\n"
    "REQUIRE ALL of the following before flagging a rodent:\n"
    "  1. A DISTINCT ANIMAL BODY — a rounded torso mass, NOT just a thin line. A rodent "
    "     has visible bulk (head + body), not a uniform-width silhouette.\n"
    "  2. LIMBS or MOTION cues consistent with an animal (four legs, hunched posture, "
    "     scurrying pose). A stationary object with no leg articulation is NOT a rodent.\n"
    "  3. HEAD distinguishable from body — you can identify which end is the front.\n"
    "  4. If you flag rat/mouse, briefly explain in the description which of the three "
    "     conditions above are satisfied.\n"
    "\n"
    "ALWAYS REJECT (wildlife_detected: false, species: none) these common FP sources:\n"
    "  - Twigs, sticks, leaves, leaf stems, dead grass — uniform-width thin dark lines.\n"
    "  - Pieces of string, tape, wire, or cable on a surface.\n"
    "  - Small dark spots on concrete, tile grout lines, cracks, or discoloration.\n"
    "  - Edges of plastic bins, shelves, cabinets, or bags casting a shadow.\n"
    "  - Plastic bag corners, torn fabric, drawstring loops, tarp folds.\n"
    "  - IR sensor noise, JPEG compression artifacts, motion-blur streaks.\n"
    "  - Water droplets, wet spots, dust, insect wings.\n"
    "  - The camera's own IR reflection or lens flare.\n"
    "  - Any object that has not visibly moved between similar frames.\n"
    "\n"
    "OTHER WILDLIFE (cat, dog, raccoon, opossum, squirrel, bird, lizard): these are ALL "
    "acceptable detections. If you can clearly see the animal's body and identify the species, "
    "set wildlife_detected: TRUE with the correct species. The operator reviews species in the "
    "alerts UI. Only 'other' (unclassifiable) or 'none' (nothing there) → wildlife_detected: FALSE.\n"
    "\n"
    "TIE-BREAKERS:\n"
    "  - Bounding box smaller than a fist relative to the scene → likely too small to "
    "    identify reliably; confidence ≤ 0.4 unless a body+head are clearly visible.\n"
    "  - IR night footage of grainy/dark areas → be extra skeptical; halve your confidence.\n"
    "  - Descriptions containing 'possibly', 'appears to be', or 'consistent with' should "
    "    default to wildlife_detected: false — those words mean you are not sure.\n"
    "  - When uncertain, prefer wildlife_detected: false. A missed rodent is fine; a false "
    "    alarm on debris burns operator trust in the system.\n"
)


_COMPARE_USER_PROMPT = (
    "You are given TWO images of the same scene:\n"
    "  IMAGE 1 = the empty baseline. Nothing of interest is in this frame.\n"
    "  IMAGE 2 = the current frame. Motion was detected somewhere between the two.\n"
    "\n"
    "Task: identify what has APPEARED or MOVED in image 2 relative to image 1, and\n"
    "classify only that change. Ignore everything that looks identical in both images.\n"
    "\n"
    "Return JSON with these exact keys:\n"
    "{\n"
    '  "wildlife_detected": <bool>,   // TRUE only if a live, fully-visible rat/mouse\n'
    '  "species":           <string>, // "rat" | "mouse" | "raccoon" | "opossum" | "cat" | "dog" | "squirrel" | "bird" | "other" | "none"\n'
    '  "is_rodent":         <bool>,   // true only for rat or mouse\n'
    '  "confidence":        <0..1>,   // must be < 0.5 for any ambiguous case; < 0.3 if you use hedge words\n'
    '  "description":       <one factual sentence: what changed and where — NO hedging, NO guessing at hidden parts>\n'
    "}\n"
    "\n"
    "═══ TWO ACCEPTABLE EVIDENCE PATTERNS — one must be met for a positive ═══\n"
    "\n"
    "  PATTERN 1 — VISIBLE BODY (daytime, side-on, or well-lit rodent):\n"
    "     All FIVE must be visible: (1) rounded torso mass, (2) at least one leg or paw,\n"
    "     (3) distinguishable head end vs tail end, (4) proportions consistent with a\n"
    "     rat/mouse (not a shoe, not a bag, not debris), (5) the body is IN CONTACT WITH\n"
    "     the ground/floor/a horizontal surface (rodents don't fly). If ANY one is missing\n"
    "     or unclear, Pattern 1 is NOT met.\n"
    "\n"
    "  PATTERN 2 — EYESHINE (night IR, rodent facing the camera):\n"
    "     Nocturnal rodents have a tapetum lucidum that reflects IR strongly. If you see\n"
    "     TWO small, bright, reflective points CLOSE TOGETHER at ground level (typically\n"
    "     < 2 cm apart on a rat, < 1 cm on a mouse), and the surrounding shape is small\n"
    "     and dark-bodied, this IS acceptable evidence — even if the rest of the body\n"
    "     isn't fully visible. Describe the eyeshine specifically (position, spacing).\n"
    "\n"
    "     Rules that distinguish eyeshine from a false positive:\n"
    "       - Two points, roughly symmetric, at approximately the same height.\n"
    "       - Points sit ON or JUST ABOVE the ground/floor (not up on a shelf or wall).\n"
    "       - Spacing is small — a rat's eyes are ~1.5 cm apart, a mouse's ~0.7 cm.\n"
    "       - A single bright point is NOT eyeshine (probably a reflection on hardware).\n"
    "       - Widely-spaced eyes are cat, raccoon, or opossum — species is those, NOT rat.\n"
    "\n"
    "═══ HARD REJECTS (regardless of pattern) ═══\n"
    "\n"
    "  A. NO PARTIAL / OBSCURED RODENTS *WHEN THE EVIDENCE IS BODY-BASED*. If Pattern 1\n"
    "     is your only path and the body is hidden behind cloth/bin/shadow, return false.\n"
    "     Eyeshine (Pattern 2) is a different signal and is allowed to stand alone.\n"
    "\n"
    "  B. NO UNIFORMLY BRIGHT / UNIFORMLY DARK BLOBS. A blob with no internal contrast is\n"
    "     paper, plastic, cloth, or scrap — NOT a rodent. (Eyeshine is TWO small points,\n"
    "     not one large uniform blob — different signature.)\n"
    "\n"
    "  C. No vague pattern-matching. 'Consistent with rodent behavior' without a specific\n"
    "     Pattern 1 anatomy or Pattern 2 eyeshine description → false.\n"
    "\n"
    "═══ COMMON FALSE POSITIVES to explicitly REJECT ═══\n"
    "\n"
    "  - Plastic bags, tarps, sheets of cloth (flapping in wind, drooping edges).\n"
    "  - Twigs, sticks, leaves — thin uniform-width dark shapes.\n"
    "  - Bright reflective objects: paper scraps, tape, foil, tissue, packaging.\n"
    "  - Storage container edges, tarp corners, bag corners (shadows changing).\n"
    "  - IR auto-gain shifts making the whole frame brighter or dimmer.\n"
    "  - Shadow drift (time of day moved on since baseline was captured).\n"
    "  - Camera micro-vibration causing uniform scene shift.\n"
    "  - A shoe, a hand tool head, a piece of hardware on the floor.\n"
    "  - FLYING INSECTS (moths, flies, mosquitoes, gnats) — very common under IR:\n"
    "      * Small bright blobs at ANY height, often mid-air (above ground level).\n"
    "      * Erratic motion — sudden direction changes, appear/disappear frame to frame.\n"
    "      * Chitin + wings reflect IR strongly, causing overexposed white spots.\n"
    "      * No consistent body shape between adjacent frames.\n"
    "      * Rodents ALWAYS move along the ground/floor and follow surfaces (walls, curbs,\n"
    "        the edge of a piece of furniture). If the moving object is airborne, off the\n"
    "        ground, or moves in an erratic curved path, it's an insect — return false.\n"
    "\n"
    "  - CAMERA OSD / WATERMARK / TIMESTAMP OVERLAY — extremely common FP:\n"
    "      * Cameras burn text into the video feed showing the current time, date, and\n"
    "        camera model (e.g. '2026-07-17 Friday 05:22:00', '5MP', 'CAM01').\n"
    "      * The timestamp changes every second, so it looks like a 'change' between\n"
    "        image 1 (baseline) and image 2 (current) — the pixels at that region are\n"
    "        completely different.\n"
    "      * These overlays are ALWAYS in a corner of the frame (usually top-right for\n"
    "        timestamp, bottom-right for camera model) and are NEVER rodents.\n"
    "      * Text characters can look like small elongated shapes under IR — do NOT be\n"
    "        fooled by numeric or alphabetic glyphs.\n"
    "      * If the 'change' between image 1 and image 2 is text/numbers in a corner\n"
    "        of the frame, and the surrounding pixels are identical, return false with\n"
    "        species: none, description: 'OSD text overlay change, not a wildlife event.'\n"
    "\n"
    "  - CAMERA ONBOARD AI DETECTION MARKERS — this camera has its own object detection\n"
    "    that draws boxes and labels ON the video feed:\n"
    "      * Rectangular outlines that are UNIFORM color and UNIFORM thickness (usually\n"
    "        thin lines) — real rodent bodies have varied contrast; camera-drawn boxes\n"
    "        are pixel-perfect rectangles.\n"
    "      * Text labels next to boxes ('Person', 'Motion', 'Object', 'Cat', numeric\n"
    "          confidence like '85%') — these are camera UI, not wildlife.\n"
    "      * Appear/disappear INSTANTLY between frames (real motion is gradual across\n"
    "        multiple frames — a UI overlay is either on or off, no in-between).\n"
    "      * Solid-color icons, arrows, or dots (blue, green, yellow, red) burned into\n"
    "          the pixels — again, camera UI, never wildlife.\n"
    "      * These can appear ANYWHERE in the frame, not just corners — the camera's\n"
    "        AI draws them wherever IT thinks it detected something.\n"
    "      * If the 'change' between image 1 and image 2 is a rectangular outline with\n"
    "        uniform pixel values, or accompanying UI text/icons, return false with\n"
    "        species: none, description: 'Camera-drawn AI overlay, not a wildlife event.'\n"
    "\n"
    "═══ NEGATIVE EXAMPLES — DO NOT DO THIS ═══\n"
    "\n"
    "  BAD:  'A mouse has appeared and is partially obscured by a piece of cloth.'\n"
    "        (You cannot see the mouse. This is inference, not observation. → FALSE)\n"
    "  BAD:  'A rat is visible in the lower right corner.' (with no anatomical detail)\n"
    "        (No head/tail/legs described. → FALSE unless you actually see all four)\n"
    "  BAD:  'The elongated shape is consistent with rodent behavior.'\n"
    "        ('Consistent with' is hedging. → FALSE)\n"
    "\n"
    "  GOOD (positive, Pattern 1): 'A small brown rodent with visible ears, four legs,\n"
    "                    and a long tail is walking left-to-right along the base of the\n"
    "                    workbench.'\n"
    "  GOOD (positive, Pattern 2): 'Two small bright reflective points about 1.5 cm apart\n"
    "                    are visible at ground level near the concrete crack — consistent\n"
    "                    with rat eyeshine. Body outline is dark but the eye spacing\n"
    "                    matches a rat.' (Note: this description WOULD trigger the hedge\n"
    "                    rail on 'consistent with' — so use plain language like 'shows'\n"
    "                    or 'matches' instead. Say what you see, not what it resembles.)\n"
    "  GOOD (negative): 'A rectangular piece of white paper has appeared on the floor;\n"
    "                    it has no legs, head, or tail structure, and no bright reflective\n"
    "                    points visible.'\n"
    "\n"
    "OTHER WILDLIFE (cat, dog, raccoon, opossum, squirrel, bird, lizard): these are ALL\n"
    "acceptable detections. If you can clearly see the animal's body and identify the species,\n"
    "set wildlife_detected: TRUE with the correct species. Only 'other' (unclassifiable) or\n"
    "'none' (nothing there) → wildlife_detected: FALSE. The FP rejects above still apply —\n"
    "cloth/paper/shadow/OSD-text overlays are NEVER any species.\n"
    "\n"
    "When uncertain, wildlife_detected: false. Missing a real animal is fine (they return).\n"
    "A false alarm on cloth/paper/shadow burns operator trust for the whole system.\n"
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

    def _mode_prefix(self, is_daytime: bool | None) -> str:
        """Short leading paragraph that tells the VLM whether it's day or night.
        Prevents the model from invoking eyeshine (Pattern 2) in daytime footage
        or expecting IR-grayscale characteristics in a color frame."""
        if is_daytime is True:
            return (
                "CONTEXT: this is DAYTIME footage. The camera is in visible-light "
                "(color) mode. IR emitters are OFF. Pattern 2 (eyeshine) is "
                "PHYSICALLY IMPOSSIBLE in this frame — do NOT invoke 'eyeshine', "
                "'tapetum', 'bright reflective points', or 'two bright points at "
                "ground level' as evidence. Use Pattern 1 (visible body) only.\n\n"
            )
        if is_daytime is False:
            return (
                "CONTEXT: this is NIGHT IR footage. The camera is grayscale IR. "
                "Pattern 2 (eyeshine) is available when a rodent faces the camera.\n\n"
            )
        return ""

    def analyze(self, image_bytes: bytes | list[bytes],
                is_daytime: bool | None = None) -> dict:
        """Send one or more JPEG frames to the VLM and return the parsed result.

        When two frames are passed, we assume [baseline, current] and swap in
        the baseline-comparison prompt — the VLM is asked to identify what
        appeared or moved between the two images, which sharply reduces
        hallucinated 'small elongated body' FPs on IR night footage.

        is_daytime lets _normalize apply a stricter confidence gate for
        rodents during daytime (rats and mice are nocturnal — a daytime
        rat/mouse call at conf < 0.92 is almost always a FP on a plastic
        scrap or shadow).
        """
        if self._backend == "mock":
            return dict(_MOCK_RESULT)

        frames = image_bytes if isinstance(image_bytes, list) else [image_bytes]
        # Two-frame mode → pass the comparison prompt through as a per-call
        # override so concurrent workers don't race on self._user_prompt.
        base_prompt = _COMPARE_USER_PROMPT if len(frames) == 2 else self._user_prompt
        # Prepend day/night context so the model knows when Pattern 2 is invalid.
        user_prompt = self._mode_prefix(is_daytime) + base_prompt
        try:
            if self._backend == "claude":
                raw = self._analyze_claude(frames, user_prompt=user_prompt)
            else:
                raw = self._analyze_ollama(frames, user_prompt=user_prompt)
            return _normalize(raw, is_daytime=is_daytime)
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
            # 500s from Ollama are usually benign hiccups (inference crash /
            # VRAM pressure / model reloading). Log as a one-line warning; the
            # traceback isn't actionable for the operator.
            logger.warning("VLM HTTP %d (%s) — treating as negative decision",
                           exc.response.status_code, self.model_name)
            fb = _FALLBACK.copy()
            fb["description"] = f"VLM HTTP {exc.response.status_code} — {self.model_name}"
            return fb
        except Exception as exc:
            logger.exception("VLM analysis error (%s)", self.model_name)
            fb = _FALLBACK.copy()
            fb["description"] = f"VLM error — {type(exc).__name__}: {exc}"
            return fb

    def _analyze_claude(self, frames: list[bytes], user_prompt: str | None = None) -> dict:
        prompt_text = user_prompt or self._user_prompt
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
        content.append({"type": "text", "text": prompt_text})
        response = self._claude.messages.create(
            model=self._claude_model,
            max_tokens=512,
            system=[
                {"type": "text", "text": self._system_prompt, "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": content}],
        )
        return _parse_json(response.content[0].text)

    def _analyze_ollama(self, frames: list[bytes], user_prompt: str | None = None) -> dict:
        payload = {
            "model": self._ollama_model,
            "prompt": user_prompt or self._user_prompt,
            "system": self._system_prompt,
            "images": [base64.standard_b64encode(f).decode() for f in frames],
            "stream": False,
            "format": "json",
            "keep_alive": _OLLAMA_KEEP_ALIVE,
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


_VALID_SPECIES = {"rat", "mouse", "raccoon", "opossum", "cat", "dog", "squirrel", "bird", "lizard", "other", "none"}
_RODENT_SPECIES = {"rat", "mouse"}
# Species that CAN trigger a wildlife_detected: true alert. "other" and "none"
# never trigger; "cat/dog/bird/squirrel/lizard/etc." are acceptable when
# clearly identified — the operator sees the species in the alerts UI.
_ALERTABLE_SPECIES = {"rat", "mouse", "raccoon", "opossum", "cat", "dog", "squirrel", "bird", "lizard"}

# Hedge-word hard rail: if the VLM's own description contains any of these
# tokens, we override wildlife_detected to False regardless of what the model
# claimed. Small VLMs (qwen2.5vl:7b, llava:7b) are prone to returning conf=0.95
# alongside descriptions that reveal they didn't actually see the animal.
# Post-processing catches what the prompt fails to prevent.
_HEDGE_TOKENS = [
    "partially", "obscured", "hidden", "peeking",
    "appears to be", "seems to be", "looks like it could",
    "consistent with", "possibly", "might be",
    "behind a", "behind the", "under a", "under the",
    "silhouette of", "outline of what",
]

# Whitelist — descriptions containing any of these tokens bypass the hedge rail.
# Reason: eyeshine (tapetum lucidum IR reflection) is a legitimate rodent signal
# where the ONLY visible evidence is a pair of bright points; the body will
# naturally be "partially" visible or "behind" darkness. Rejecting eyeshine
# because it needs to mention "eyes" alongside language like "small dark body"
# would kill genuine nighttime detections.
_EVIDENCE_WHITELIST = [
    "eyeshine", "eye shine", "eye-shine",
    "tapetum", "reflective points", "reflective eye",
    "bright eye", "eyes glow", "pair of eyes",
    "two bright points", "eye pair",
]


def _description_is_hedged(text: str) -> str | None:
    """Return the first hedge token found in the description, or None if the
    description contains eyeshine evidence (whitelist) or has no hedge tokens.
    """
    if not text:
        return None
    lo = text.lower()
    # Whitelist first — eyeshine descriptions get a free pass on hedge rail.
    if any(w in lo for w in _EVIDENCE_WHITELIST):
        return None
    for tok in _HEDGE_TOKENS:
        if tok in lo:
            return tok
    return None


_DAYTIME_RODENT_MIN_CONF = float(os.getenv("DAYTIME_RODENT_MIN_CONF", "0.95"))
# Tokens that imply IR-eyeshine evidence — physically impossible during
# daytime, when no IR emitter is active. Any of these in the description
# during a daytime detection means the VLM hallucinated.
_EYESHINE_TOKENS = [
    "eyeshine", "eye shine", "eye-shine",
    "tapetum", "reflective points", "reflective eye",
    "bright eye", "eyes glow", "pair of eyes",
    "two bright points",
]


def _normalize(data: dict, is_daytime: bool | None = None) -> dict:
    """Coerce arbitrary VLM output into the canonical shape.

    Also runs the hedge-word hard rail — if the VLM's description reveals
    it inferred rather than observed the animal, we flip the detection to
    False and cap confidence, no matter what the model self-reported.

    is_daytime enables the daytime rodent skepticism gate — rats and mice
    are nocturnal, so a daytime rat/mouse call at confidence below
    DAYTIME_RODENT_MIN_CONF (0.92 default) is treated as a false positive.
    Real daytime rodent sightings ARE possible but rare; false positives
    from plastic scrap / shadows / etc. dominate at lower confidences.
    """
    species = str(data.get("species", "none")).lower().strip()
    if species not in _VALID_SPECIES:
        species = "other"
    # Ground-truth rodent flag from species — ignore what the VLM claims about
    # is_rodent, since small VLMs sometimes mis-set that boolean.
    is_rodent = species in _RODENT_SPECIES
    detected = bool(data.get("wildlife_detected", False))
    conf = data.get("confidence", 0.0)
    try:
        conf_f = max(0.0, min(1.0, float(conf)))
    except (TypeError, ValueError):
        conf_f = 0.0
    description = str(data.get("description", ""))[:500]

    # Species-based hard rail — 'other' and 'none' can NEVER fire an alert,
    # regardless of what the VLM claims (those species labels mean the VLM
    # couldn't identify a specific animal, which we treat as no detection).
    # Everything in _ALERTABLE_SPECIES (rat, mouse, cat, dog, squirrel, bird,
    # lizard, opossum, raccoon) is allowed to fire.
    if detected and species not in _ALERTABLE_SPECIES:
        logger.info("VLM species override: '%s' is not alertable → forcing wildlife_detected=false",
                    species)
        detected = False
        description = f"[non-alertable species: {species}] {description}"

    # Hedge-word hard rail — only apply when the VLM claimed a positive.
    # Negative detections keep their hedged descriptions (they're already false).
    if detected and is_rodent:
        hedge = _description_is_hedged(description)
        if hedge:
            logger.info("VLM hedge-word override: '%s' in description → forcing wildlife_detected=false",
                        hedge)
            detected = False
            conf_f = min(conf_f, 0.30)
            description = f"[hedge-word rejected: '{hedge}'] {description}"

    # Daytime rodent skepticism rail — rats/mice are nocturnal, so daytime
    # calls at moderate confidence are usually FPs on plastic/shadow/etc.
    if detected and is_rodent and is_daytime and conf_f < _DAYTIME_RODENT_MIN_CONF:
        logger.info(
            "VLM daytime-rodent override: species=%s conf=%.2f < %.2f → forcing wildlife_detected=false",
            species, conf_f, _DAYTIME_RODENT_MIN_CONF,
        )
        detected = False
        description = f"[daytime-rodent low-conf: {conf_f:.2f} < {_DAYTIME_RODENT_MIN_CONF:.2f}] {description}"

    # Eyeshine + daytime hard rail — eyeshine (tapetum lucidum reflection)
    # requires an active IR emitter. During color daytime footage there is
    # no IR, so any 'eyeshine / bright reflective points / two bright points'
    # language in a daytime description is a physically-impossible claim.
    # Reject regardless of what the VLM claimed for confidence.
    if detected and is_daytime:
        lo = description.lower()
        for tok in _EYESHINE_TOKENS:
            if tok in lo:
                logger.info(
                    "VLM eyeshine-in-daytime override: '%s' in daytime description → forcing wildlife_detected=false",
                    tok,
                )
                detected = False
                description = f"[eyeshine claimed in daytime (physical impossibility): '{tok}'] {description}"
                break

    return {
        "wildlife_detected": detected,
        "species":           species,
        "is_rodent":         is_rodent,
        "confidence":        conf_f,
        "description":       description,
    }
