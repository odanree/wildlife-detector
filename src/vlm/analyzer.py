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
    "You are a vision analyst reviewing IR night and daytime footage from multiple "
    "cameras on the same property: (a) a yard/patio side-angle camera, and (b) a "
    "roof-mounted overhead camera looking DOWN at the ground. Your job is to detect "
    "live wildlife (rats, mice, raccoons, opossums, cats, dogs, squirrels, birds) "
    "while rejecting common false positives.\n"
    "\n"
    "Two important calibrations for this task:\n"
    "  1. Camera angle CHANGES what evidence is available. Side-angle cameras show "
    "     legs/head/tail (Pattern 1) and eyeshine at night (Pattern 2). Overhead "
    "     cameras show only a silhouette from above (Pattern 3) — legs are hidden "
    "     under the body, eyes point sideways not up. Judge each crop by which "
    "     patterns are physically POSSIBLE for its angle.\n"
    "  2. The pipeline in front of you has ALREADY filtered obvious noise (motion "
    "     detection + zone polygon + baseline pixel-diff). If a crop reached you, "
    "     the operator thinks something meaningful appeared. Give credible "
    "     silhouettes the benefit of the doubt with a modest confidence rather "
    "     than reflexively defaulting to 'twig' or 'debris'.\n"
    "\n"
    "Always respond with valid JSON only, no markdown, no commentary."
)

