import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import type { AlertRow } from "../api/alerts";
import { useAlerts } from "../hooks/useAlerts";
import { useCameras } from "../hooks/useCameras";
import { fmtRelative, fmtTs } from "../util/time";

const RODENT_SPECIES = new Set(["rat", "mouse"]);
const GROUP_WINDOW_S = 60;

interface GroupedAlerts {
  head: AlertRow;
  children: AlertRow[];
}

/**
 * Alerts page skeleton — feature parity with the vanilla-JS _ALERTS_HTML
 * template in preview.py, minus the lightbox (that lands in PR 4).
 *
 * Patterns applied:
 *  - Strangler-fig — served at /react/alerts alongside the old /alerts.
 *    Zero-downtime cutover once we're confident.
 *  - Typed API contract — every fetch flows through src/api/*.ts so a
 *    schema drift in Flask surfaces as a TS error at build time.
 *  - Stale-while-revalidate — useAlerts keeps the previous rows on screen
 *    while refetching, so filter changes / auto-refresh don't blank the
 *    table.
 *  - Filter as URL state — species + camera live in localStorage-persisted
 *    UI state; PR 4 could promote to query-string for shareable URLs.
 */
export function AlertsPage() {
  const [species, setSpecies] = useState<string>("");
  const [camera, setCamera] = useState<string>(
    () => localStorage.getItem("alertsCameraFilter") ?? "",
  );
  const [grouped, setGrouped] = useState<boolean>(true);
  const [autoRefresh, setAutoRefresh] = useState<boolean>(true);

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

  // Mark current total as "seen" so the badge on the live-preview page
  // clears on next poll. Same convention as the vanilla-JS alerts page.
  if (data && typeof window !== "undefined") {
    localStorage.setItem("alertsLastSeenTotal", String(data.total));
  }

  return (
    <div style={styles.wrap}>
      <header style={styles.header}>
        <Link to="/" style={styles.title}>
          wildlife-detector — alerts
        </Link>
        <span style={styles.stat}>
          total <b style={styles.b}>{data?.total ?? "–"}</b>
        </span>
        <span style={styles.stat}>
          shown <b style={styles.b}>{items.length}</b>
        </span>
        <div style={styles.tools}>
          <label style={styles.label}>
            species
            <select
              style={styles.select}
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
          <label style={styles.label}>
            camera
            <select
              style={styles.select}
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
          <label style={styles.label}>
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
            />{" "}
            auto
          </label>
          <label style={styles.label}>
            <input
              type="checkbox"
              checked={grouped}
              onChange={(e) => setGrouped(e.target.checked)}
            />{" "}
            group
          </label>
          <Link to="/" style={styles.closeBtn} aria-label="Back to live preview">
            ×
          </Link>
        </div>
      </header>

      {error && <div style={styles.error}>Error: {error.message}</div>}

      {loading && !data ? (
        <div style={styles.empty}>Loading…</div>
      ) : items.length === 0 ? (
        <div style={styles.empty}>
          No alerts. When one fires, or when the <code>snapshots/</code> folder has JPEGs from a
          prior session, they'll show up here.
        </div>
      ) : (
        <table style={styles.table}>
          <thead style={styles.thead}>
            <tr>
              <th style={{ ...styles.th, width: 176 }}>Snapshot</th>
              <th style={styles.th}>When</th>
              <th style={styles.th}>Species</th>
              <th style={styles.th}>Conf</th>
              <th style={styles.th}>Description</th>
              <th style={styles.th}>Track</th>
            </tr>
          </thead>
          <tbody>{groups.flatMap((g) => renderGroup(g, camera === ""))}</tbody>
        </table>
      )}
      <footer style={styles.footer}>
        Ring buffer capacity 500 · rolls oldest first · JPEGs on disk backfilled at startup (marked{" "}
        <span style={styles.badgeHist}>from disk</span> — confidence + description not persisted)
      </footer>
    </div>
  );
}

function renderGroup(g: GroupedAlerts, showCameraBadge: boolean): JSX.Element[] {
  const rows = [
    <Row
      key={g.head.id}
      alert={g.head}
      showCameraBadge={showCameraBadge}
      groupSize={g.children.length + 1}
    />,
  ];
  return rows;
}

