import { useState } from "react";
import type { LabelVerdict } from "../api/alerts";
import styles from "./LabelPicker.module.css";

interface LabelPickerProps {
  /** Current verdict — parent-owned. Component never mutates this locally. */
  verdict: LabelVerdict;
  /** Current species tag — parent-owned. */
  species: string | null;
  /** True while a server write is in flight — parent-owned. Disables buttons. */
  busy?: boolean;
  /** Fires on any user click / dropdown change. Parent should apply an
   *  optimistic overlay update AND kick off the server write. Component
   *  is pure display + dispatch — no local state for verdict/species. */
  onChange: (verdict: LabelVerdict, species: string | null) => void;
  /** When true, skip the 'tag…' toggle and always show the species dropdown
   *  once a verdict is set. Use in the lightbox modal where the operator
   *  wants species picking one-click away, not hidden behind a popover. */
  showSpeciesDefault?: boolean;
}

/**
 * Two-tier labeling UI for supervised training data collection.
 *
 * Fully controlled — verdict + species come from props only. Parent owns
 * both the state (via labelOverlay Map merged with server row data) and
 * the side effect (setAlertLabel call). This keeps a single source of
 * truth for the label and avoids the derived-state anti-pattern where an
 * internal useState shadows props and drifts under prop changes.
 *
 * The only local state is UI-only: `showPicker` for the manual popover
 * toggle. Neither drives labeling behavior.
 *
 * Tier 1: quick ✔ / ❌ / ? — one click, no popover. Applies verdict
 *         with no fine-grained species tag. The 80/20 case.
 *
 * Tier 2: click a verdict button to reveal the species-detail dropdown.
 *         For "correct" → pick which real species. For "incorrect" →
 *         pick the FP category. Fine-grained data for training but
 *         not required.
 *
 * Visual state: the active verdict button is highlighted so operator sees
 * which way this row is labeled at a glance. This is the primary signal
 * that separates "labeled" from "unlabeled" rows in the table view.
 */
const CORRECT_SPECIES = [
  // Rodent species merged — mouse vs rat is usually indistinguishable at
  // IR viewing distance for the yard cams, and for a binary rodent-vs-FP
  // classifier the finer split is noise. Bring separate species back
  // later if we ever want to distinguish (e.g. eradication reporting).
  "real_rodent",
  "real_raccoon",
  "real_opossum",
  "real_cat",
  "real_dog",
  "real_squirrel",
  "real_bird",
  "real_other",
];
const INCORRECT_SPECIES = [
  "FP:insect",
  "FP:reflection",
  "FP:shadow",
  "FP:human",
  "FP:noise",
  "FP:other",
];

export function LabelPicker({
  verdict,
  species,
  busy = false,
  onChange,
  showSpeciesDefault = false,
}: LabelPickerProps) {
  const [showPicker, setShowPicker] = useState(showSpeciesDefault);
  const speciesOpts = verdict === "incorrect" ? INCORRECT_SPECIES : CORRECT_SPECIES;
  return (
    <div className={styles.wrap}>
      <button
        type="button"
        className={`${styles.btn} ${styles.correct} ${verdict === "correct" ? styles.active : ""}`}
        title="Correct detection — this alert is a real animal"
        onClick={() => onChange("correct", null)}
        disabled={busy}
      >
        ✓
      </button>
      <button
        type="button"
        className={`${styles.btn} ${styles.incorrect} ${verdict === "incorrect" ? styles.active : ""}`}
        title="False positive — this is not what the model claimed"
        onClick={() => onChange("incorrect", null)}
        disabled={busy}
      >
        ✗
      </button>
      <button
        type="button"
        className={`${styles.btn} ${styles.unclear} ${verdict === "unclear" ? styles.active : ""}`}
        title="Unclear — can't tell from the snapshot"
        onClick={() => onChange("unclear", null)}
        disabled={busy}
      >
        ?
      </button>
      {verdict != null && !showSpeciesDefault && (
        <button
          type="button"
          className={styles.detailBtn}
          title="Set fine-grained species / FP category"
          onClick={() => setShowPicker((s) => !s)}
        >
          {species ? species.replace(/^(real_|FP:)/, "") : "tag…"}
        </button>
      )}
      {showPicker && verdict != null && verdict !== "unclear" && (
        <select
          className={styles.speciesSel}
          value={species ?? ""}
          onChange={(e) => {
            const v = e.target.value || null;
            onChange(verdict, v);
            // Only auto-close the popover when it's the manual-toggle mode
            // — in showSpeciesDefault mode we keep the dropdown visible
            // so operator can adjust the pick without re-opening.
            if (!showSpeciesDefault) setShowPicker(false);
          }}
          disabled={busy}
        >
          <option value="">— pick species —</option>
          {speciesOpts.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      )}
    </div>
  );
}
