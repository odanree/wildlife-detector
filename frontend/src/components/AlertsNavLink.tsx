import { Link } from "react-router-dom";
import { useUnreadAlerts } from "../hooks/useUnreadAlerts";
import styles from "./AlertsNavLink.module.css";

interface AlertsNavLinkProps {
  className?: string;
  /** Scope badge count + link destination to a specific camera.
   *  Undefined = cross-camera badge + "/alerts" (no filter). */
  camera?: string;
}

/**
 * "Alerts →" nav link with an unread-count pill.
 *
 * When `camera` is provided (e.g. the primary pane's camera), the
 * badge counts alerts for THAT camera only and the link navigates to
 * `/alerts?camera=<id>` so the alerts page pre-filters to match. This
 * prevents a yard viewer from being alarmed by rooftop's badge ticks.
 *
 * Badge is absolutely-positioned so a growing count doesn't shift
 * toolbar layout. Capped at "99+".
 */
export function AlertsNavLink({ className, camera }: AlertsNavLinkProps) {
  const { unread } = useUnreadAlerts(camera);
  const href = camera ? `/alerts?camera=${encodeURIComponent(camera)}` : "/alerts";
  const badgeLabel = camera ? `${unread} unread ${camera} alerts` : `${unread} unread alerts`;
  return (
    <Link to={href} className={`${className ?? ""} ${styles.link}`}>
      Alerts →
      {unread > 0 && (
        <span className={styles.badge} aria-label={badgeLabel}>
          {unread > 99 ? "99+" : unread}
        </span>
      )}
    </Link>
  );
}
