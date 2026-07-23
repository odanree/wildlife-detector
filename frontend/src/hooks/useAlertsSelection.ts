import { useCallback, useState } from "react";

/**
 * Bulk-selection state for the alerts table — checkbox column + select-
 * all in header. Extracted from AlertsPage during the god-component
 * refactor (#33) so the ~15 LOC of Set-manipulation logic doesn't
 * clutter the page's state block.
 *
 * All three callbacks are memoized with `useCallback([])` — stable
 * across renders because they only touch the stable `setSelectedIds`
 * setter via functional updates. That stability matters when they
 * flow through the render tree into Row components (which pass them
 * to individual row checkboxes) — a fresh closure per parent render
 * would defeat any React.memo optimization further down.
 *
 * Pattern name: **single-owner-of-state + stable-dispatcher** — the
 * hook owns the Set, exposes derived readouts + pure event handlers,
 * consumers never reach in and mutate directly.
 */
export interface AlertsSelectionApi {
  selectedIds: Set<number>;
  isSelected: (id: number) => boolean;
  toggleOne: (id: number) => void;
  clear: () => void;
  setAll: (ids: readonly number[]) => void;
  size: number;
}

export function useAlertsSelection(): AlertsSelectionApi {
  const [selectedIds, setSelectedIds] = useState<Set<number>>(() => new Set());

  const isSelected = useCallback((id: number) => selectedIds.has(id), [selectedIds]);

  const toggleOne = useCallback((id: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const clear = useCallback(() => setSelectedIds(new Set()), []);

  const setAll = useCallback((ids: readonly number[]) => setSelectedIds(new Set(ids)), []);

  return { selectedIds, isSelected, toggleOne, clear, setAll, size: selectedIds.size };
}
