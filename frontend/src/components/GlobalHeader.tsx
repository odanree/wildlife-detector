import { Link, useLocation } from "react-router-dom";
import { useCameras } from "../hooks/useCameras";
import { AlertsNavLink } from "./AlertsNavLink";
import styles from "./GlobalHeader.module.css";

interface GlobalHeaderProps {
  /** Optional slot for page-specific controls (camera dropdown,
   *  filters, buttons) rendered on the right side. Anything the page
   *  itself uniquely owns goes here. Nav links + alerts badge stay
   *  in the global part on the left. */
  right?: React.ReactNode;
}

/**
 * Shared header rendered on every page. Owns the cross-page nav
 * (Preview / Alerts / Baselines / Status) and the unread-alerts
 * badge so the operator sees new activity regardless of which page
 * they're on.
 *
 * Badge scope: sum across ALL cameras in the roster (not just visible
 * panes on live-preview). Rationale: the badge represents
 * system-wide unread — the same number whether you're on preview,
 * alerts, or baselines. Consistent > contextual for a top-of-page
 * indicator.
 *
 * Active-route highlight: the current path (matched against the
 * route pattern) gets a subtle active class so operators know where
 * they are without reading the URL.
 */
export function GlobalHeader({ right }: GlobalHeaderProps) {
  const { data: camerasData } = useCameras();
  const cameras = camerasData?.cameras ?? [];
  const { pathname } = useLocation();
  const isActive = (path: string) => pathname === path || pathname.startsWith(`${path}/`);
  const linkCls = (path: string) =>
    `${styles.linkBtn} ${isActive(path) ? styles.linkBtnActive : ""}`;

  return (
    <header className={styles.header}>
      <Link to="/preview" className={styles.title}>
        wildlife-detector
      </Link>
      <nav className={styles.nav}>
        <Link to="/preview" className={linkCls("/preview")}>
          Preview
        </Link>
        <AlertsNavLink
          className={linkCls("/alerts")}
          cameras={cameras.length > 0 ? cameras : undefined}
        />
        <Link to="/baselines" className={linkCls("/baselines")}>
          Baselines
        </Link>
        <Link to="/status" className={linkCls("/status")}>
          Status
        </Link>
      </nav>
      {right && <div className={styles.right}>{right}</div>}
    </header>
  );
}
