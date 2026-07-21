import { useStatus } from "../hooks/useStatus";
import { Chip } from "./Chip";

interface CostChipProps {
  camera: string;
}

/**
 * Session cost + cache hit-rate chip. Same shape as the vanilla-JS
 * header chip in preview.py (`vlm $X.XXXX · cache YY%`) but rendered
 * as a proper React component with typed data and composed on the
 * shared <Chip /> primitive. Was PR 1's proof-of-pipeline; refactored
 * in PR 7 to use the shared chip design system.
 */
export function CostChip({ camera }: CostChipProps) {
  const { data, error, loading } = useStatus(camera);

  if (loading && !data)
    return (
      <Chip label="vlm" title="Waiting for /status">
        —
      </Chip>
    );
  if (error)
    return (
      <Chip label="vlm" title={error.message}>
        <span style={{ color: "#f88" }}>error</span>
      </Chip>
    );
  if (!data) return null;

  const cost = data.vlm_cost;
  if (!cost) {
    return (
      <Chip label="vlm" title="Backend build predates cost tracker">
        no tracker
      </Chip>
    );
  }
  return (
    <Chip
      label="vlm"
      title="Session-lifetime VLM cost (USD) + prompt-cache hit rate. Cache should stay near 1.0 after warmup."
    >
      <b style={{ color: "#ddd", fontWeight: 600 }}>${cost.cost_usd.toFixed(4)}</b>
      <span style={{ color: "#555", margin: "0 6px" }}>·</span>
      <span>
        cache{" "}
        <b style={{ color: "#ddd", fontWeight: 600 }}>{Math.round(cost.cache_hit_rate * 100)}%</b>
      </span>
    </Chip>
  );
}
