import { useCallback, useState } from "react";
import { renameRat } from "../api/rats";
import { GlobalHeader } from "../components/GlobalHeader";
import { RatCard } from "../components/RatCard";
import { useCameras } from "../hooks/useCameras";
import { type RatsSort, useRats, useRatsFilters } from "../hooks/useRats";
import styles from "./RatsPage.module.css";

// Cards above the fold load their thumbnail eagerly — same SPA-route
// IntersectionObserver race the alerts table works around (see
// EAGER_THUMB_ROWS in AlertsPage).
const EAGER_THUMB_CARDS = 12;

/**
 * Gallery of known rats (rat re-id Phase 2c). One card per `rats` row
 * the clusterer has catalogued; click through to the per-rat timeline.
 *
 * Camera filter is server-side ("rats with ≥1 alert on this camera"),
 * sort is client-side (≈20 rows). Rename is a **single-writer
 * optimistic overlay**: patch the list row, PATCH the server, roll the
 * row back on failure — same shape as the label overlay on /alerts.
 */
export function RatsPage() {
  const filters = useRatsFilters();
  const camerasResp = useCameras();
  const { rats, data, error, loading, refresh, patchRat } = useRats(
    { camera: filters.camera || undefined, include_retired: filters.includeRetired },
    filters.sort,
  );
  const [renameErr, setRenameErr] = useState<string | null>(null);

  const onRename = useCallback(
    async (id: number, name: string | null) => {
      const prev = rats.find((r) => r.id === id)?.name ?? null;
      patchRat(id, { name });
      setRenameErr(null);
      try {
        const row = await renameRat(id, name);
        patchRat(id, { name: row.name });
      } catch (e) {
        patchRat(id, { name: prev });
        setRenameErr(e instanceof Error ? e.message : String(e));
        throw e;
      }
    },
    [rats, patchRat],
  );

  const activeCount = rats.filter((r) => r.retired_at == null).length;
  const retiredCount = rats.length - activeCount;

  return (
    <div className={styles.wrap}>
      <GlobalHeader
        right={
          <>
            <span className={styles.stat}>
              rats <b className={styles.b}>{loading && !data ? "–" : activeCount}</b>
              {filters.includeRetired && retiredCount > 0 && (
                <span className={styles.dim}> +{retiredCount} retired</span>
              )}
            </span>
            <label className={styles.label}>
              camera
              <select
                className={styles.select}
                value={filters.camera}
                onChange={(e) => filters.setCamera(e.target.value)}
                title="Rats with at least one alert on this camera"
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
              sort
              <select
                className={styles.select}
                value={filters.sort}
                onChange={(e) => filters.setSort(e.target.value as RatsSort)}
              >
                <option value="recent">most recent activity</option>
                <option value="active">most alerts</option>
                <option value="tenured">longest tenured</option>
              </select>
            </label>
            <label className={styles.label} title="Show rats the clusterer or a merge has retired">
              <input
                type="checkbox"
                checked={filters.includeRetired}
                onChange={(e) => filters.setIncludeRetired(e.target.checked)}
              />{" "}
              include retired
            </label>
            <button type="button" className={styles.selectBtn} onClick={refresh} title="Refetch">
              ↻
            </button>
          </>
        }
      />

      {error && <div className={styles.error}>Error: {error.message}</div>}
      {renameErr && <div className={styles.error}>Rename failed: {renameErr}</div>}

      {loading && !data ? (
        <div className={styles.empty}>Loading rats…</div>
      ) : rats.length === 0 ? (
        <div className={styles.empty}>
          {data?.error ??
            "No rats match. The catalog fills in after the nightly clusterer run; try another camera or include retired."}
        </div>
      ) : (
        <div className={styles.grid}>
          {rats.map((r, i) => (
            <RatCard key={r.id} rat={r} onRename={onRename} eagerThumb={i < EAGER_THUMB_CARDS} />
          ))}
        </div>
      )}
      <footer className={styles.footer}>
        Identities are recurring appearance clusters from the nightly re-id run (CLIP + HDBSCAN),
        not verified individuals. Click a rat to review its timeline; merge from there if two cards
        are the same animal.
      </footer>
    </div>
  );
}
