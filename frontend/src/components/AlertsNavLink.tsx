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
 * "Alerts →" nav link with TWO badges:
 *   - **red badge** = unread (per-user, click-driven watermark drift).
 *     Signals "new activity since you were last active" — decrements on
 *     any acknowledge action (open lightbox, label from row, bulk-label).
 *     Clearable via the "Mark all as read" button on the alerts page.
 *   - **amber badge** = unlabeled (server-side `label_verdict IS NULL`
 *     count). Signals "still needs your verdict" — how much labeling
 *     work is left. Decrements only when a row is actually labeled.
 *
 * The two axes are independent by design — you can have unread=5,
 * unlabeled=42 (clicked but not labeled) or unread=42, unlabeled=5
 * (labeled without clicking, if any code path skips `markAlertRead`).
 * They only align by coincidence.
 *
 * Badge counts all `cameras` (union scope) so dual-pane sees activity
 * on either visible camera in one badge. Single-camera view still
 * pre-filters the alerts page via `?camera=X` in the link href;
 * multi-camera view drops the filter (list shows all so any of the
 * unread rows are visible).
 */
export function AlertsNavLink({ className, cameras }: AlertsNavLinkProps) {
  const { unread, unlabeled } = useUnreadAlerts(cameras);
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
        <span
          className={styles.badge}
          title={`${unread} unread ${scopeLabel} — clears on click`}
          aria-label={`${unread} unread ${scopeLabel}`}
        >
          {unread > 99 ? "99+" : unread}
        </span>
      )}
      {unlabeled > 0 && (
        <span
          className={styles.badgeUnlabeled}
          title={`${unlabeled} unlabeled ${scopeLabel} — clears on label`}
          aria-label={`${unlabeled} unlabeled ${scopeLabel}`}
        >
          {unlabeled > 99 ? "99+" : unlabeled}
        </span>
      )}
    </Link>
  );
}
