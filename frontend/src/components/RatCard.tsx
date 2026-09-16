import { memo } from "react";
import { Link } from "react-router-dom";
import { type RatSummary, sampleSnapshotRel } from "../api/rats";
import { fmtAgo, fmtRelativeIso, fmtTsIso } from "../util/time";
import { CameraBadge } from "./CameraBadge";
import styles from "./RatCard.module.css";
import { RatNameEditor } from "./RatNameEditor";
import { SnapshotImg } from "./SnapshotImg";

interface RatCardProps {
  rat: RatSummary;
  onRename: (id: number, name: string | null) => Promise<void>;
  eagerThumb?: boolean;
}

/**
 * One rat in the gallery grid. The thumbnail + stats are a Link to the
 * timeline; the name row sits OUTSIDE the link so an inline rename
 * doesn't navigate (nested interactive content inside <a> is invalid
 * HTML and fires both). Retired rats render dimmed with a strip — they
 * stay visible under "include retired" so an operator can audit what
 * the clusterer (or a merge) put to bed.
 *
 * Memoized: a rename on one card re-renders the parent, and 20 cards
 * with lazy <img>s shouldn't all reconcile for it.
 */
export const RatCard = memo(function RatCard({ rat, onRename, eagerThumb = false }: RatCardProps) {
  const retired = rat.retired_at != null;
  const snap = sampleSnapshotRel(rat.sample_snapshot);
  const retiredAgo = retired
    ? fmtAgo(Math.max(0, Math.floor((Date.now() - Date.parse(rat.retired_at as string)) / 1000)))
    : null;

  return (
    <div className={`${styles.card} ${retired ? styles.cardRetired : ""}`}>
      <Link to={`/rats/${rat.id}`} className={styles.link} aria-label={`open rat ${rat.id}`}>
        <div className={styles.thumbWrap}>
          {snap ? (
            <SnapshotImg
              className={styles.thumb}
              snapshot={snap}
              alt={`rat ${rat.id} sample`}
              loading={eagerThumb ? "eager" : "lazy"}
              preferThumb
            />
          ) : (
            <div className={styles.noThumb}>no snapshot</div>
          )}
          {retired && <div className={styles.retiredStrip}>retired {retiredAgo} ago</div>}
        </div>
      </Link>
      <div className={styles.body}>
        <div className={styles.nameRow}>
          <RatNameEditor ratId={rat.id} name={rat.name} onSave={(n) => onRename(rat.id, n)} />
          <span className={styles.idTag}>#{rat.id}</span>
        </div>
        <Link to={`/rats/${rat.id}`} className={styles.metaLink}>
          <div className={styles.metaRow}>
            <span
              className={styles.span}
              title={`${fmtTsIso(rat.first_seen)} → ${fmtTsIso(rat.last_seen)}`}
            >
              {fmtRelativeIso(rat.first_seen)} → {fmtRelativeIso(rat.last_seen)}
            </span>
          </div>
          <div className={styles.metaRow}>
            <span className={styles.count}>
              <b>{rat.alert_count}</b> alerts
            </span>
            {rat.primary_camera && <CameraBadge cameraId={rat.primary_camera} />}
          </div>
        </Link>
      </div>
    </div>
  );
});
