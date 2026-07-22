import { useState } from "react";
import { type LabelVerdict, setAlertLabel } from "../api/alerts";
import styles from "./LabelPicker.module.css";

interface LabelPickerProps {
  alertId: number;
  initialVerdict: LabelVerdict;
  initialSpecies: string | null;
  /** Called after a successful label write so parent can update local row state. */
  onLabeled: (verdict: LabelVerdict, species: string | null) => void;
}

/**
 * Two-tier labeling UI for supervised training data collection.
 *
 * Tier 1: quick ✔ / ❌ / clear — one click, no popover. Applies verdict
 *         with no fine-grained species tag. The 80/20 case: operator
 *         eyeballs the snapshot, votes, moves on.
 *
 * Tier 2: click a verdict button to reveal the species-detail dropdown.
 *         For "correct" verdicts → pick which real species. For "incorrect"
 *         → pick the FP category. Fine-grained data for later training,
 *         but not required.
 *
 * Visual state: the active verdict button is highlighted so operator sees
 * which way this row is labeled at a glance. This is the primary signal
 * that separates "labeled" from "unlabeled" rows in the table view.
 */
const CORRECT_SPECIES = [
  "real_rat",
  "real_mouse",
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
  alertId,
  initialVerdict,
  initialSpecies,
  onLabeled,
}: LabelPickerProps) {
  const [verdict, setVerdict] = useState<LabelVerdict>(initialVerdict);
  const [species, setSpecies] = useState<string | null>(initialSpecies);
  const [showPicker, setShowPicker] = useState(false);
  const [busy, setBusy] = useState(false);

  const apply = async (v: LabelVerdict, sp: string | null) => {
    setBusy(true);
    try {
      await setAlertLabel(alertId, v, sp);
      setVerdict(v);
      setSpecies(sp);
      onLabeled(v, sp);
    } catch (e) {
      alert(`Label failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const speciesOpts = verdict === "incorrect" ? INCORRECT_SPECIES : CORRECT_SPECIES;

  return (
    <div className={styles.wrap}>
      <button
        type="button"
        className={`${styles.btn} ${styles.correct} ${verdict === "correct" ? styles.active : ""}`}
        title="Correct detection — this alert is a real animal"
        onClick={() => apply("correct", null)}
        disabled={busy}
      >
        ✓
      </button>
      <button
        type="button"
        className={`${styles.btn} ${styles.incorrect} ${verdict === "incorrect" ? styles.active : ""}`}
        title="False positive — this is not what the model claimed"
        onClick={() => apply("incorrect", null)}
        disabled={busy}
      >
        ✗
      </button>
      <button
        type="button"
        className={`${styles.btn} ${styles.unclear} ${verdict === "unclear" ? styles.active : ""}`}
        title="Unclear — can't tell from the snapshot"
        onClick={() => apply("unclear", null)}
        disabled={busy}
      >
        ?
      </button>
      {verdict != null && (
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
            apply(verdict, v);
            setShowPicker(false);
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
