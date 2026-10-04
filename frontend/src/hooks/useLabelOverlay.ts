import { useCallback, useState } from "react";
import { type AlertRow, type LabelVerdict, setAlertLabel } from "../api/alerts";

/**
 * Optimistic-UI overlay for label writes — extracted from AlertsPage
 * during the god-component refactor (#33). The most architecturally
 * significant of the four extractions because it encapsulates:
 *
 * 1. **Single-source-of-truth for freshly-applied labels**: a Map keyed
 *    on alert id. Both the table row's LabelPicker and the lightbox
 *    modal read from the same Map so a vote in either place shows
 *    everywhere without waiting for the next useAlerts poll (5s tick).
 *
 * 2. **Optimistic UI with rollback**: `writeLabel` updates the overlay
 *    synchronously → fires setAlertLabel → on error reverts the
 *    overlay to its prior value. Operator sees the vote land
 *    instantly; only a server error backs it out.
 *
 * 3. **Per-alert busy set**: tracks in-flight writes so the LabelPicker
 *    disables its buttons for that row while a request is pending.
 *    Prevents rapid-fire double-clicks from queueing multiple writes.
 *
 * 4. **Bulk apply** for BulkLabelBar — `applyOverlay(rows, verdict, sp)`
 *    updates N rows in one setState. The server-side bulk endpoint
 *    handles the persistence; this hook only mirrors the local view.
 *
 * 5. **Pinned-rows session cache**: every writeLabel/applyOverlay also
 *    stashes the full AlertRow in `pinnedRows`. AlertsPage merges this
 *    set into its render list so a row that was just labeled stays
 *    visible IN PLACE (preserving ts-DESC position) even after the
 *    `label_filter=unlabeled` server response drops it. Lets the
 *    operator backfix a mislabel mid-sift without losing scroll
 *    position. Caller is expected to clear via `clearPinnedRows()`
 *    when it detects a filter change — otherwise cross-filter pins
 *    leak. Session-scoped (lost on reload) by design: active sifting
 *    is single-tab, single-session.
 *
 * Stability:
 * - writeLabel is `useCallback([])` — stable across renders.
 * - Body only touches the setState functions (guaranteed-stable by
 *   React) and reads previous-overlay via **functional update**, not
 *   the closure-captured labelOverlay.
 * - That stability matters: writeLabel is passed as a prop into every
 *   Row (up to 500) and into AlertLightbox's keydown-effect deps; a
 *   fresh closure per render would churn a window listener every
 *   parent re-render (5s poll tick). Fix landed in #37.
 *
 * Pattern name: **optimistic UI with rollback via captured-previous-
 * value** — single hook owns state + side effect; consumers only see
 * the final API (writeLabel), not the plumbing.
 */

export type OverlayEntry = { verdict: LabelVerdict; species: string | null };

export interface LabelOverlayApi {
  /** Map of just-written labels, keyed on alert id. Merged over server
   *  data at render time to give instant-feedback UI without waiting
   *  for the next poll. */
  labelOverlay: Map<number, OverlayEntry>;
  /** Alert ids with a server write in flight — LabelPicker disables
   *  buttons for these. */
  busyIds: Set<number>;
  /** Row-pin session cache: full AlertRow snapshotted at label-time so
   *  the table can keep it rendered in place after a server-side filter
   *  (e.g. `label_filter=unlabeled`) would otherwise drop it. */
  pinnedRows: Map<number, AlertRow>;
  /** Optimistic single-row write: update overlay + fire API + rollback
   *  overlay on error. Also pins the full row into `pinnedRows`. */
  writeLabel: (alert: AlertRow, verdict: LabelVerdict, species: string | null) => Promise<void>;
  /** Bulk apply — mirror a bulk write in the overlay AND pin the rows.
   *  Caller is responsible for the server-side bulk endpoint
   *  (BulkLabelBar handles that itself). */
  applyOverlay: (alerts: AlertRow[], verdict: LabelVerdict, species: string | null) => void;
  /** Drop all pinned rows. Call when the sifting context changes
   *  (filter / scope / camera change) so stale pins don't leak into a
   *  different view. */
  clearPinnedRows: () => void;
}

export function useLabelOverlay(): LabelOverlayApi {
  const [labelOverlay, setLabelOverlay] = useState<Map<number, OverlayEntry>>(() => new Map());
  const [busyIds, setBusyIds] = useState<Set<number>>(() => new Set());
  const [pinnedRows, setPinnedRows] = useState<Map<number, AlertRow>>(() => new Map());

  const applyOverlay = useCallback(
    (alerts: AlertRow[], verdict: LabelVerdict, species: string | null) => {
      setLabelOverlay((prev) => {
        const next = new Map(prev);
        for (const a of alerts) next.set(a.id, { verdict, species });
        return next;
      });
      setPinnedRows((prev) => {
        const next = new Map(prev);
        for (const a of alerts) next.set(a.id, a);
        return next;
      });
    },
    [],
  );

  const writeLabel = useCallback(
    async (alert: AlertRow, verdict: LabelVerdict, species: string | null) => {
      const alertId = alert.id;
      // Capture previous value via functional update so we can roll back
      // on server error without depending on closure-captured overlay.
      let prev: OverlayEntry | undefined;
      setLabelOverlay((cur) => {
        prev = cur.get(alertId);
        const next = new Map(cur);
        next.set(alertId, { verdict, species });
        return next;
      });
      setPinnedRows((cur) => {
        if (cur.has(alertId)) return cur;
        const next = new Map(cur);
        next.set(alertId, alert);
        return next;
      });
      setBusyIds((cur) => {
        const next = new Set(cur);
        next.add(alertId);
        return next;
      });
      try {
        await setAlertLabel(alertId, verdict, species);
      } catch (e) {
        // Roll back overlay so UI reverts and operator knows the write failed.
        setLabelOverlay((cur) => {
          const next = new Map(cur);
          if (prev) next.set(alertId, prev);
          else next.delete(alertId);
          return next;
        });
        window.alert(`Label failed: ${e instanceof Error ? e.message : String(e)}`);
      } finally {
        setBusyIds((cur) => {
          const next = new Set(cur);
          next.delete(alertId);
          return next;
        });
      }
    },
    [],
  );

  const clearPinnedRows = useCallback(() => {
    setPinnedRows((cur) => (cur.size === 0 ? cur : new Map()));
  }, []);

  return { labelOverlay, busyIds, pinnedRows, writeLabel, applyOverlay, clearPinnedRows };
}
