import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import type { AlertRow } from "../api/alerts";
import { AlertLightbox } from "../components/AlertLightbox";
import { useAlerts } from "../hooks/useAlerts";
import { useCameras } from "../hooks/useCameras";
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
  const [camera, setCamera] = useState<string>(
    () => localStorage.getItem("alertsCameraFilter") ?? "",
  );
  const [grouped, setGrouped] = useState<boolean>(true);
  const [autoRefresh, setAutoRefresh] = useState<boolean>(true);
  const [openId, setOpenId] = useState<number | null>(null);

  const camerasResp = useCameras();
  const { data, error, loading } = useAlerts(
    { species: species || undefined, camera: camera || undefined },
    autoRefresh ? 5000 : 3600_000,
  );

  const items = data?.items ?? [];
  const groups = useMemo(
    () => (grouped ? groupItems(items) : items.map((h) => ({ head: h, children: [] }))),
    [items, grouped],
  );

  if (data && typeof window !== "undefined") {
    localStorage.setItem("alertsLastSeenTotal", String(data.total));
  }

  return (
    <div className={styles.wrap}>
      <header className={styles.header}>
        <Link to="/" className={styles.title}>
          wildlife-detector — alerts
        </Link>
        <span className={styles.stat}>
          total <b className={styles.b}>{data?.total ?? "–"}</b>
        </span>
        <span className={styles.stat}>
          shown <b className={styles.b}>{items.length}</b>
        </span>
        <div className={styles.tools}>
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
                setCamera(e.target.value);
                localStorage.setItem("alertsCameraFilter", e.target.value);
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
          <Link to="/" className={styles.closeBtn} aria-label="Back to live preview">
            ×
          </Link>
        </div>
      </header>

      {error && <div className={styles.error}>Error: {error.message}</div>}

      {loading && !data ? (
        <div className={styles.empty}>Loading…</div>
      ) : items.length === 0 ? (
        <div className={styles.empty}>
          No alerts. When one fires, or when the <code>snapshots/</code> folder has JPEGs from a
          prior session, they'll show up here.
        </div>
      ) : (
        <table className={styles.table}>
          <thead className={styles.thead}>
            <tr>
              <th className={styles.thSnap}>Snapshot</th>
              <th className={styles.th}>When</th>
              <th className={styles.th}>Species</th>
              <th className={styles.th}>Conf</th>
              <th className={styles.th}>Description</th>
              <th className={styles.th}>Track</th>
            </tr>
          </thead>
          <tbody>{groups.flatMap((g) => renderGroup(g, camera === "", setOpenId))}</tbody>
        </table>
      )}
      <footer className={styles.footer}>
        Ring buffer capacity 500 · rolls oldest first · JPEGs on disk backfilled at startup (marked{" "}
        <span className={styles.badgeHist}>from disk</span> — confidence + description not
        persisted)
      </footer>
      <AlertLightbox items={items} openId={openId} setOpenId={setOpenId} />
    </div>
  );
}

function renderGroup(
  g: GroupedAlerts,
  showCameraBadge: boolean,
  onOpen: (id: number) => void,
): JSX.Element[] {
  return [
    <Row
      key={g.head.id}
      alert={g.head}
      showCameraBadge={showCameraBadge}
      groupSize={g.children.length + 1}
      onOpen={onOpen}
    />,
  ];
}

function Row({
  alert,
  showCameraBadge,
  groupSize,
  onOpen,
}: {
  alert: AlertRow;
  showCameraBadge: boolean;
  groupSize: number;
  onOpen: (id: number) => void;
}): JSX.Element {
  const isRodent = RODENT_SPECIES.has(alert.species);
  const isHist = alert.historical;
  const speciesCls = isHist
    ? `${styles.species} ${styles.speciesHist}`
    : isRodent
      ? `${styles.species} ${styles.speciesRodent}`
      : `${styles.species} ${styles.speciesOther}`;
  const confPct = alert.confidence != null ? `${Math.round(alert.confidence * 100)}%` : "—";
  return (
    <tr className={styles.row}>
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
