import type { ReactNode } from "react";

interface ChipProps {
  /** Label shown at chip start, low-emphasis color. */
  label: string;
  /** Tooltip on hover — describes what the metric means and its acceptable range. */
  title?: string;
  children: ReactNode;
}

/**
 * Shared visual primitive for header/status chips. Every subsystem
 * chip (Cost, Resources, GateFunnel, CameraBadge, etc.) composes this
 * so a design tweak (padding, border, font) lands in one place.
 *
 * Pattern: shared visual primitive — the design-system version of DRY.
 * Prevents the "each page invented its own chip styling" drift the
 * vanilla JS templates suffered from.
 */
export function Chip({ label, title, children }: ChipProps) {
  return (
    <span style={styles.chip} title={title}>
      <span style={styles.label}>{label}</span>
      <span style={styles.body}>{children}</span>
    </span>
  );
}

/** Value + optional secondary (e.g. "12% · peak 89%"). Bold value, dim separator. */
export function ChipValue({
  value,
  secondary,
}: {
  value: string;
  secondary?: string;
}) {
  return (
    <>
      <b style={styles.b}>{value}</b>
      {secondary && (
        <span style={styles.sec}>
          <span style={styles.sep}> / </span>
          {secondary}
        </span>
      )}
    </>
  );
}

const styles = {
  chip: {
    display: "inline-flex",
    alignItems: "baseline",
    gap: 4,
    background: "#1a1a20",
    border: "1px solid #26262c",
    borderRadius: 4,
    padding: "6px 10px",
    fontSize: 13,
    color: "#9aa",
    fontFamily: "ui-monospace, monospace",
    whiteSpace: "nowrap" as const,
  },
  label: { color: "#667" },
  body: { color: "#9aa" },
  b: { color: "#ddd", fontWeight: 600 },
  sec: { color: "#9aa" },
  sep: { color: "#555" },
} as const;
