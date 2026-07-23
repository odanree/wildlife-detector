import { useMemo, useState } from "react";
import type { AlertRow, LabelVerdict } from "../api/alerts";
import { AlertLightbox } from "../components/AlertLightbox";
import { BulkLabelBar } from "../components/BulkLabelBar";
import { GlobalHeader } from "../components/GlobalHeader";
import { LabelPicker } from "../components/LabelPicker";
import { ReplayButton } from "../components/ReplayButton";
import { useAlerts } from "../hooks/useAlerts";
import { useAlertsFilters } from "../hooks/useAlertsFilters";
import { useAlertsSelection } from "../hooks/useAlertsSelection";
import { useAlertsWatermark } from "../hooks/useAlertsWatermark";
import { useCameras } from "../hooks/useCameras";
import { useLabelOverlay } from "../hooks/useLabelOverlay";
import { fmtRelative, fmtTs } from "../util/time";
import styles from "./AlertsPage.module.css";

const RODENT_SPECIES = new Set(["rat", "mouse"]);
const GROUP_WINDOW_S = 60;

interface GroupedAlerts {
  head: AlertRow;
  children: AlertRow[];
}

/**
 * Alerts page — glue between four focused hooks and the table view.
 *
 * State ownership is delegated:
 *   - useAlertsFilters: URL + localStorage filter state.
 *   - useAlertsSelection: bulk checkbox Set + toggle/clear.
 *   - useLabelOverlay: optimistic label map + rollback + busy set.
 *   - useAlertsWatermark: initialSeenId snapshot + markAlertsSeen ledger.
 *
 * What's left here: the useAlerts call, table + modal composition,
 * and the row component. Was 557 LOC + 12 useState + 3 useEffect
 * before #33; now ~180 LOC + 1 useState + 0 useEffect.
 */
export function AlertsPage() {
  const filters = useAlertsFilters();
  const selection = useAlertsSelection();
  const overlay = useLabelOverlay();
  const [openId, setOpenId] = useState<number | null>(null);

  const camerasResp = useCameras();
  const { data, error, loading } = useAlerts(
    {
      species: filters.species || undefined,
      camera: filters.camera || undefined,
      scope: filters.scope === "all" ? undefined : filters.scope,
      label_filter: filters.labelFilter === "all" ? undefined : filters.labelFilter,
    },
    filters.autoRefresh ? 5000 : 3600_000,
  );

  const items = data?.items ?? [];
  const groups = useMemo(
    () => (filters.grouped ? groupItems(items) : items.map((h) => ({ head: h, children: [] }))),
    [items, filters.grouped],
  );

  const { initialSeenId } = useAlertsWatermark({ data, camera: filters.camera });

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
                value={filters.species}
                onChange={(e) => filters.setSpecies(e.target.value)}
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
                value={filters.camera}
                onChange={(e) => filters.setCamera(e.target.value)}
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
                value={filters.scope}
                onChange={(e) => filters.setScope(e.target.value as "historical" | "live" | "all")}
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
                value={filters.labelFilter}
                onChange={(e) =>
                  filters.setLabelFilter(
                    e.target.value as
                      | "unlabeled"
                      | "labeled"
                      | "correct"
                      | "incorrect"
                      | "unclear"
                      | "all",
                  )
                }
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
                checked={filters.autoRefresh}
                onChange={(e) => filters.setAutoRefresh(e.target.checked)}
              />{" "}
              auto
            </label>
            <label className={styles.label}>
              <input
                type="checkbox"
                checked={filters.grouped}
                onChange={(e) => filters.setGrouped(e.target.checked)}
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
          {selection.size > 0 && (
            <BulkLabelBar
              selectedIds={Array.from(selection.selectedIds)}
              onCleared={selection.clear}
              onApplied={(verdict, species) =>
                overlay.applyOverlay(Array.from(selection.selectedIds), verdict, species)
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
                    checked={items.length > 0 && items.every((a) => selection.isSelected(a.id))}
                    onChange={(e) => {
                      if (e.target.checked) selection.setAll(items.map((a) => a.id));
                      else selection.clear();
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
                  filters.camera === "",
                  setOpenId,
                  initialSeenId ?? Number.POSITIVE_INFINITY,
                  selection.selectedIds,
                  selection.toggleOne,
                  overlay.labelOverlay,
                  overlay.writeLabel,
                  overlay.busyIds,
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
        labelOverlay={overlay.labelOverlay}
        busyIds={overlay.busyIds}
        writeLabel={overlay.writeLabel}
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