function Row({
  alert,
  showCameraBadge,
  groupSize,
}: {
  alert: AlertRow;
  showCameraBadge: boolean;
  groupSize: number;
}): JSX.Element {
  const isRodent = RODENT_SPECIES.has(alert.species);
  const isHist = alert.historical;
  const speciesColor = isHist ? "#667" : isRodent ? "#f66" : "#9c6";
  const confPct = alert.confidence != null ? `${Math.round(alert.confidence * 100)}%` : "—";
  return (
    <tr style={styles.row}>
      <td style={styles.thumbCell}>
        {alert.snapshot ? (
          <img
            style={styles.thumb}
            src={`/snapshots/${encodeURIComponent(alert.snapshot)}`}
            alt="snapshot"
            loading="lazy"
          />
        ) : (
          <div style={styles.noSnapshot}>no snapshot</div>
        )}
      </td>
      <td style={styles.ts}>
        {fmtTs(alert.ts)}
        <span style={styles.rel}> {fmtRelative(alert.ts)}</span>
      </td>
      <td style={{ ...styles.species, color: speciesColor }}>
        {alert.species || "?"}
        {isHist && <span style={styles.badgeHist}>from disk</span>}
        {showCameraBadge && alert.camera_id && (
          <span style={styles.badgeCam}>{alert.camera_id}</span>
        )}
        {groupSize > 1 && <span style={styles.badgeCount}>×{groupSize}</span>}
      </td>
      <td style={styles.conf}>{confPct}</td>
      <td style={styles.desc}>{alert.description ?? ""}</td>
      <td style={styles.track}>{alert.track_id != null ? `#${alert.track_id}` : "—"}</td>
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
  // Consecutive same-track alerts within GROUP_WINDOW_S collapse. Mixing
  // live and historical rows in one group is a mode error — separate them.
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

const styles = {
  wrap: {
    margin: 0,
    background: "#0e0e10",
    color: "#ddd",
    fontFamily: "-apple-system, 'Segoe UI', sans-serif",
    minHeight: "100vh",
  },
  header: {
    display: "flex",
    gap: 16,
    padding: "8px 16px",
    fontSize: 13,
    borderBottom: "1px solid #2a2a30",
    background: "#16161a",
    alignItems: "center",
  },
  title: { color: "#ddd", textDecoration: "none", fontWeight: 600 },
  stat: { color: "#9aa" },
  b: { color: "#ddd", marginLeft: 4 },
  tools: { marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" },
  label: { color: "#9aa", fontSize: 12, display: "inline-flex", gap: 4, alignItems: "center" },
  select: {
    background: "#26262c",
    color: "#ddd",
    border: "1px solid #3a3a40",
    padding: "4px 10px",
    borderRadius: 4,
    fontSize: 12,
  },
  closeBtn: {
    background: "#26262c",
    color: "#ddd",
    border: "1px solid #3a3a40",
    padding: "4px 10px",
    borderRadius: 4,
    fontSize: 14,
    textDecoration: "none",
    lineHeight: 1,
    marginLeft: 8,
    fontWeight: 600,
  },
  error: { padding: "8px 16px", color: "#f88", fontSize: 12 },
  empty: { padding: 40, textAlign: "center" as const, color: "#667", fontSize: 14 },
  table: { width: "100%", borderCollapse: "collapse" as const, fontSize: 13 },
  thead: {
    position: "sticky" as const,
    top: 0,
    background: "#16161a",
    borderBottom: "1px solid #2a2a30",
  },
  th: {
    padding: "8px 12px",
    textAlign: "left" as const,
    fontWeight: 500,
    color: "#9aa",
    fontSize: 12,
  },
  row: { borderBottom: "1px solid #1e1e24" },
  thumbCell: { padding: "8px 12px", width: 176 },
  thumb: { width: 160, height: "auto", display: "block", borderRadius: 3, cursor: "zoom-in" },
  noSnapshot: { color: "#667", fontSize: 11, padding: 12 },
  ts: {
    padding: "8px 12px",
    color: "#9aa",
    fontVariantNumeric: "tabular-nums" as const,
    whiteSpace: "nowrap" as const,
  },
  rel: { color: "#667", fontSize: 11, marginLeft: 4 },
  species: { padding: "8px 12px", fontWeight: 600 },
  conf: { padding: "8px 12px", fontVariantNumeric: "tabular-nums" as const, color: "#ddd" },
  desc: { padding: "8px 12px", color: "#bbc", maxWidth: 480 },
  track: {
    padding: "8px 12px",
    color: "#667",
    fontSize: 11,
    fontVariantNumeric: "tabular-nums" as const,
  },
  badgeHist: {
    display: "inline-block",
    background: "#33333a",
    color: "#aab",
    fontSize: 10,
    padding: "1px 5px",
    borderRadius: 3,
    marginLeft: 6,
    verticalAlign: "middle" as const,
  },
  badgeCam: {
    display: "inline-block",
    background: "#26262c",
    color: "#9cf",
    fontSize: 10,
    padding: "1px 5px",
    borderRadius: 3,
    marginLeft: 6,
    verticalAlign: "middle" as const,
  },
  badgeCount: {
    display: "inline-block",
    background: "#2a6cbf",
    color: "#fff",
    fontSize: 11,
    padding: "1px 6px",
    borderRadius: 8,
    marginLeft: 6,
    fontWeight: 500,
  },
  footer: {
    padding: "8px 16px",
    fontSize: 12,
    color: "#667",
    borderTop: "1px solid #2a2a30",
    textAlign: "center" as const,
  },
} as const;
