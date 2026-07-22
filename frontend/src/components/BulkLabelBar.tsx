import { useState } from "react";
import { type LabelVerdict, setAlertLabelsBulk } from "../api/alerts";
import styles from "./BulkLabelBar.module.css";

interface BulkLabelBarProps {
  selectedIds: number[];
  onCleared: () => void;
  onApplied: (verdict: LabelVerdict, species: string | null) => void;
}

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

/**
 * Sticky action bar for mass-labeling — appears when ≥1 alerts are
 * selected via row checkboxes. One HTTP round-trip per apply (backend
 * uses IN (?, ?, ...) update in a single transaction), so tagging 100
 * rows is one network call, not 100.
 *
 * Species dropdown auto-switches its options based on chosen verdict:
 * correct → real_* list, incorrect → FP:* list. Unclear/clear-label
 * verdicts don't need a species picker.
 */
export function BulkLabelBar({ selectedIds, onCleared, onApplied }: BulkLabelBarProps) {
  const [verdict, setVerdict] = useState<LabelVerdict>("correct");
  const [species, setSpecies] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const speciesOpts =
    verdict === "correct" ? CORRECT_SPECIES : verdict === "incorrect" ? INCORRECT_SPECIES : [];

  const apply = async () => {
    setBusy(true);
    try {
      const n = await setAlertLabelsBulk(selectedIds, verdict, species || null);
      onApplied(verdict, species || null);
      onCleared();
      // Small confirmation — bulk operations deserve a receipt.
      if (n !== selectedIds.length) {
        alert(
          `Labeled ${n} of ${selectedIds.length} (some rows may have been deleted or out of scope).`,
        );
      }
    } catch (e) {
      alert(`Bulk label failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const clearLabels = async () => {
    if (
      !confirm(`Clear labels on ${selectedIds.length} rows? (verdict, species, notes all removed)`)
    )
      return;
    setBusy(true);
    try {
      await setAlertLabelsBulk(selectedIds, null, null, null);
      onApplied(null, null);
      onCleared();
    } catch (e) {
      alert(`Clear failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={styles.bar}>
      <span className={styles.count}>{selectedIds.length} selected</span>
      <select
        className={styles.select}
        value={verdict ?? ""}
        onChange={(e) => {
          const v = (e.target.value || null) as LabelVerdict;
          setVerdict(v);
          setSpecies("");
        }}
        disabled={busy}
      >
        <option value="correct">✓ correct</option>
        <option value="incorrect">✗ incorrect</option>
        <option value="unclear">? unclear</option>
      </select>
      {(verdict === "correct" || verdict === "incorrect") && (
        <select
          className={styles.select}
          value={species}
          onChange={(e) => setSpecies(e.target.value)}
          disabled={busy}
        >
          <option value="">— species (optional) —</option>
          {speciesOpts.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      )}
      <button type="button" className={styles.apply} onClick={apply} disabled={busy}>
        Apply to {selectedIds.length}
      </button>
      <button type="button" className={styles.clearLabels} onClick={clearLabels} disabled={busy}>
        Clear labels
      </button>
      <button type="button" className={styles.cancel} onClick={onCleared} disabled={busy}>
        Deselect
      </button>
    </div>
  );
}
