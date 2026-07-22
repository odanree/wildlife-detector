import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { type AlertRow, type LabelVerdict, setAlertLabel } from "../api/alerts";
import { AlertLightbox } from "../components/AlertLightbox";
import { BulkLabelBar } from "../components/BulkLabelBar";
import { GlobalHeader } from "../components/GlobalHeader";
import { LabelPicker } from "../components/LabelPicker";
import { ReplayButton } from "../components/ReplayButton";
import { useAlerts } from "../hooks/useAlerts";
import { useCameras } from "../hooks/useCameras";
import { markAlertsSeen, readLastSeenId } from "../hooks/useUnreadAlerts";
import { fmtRelative, fmtTs } from "../util/time";
import styles from "./AlertsPage.module.css";

const RODENT_SPECIES = new Set(["rat", "mouse"]);
const GROUP_WINDOW_S = 60;

interface GroupedAlerts {
  head: AlertRow;
  children: AlertRow[];
}

/**
 * Alerts page. Ported to CSS Modules in PR 8 — dynamic species color
 * is now a modifier class per row (speciesHist / speciesRodent /
 * speciesOther) rather than an inline color prop, which keeps the
 * hover row-highlight rule reachable and stops the row style from
 * fighting the cell style.
 */
export function AlertsPage() {
  const [species, setSpecies] = useState<string>("");
  // URL `?camera=` takes precedence over the sticky localStorage filter
  // so navigating from a specific pane's "Alerts →" link lands with that
  // camera pre-filtered (matches the badge scope the user just saw).
  const [urlParams, setUrlParams] = useSearchParams();
  const [camera, setCamera] = useState<string>(
    () => urlParams.get("camera") ?? localStorage.getItem("alertsCameraFilter") ?? "",
  );
  const [grouped, setGrouped] = useState<boolean>(true);
  const [autoRefresh, setAutoRefresh] = useState<boolean>(true);
  const [openId, setOpenId] = useState<number | null>(null);
  // Labeling-workflow filter: "historical" restricts to backfilled/pre-tuning
  // rows so training-data collection focuses on the noisy old pile without
  // getting mixed with today's cleaner live alerts. Persisted so page
  // reloads don't yank the operator out of a labeling session.
  const [scope, setScope] = useState<"historical" | "live" | "all">(() => {
    const v = localStorage.getItem("alertsScope");
    return v === "historical" || v === "live" ? v : "all";
  });
  // Sifting filter: 'unlabeled' hides rows already voted on so operator
  // walks the backlog without re-reviewing their own work. Composes with
  // scope — e.g. scope=historical + labelFilter=unlabeled = "un-voted
  // slice of the old pile", the training-data-hunt workflow.
  type LabelFilterVal = "unlabeled" | "labeled" | "correct" | "incorrect" | "unclear" | "all";
  const [labelFilter, setLabelFilter] = useState<LabelFilterVal>(() => {
    const v = localStorage.getItem("alertsLabelFilter");
    const valid: LabelFilterVal[] = ["unlabeled", "labeled", "correct", "incorrect", "unclear"];
    return (valid as string[]).includes(v ?? "") ? (v as LabelFilterVal) : "all";
  });

  // Bulk-selection state — checkbox column + select-all in header lets
  // operator mass-label N alerts with one Apply click (backend does the
  // update in a single IN(?,?,?) transaction).
  const [selectedIds, setSelectedIds] = useState<Set<number>>(() => new Set());
  const clearSelection = () => setSelectedIds(new Set());
  const toggleOne = (id: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  // Local overlay of labels — writes go to the server via writeLabel below,
  // but the useAlerts polling refresh is on a 5s tick, so we mirror the
  // last write locally to give an instant visual response. Merged onto
  // the row via effectiveLabel() when rendering. LabelPicker + lightbox
  // are now fully controlled — this Map is the single source of truth
  // for freshly-applied labels; the picker never keeps its own copy.
  const [labelOverlay, setLabelOverlay] = useState<
    Map<number, { verdict: LabelVerdict; species: string | null }>
  >(() => new Map());
  const applyLabelOverlay = (ids: number[], verdict: LabelVerdict, species: string | null) => {
    setLabelOverlay((prev) => {
      const next = new Map(prev);
      for (const id of ids) next.set(id, { verdict, species });
      return next;
    });
  };
  // Per-row busy set — tracks in-flight server writes so the LabelPicker
  // in that row disables its buttons while a request is pending. Prevents
  // rapid-fire double-clicks from queueing multiple writes for one alert.
  const [busyIds, setBusyIds] = useState<Set<number>>(() => new Set());
  const setBusy = (id: number, on: boolean) => {
    setBusyIds((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  };
  // Single owner of label state + server write. Optimistic overlay update
  // followed by async setAlertLabel; on failure, roll back the overlay to
  // whatever it was before (or delete the key if we didn't have one).
  // This is the API that LabelPicker's onChange callbacks pipe through —
  // no other component should call setAlertLabel directly.
  const writeLabel = async (alertId: number, verdict: LabelVerdict, species: string | null) => {
    const prev = labelOverlay.get(alertId);
    applyLabelOverlay([alertId], verdict, species);
    setBusy(alertId, true);
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
      alert(`Label failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(alertId, false);
    }
  };

  const camerasResp = useCameras();
  const { data, error, loading } = useAlerts(
    {
      species: species || undefined,
      camera: camera || undefined,
      scope: scope === "all" ? undefined : scope,
      label_filter: labelFilter === "all" ? undefined : labelFilter,
    },
    autoRefresh ? 5000 : 3600_000,
  );

  const items = data?.items ?? [];
  const groups = useMemo(
    () => (grouped ? groupItems(items) : items.map((h) => ({ head: h, children: [] }))),
    [items, grouped],
  );

  // Snapshot the last-seen-id watermark at mount BEFORE markAlertsSeen
  // rolls it forward on the first data-arrival effect. Frozen for the
  // page's lifetime so rows highlighted as "unread" stay highlighted
  // even as new data arrives — otherwise the highlight would flicker
  // away the moment the polling tick writes the new watermark.
  //
  // Cold-start (never visited): initialSeenId stays null → adopted as
  // max(current ids) on first data (see effect below) so we don't
  // highlight all historical alerts as unread on the first-ever load.
  // Watermarks are camera-scoped so a yard-alerts view doesn't clear
  // the rooftop badge (and vice versa). Snapshot at mount for row
  // highlighting; re-snapshot when the filter camera changes.
  const [initialSeenId, setInitialSeenId] = useState<number | null>(() =>
    readLastSeenId(camera || null),
  );
  useEffect(() => {
    setInitialSeenId(readLastSeenId(camera || null));
  }, [camera]);

  // Being on this page IS the "seen" event — stamp total + highest-id
  // seen. Two paths:
  //
  // 1. Filtered view (?camera=X): stamp X's watermark. Simple case,
  //    same behavior as before.
  // 2. Unfiltered view (all cameras): stamp EVERY known camera's
  //    watermark so the dual-pane badge on the preview page clears
  //    after this visit. Per-camera totals come from /api/alerts/counts
  //    (the same batch endpoint the badge uses); per-camera maxIds
  //    are computed from the current items view. Also stamps the
  //    "all"-scope watermark for any cross-camera badge consumers.
  useEffect(() => {
    if (!data) return;
    const overallMaxId = items.reduce((m, a) => Math.max(m, a.id), 0);
    if (initialSeenId === null) setInitialSeenId(overallMaxId);

    if (camera) {
      // Filtered view — stamp just this camera.
      markAlertsSeen(camera, data.total, overallMaxId);
      return;
    }

    // Unfiltered — mark every camera as seen based on per-camera totals.
    // Also stamp the "all" scope so a cross-camera header badge zeros.
    markAlertsSeen(null, data.total, overallMaxId);
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch("/api/alerts/counts");
        if (!r.ok) return;
        const counts = (await r.json()) as Record<string, number>;
        if (cancelled) return;
        // Per-camera max-id from what's in the current items list.
        const perCamMaxId: Record<string, number> = {};
        for (const a of items) {
          if (!a.camera_id) continue;
          const prev = perCamMaxId[a.camera_id] ?? 0;
          if (a.id > prev) perCamMaxId[a.camera_id] = a.id;
        }
        for (const [cam, total] of Object.entries(counts)) {
          markAlertsSeen(cam, total, perCamMaxId[cam]);
        }
      } catch {
        // Silent — filtered-view watermark already stamped above,
        // dual-pane badge just won't clear this cycle.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [data, items, initialSeenId, camera]);

  return (
    <div className={styles.wrap}>
      <GlobalHeader
        right={
          <>
            <span className={styles.stat}>
              total <b className={styles.b}>{data?.total ?? "–"}</b>
            </span>
            <span className={styles.stat}>
              shown <b className={styles.b}>{items.length}</b>
            </span>
            <label className={styles.label}>
              species
              <select
                className={styles.select}
                value={species}
                onChange={(e) => setSpecies(e.target.value)}
              >
                <option value="">all</option>
                <option value="rat">rat</option>
                <option value="mouse">mouse</option>
                <option value="raccoon">raccoon</option>
                <option value="opossum">opossum</option>
                <option value="cat">cat</option>
                <option value="dog">dog</option>
                <option value="squirrel">squirrel</option>
                <option value="bird">bird</option>
                <option value="other">other</option>
              </select>
            </label>
            <label className={styles.label}>
              camera
              <select
                className={styles.select}
                value={camera}
                onChange={(e) => {
                  const v = e.target.value;
                  setCamera(v);
                  localStorage.setItem("alertsCameraFilter", v);
                  const nextParams = new URLSearchParams(urlParams);
                  if (v) nextParams.set("camera", v);
                  else nextParams.delete("camera");
                  setUrlParams(nextParams, { replace: true });
                }}
              >
                <option value="">all</option>
                {(camerasResp.data?.cameras ?? []).map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </label>
            <label className={styles.label}>
              show
              <select
                className={styles.select}
                value={scope}
                onChange={(e) => {
                  const v = e.target.value as "historical" | "live" | "all";
                  setScope(v);
                  localStorage.setItem("alertsScope", v);
                }}
                title="historical = backfilled/pre-tuning pile for labeling; live = today's fresh VLM alerts; all = both"
              >
                <option value="all">all</option>
                <option value="live">live only</option>
                <option value="historical">historical only (labeling)</option>
              </select>
            </label>
            <label className={styles.label}>
              label
              <select
                className={styles.select}
                value={labelFilter}
                onChange={(e) => {
                  const v = e.target.value as LabelFilterVal;
                  setLabelFilter(v);
                  localStorage.setItem("alertsLabelFilter", v);
                }}
                title="Filter by label state: unlabeled = still-to-vote; labeled = all voted; correct/incorrect/unclear = specific verdict"
              >
                <option value="all">all</option>
                <option value="unlabeled">unlabeled (sift for TPs)</option>
                <option value="labeled">labeled (any verdict)</option>
                <option value="correct">correct only (positives)</option>
                <option value="incorrect">incorrect only (FPs)</option>
                <option value="unclear">unclear only</option>
              </select>
            </label>
            <label className={styles.label}>
              <input
                type="checkbox"
                checked={autoRefresh}
                onChange={(e) => setAutoRefresh(e.target.checked)}
              />{" "}
              auto
            </label>
            <label className={styles.label}>
              <input
                type="checkbox"
                checked={grouped}
                onChange={(e) => setGrouped(e.target.checked)}
              />{" "}
              group
            </label>
          </>
        }
      />

      {error && <div className={styles.error}>Error: {error.message}</div>}

      {loading && !data ? (
        <div className={styles.empty}>Loading…</div>
      ) : items.length === 0 ? (
        <div className={styles.empty}>
          No alerts. When one fires, or when the <code>snapshots/</code> folder has JPEGs from a
          prior session, they'll show up here.
        </div>
      ) : (
        <>
          {selectedIds.size > 0 && (
            <BulkLabelBar
              selectedIds={Array.from(selectedIds)}
              onCleared={clearSelection}
              onApplied={(verdict, species) =>
                applyLabelOverlay(Array.from(selectedIds), verdict, species)
              }
            />
          )}
          <table className={styles.table}>
            <thead className={styles.thead}>
              <tr>
                <th className={styles.th}>
                  <input
                    type="checkbox"
                    aria-label="select all rows in view"
                    checked={items.length > 0 && items.every((a) => selectedIds.has(a.id))}
                    onChange={(e) => {
                      if (e.target.checked) setSelectedIds(new Set(items.map((a) => a.id)));
                      else clearSelection();
                    }}
                  />
                </th>
                <th className={styles.thSnap}>Snapshot</th>
                <th className={styles.th}>When</th>
                <th className={styles.th}>Label</th>
                <th className={styles.th}>Species</th>
                <th className={styles.th}>Conf</th>
                <th className={styles.th}>Description</th>
                <th className={styles.th}>Track</th>
                <th className={styles.th}>Replay</th>
              </tr>
            </thead>
            <tbody>
              {groups.flatMap((g) =>
                renderGroup(
                  g,
                  camera === "",
                  setOpenId,
                  initialSeenId ?? Number.POSITIVE_INFINITY,
                  selectedIds,
                  toggleOne,
                  labelOverlay,
                  writeLabel,
                  busyIds,
                ),
              )}
            </tbody>
          </table>
        </>
      )}
      <footer className={styles.footer}>
        Ring buffer capacity 500 · rolls oldest first · JPEGs on disk backfilled at startup (marked{" "}
        <span className={styles.badgeHist}>from disk</span> — confidence + description not
        persisted)
      </footer>
      <AlertLightbox
        items={items}
        openId={openId}
        setOpenId={setOpenId}
        labelOverlay={labelOverlay}
        busyIds={busyIds}
        writeLabel={writeLabel}
      />
    </div>
  );
}

function renderGroup(
  g: GroupedAlerts,
  showCameraBadge: boolean,
  onOpen: (id: number) => void,
  unreadThreshold: number,
  selectedIds: Set<number>,
  toggleOne: (id: number) => void,
  labelOverlay: Map<number, { verdict: LabelVerdict; species: string | null }>,
  writeLabel: (id: number, verdict: LabelVerdict, species: string | null) => Promise<void>,
  busyIds: Set<number>,
): JSX.Element[] {
  return [
    <Row
      key={g.head.id}
      alert={g.head}
      showCameraBadge={showCameraBadge}
      groupSize={g.children.length + 1}
      onOpen={onOpen}
      isUnread={g.head.id > unreadThreshold}
      isSelected={selectedIds.has(g.head.id)}
      onToggleSelect={() => toggleOne(g.head.id)}
      labelOverride={labelOverlay.get(g.head.id)}
      writeLabel={writeLabel}
      busy={busyIds.has(g.head.id)}
    />,
  ];
}

function Row({
  alert,
  showCameraBadge,
  groupSize,
  onOpen,
  isUnread,
  isSelected,
  onToggleSelect,
  labelOverride,
  writeLabel,
  busy,
}: {
  alert: AlertRow;
  showCameraBadge: boolean;
  groupSize: number;
  onOpen: (id: number) => void;
  isUnread: boolean;
  isSelected: boolean;
  onToggleSelect: () => void;
  labelOverride?: { verdict: LabelVerdict; species: string | null };
  writeLabel: (id: number, verdict: LabelVerdict, species: string | null) => Promise<void>;
  busy: boolean;
}): JSX.Element {
  const isRodent = RODENT_SPECIES.has(alert.species);
  const isHist = alert.historical;
  const speciesCls = isHist
    ? `${styles.species} ${styles.speciesHist}`
    : isRodent
      ? `${styles.species} ${styles.speciesRodent}`
      : `${styles.species} ${styles.speciesOther}`;
  const confPct = alert.confidence != null ? `${Math.round(alert.confidence * 100)}%` : "—";
  // Prefer local overlay (just-written) over the row's server-side value —
  // useAlerts polls every 5s, so the overlay covers the gap.
  const effVerdict: LabelVerdict = labelOverride
    ? labelOverride.verdict
    : (alert.label_verdict ?? null);
  const effSpecies = labelOverride ? labelOverride.species : (alert.label_species ?? null);
  return (
    <tr
      className={`${styles.row} ${isUnread ? styles.rowUnread : ""} ${isSelected ? styles.rowSelected : ""}`}
    >
      <td className={styles.thumbCell}>
        <input
          type="checkbox"
          aria-label={`select alert ${alert.id}`}
          checked={isSelected}
          onChange={onToggleSelect}
        />
      </td>
      <td className={styles.thumbCell}>
        {alert.snapshot ? (
          <button
            type="button"
            onClick={() => onOpen(alert.id)}
            className={styles.thumbBtn}
            aria-label={`Open ${alert.species} snapshot from ${alert.camera_id}`}
          >
            <img
              className={styles.thumb}
              src={`/snapshots/${encodeURIComponent(alert.snapshot)}`}
              alt="snapshot"
              loading="lazy"
            />
          </button>
        ) : (
          <div className={styles.noSnapshot}>no snapshot</div>
        )}
      </td>
      <td className={styles.ts}>
        {fmtTs(alert.ts)}
        <span className={styles.rel}> {fmtRelative(alert.ts)}</span>
      </td>
      <td className={styles.track}>
        <LabelPicker
          verdict={effVerdict}
          species={effSpecies}
          busy={busy}
          onChange={(v, s) => writeLabel(alert.id, v, s)}
        />
      </td>
      <td className={speciesCls}>
        {alert.species || "?"}
        {isHist && <span className={styles.badgeHist}>from disk</span>}
        {showCameraBadge && alert.camera_id && (
          <span className={styles.badgeCam}>{alert.camera_id}</span>
        )}
        {groupSize > 1 && <span className={styles.badgeCount}>×{groupSize}</span>}
      </td>
      <td className={styles.conf}>{confPct}</td>
      <td className={styles.desc}>{alert.description ?? ""}</td>
      <td className={styles.track}>{alert.track_id != null ? `#${alert.track_id}` : "—"}</td>
      <td className={styles.track}>
        <ReplayButton alertId={alert.id} />
      </td>
    </tr>
  );
}

function groupItems(items: AlertRow[]): GroupedAlerts[] {
  const groups: GroupedAlerts[] = [];
  for (const a of items) {
    const g = groups[groups.length - 1];
    if (g && sameGroup(g.head, a)) {
      g.children.push(a);
    } else {
      groups.push({ head: a, children: [] });
    }
  }
  return groups;
}

function sameGroup(a: AlertRow, b: AlertRow): boolean {
  if (a.historical !== b.historical) return false;
  if (
    !a.historical &&
    a.track_id != null &&
    a.track_id === b.track_id &&
    Math.abs(a.ts - b.ts) < GROUP_WINDOW_S
  ) {
    return true;
  }
  if (a.historical && a.snapshot && a.snapshot === b.snapshot) return true;
  return false;
}
