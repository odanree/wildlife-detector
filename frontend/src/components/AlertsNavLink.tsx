import { Link } from "react-router-dom";
import { useUnreadAlerts } from "../hooks/useUnreadAlerts";
import styles from "./AlertsNavLink.module.css";

interface AlertsNavLinkProps {
  className?: string;
  /** Set of cameras whose unread counts should sum into the badge.
   *  In practice: the primary + optional secondary from LivePreviewPage
   *  (dual-pane covers both). Empty/undefined = cross-camera "all". */
  cameras?: readonly string[];
}

/**
 * "Alerts →" nav link with an unread-count pill.
 *
 * Badge counts all `cameras` (union scope) so dual-pane sees activity
 * on either visible camera in one badge. Single-camera view still
 * pre-filters the alerts page via `?camera=X` in the link href;
 * multi-camera view drops the filter (list shows all so any of the
 * unread rows are visible).
 */
export function AlertsNavLink({ className, cameras }: AlertsNavLinkProps) {
  const { unread } = useUnreadAlerts(cameras);
  const href =
    cameras && cameras.length === 1
      ? `/alerts?camera=${encodeURIComponent(cameras[0])}`
      : "/alerts";
  const scopeLabel =
    !cameras || cameras.length === 0
      ? "alerts"
      : cameras.length === 1
        ? `${cameras[0]} alerts`
        : `alerts (${cameras.join(" + ")})`;
  return (
    <Link to={href} className={`${className ?? ""} ${styles.link}`}>
      Alerts →
      {unread > 0 && (
        <span className={styles.badge} aria-label={`${unread} unread ${scopeLabel}`}>
          {unread > 99 ? "99+" : unread}
        </span>
      )}
    </Link>
  );
}
