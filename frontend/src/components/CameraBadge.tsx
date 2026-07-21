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
    <span style={styles.badge}>
      {cameraId}
      {backend && <span style={styles.backend}> · {backend}</span>}
    </span>
  );
}

const styles = {
  badge: {
    display: "inline-block",
    background: "#26262c",
    color: "#9cf",
    fontSize: 11,
    padding: "2px 8px",
    borderRadius: 4,
    fontFamily: "ui-monospace, monospace",
    fontWeight: 600,
  },
  backend: { color: "#667", fontWeight: 400 },
} as const;
