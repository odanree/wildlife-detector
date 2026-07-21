import type { ReactNode } from "react";
import styles from "./Chip.module.css";

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
    <span className={styles.chip} title={title}>
      <span className={styles.label}>{label}</span>
      <span className={styles.body}>{children}</span>
    </span>
  );
}

/** Value + optional secondary (e.g. "12% · peak 89%"). Bold value, dim separator. */
export function ChipValue({ value, secondary }: { value: string; secondary?: string }) {
  return (
    <>
      <b className={styles.b}>{value}</b>
      {secondary && (
        <span className={styles.sec}>
          <span className={styles.sep}> / </span>
          {secondary}
        </span>
      )}
    </>
  );
}
