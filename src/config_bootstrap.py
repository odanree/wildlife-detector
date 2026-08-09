"""Config file bootstrap — copy example → real on first startup.

detection.yaml serves two conflicting roles:
  1. Shipped defaults (starting config for a fresh clone)
  2. Runtime-mutable state (operator polygons, zone edits written via
     the UI)

Making the file BOTH git-tracked AND app-mutable means git operations
(`reset --hard`, `checkout` across branches, `clean -fd`) can silently
clobber runtime state. Bit us Aug 9 when a stray `git reset --hard`
during a branch-recovery workflow wiped drawn slew polygons.

Fix: git-ignore the actual file; ship a `<name>.example` counterpart
with defaults; on container startup copy example→real if missing.
Runtime edits go to the ignored file, safe from git ops forever.

Pattern: **git-ignored runtime config with tracked template** — same
shape as `.env` + `.env.example`, `settings.local.json` +
`settings.json.example`, etc. The template is docs; the real file is
state.

Idempotency: only copies when the real file is absent. Never
overwrites existing state (even if example has drifted).
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def ensure_from_example(target: str | Path) -> Path:
    """If `target` is missing but `target.example` exists, copy it.

    Returns the resolved target path either way. Safe to call from
    every container's startup — first-caller-wins semantics via
    plain-file existence check (races are benign: same-content copy).
    """
    tgt = Path(target)
    if tgt.exists():
        return tgt
    example = tgt.with_suffix(tgt.suffix + ".example")
    if not example.exists():
        # Nothing to copy — let the caller fail on its own read attempt
        # with the proper "file not found" message.
        logger.warning(
            "config_bootstrap: %s missing and %s also absent — no template to copy",
            tgt, example,
        )
        return tgt
    tgt.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(example, tgt)
    logger.info("config_bootstrap: copied %s → %s (first startup)", example, tgt)
    return tgt


def ensure_detection_config() -> Path:
    """Convenience wrapper for the well-known detection.yaml path."""
    return ensure_from_example("config/detection.yaml")
