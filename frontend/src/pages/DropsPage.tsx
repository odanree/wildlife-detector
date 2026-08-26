import { useCallback, useEffect, useMemo, useState } from "react";
import { type DropLabel, type DropsFilter, setDropLabel } from "../api/drops";
import { GlobalHeader } from "../components/GlobalHeader";
import { useCameras } from "../hooks/useCameras";
import { useDrops } from "../hooks/useDrops";
import styles from "./DropsPage.module.css";

/**
 * Labeling page for pre-VLM drops (rows the insect pre-filter kills
 * before the VLM sees them). Positive-hunt UX: default filter shows
 * unlabeled crops in the 110-145 boundary band — the near-threshold
 * rows most likely to be real animals mis-flagged as insects. Feeds
 * the future pre-VLM classifier's training set.
 *
 * Pattern name: **single-writer optimistic overlay** — every label
 * mutation goes through applyLabel() which updates the local overlay
 * map BEFORE the POST completes, then rolls back on error. Same shape
 * as useLabelOverlay in the alerts page. Keeps the UI snappy on
 * repeated clicks without waiting for round-trips.
 *
 * Keyboard shortcuts for fast labeling:
 *   M = moth        A = real_animal        U = unclear
 *   J = prev row    K = next row           X = clear label
 */
export function DropsPage() {
  const camerasResp = useCameras();
  const [camera, setCamera] = useState<string>("");
  const [filter, setFilter] = useState<DropsFilter>("unlabeled");
  const [boundary, setBoundary] = useState<boolean>(true);
  const [meanRange, setMeanRange] = useState<[number, number]>([110, 145]);
  const [limit] = useState<number>(60);
  const [offset, setOffset] = useState<number>(0);
  const [selected, setSelected] = useState<number>(0);

  const { data, loading, error, refresh } = useDrops({
    camera: camera || undefined,
    filter,
    boundary,
    mean_min: boundary ? meanRange[0] : undefined,
    mean_max: boundary ? meanRange[1] : undefined,
    limit,
    offset,
  });

  const items = data?.items ?? [];
  const total = data?.total ?? 0;

  // Optimistic label overlay — local map keyed by drop_id. Rolls back
  // on POST error. Cleared when the underlying items change (page or
  // filter switch) so stale overlays don't linger.
  const [labelOverlay, setLabelOverlay] = useState<Map<string, DropLabel | null>>(() => new Map());
  const [busyIds, setBusyIds] = useState<Set<string>>(() => new Set());
  // Reset overlays when the underlying query changes — deps are the
  // fire trigger, effect body only calls setters (no reads of the
  // deps). biome-ignore matches useSecondaryPane's pattern.
  // biome-ignore lint/correctness/useExhaustiveDependencies: intentional — deps ARE the fire trigger for the reset
  useEffect(() => {
    setLabelOverlay(new Map());
    setBusyIds(new Set());
    setSelected(0);
  }, [offset, camera, filter, boundary, meanRange]);

  const applyLabel = useCallback(
    async (drop_id: string, label: DropLabel | null) => {
      // Snapshot the current effective label so we can roll back.
      const prev = labelOverlay.has(drop_id)
        ? (labelOverlay.get(drop_id) ?? null)
        : (items.find((r) => r.drop_id === drop_id)?.label ?? null);
      setLabelOverlay((m) => new Map(m).set(drop_id, label));
      setBusyIds((s) => new Set(s).add(drop_id));
      try {
        await setDropLabel(drop_id, label);
      } catch (e) {
        // Rollback + surface error via console (a toast would be nicer
        // but this is a labeling flow — repeated clicks re-attempt).
        setLabelOverlay((m) => new Map(m).set(drop_id, prev));
        console.error("setDropLabel failed:", e);
      } finally {
        setBusyIds((s) => {
          const next = new Set(s);
          next.delete(drop_id);
          return next;
        });
      }
    },
    [items, labelOverlay],
  );

  // Keyboard shortcuts — labels the CURRENTLY SELECTED row, or all if
  // the operator hasn't clicked yet (defaults to items[0]).
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      // Don't intercept when the user is typing in a text input.
      const target = e.target as HTMLElement;
      if (
        target?.tagName === "INPUT" ||
        target?.tagName === "TEXTAREA" ||
        target?.tagName === "SELECT"
      )
        return;
      const cur = items[selected];
      if (!cur) return;
      const key = e.key.toLowerCase();
      if (key === "m") {
        e.preventDefault();
        void applyLabel(cur.drop_id, "moth");
        setSelected((n) => Math.min(n + 1, items.length - 1));
      } else if (key === "a") {
        e.preventDefault();
        void applyLabel(cur.drop_id, "real_animal");
        setSelected((n) => Math.min(n + 1, items.length - 1));
      } else if (key === "u") {
        e.preventDefault();
        void applyLabel(cur.drop_id, "unclear");
        setSelected((n) => Math.min(n + 1, items.length - 1));
      } else if (key === "x") {
        e.preventDefault();
        void applyLabel(cur.drop_id, null);
      } else if (key === "j") {
        e.preventDefault();
        setSelected((n) => Math.max(0, n - 1));
      } else if (key === "k") {
        e.preventDefault();
        setSelected((n) => Math.min(items.length - 1, n + 1));
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [items, selected, applyLabel]);

  const canPrev = offset > 0;
  const canNext = offset + limit < total;

  const effectiveLabel = useMemo(() => {
    return (drop_id: string, serverLabel: DropLabel | null): DropLabel | null =>
      labelOverlay.has(drop_id) ? (labelOverlay.get(drop_id) ?? null) : serverLabel;
  }, [labelOverlay]);

  return (
    <div className={styles.wrap}>
      <GlobalHeader
        right={
          <>
            <span className={styles.stat}>
              total <b className={styles.b}>{total}</b>
            </span>
            <span className={styles.stat}>
              page <b className={styles.b}>{items.length}</b>
            </span>
            <label className={styles.label}>
              camera
              <select
                className={styles.select}
                value={camera}
                onChange={(e) => {
                  setCamera(e.target.value);
                  setOffset(0);
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
                value={filter}
                onChange={(e) => {
                  setFilter(e.target.value as DropsFilter);
                  setOffset(0);
                }}
              >
                <option value="unlabeled">unlabeled</option>
                <option value="labeled">labeled</option>
                <option value="all">all</option>
              </select>
            </label>
            <label
              className={styles.label}
              title="Only near-threshold rows (110-145 mean brightness) — the positive-hunt band"
            >
              <input
                type="checkbox"
                checked={boundary}
                onChange={(e) => {
                  setBoundary(e.target.checked);
                  setOffset(0);
                }}
              />
              boundary
            </label>
            {boundary && (
              <span className={styles.stat}>
                mean{" "}
                <input
                  className={styles.numInput}
                  type="number"
                  min={0}
                  max={255}
                  value={meanRange[0]}
                  onChange={(e) => {
                    setMeanRange([Number.parseInt(e.target.value, 10) || 0, meanRange[1]]);
                    setOffset(0);
                  }}
                />
                {"–"}
                <input
                  className={styles.numInput}
                  type="number"
                  min={0}
                  max={255}
                  value={meanRange[1]}
                  onChange={(e) => {
                    setMeanRange([meanRange[0], Number.parseInt(e.target.value, 10) || 255]);
                    setOffset(0);
                  }}
                />
              </span>
            )}
            <button
              type="button"
              className={styles.pageBtn}
              onClick={() => setOffset(Math.max(0, offset - limit))}
              disabled={!canPrev}
            >
              ‹ prev
            </button>
            <button
              type="button"
              className={styles.pageBtn}
              onClick={() => setOffset(offset + limit)}
              disabled={!canNext}
            >
              next ›
            </button>
          </>
        }
      />
      <div className={styles.help}>
        keys: <b>M</b> moth · <b>A</b> real animal · <b>U</b> unclear · <b>X</b> clear · <b>J/K</b>{" "}
        prev/next selection
      </div>
      {loading && !data ? (
        <div className={styles.empty}>Loading drops…</div>
      ) : error ? (
        <div className={styles.err}>Error: {error.message}</div>
      ) : items.length === 0 ? (
        <div className={styles.empty}>No drops match the current filter.</div>
      ) : (
        <div className={styles.grid}>
          {items.map((r, i) => {
            const currentLabel = effectiveLabel(r.drop_id, r.label);
            const isSelected = i === selected;
            const isBusy = busyIds.has(r.drop_id);
            const labelClass =
              currentLabel === "moth"
                ? styles.labelMoth
                : currentLabel === "real_animal"
                  ? styles.labelAnimal
                  : currentLabel === "unclear"
                    ? styles.labelUnclear
                    : "";
            return (
              <div
                key={r.drop_id}
                className={`${styles.card} ${isSelected ? styles.cardSelected : ""} ${labelClass}`}
              >
                <img
                  className={styles.crop}
                  src={r.crop_url}
                  alt={`drop ${r.drop_id}`}
                  loading={i < 20 ? "eager" : "lazy"}
                />
                <div className={styles.meta}>
                  <div className={styles.metaRow}>
                    <span className={styles.badgeCam}>{r.camera_id}</span>
                    <span className={styles.stat}>
                      mean <b>{r.mean != null ? Math.round(r.mean) : "?"}</b>
                    </span>
                    <span className={styles.stat}>
                      max <b>{r.max != null ? Math.round(r.max) : "?"}</b>
                    </span>
                    <span className={styles.stat}>
                      {r.bbox_w}×{r.bbox_h}
                    </span>
                  </div>
                  <div className={styles.metaRow}>
                    <button
                      type="button"
                      className={currentLabel === "moth" ? styles.btnMothActive : styles.btn}
                      onClick={(e) => {
                        e.stopPropagation();
                        void applyLabel(r.drop_id, "moth");
                      }}
                      disabled={isBusy}
                      title="M — mark as moth/insect"
                    >
                      moth
                    </button>
                    <button
                      type="button"
                      className={
                        currentLabel === "real_animal" ? styles.btnAnimalActive : styles.btn
                      }
                      onClick={(e) => {
                        e.stopPropagation();
                        void applyLabel(r.drop_id, "real_animal");
                      }}
                      disabled={isBusy}
                      title="A — mark as real animal (the positive-hunt goal)"
                    >
                      animal
                    </button>
                    <button
                      type="button"
                      className={currentLabel === "unclear" ? styles.btnUnclearActive : styles.btn}
                      onClick={(e) => {
                        e.stopPropagation();
                        void applyLabel(r.drop_id, "unclear");
                      }}
                      disabled={isBusy}
                      title="U — mark as unclear"
                    >
                      ?
                    </button>
                    {currentLabel != null && (
                      <button
                        type="button"
                        className={styles.btnClear}
                        onClick={(e) => {
                          e.stopPropagation();
                          void applyLabel(r.drop_id, null);
                        }}
                        disabled={isBusy}
                        title="X — clear label"
                      >
                        ×
                      </button>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
      <button
        type="button"
        className={styles.refreshFloat}
        onClick={refresh}
        title="Refetch — useful after labeling to remove now-labeled cards from the unlabeled view"
      >
        ↻
      </button>
    </div>
  );
}