_USER_PROMPT = (
    "Look at the image and decide whether a LIVE RODENT is clearly visible. Return JSON "
    "with these exact keys:\n"
    "{\n"
    '  "wildlife_detected": <bool>,   // TRUE only if you can see a live rat or mouse with confidence ≥ 0.75\n'
    '  "species":           <string>, // "rat" | "mouse" | "raccoon" | "opossum" | "cat" | "dog" | "squirrel" | "bird" | "lizard" | "insect" | "other" | "none"\n'
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
    '  "species":           <string>, // "rat" | "mouse" | "raccoon" | "opossum" | "cat" | "dog" | "squirrel" | "bird" | "lizard" | "insect" | "other" | "none"\n'
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
    "       - **MANDATORY: a traceable dark body outline around/behind the bright\n"
    "         point(s) is REQUIRED for ANY eyeshine claim** — single point or\n"
    "         double, ground-level or elevated. If you cannot trace a discrete\n"
    "         dark shape holding the bright point(s), it is a reflection or\n"
    "         electronic indicator light (LED, camera IR emitter, charger,\n"
    "         reflective screw, tape, water droplet). Reject with 'no traceable\n"
    "         dark body — reflection/LED, not eyeshine' in the description.\n"
    "         Do NOT hedge with 'spaced widely enough to suggest eyeshine' —\n"
    "         if body isn't traceable, the answer is FALSE, no exceptions.\n"
    "       - TWO points, roughly symmetric, at approximately the same height, WITH a\n"
    "         visibly darker body outline around/behind them at ground level — that's a\n"
    "         rodent facing forward. STRONG evidence, confidence 0.7-0.85.\n"
    "       - ONE bright point ONLY qualifies if there is a MEASURABLE dark body\n"
    "         OUTLINE around it — a dark shape at least 3× larger than the bright\n"
    "         point itself, with visible edges distinguishable from the floor.\n"
    "         'The floor is darker than the point' is NOT a dark body — the floor\n"
    "         is EVERYWHERE. What we need is a discrete dark BLOB with edges,\n"
    "         with the bright point sitting INSIDE that blob. If you can't\n"
    "         trace an outline around a dark shape holding the bright point,\n"
    "         the bright point is a moth or reflective debris and MUST be rejected.\n"
    "       - **CLASSIFY AS INSECT: any elongated, streaky, pill-shaped, oblong,\n"
    "         or irregular bright shape with soft edges is a moth in motion blur,\n"
    "         NOT eyeshine.** Eyeshine points are TINY (~2-4 pixels), PIN-SHARP\n"
    "         dots. Anything that's more than 8 pixels long in any direction, or\n"
    "         has soft/blurred edges, or is elongated 2:1 or higher aspect — that\n"
    "         is a moth or wing motion blur, period. This includes 'a small bright\n"
    "         reflective streak'. Return wildlife_detected=true, species='insect',\n"
    "         is_rodent=false, confidence 0.7-0.85 with a description noting the\n"
    "         wing-blur / streak / elongation. (Insect is a count-only category —\n"
    "         classifying it as insect keeps it out of the rodent alert stream.)\n"
    "       - **AMBIGUOUS SINGLE BRIGHT POINT: a single isolated bright spot on\n"
    "         an otherwise clean floor tile, with NO discernible dark body\n"
    "         morphology, is EITHER a moth wing OR an electronic reflector/LED.**\n"
    "         If you can tell it's a wing (soft edges, faint streak) → species=\n"
    "         'insect'. If it's pin-sharp with hard edges and no motion trace →\n"
    "         reject as reflection/LED with species='none'. Do NOT be tempted to\n"
    "         call the surrounding darker floor 'a body' — a body has a discrete\n"
    "         outline distinguishable from the background.\n"
    "       - Motion blur streaks (elongated white shapes with soft edges) are\n"
    "         ALMOST ALWAYS flying insects. Rodents move too slowly to motion-blur\n"
    "         at 15 fps. Any streak — no matter how faint — should classify as\n"
    "         species='insect' per the rule above.\n"
    "       - WIDELY-SPACED eyeshine (~4-8 cm apart on frame) + LARGER dark body\n"
    "         (bigger than a rat, distinct silhouette) = raccoon, opossum, or\n"
    "         cat. STILL a valid wildlife detection — return wildlife_detected=true\n"
    "         with species='raccoon', 'opossum', 'cat', or 'dog' as appropriate,\n"
    "         is_rodent=false, confidence 0.7-0.85. Note the wide eye spacing and\n"
    "         estimated body size in the description.\n"
    "       - A bright point up on a shelf, wall, or hardware with no dark body around it\n"
    "         is a reflection or LED — that's false.\n"
    "\n"
    "  PATTERN 3 — OVERHEAD SILHOUETTE (top-down / roof-mounted camera):\n"
    "     When the camera looks DOWN at the ground from a rooftop, birds-eye angle,\n"
    "     the animal projects as a compact dark shape against a lighter surface\n"
    "     (concrete, tile, dirt). At this angle, Pattern 1's legs/head/tail\n"
    "     articulation and Pattern 2's eyeshine are BOTH invisible — legs are\n"
    "     hidden under the body, eyes point sideways not up. DO NOT reject an\n"
    "     overhead crop for missing those features; they're geometrically\n"
    "     impossible from above.\n"
    "\n"
    "     **CLASSIFY AS INSECT — FLYING INSECT AT OVERHEAD**: The #1 daytime\n"
    "     FP at rooftop is a moth, wasp, or flying insect passing through the\n"
    "     frame. Insects have DISTINCTIVE visual traits that mammals never share\n"
    "     — return wildlife_detected=true, species='insect', is_rodent=false,\n"
    "     confidence 0.7-0.85 ONLY when ALL THREE of these apply, in combination:\n"
    "       1. Shape is BRIGHT or PALE (white/tan/light-grey), not a dark\n"
    "          silhouette. Real mammals from overhead are consistently dark\n"
    "          against lighter ground; moths reflect IR strongly.\n"
    "       2. Shape is streaked, elongated, blurry, wing-shaped, or has\n"
    "          soft/fuzzy edges from wing motion blur. Compact mammal bodies\n"
    "          have crisp edges even in motion.\n"
    "       3. Shape has NO visible cast shadow on the ground beneath it —\n"
    "          it looks like it's floating on top of the surface rather than\n"
    "          sitting on it. Real mammals cast a small shadow directly under\n"
    "          them in overhead IR.\n"
    "     ALL THREE must be present to classify as insect. A dark compact\n"
    "     shape with crisp edges — even if small and lacking visible legs —\n"
    "     is a mammal (raccoon/cat/possum) at overhead, NOT a moth. Overhead\n"
    "     mammals routinely appear as 40-100 px dark blobs without countable\n"
    "     legs; that is expected and MUST NOT be labeled as insect.\n"
    "\n"
    "     At overhead angle in IR footage, a real rat / mouse commonly appears as\n"
    "     one of the following legitimate silhouettes — treat ALL of these as\n"
    "     positive evidence:\n"
    "       - A dark ELONGATED shape with a rounded 'head' end and a thinner\n"
    "         trailing tail (comma or teardrop). Body ~2-4x wider than the tail.\n"
    "       - A dark line where the body-to-tail transition is SUBTLE — the whole\n"
    "         thing may look nearly uniform width at low crop resolution. This is\n"
    "         normal for a small rodent overhead in IR; DO NOT auto-reject as\n"
    "         a twig on width alone.\n"
    "       - A single compact dark blob (rat coiled or facing straight down)\n"
    "         with slight curvature at ground level.\n"
    "\n"
    "     Motion is the primary rodent-vs-debris discriminator. If TWO frames are\n"
    "     provided (baseline + current):\n"
    "       - Shape APPEARED between baseline and current, or MOVED to a new\n"
    "         position → rodent, high confidence (0.6-0.8).\n"
    "       - Shape identical in both frames, same position → stationary debris,\n"
    "         reject (twig, stick, leaf on ground).\n"
    "\n"
    "     If only ONE frame is provided (probe testing, no baseline available):\n"
    "       - Give the shape the BENEFIT OF THE DOUBT if it has any of the\n"
    "         silhouettes above and its length is 5-25% of the frame width.\n"
    "         Return true with confidence 0.4-0.6 and species='rat' (or 'mouse'\n"
    "         if smaller) and say the motion evidence is unavailable in the\n"
    "         description so the operator knows it's a single-frame call.\n"
    "       - Only reject a single-frame overhead shape if it is CLEARLY attached\n"
    "         to vegetation, embedded in clutter, or has right-angle joints\n"
    "         (planks, screws, hardware).\n"
    "\n"
    "     Sizing / species:\n"
    "       - Shape < 3% of frame width in longest dimension → probably a leaf\n"
    "         piece or insect. Reject unless motion is unambiguous.\n"
    "       - Shape 3-8% of frame width → mouse (species='mouse').\n"
    "       - Shape 8-25% of frame width → rat (species='rat').\n"
    "       - Shape > 25% of frame width → cat/raccoon/opossum. Still return\n"
    "         wildlife_detected=true with the appropriate larger species.\n"
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
    "  D. **HUMAN HARD REJECT.** If the crop contains a human — foot, shoe, hand,\n"
    "     arm, leg, torso, clothing (denim, sock, sleeve, jacket, hoodie),\n"
    "     tool being wielded (broom, hose, flashlight), or any body part or\n"
    "     accessory that unambiguously belongs to a person — return\n"
    "     wildlife_detected=false, species='none', is_rodent=false,\n"
    "     description=\"human in frame — not wildlife\". This overrides ALL other\n"
    "     patterns. A rat-shaped shadow next to a shoe is a shoe. A dark blob\n"
    "     under a pant leg is fabric. When any part of a human is visible, the\n"
    "     entire crop is not wildlife regardless of what else the model 'sees'\n"
    "     nearby. This rule exists because the property has occasional foot\n"
    "     traffic (owner, gardener, delivery); alerting on people would burn\n"
    "     operator trust immediately.\n"
    "\n"
    "═══ COMMON FALSE POSITIVES — reject only when the fit is clear ═══\n"
    "\n"
    "  You are the confirmation stage — a motion detector and pixel-diff baseline filter\n"
    "  have already agreed something meaningful changed in this crop. Assume the crop\n"
    "  is worth looking at. Trust your own vision on debris/insect/lighting artifacts;\n"
    "  the two overlay families below are the ones you MUST catch specifically:\n"
    "\n"
    "  - FLYING INSECT: a bright small blob clearly in mid-air (above ground line),\n"
    "    or with wing-like white halos, or with erratic non-ground-following motion.\n"
    "    A dark shape on the ground with any eyeshine is NOT this — it's a rodent.\n"
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
    "═══ EXAMPLES ═══\n"
    "\n"
    "  POSITIVE (Pattern 1): 'A small brown rodent with visible ears, four legs, and a\n"
    "     long tail is walking left-to-right along the base of the workbench.'\n"
    "  POSITIVE (Pattern 2, both eyes): 'Two small bright reflective points about 1.5 cm\n"
    "     apart are visible at ground level near the concrete crack — rat eyeshine.\n"
    "     The surrounding dark body outline matches a rat.'\n"
    "  POSITIVE (Pattern 2, one eye, partial angle): 'A small dark ground-level shape with\n"
    "     one bright reflective point is visible at center-floor — rodent turned slightly\n"
    "     away, only one eye catching the IR. Body proportions match a rat.'\n"
    "  NEGATIVE: 'A rectangular piece of white paper has appeared on the floor; no legs,\n"
    "     head, or tail structure, and no reflective points inside a dark body.'\n"
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

        # Reusable HTTP client for Ollama backend. Sharing one Client
        # across calls avoids the per-call `with httpx.Client(...)`
        # allocation pattern that leaked ~7GB / hour on yard: at
        # ~4000 VLM calls/hour, each transient client's socket pool +
        # DNS cache + auth state added up (Python's allocator doesn't
        # return freed heap to the OS, so fragmentation looked like a
        # memory leak). Also cuts TCP handshake cost.
        self._ollama_http: httpx.Client | None = (
            httpx.Client(timeout=_OLLAMA_TIMEOUT) if backend not in ("claude", "mock") else None
        )

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
        mode_prefix = self._mode_prefix(is_daytime)
        user_prompt = mode_prefix + base_prompt
        try:
            if self._backend == "claude":
                # Split static-bulk (base_prompt) from per-call-dynamic (mode_prefix)
                # so the static bulk can join the cached system prompt — otherwise
                # Anthropic prompt caching silently ignores our tiny system prompt
                # (below 1024-token threshold) and every call pays full input rate.
                # For 1-frame calls, _user_prompt alone is only ~700 tokens; even
                # combined with _system_prompt (~290) that's below the 1024 floor,
                # so cache silently no-ops. Always ship _COMPARE_USER_PROMPT as the
                # cacheable bulk — it's a superset of the rules Sonnet uses in
                # either mode, and clears the cache floor unconditionally.
                cacheable = _COMPARE_USER_PROMPT
                raw = self._analyze_claude(
                    frames, user_prompt=mode_prefix, cacheable_bulk=cacheable,
                )
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

    @staticmethod
    def _sniff_media_type(data: bytes) -> str:
        """Detect image mime by magic bytes so callers can pass PNG (probe
        harness, saved diagnostic crops) or JPEG (pipeline norm) without
        having to convert. Falls back to JPEG when unknown — Anthropic's API
        rejects the mismatch loudly if we guess wrong."""
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith(b"GIF8"):
            return "image/gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        return "image/jpeg"

    def _analyze_claude(
        self,
        frames: list[bytes],
        user_prompt: str | None = None,
        cacheable_bulk: str | None = None,
    ) -> dict:
        """Send frames + prompts to Claude.

        user_prompt: per-call dynamic text (mode prefix, day/night context).
            Stays in the user content block since it changes across calls.
        cacheable_bulk: static rules text (Pattern 1/2/3, hard-rejects, species
            rules) that would otherwise be sent per-call as user content. Moved
            into the system prompt block so Anthropic prompt caching can hold
            it — our _SYSTEM_PROMPT alone is only ~290 tokens, well below the
            1024-token cache floor. Combined with the bulk (~3300 tokens),
            the system prompt clears the threshold and cache_read hits fire.
        """
        prompt_text = user_prompt or self._user_prompt
        content: list[dict] = []
        for fb in frames:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": self._sniff_media_type(fb),
                    "data": base64.standard_b64encode(fb).decode(),
                },
            })
        content.append({"type": "text", "text": prompt_text})

        # Build cacheable system: base + static rules bulk. Cache_control on
        # the LAST block only — Anthropic caches everything up to that marker.
        system_blocks: list[dict] = [{"type": "text", "text": self._system_prompt}]
        if cacheable_bulk:
            system_blocks.append({"type": "text", "text": cacheable_bulk})
        system_blocks[-1]["cache_control"] = {"type": "ephemeral"}

        # Extended thinking (default on for Sonnet-5+) adds ~3x cost for no
        # observable quality gain on this visual-classification task — the
        # prompt is a decision tree, not a reasoning problem. Disable
        # explicitly so we get a plain TextBlock response.
        response = self._claude.messages.create(
            model=self._claude_model,
            max_tokens=512,
            thinking={"type": "disabled"},
            system=system_blocks,
            messages=[{"role": "user", "content": content}],
        )
        # Verify prompt caching is actually working — cache_read_input_tokens
        # should be >0 on the 2nd+ call within the ephemeral cache TTL (5 min).
        # If it's stuck at 0, the system prompt isn't being cached and we're
        # paying full input rates on every call.
        _u = getattr(response, "usage", None)
        if _u is not None:
            _tin  = getattr(_u, "input_tokens", 0)
            _tcr  = getattr(_u, "cache_read_input_tokens", 0) or 0
            _tcc  = getattr(_u, "cache_creation_input_tokens", 0) or 0
            _tout = getattr(_u, "output_tokens", 0)
            logger.info(
                "VLM tokens: input=%d cache_read=%d cache_create=%d output=%d",
                _tin, _tcr, _tcc, _tout,
            )
            # Feed the aggregate cost tracker on Stats. Lazy import to avoid
            # a circular dependency (preview → pipeline → vlm → preview).
            # Missing/broken preview shouldn't crash a VLM call — swallow.
            try:
                from src.web.preview import stats as _preview_stats
                _preview_stats.record_vlm_tokens(
                    model=self._claude_model,
                    input_tok=_tin,
                    cache_read=_tcr,
                    cache_create=_tcc,
                    output_tok=_tout,
                )
            except Exception:
                pass
        # Sonnet-5+ / Opus with extended thinking may return ThinkingBlock
        # entries before the TextBlock — filter to text blocks only so we
        # don't AttributeError on the thinking preamble.
        text = next(
            (b.text for b in response.content if getattr(b, "type", None) == "text"),
            None,
        )
        if text is None:
            raise RuntimeError(f"No text block in Claude response (blocks={[type(b).__name__ for b in response.content]})")
        return _parse_json(text)

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
        assert self._ollama_http is not None, "OllamaAnalyzer used without ollama backend"
        r = self._ollama_http.post(f"{self._ollama_url}/api/generate", json=payload)
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


