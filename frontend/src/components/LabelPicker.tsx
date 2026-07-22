import { useEffect, useState } from "react";
import { type LabelVerdict, setAlertLabel } from "../api/alerts";
import styles from "./LabelPicker.module.css";

interface LabelPickerProps {
  alertId: number;
  initialVerdict: LabelVerdict;
  initialSpecies: string | null;
  /** Called after a successful label write so parent can update local row state. */
  onLabeled: (verdict: LabelVerdict, species: string | null) => void;
  /** When true, skip the 'tag…' toggle and always show the species dropdown
   *  once a verdict is set. Use in the lightbox modal where the operator
   *  wants species picking one-click away, not hidden behind a popover. */
  showSpeciesDefault?: boolean;
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
  alertId,
  initialVerdict,
  initialSpecies,
  onLabeled,
  showSpeciesDefault = false,
}: LabelPickerProps) {
  const [verdict, setVerdict] = useState<LabelVerdict>(initialVerdict);
  const [species, setSpecies] = useState<string | null>(initialSpecies);
  const [showPicker, setShowPicker] = useState(showSpeciesDefault);
  const [busy, setBusy] = useState(false);

  // Sync local state when parent's overlay updates the effective label —
  // e.g. operator voted from the lightbox modal, or a bulk operation
  // covered this row. useState only initializes on mount, so without
  // this effect the row's picker would stay frozen at the mount-time
  // value until the useAlerts poll (5s) shipped a fresh row and React
  // recreated the component. Loose reference equality in deps is fine
  // because parents build the overlay Map immutably (new Map on write).
  useEffect(() => {
    setVerdict(initialVerdict);
    setSpecies(initialSpecies);
  }, [initialVerdict, initialSpecies]);

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
            apply(verdict, v);
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
