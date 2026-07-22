import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import type { AlertRow } from "../api/alerts";
import { AlertLightbox } from "../components/AlertLightbox";
import { GlobalHeader } from "../components/GlobalHeader";
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
        <table className={styles.table}>
          <thead className={styles.thead}>
            <tr>
              <th className={styles.thSnap}>Snapshot</th>
              <th className={styles.th}>When</th>
              <th className={styles.th}>Species</th>
              <th className={styles.th}>Conf</th>
              <th className={styles.th}>Description</th>
              <th className={styles.th}>Track</th>
              <th className={styles.th}>Replay</th>
            </tr>
          </thead>
          <tbody>
            {groups.flatMap((g) =>
              renderGroup(g, camera === "", setOpenId, initialSeenId ?? Number.POSITIVE_INFINITY),
            )}
          </tbody>
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
  unreadThreshold: number,
): JSX.Element[] {
  return [
    <Row
      key={g.head.id}
      alert={g.head}
      showCameraBadge={showCameraBadge}
      groupSize={g.children.length + 1}
      onOpen={onOpen}
      isUnread={g.head.id > unreadThreshold}
    />,
  ];
}

function Row({
  alert,
  showCameraBadge,
  groupSize,
  onOpen,
  isUnread,
}: {
  alert: AlertRow;
  showCameraBadge: boolean;
  groupSize: number;
  onOpen: (id: number) => void;
  isUnread: boolean;
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
    <tr className={`${styles.row} ${isUnread ? styles.rowUnread : ""}`}>
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
      <td className={styles.track}>
        <ReplayButton alertId={alert.id} />
      </td>
    </tr>
  );
}

/**
 * Opens the alert's timestamp in an external RTSP player (VLC / mpv).
 * Fetches the NVR playback URL from the backend and hands it to the
 * OS via a plain rtsp:// link. Copy-to-clipboard fallback for when the
 * OS has no registered rtsp:// handler.
 *
 * Requires NVR_CHANNEL_<CAMERA> env on the web container to hit the
 * right channel (see /api/alerts/<id>/playback-url note field).
 */
function ReplayButton({ alertId }: { alertId: number }): JSX.Element {
  const onClick = async () => {
    try {
      const r = await fetch(`/api/alerts/${alertId}/playback-url`);
      if (!r.ok) {
        alert(`Playback URL fetch failed: HTTP ${r.status}`);
        return;
      }
      const j = (await r.json()) as { url: string; note?: string };
      // Belt: try to launch the OS rtsp:// handler (VLC/mpv on Windows/Mac
      // register themselves as handlers by default). Suspenders: also copy
      // to clipboard so operator can paste into VLC → "Open Network Stream"
      // if the handler isn't registered.
      try {
        await navigator.clipboard.writeText(j.url);
      } catch {
        /* clipboard blocked in insecure context — non-fatal */
      }
      window.location.href = j.url;
      if (j.note) {
        // Slight delay so the rtsp:// navigation is already dispatched.
        setTimeout(() => alert(`Playback URL copied to clipboard.\n${j.note}`), 200);
      }
    } catch (e) {
      alert(`Playback URL error: ${e instanceof Error ? e.message : String(e)}`);
    }
  };
  return (
    <button
      type="button"
      onClick={onClick}
      className={styles.replayBtn}
      title="Open in VLC / mpv via rtsp:// (URL also copied to clipboard)"
    >
      Replay
    </button>
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