_VALID_SPECIES = {"rat", "mouse", "raccoon", "opossum", "cat", "dog", "squirrel", "bird", "lizard", "insect", "other", "none"}
_RODENT_SPECIES = {"rat", "mouse"}
# Species that CAN trigger a wildlife_detected: true alert. "insect" is
# DELIBERATELY EXCLUDED: moths/wasps/flies are the #1 nighttime FP source
# for rodent detection, and classifying them separately (rather than
# rejecting them) frees the rodent pipeline from their noise. But we do
# NOT want a moth alert per night event — insect classification is a
# count-only signal visible in DECISION log lines + funnel stats.
# The normalize() gate at line 754 forces wildlife_detected=false for
# non-alertable species, so no alert row is written for insects. That's
# the count-only behavior by design.
_ALERTABLE_SPECIES = {"rat", "mouse", "raccoon", "opossum", "cat", "dog", "squirrel", "bird", "lizard", "other"}
_INSECT_SPECIES = {"insect"}

# Hedge-word hard rail: if the VLM's own description contains any of these
# tokens, we override wildlife_detected to False regardless of what the model
# claimed. Small VLMs (qwen2.5vl:7b, llava:7b) are prone to returning conf=0.95
# alongside descriptions that reveal they didn't actually see the animal.
# Post-processing catches what the prompt fails to prevent.
_HEDGE_TOKENS = [
    # True uncertainty markers — model is genuinely unsure.
    "partially", "obscured", "hidden", "peeking",
    "appears to be", "seems to be", "looks like it could",
    "possibly", "might be",
    "behind a", "behind the", "under a", "under the",
    "silhouette of", "outline of what",
    # Note: "consistent with" was removed 2026-07-19 — Opus-tier models use it
    # as standard technical positive language ("body-plus-tail consistent
    # with a rat viewed from above"), not as hedging. Rejecting on it killed
    # correct overhead identifications during the rooftop rat replay eval.
]

