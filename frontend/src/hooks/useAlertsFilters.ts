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
  | "all";

const LABEL_FILTER_VALUES: readonly AlertsLabelFilterUi[] = [
  "unlabeled",
  "labeled",
  "correct",
  "incorrect",
  "unclear",
  "all",
];

const SCOPE_VALUES: readonly AlertsScope[] = ["historical", "live", "all"];

export interface AlertsFiltersApi {
  species: string;
  camera: string;
  scope: AlertsScope;
  labelFilter: AlertsLabelFilterUi;
  autoRefresh: boolean;
  grouped: boolean;
  setSpecies: (v: string) => void;
  setCamera: (v: string) => void;
  setScope: (v: AlertsScope) => void;
  setLabelFilter: (v: AlertsLabelFilterUi) => void;
  setAutoRefresh: (v: boolean) => void;
  setGrouped: (v: boolean) => void;
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
  const [grouped, setGrouped] = useState<boolean>(true);
  const [autoRefresh, setAutoRefresh] = useState<boolean>(true);

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

  return {
    species,
    camera,
    scope,
    labelFilter,
    autoRefresh,
    grouped,
    setSpecies,
    setCamera,
    setScope,
    setLabelFilter,
    setAutoRefresh,
    setGrouped,
  };
}
