import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

const SECONDARY_STORAGE_KEY = "livePreview.secondaryCamera";

/**
 * Secondary-pane orchestration for the live-preview page — extracted
 * from LivePreviewPage during the god-component refactor (#34).
 *
 * Owns:
 * - Persisted `secondary` camera id (or null when the pane is closed).
 * - Handlers to add / remove / re-select / promote the pane.
 * - Collision-clear effect: closes the pane if secondary ends up
 *   matching primary (e.g. swap-in-another-tab, or a promote that
 *   would otherwise duplicate).
 *
 * Persistence design:
 * - **URL as source of truth for primary** — bookmarkable, shareable.
 *   The consumer owns URL routing; this hook only reads it via
 *   useSearchParams for the `promoteSecondary` swap.
 * - **localStorage for secondary** — per-viewer preference, not
 *   shareable. Written via **single-writer wrapper** so the sync
 *   lives in the mutation, not in a subscribing effect (avoids the
 *   effect-as-event-handler anti-pattern per PR #37's cleanup).
 *
 * The collision-clear effect IS a legitimate reactive effect —
 * cross-tab writes to localStorage could push primary and secondary
 * into agreement, and we need to react by closing the pane. Not the
 * same category as sync-via-effect.
 */
export interface SecondaryPaneApi {
  /** null when pane is closed, otherwise the secondary camera id. */
  secondary: string | null;
  /** True when a secondary can be added — needs ≥2 cameras and no
   *  existing secondary. */
  canAdd: boolean;
  /** Open a new secondary pane, defaulting to the first camera that
   *  isn't primary. */
  add: () => void;
  /** Close the secondary pane. */
  remove: () => void;
  /** Change the secondary camera to `c`. No-op if `c` is primary. */
  select: (c: string) => void;
  /** Swap primary ↔ secondary. Primary gets the new value via URL
   *  update (consumer's useSearchParams re-reads it). Secondary
   *  takes the old primary. */
  promote: () => void;
}

export function useSecondaryPane(cameras: readonly string[], primary: string): SecondaryPaneApi {
  const [, setSearchParams] = useSearchParams();
  const [secondary, setSecondaryState] = useState<string | null>(() => {
    try {
      return localStorage.getItem(SECONDARY_STORAGE_KEY);
    } catch {
      return null;
    }
  });

  // Single-writer wrapper: every mutation of `secondary` goes through
  // this. The localStorage sync lives in the mutation, not in a
  // subscribing effect — writing via effect is the effect-as-event-
  // handler anti-pattern.
  const setSecondary = useCallback((v: string | null) => {
    setSecondaryState(v);
    try {
      if (v) localStorage.setItem(SECONDARY_STORAGE_KEY, v);
      else localStorage.removeItem(SECONDARY_STORAGE_KEY);
    } catch {
      // localStorage quota or disabled — the pane still works this session.
    }
  }, []);

  // Collision-clear: same camera on both panes is a UX bug (e.g.
  // cross-tab swap). This IS a legitimate reactive effect, not
  // sync-via-effect — it reacts to primary changing and closes the
  // pane accordingly.
  useEffect(() => {
    if (secondary && secondary === primary) setSecondary(null);
  }, [primary, secondary, setSecondary]);

  const canAdd = cameras.length >= 2 && !secondary;

  const add = useCallback(() => {
    const next = cameras.find((c) => c !== primary);
    if (next) setSecondary(next);
  }, [cameras, primary, setSecondary]);

  const remove = useCallback(() => setSecondary(null), [setSecondary]);

  const select = useCallback(
    (c: string) => {
      if (c === primary) return;
      setSecondary(c);
    },
    [primary, setSecondary],
  );

  const promote = useCallback(() => {
    if (!secondary) return;
    const oldPrimary = primary;
    setSearchParams({ camera: secondary });
    setSecondary(oldPrimary);
  }, [primary, secondary, setSearchParams, setSecondary]);

  return { secondary, canAdd, add, remove, select, promote };
}
