import { useEffect, useState } from "react";
import { type RatSummary, ratDisplayName, sampleSnapshotRel } from "../api/rats";
import { useRats } from "../hooks/useRats";
import { fmtRelativeIso } from "../util/time";
import { CameraBadge } from "./CameraBadge";
import styles from "./MergeRatModal.module.css";
import { SnapshotImg } from "./SnapshotImg";

interface MergeRatModalProps {
  source: { id: number; name: string | null; alert_count: number };
  onClose: () => void;
  /** Owner performs the merge (optimistic UI + rollback live there).
   *  Resolves → modal closes; rejects → error shown inline, modal stays. */
  onConfirm: (targetId: number) => Promise<void>;
}

/**
 * Pick a target for "merge this rat into…". Lists ACTIVE rats only (a
 * retired target is a 400 server-side; don't offer it), excluding the
 * source itself. Two-step: select a card, then confirm with the alert
 * count spelled out — the merge is reversible only by hand, so the
 * confirm copy states exactly what moves.
 */
export function MergeRatModal({ source, onClose, onConfirm }: MergeRatModalProps) {
  const { rats, loading, error } = useRats({ include_retired: false }, "recent");
  const candidates = rats.filter((r) => r.id !== source.id);
  const [selected, setSelected] = useState<RatSummary | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const originalOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = originalOverflow;
    };
  }, [onClose]);

  const confirm = async () => {
    if (!selected || busy) return;
    setBusy(true);
    setErr(null);
    try {
      await onConfirm(selected.id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  };

  return (
    // biome-ignore lint/a11y/useKeyWithClickEvents: Esc + the × button are the keyboard paths; backdrop-click is a bonus
    <div
      className={styles.backdrop}
      // biome-ignore lint/a11y/useSemanticElements: native <dialog> fights React's declarative model
      role="dialog"
      aria-modal="true"
      aria-label="merge rat into another rat"
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div className={styles.panel}>
        <div className={styles.head}>
          <div>
            <div className={styles.title}>
              Merge <b>{ratDisplayName(source)}</b> into…
            </div>
            <div className={styles.sub}>
              Moves all <b>{source.alert_count}</b> of its alerts to the rat you pick, then retires
              #{source.id}. Undo is manual — pick carefully.
            </div>
          </div>
          <button
            type="button"
            className={styles.closeBtn}
            onClick={onClose}
            disabled={busy}
            aria-label="close"
          >
            ×
          </button>
        </div>

        {error && <div className={styles.err}>Couldn't load rats: {error.message}</div>}
        {loading && !error ? (
          <div className={styles.empty}>Loading rats…</div>
        ) : candidates.length === 0 ? (
          <div className={styles.empty}>No other active rats to merge into.</div>
        ) : (
          <div className={styles.grid}>
            {candidates.map((r) => {
              const snap = sampleSnapshotRel(r.sample_snapshot);
              const isSel = selected?.id === r.id;
              return (
                <button
                  type="button"
                  key={r.id}
                  className={`${styles.cand} ${isSel ? styles.candSelected : ""}`}
                  onClick={() => setSelected(r)}
                  disabled={busy}
                  aria-pressed={isSel}
                >
                  <div className={styles.candThumb}>
                    {snap ? (
                      <SnapshotImg
                        className={styles.candImg}
                        snapshot={snap}
                        alt={`rat ${r.id}`}
                        preferThumb
                      />
                    ) : (
                      <span className={styles.noThumb}>—</span>
                    )}
                  </div>
                  <div className={styles.candMeta}>
                    <div className={styles.candName}>
                      {ratDisplayName(r)} <span className={styles.candId}>#{r.id}</span>
                    </div>
                    <div className={styles.candRow}>
                      {r.primary_camera && <CameraBadge cameraId={r.primary_camera} />}
                      <span className={styles.candStat}>{r.alert_count} alerts</span>
                    </div>
                    <div className={styles.candStat}>last {fmtRelativeIso(r.last_seen)}</div>
                  </div>
                </button>
              );
            })}
          </div>
        )}

        <div className={styles.foot}>
          {err && <span className={styles.err}>{err}</span>}
          <button type="button" className={styles.btn} onClick={onClose} disabled={busy}>
            cancel
          </button>
          <button
            type="button"
            className={styles.btnPrimary}
            onClick={() => void confirm()}
            disabled={!selected || busy}
            title={selected ? `Merge into ${ratDisplayName(selected)}` : "Pick a target first"}
          >
            {busy
              ? "merging…"
              : selected
                ? `merge ${source.alert_count} alerts into ${ratDisplayName(selected)}`
                : "pick a target"}
          </button>
        </div>
      </div>
    </div>
  );
}
