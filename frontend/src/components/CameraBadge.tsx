import styles from "./CameraBadge.module.css";

interface CameraBadgeProps {
  cameraId: string;
  /** Optional context (e.g. "cascade" backend) rendered as dim suffix. */
  backend?: string;
}

/**
 * Small colored badge for camera identity. Blue text to match the
 * badge convention used on the alerts page (where camera_id is shown
 * on each alert row in unified view). Consistent color everywhere the
 * operator sees a camera name.
 */
export function CameraBadge({ cameraId, backend }: CameraBadgeProps) {
  return (
    <span className={styles.badge}>
      {cameraId}
      {backend && <span className={styles.backend}> · {backend}</span>}
    </span>
  );
}
