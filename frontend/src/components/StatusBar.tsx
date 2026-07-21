import { useStatus } from "../hooks/useStatus";
import { CameraBadge } from "./CameraBadge";
import { CostChip } from "./CostChip";
import { GateFunnelChip } from "./GateFunnelChip";
import { ResourceChip } from "./ResourceChip";

interface StatusBarProps {
  camera: string;
}

/**
 * One-line summary strip for a single camera. Composes CameraBadge +
 * ResourceChip + GateFunnelChip + CostChip. Meant to slot into any
 * page header (StatusDashboard uses it today; the live-preview
 * migration in PR 8+ will reuse it above the stream image).
 *
 * Pattern: composed-of-primitives — each chip is independently
 * useful; StatusBar is just the layout glue. Adding a new chip
 * (e.g. FPS or backend) is a one-liner here, no cross-cutting change.
 */
export function StatusBar({ camera }: StatusBarProps) {
  const { data } = useStatus(camera);
  return (
    <div style={styles.wrap}>
      <CameraBadge cameraId={camera} backend={data?.backend} />
      <ResourceChip camera={camera} />
      <GateFunnelChip camera={camera} />
      <CostChip camera={camera} />
    </div>
  );
}

const styles = {
  wrap: {
    display: "flex",
    flexWrap: "wrap" as const,
    gap: 8,
    alignItems: "center",
    padding: "8px 0",
  },
} as const;
