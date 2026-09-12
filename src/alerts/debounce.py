"""Priority-aware alert debounce (closes #203).

The pipeline fires alerts through two branches:

- rodent-positive (species=rat/mouse/rodent, confidence gate passed)
- VLM-reject-override (species=other, "human review" path)

Both share one per-camera wall-clock window (`ALERT_DEBOUNCE_S`). PR #202
gated both branches on a single last-fire timestamp — correct for volume
suppression, but that let a low-value `other` alert **shadow** a real
rodent-positive alert for the full window on the noisiest cameras.

`should_fire()` fixes that by taking the previous alert's priority into
account: a higher-priority (rodent) alert can *upgrade* through the
window even when a lower-priority (`other`) alert is still holding it.
Same-priority repeat suppression is unchanged.

Pure function; no side effects. Caller updates its own state after
firing. All state lives in the pipeline's local scope."""
from __future__ import annotations

# Priority ladder. Higher wins. Rodent-positive alerts represent a
# confirmed animal event and must not be silently suppressed by prior
# reject-override "other" alerts fired for human labeling.
ALERT_PRIORITY_OTHER = 1
ALERT_PRIORITY_RODENT = 2


def should_fire(
    *,
    now: float,
    last_ts: float,
    last_priority: int,
    this_priority: int,
    window_seconds: float,
    is_manual: bool,
) -> bool:
    """True when an alert should fire; False when the debounce blocks it.

    Rules, in order:
      1. window_seconds <= 0 → always fire (feature disabled)
      2. is_manual → always fire (operator-initiated bypass, mirrors
         the notifier's own bypass_cooldown gate)
      3. window has elapsed since last fire → fire (normal path)
      4. this_priority > last_priority → fire (rodent upgrades over
         a prior `other`)
      5. else → debounce, don't fire
    """
    if window_seconds <= 0:
        return True
    if is_manual:
        return True
    if (now - last_ts) >= window_seconds:
        return True
    if this_priority > last_priority:
        return True
    return False
