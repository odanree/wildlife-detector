import { useCallback, useState } from "react";
import { useSearchParams } from "react-router-dom";

/**
 * All persisted filter state for the alerts table — extracted from
 * AlertsPage during the god-component refactor (#33).
 *
 * Persistence rules per field:
 * - **camera**: URL query-param `?camera=X` takes precedence over the
 *   sticky localStorage value so navigating from a specific pane's
 *   "Alerts →" link lands with that camera pre-filtered. Mutations
 *   sync BOTH URL params and localStorage so the sticky value survives
 *   a page reload without the URL param.
 * - **species / scope / labelFilter**: localStorage only. No URL param
 *   because they're operator-workflow choices, not shareable views.
 * - **autoRefresh / grouped**: session-only (useState default). Reset
 *   on page reload; not worth persisting.
 *
 * Pattern names in play:
 * - **URL-as-primary-key-with-localStorage-fallback** for camera —
 *   deep-linkable + sticky.
 * - **Persistence via handler-owned side effect** for the three
 *   localStorage fields — write happens in the setter, not a
 *   subscribing effect (avoids the effect-as-event-handler
 *   anti-pattern the audit flagged elsewhere).
 */

export type AlertsScope = "historical" | "live" | "all";
export type AlertsLabelFilterUi =
  | "unlabeled"
  | "labeled"
  | "correct"
  | "incorrect"
  | "unclear"
  | "needs-species"
  | "all";

const LABEL_FILTER_VALUES: readonly AlertsLabelFilterUi[] = [
  "unlabeled",
  "labeled",
  "correct",
  "incorrect",
  "unclear",
  "needs-species",
  "all",
];

const SCOPE_VALUES: readonly AlertsScope[] = ["historical", "live", "all"];

export interface AlertsFiltersApi {
  species: string;
  camera: string;
  scope: AlertsScope;
  labelFilter: AlertsLabelFilterUi;
  labelSpecies: string;
  autoRefresh: boolean;
  grouped: boolean;
  /** YYYY-MM-DD, empty = no lower bound. Session-only (not persisted). */
  dateFrom: string;
  /** YYYY-MM-DD end-of-day inclusive, empty = no upper bound. Session-only. */
  dateTo: string;
  setSpecies: (v: string) => void;
  setCamera: (v: string) => void;
  setScope: (v: AlertsScope) => void;
  setLabelFilter: (v: AlertsLabelFilterUi) => void;
  setLabelSpecies: (v: string) => void;
  setAutoRefresh: (v: boolean) => void;
  setGrouped: (v: boolean) => void;
  setDateFrom: (v: string) => void;
  setDateTo: (v: string) => void;
}

function readLocal<T extends string>(key: string, valid: readonly T[], fallback: T): T {
  const v = localStorage.getItem(key);
  return (valid as readonly string[]).includes(v ?? "") ? (v as T) : fallback;
}

export function useAlertsFilters(): AlertsFiltersApi {
  const [urlParams, setUrlParams] = useSearchParams();
  const [species, setSpeciesState] = useState<string>("");
  const [camera, setCameraState] = useState<string>(
    () => urlParams.get("camera") ?? localStorage.getItem("alertsCameraFilter") ?? "",
  );
  const [scope, setScopeState] = useState<AlertsScope>(() =>
    readLocal("alertsScope", SCOPE_VALUES, "all"),
  );
  const [labelFilter, setLabelFilterState] = useState<AlertsLabelFilterUi>(() =>
    readLocal("alertsLabelFilter", LABEL_FILTER_VALUES, "all"),
  );
  const [labelSpecies, setLabelSpeciesState] = useState<string>(
    () => localStorage.getItem("alertsLabelSpecies") ?? "",
  );
  const [grouped, setGrouped] = useState<boolean>(true);
  const [autoRefresh, setAutoRefresh] = useState<boolean>(true);
  // Date range — session-only (not persisted). Date filters are
  // context-specific ("what did the rats do overnight?" today, "what
  // did that raccoon do last Tuesday?" tomorrow) — never something
  // the operator wants stuck across a reload.
  const [dateFrom, setDateFromState] = useState<string>("");
  const [dateTo, setDateToState] = useState<string>("");

  const setSpecies = useCallback((v: string) => setSpeciesState(v), []);

  const setCamera = useCallback(
    (v: string) => {
      setCameraState(v);
      localStorage.setItem("alertsCameraFilter", v);
      // Mirror to URL so a deep-link + reload keeps the same filter.
      // Use functional-form to avoid depending on the current urlParams
      // reference (which changes every render).
      setUrlParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (v) next.set("camera", v);
          else next.delete("camera");
          return next;
        },
        { replace: true },
      );
    },
    [setUrlParams],
  );

  const setScope = useCallback((v: AlertsScope) => {
    setScopeState(v);
    localStorage.setItem("alertsScope", v);
  }, []);

  const setLabelFilter = useCallback((v: AlertsLabelFilterUi) => {
    setLabelFilterState(v);
    localStorage.setItem("alertsLabelFilter", v);
  }, []);

  const setLabelSpecies = useCallback((v: string) => {
    setLabelSpeciesState(v);
    if (v) localStorage.setItem("alertsLabelSpecies", v);
    else localStorage.removeItem("alertsLabelSpecies");
  }, []);

  const setDateFrom = useCallback((v: string) => setDateFromState(v), []);
  const setDateTo = useCallback((v: string) => setDateToState(v), []);

  return {
    species,
    camera,
    scope,
    labelFilter,
    labelSpecies,
    autoRefresh,
    grouped,
    dateFrom,
    dateTo,
    setSpecies,
    setCamera,
    setScope,
    setLabelFilter,
    setLabelSpecies,
    setAutoRefresh,
    setGrouped,
    setDateFrom,
    setDateTo,
  };
}
