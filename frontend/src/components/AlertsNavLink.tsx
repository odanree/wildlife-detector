import { Link } from "react-router-dom";
import { useUnreadAlerts } from "../hooks/useUnreadAlerts";
import styles from "./AlertsNavLink.module.css";

interface AlertsNavLinkProps {
  className?: string;
}

/**
 * "Alerts →" nav link with an unread-count pill. Cross-camera count
 * from /api/alerts?limit=1's `total` field, diffed against the
 * last-seen watermark that AlertsPage stamps into localStorage.
 *
 * Badge is absolutely-positioned relative to the link so it doesn't
 * shift the toolbar layout as the count grows/shrinks. Capped at
 * "99+" for the same reason.
 */
export function AlertsNavLink({ className }: AlertsNavLinkProps) {
  const { unread } = useUnreadAlerts();
  return (
    <Link to="/alerts" className={`${className ?? ""} ${styles.link}`}>
      Alerts →
      {unread > 0 && (
        <span className={styles.badge} aria-label={`${unread} unread alerts`}>
          {unread > 99 ? "99+" : unread}
        </span>
      )}
    </Link>
  );
}
