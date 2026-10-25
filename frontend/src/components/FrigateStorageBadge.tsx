import { useEffect, useState } from "react";

/**
 * Tile that polls `/api/frigate/storage-health` and surfaces when Frigate's
 * 4TB external drive has silently unmounted. Added 2026-10-25 after an
 * incident where the external drive got detached, Frigate silently fell
 * back to the boot disk, filled it, and crashed — losing ~60 hours of
 * recordings across all cameras before anyone noticed.
 *
 * Three states:
 *   ok       — green, external drive mounted and under 90% full
 *   warning  — yellow, over 90% full (retention knife-edge)
 *   critical — red, under 1TB total (external unmounted) OR over 95% full
 */

type Health = {
  status: "ok" | "warning" | "critical" | "unknown" | "unreachable";
  reason: string | null;
  total_gb: number;
  used_gb: number;
  used_pct: number;
};

export function FrigateStorageBadge(): JSX.Element | null {
  const [health, setHealth] = useState<Health | null>(null);

  useEffect(() => {
    let cancelled = false;
    const fetchOnce = async () => {
      try {
        const r = await fetch("/api/frigate/storage-health");
        if (!r.ok) return;
        const j = (await r.json()) as Health;
        if (!cancelled) setHealth(j);
      } catch {
        /* transient, next poll will retry */
      }
    };
    fetchOnce();
    const id = window.setInterval(fetchOnce, 60_000); // 1-min polling
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  if (!health) return null;
  if (health.status === "ok") {
    return (
      <span
        title={`${health.used_gb} / ${health.total_gb} GB (${health.used_pct}%)`}
        style={{
          display: "inline-block",
          padding: "2px 8px",
          borderRadius: 4,
          background: "#1e4620",
          color: "#9ad99f",
          fontSize: 12,
          fontFamily: "monospace",
        }}
      >
        frigate disk {health.used_pct}%
      </span>
    );
  }
  const palette =
    health.status === "warning"
      ? { bg: "#5a4a0a", fg: "#ffd36b" }
      : { bg: "#5a1a1a", fg: "#ff8c8c" };
  return (
    <span
      title={health.reason || ""}
      style={{
        display: "inline-block",
        padding: "2px 8px",
        borderRadius: 4,
        background: palette.bg,
        color: palette.fg,
        fontSize: 12,
        fontFamily: "monospace",
        fontWeight: 600,
      }}
    >
      frigate disk: {health.status} — {health.reason || "see /api/frigate/storage-health"}
    </span>
  );
}
