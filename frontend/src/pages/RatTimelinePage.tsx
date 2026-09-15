import { memo, useCallback, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import type { AlertRow } from "../api/alerts";
import { mergeRat, renameRat } from "../api/rats";
import { AlertLightbox } from "../components/AlertLightbox";
import { CameraBadge } from "../components/CameraBadge";
import { CameraFrequencyBar } from "../components/CameraFrequencyBar";
import { GlobalHeader } from "../components/GlobalHeader";
import { MergeRatModal } from "../components/MergeRatModal";
import { RatNameEditor } from "../components/RatNameEditor";
import { SnapshotImg } from "../components/SnapshotImg";
import { useRat } from "../hooks/useRat";
import { fmtRelative, fmtRelativeIso, fmtTs, fmtTsIso } from "../util/time";
import styles from "./RatTimelinePage.module.css";

const RODENT_SPECIES = new Set(["rat", "mouse"]);
const EAGER_THUMB_ROWS = 20;

/**
 * Single-rat timeline + the operator's cluster-correction surface.
 *
 * Reads: header stats, camera-share histogram, newest-first alert
 * thread across all cameras (≤500 rows; server caps), lightbox reuse.
 *
 * Writes (both **single-writer optimistic with rollback**):
 *  - rename → patch local name, PATCH, revert on error.
 *  - merge  → patch local retired_at, POST, navigate to the target on
 *    success, revert on error. The server does the real work in one
 *    transaction; the optimistic bit is only so the header flips to
 *    "retired" instantly instead of after the round-trip.
 */
export function RatTimelinePage() {
  const { id: idParam } = useParams<{ id: string }>();
  const id = idParam != null ? Number.parseInt(idParam, 10) : Number.NaN;
  const navigate = useNavigate();
  const { data: rat, error, loading, patch } = useRat(Number.isFinite(id) ? id : null);
  const [openId, setOpenId] = useState<number | null>(null);
  const [mergeOpen, setMergeOpen] = useState(false);
  const [actionErr, setActionErr] = useState<string | null>(null);

  const onRename = useCallback(
    async (name: string | null) => {
      if (!rat) return;
      const prev = rat.name;
      patch({ name });
      setActionErr(null);
      try {
        const row = await renameRat(rat.id, name);
        patch({ name: row.name });
      } catch (e) {
        patch({ name: prev });
        setActionErr(e instanceof Error ? e.message : String(e));
        throw e;
      }
    },
    [rat, patch],
  );

  const onMerge = useCallback(
    async (targetId: number) => {
      if (!rat) return;
      const prevRetired = rat.retired_at;
      patch({ retired_at: new Date().toISOString() });
      setActionErr(null);
      try {
        const res = await mergeRat(rat.id, targetId);
        setMergeOpen(false);
        navigate(`/rats/${res.target_rat_id}`, { replace: false });
      } catch (e) {
        patch({ retired_at: prevRetired });
        setActionErr(e instanceof Error ? e.message : String(e));
        throw e;
      }
    },
    [rat, patch, navigate],
  );

  const alerts: AlertRow[] = rat?.alerts ?? [];
  const retired = rat?.retired_at != null;

  return (
    <div className={styles.wrap}>
      <GlobalHeader
        right={
          <Link to="/rats" className={styles.selectBtn}>
            ‹ all rats
          </Link>
        }
      />

      {error && <div className={styles.error}>Error: {error.message}</div>}
      {actionErr && <div className={styles.error}>{actionErr}</div>}

      {loading && !rat ? (
        <div className={styles.empty}>Loading rat…</div>
      ) : !rat ? (
        <div className={styles.empty}>Rat not found.</div>
      ) : (
        <>
          <section className={`${styles.head} ${retired ? styles.headRetired : ""}`}>
            <div className={styles.headMain}>
              <div className={styles.nameRow}>
                <RatNameEditor ratId={rat.id} name={rat.name} onSave={onRename} size="lg" />
                <span className={styles.idTag}>#{rat.id}</span>
                {rat.primary_camera && <CameraBadge cameraId={rat.primary_camera} />}
                {retired && (
                  <span className={styles.retiredTag} title={fmtTsIso(rat.retired_at)}>
                    retired {fmtRelativeIso(rat.retired_at)}
                  </span>
                )}
              </div>
              <div className={styles.statsRow}>
                <span title={`${fmtTsIso(rat.first_seen)} → ${fmtTsIso(rat.last_seen)}`}>
                  first seen <b>{fmtRelativeIso(rat.first_seen)}</b> → last seen{" "}
                  <b>{fmtRelativeIso(rat.last_seen)}</b>
                </span>
                <span>
                  <b>{rat.alert_count}</b> alerts
                </span>
                <span>
                  <b>{Object.keys(rat.camera_frequency).length}</b> camera
                  {Object.keys(rat.camera_frequency).length === 1 ? "" : "s"}
                </span>
              </div>
              <div className={styles.actions}>
                <button
                  type="button"
                  className={styles.actionBtn}
                  onClick={() => setMergeOpen(true)}
                  disabled={retired}
                  title={
                    retired
                      ? "Retired rats can't be merged (already folded into another rat)"
                      : "Fold this rat into another one — use when the clusterer split one animal in two"
                  }
                >
                  merge into another rat…
                </button>
              </div>
            </div>
            <div className={styles.headChart}>
              <div className={styles.chartTitle}>camera share</div>
              <CameraFrequencyBar frequency={rat.camera_frequency} total={rat.alerts_total} />
            </div>
          </section>

          {alerts.length === 0 ? (
            <div className={styles.empty}>No alerts assigned to this rat.</div>
          ) : (
            <>
              <div className={styles.listHead}>
                showing <b>{alerts.length}</b>
                {rat.alerts_capped && <> of {rat.alerts_total}</>} alerts, newest first
                {rat.alerts_capped && (
                  <span className={styles.dim}> · older rows not loaded (cap 500)</span>
                )}
              </div>
              <table className={styles.table}>
                <thead className={styles.thead}>
                  <tr>
                    <th className={styles.thSnap}>Snapshot</th>
                    <th className={styles.th}>When</th>
                    <th className={styles.th}>Camera</th>
                    <th className={styles.th}>Species</th>
                    <th className={styles.th}>Conf</th>
                    <th className={styles.th}>Description</th>
                  </tr>
                </thead>
                <tbody>
                  {alerts.map((a, i) => (
                    <Row
                      key={a.id}
                      alert={a}
                      onOpen={setOpenId}
                      eagerThumb={i < EAGER_THUMB_ROWS}
                    />
                  ))}
                </tbody>
              </table>
            </>
          )}
        </>
      )}

      <AlertLightbox items={alerts} openId={openId} setOpenId={setOpenId} />
      {mergeOpen && rat && (
        <MergeRatModal
          source={{ id: rat.id, name: rat.name, alert_count: rat.alert_count }}
          onClose={() => setMergeOpen(false)}
          onConfirm={onMerge}
        />
      )}
    </div>
  );
}

const Row = memo(function Row({
  alert,
  onOpen,
  eagerThumb,
}: {
  alert: AlertRow;
  onOpen: (id: number) => void;
  eagerThumb: boolean;
}): JSX.Element {
  const isRodent = RODENT_SPECIES.has(alert.species);
  const speciesCls = `${styles.species} ${isRodent ? styles.speciesRodent : styles.speciesOther}`;
  const confPct = alert.confidence != null ? `${Math.round(alert.confidence * 100)}%` : "—";
  const firstLine = (alert.description ?? "").split(/\r?\n/)[0] ?? "";
  const open = () => {
    if (alert.snapshot) onOpen(alert.id);
  };
  return (
    // biome-ignore lint/a11y/useKeyWithClickEvents: the thumbnail <button> is the keyboard path; row-click is a pointer convenience
    <tr className={`${styles.row} ${alert.snapshot ? styles.rowClickable : ""}`} onClick={open}>
      <td className={styles.thumbCell}>
        {alert.snapshot ? (
          <button
            type="button"
            className={styles.thumbBtn}
            onClick={(e) => {
              e.stopPropagation();
              open();
            }}
            aria-label={`Open ${alert.species} snapshot from ${alert.camera_id}`}
          >
            <SnapshotImg
              className={styles.thumb}
              snapshot={alert.snapshot}
              alt="snapshot"
              loading={eagerThumb ? "eager" : "lazy"}
              preferThumb
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
      <td className={styles.cell}>
        <CameraBadge cameraId={alert.camera_id} />
      </td>
      <td className={speciesCls}>{alert.species || "?"}</td>
      <td className={styles.conf}>{confPct}</td>
      <td className={styles.desc} title={alert.description ?? ""}>
        {firstLine}
      </td>
    </tr>
  );
});