# Whitelist — descriptions containing any of these tokens bypass the hedge rail.
# Reason: eyeshine (tapetum lucidum IR reflection) is a legitimate rodent signal
# where the ONLY visible evidence is a pair of bright points; the body will
# naturally be "partially" visible or "behind" darkness. Rejecting eyeshine
# because it needs to mention "eyes" alongside language like "small dark body"
# would kill genuine nighttime detections. Same for overhead identifications
# where "trailing tail" is the load-bearing evidence.
_EVIDENCE_WHITELIST = [
    "eyeshine", "eye shine", "eye-shine",
    "tapetum", "reflective points", "reflective eye",
    "bright eye", "eyes glow", "pair of eyes",
    "two bright points", "eye pair",
    # Overhead-silhouette evidence tokens (Pattern 3):
    "trailing tail", "trailing behind", "tail-like extension",
    "rounded torso", "rounded body", "body mass",
    "overhead silhouette", "overhead view", "viewed from above",
    "from above",
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

    # Species-based hard rail — only 'none' (nothing there) can NEVER fire.
    # 'other' (VLM saw an animal but couldn't confidently ID species) IS
    # alertable — the core detector job is "wildlife present or not",
    # species is nice-to-have. Camouflaged raccoons in brush frequently
    # come back as 'other' when Sonnet/Opus refuses to guess a species
    # they can't discriminate; those detections are still valuable.
    # Everything else in _ALERTABLE_SPECIES (rat, mouse, cat, dog, squirrel,
    # bird, lizard, opossum, raccoon, other) is allowed to fire.
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


class CascadeVLMAnalyzer:
    """Two-stage VLM cascade — cheap primary, strict confirm.

    Same shape as parking-enforcement-detector's Ollama-primary + Claude-confirm.
    Primary catches the recall (all rats); confirm catches the precision
    (rejects primary FPs like water reflections). Confirm only runs on primary
    positives so cost is bounded by positive rate, not call rate.

    Public interface matches VLMAnalyzer so pipeline can hold either polymorphically:
    - analyze(frames, is_daytime) -> dict
    - model_name property
    - _backend attribute (used by stats)
    """

    def __init__(self, primary: "VLMAnalyzer", confirm: "VLMAnalyzer") -> None:
        self._primary = primary
        self._confirm = confirm
        self._backend = "cascade"
        logger.info(
            "VLM backend: Cascade — primary=%s, confirm=%s",
            primary.model_name, confirm.model_name,
        )

    @property
    def model_name(self) -> str:
        return f"cascade({self._primary.model_name} → {self._confirm.model_name})"

    def analyze(self, image_bytes: bytes | list[bytes],
                is_daytime: bool | None = None) -> dict:
        # Stage 1: primary. Cheap, high recall.
        primary_result = self._primary.analyze(image_bytes, is_daytime)
        if not primary_result.get("wildlife_detected"):
            # Primary rejected — done, no confirm call.
            return primary_result

        # Stage 2: confirm. Runs only on primary positives.
        confirm_result = self._confirm.analyze(image_bytes, is_daytime)
        primary_species = primary_result.get("species", "?")
        confirm_species = confirm_result.get("species", "?")

        if not confirm_result.get("wildlife_detected"):
            # Silent FP suppression — primary said rat, confirm disagreed.
            # Log the disagreement for later review; return negative verdict but
            # keep confirm's description so operators can see WHY it was killed.
            logger.info(
                "CASCADE-REJECT: primary said %s (conf=%.2f), confirm said %s (conf=%.2f) — %s",
                primary_species, primary_result.get("confidence", 0),
                confirm_species, confirm_result.get("confidence", 0),
                confirm_result.get("description", "")[:120],
            )
            fp = _FALLBACK.copy()
            fp["description"] = (
                f"[CASCADE-REJECT] primary '{primary_species}' rejected by "
                f"confirm: {confirm_result.get('description', '')[:200]}"
            )
            return fp

        # Both agreed — return confirm's authoritative verdict (usually stricter).
        logger.info(
            "CASCADE-CONFIRM: primary %s (conf=%.2f), confirm %s (conf=%.2f) — both positive",
            primary_species, primary_result.get("confidence", 0),
            confirm_species, confirm_result.get("confidence", 0),
        )
        return confirm_result


def build_vlm_analyzer_from_env() -> "VLMAnalyzer | CascadeVLMAnalyzer":
    """Env-driven factory. Returns a plain VLMAnalyzer for single-backend modes,
    or a CascadeVLMAnalyzer when VLM_BACKEND=cascade.

    Cascade env:
      CASCADE_PRIMARY_BACKEND   (default: ollama)
      CASCADE_PRIMARY_MODEL     (default: OLLAMA_MODEL)
      CASCADE_CONFIRM_BACKEND   (default: claude)
      CASCADE_CONFIRM_MODEL     (default: CLAUDE_MODEL)
    """
    backend = os.getenv("VLM_BACKEND", "claude")
    claude_model = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
    ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
    ollama_model = os.getenv("OLLAMA_MODEL", "llava:7b-v1.6-mistral-q4_K_M")

    if backend != "cascade":
        return VLMAnalyzer(
            backend=backend, claude_model=claude_model,
            ollama_url=ollama_url, ollama_model=ollama_model,
        )

    primary_backend = os.getenv("CASCADE_PRIMARY_BACKEND", "ollama")
    primary_model = os.getenv("CASCADE_PRIMARY_MODEL", ollama_model)
    confirm_backend = os.getenv("CASCADE_CONFIRM_BACKEND", "claude")
    confirm_model = os.getenv("CASCADE_CONFIRM_MODEL", claude_model)

    primary = VLMAnalyzer(
        backend=primary_backend, claude_model=confirm_model,
        ollama_url=ollama_url, ollama_model=primary_model,
    )
    confirm = VLMAnalyzer(
        backend=confirm_backend, claude_model=confirm_model,
        ollama_url=ollama_url, ollama_model=primary_model,
    )
    return CascadeVLMAnalyzer(primary=primary, confirm=confirm)
