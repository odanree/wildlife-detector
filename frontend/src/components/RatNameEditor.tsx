import { type KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import styles from "./RatNameEditor.module.css";

const SOFT_MAX_LEN = 60;
const SAVE_DEBOUNCE_MS = 500;

interface RatNameEditorProps {
  ratId: number;
  /** Server-side name (rats.notes); null → placeholder "Rat #<id>". */
  name: string | null;
  /** Owner-side writer: optimistic update + PATCH + rollback live in the
   *  parent (single-writer). Resolves on success, rejects on failure. */
  onSave: (name: string | null) => Promise<void>;
  size?: "sm" | "lg";
}

/**
 * Inline rename for a rat. Cold-start naming lives or dies on friction,
 * so: click → type → blur/Enter saves. Autosave is a **debounce
 * coalescer** on blur (500ms) — a blur/refocus flurry while the operator
 * is still deciding produces one PATCH, not five. Esc reverts the draft.
 *
 * Prop → draft sync uses the "adjust state during render off a
 * sentinel" pattern (React docs) rather than a useEffect, so a server
 * refresh that changes `name` re-seeds the draft only when the operator
 * is NOT mid-edit. Keeps the repo's you-might-not-need-an-effect gate
 * green.
 */
export function RatNameEditor({ ratId, name, onSave, size = "sm" }: RatNameEditorProps) {
  const [draft, setDraft] = useState<string>(name ?? "");
  const [nameSentinel, setNameSentinel] = useState<string | null>(name);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  if (name !== nameSentinel) {
    setNameSentinel(name);
    if (!editing) setDraft(name ?? "");
  }

  const timerRef = useRef<number | null>(null);
  const latestRef = useRef({ draft, name, onSave });
  latestRef.current = { draft, name, onSave };

  const flush = useCallback(async () => {
    const { draft: d, name: committed, onSave: save } = latestRef.current;
    const next = d.trim() || null;
    if (next === (committed?.trim() || null)) return; // no-op: nothing changed
    setSaving(true);
    setErr(null);
    try {
      await save(next);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }, []);

  const scheduleSave = useCallback(() => {
    if (timerRef.current != null) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      void flush();
    }, SAVE_DEBOUNCE_MS);
  }, [flush]);

  // Unmount with a pending debounce (operator renamed then clicked
  // straight through to the timeline) → flush immediately rather than
  // dropping the edit on the floor.
  useEffect(
    () => () => {
      if (timerRef.current != null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
        void flush();
      }
    },
    [flush],
  );

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      (e.target as HTMLInputElement).blur(); // blur → scheduleSave
    } else if (e.key === "Escape") {
      e.preventDefault();
      setDraft(name ?? "");
      (e.target as HTMLInputElement).blur();
    }
  };

  const cls = `${styles.input} ${size === "lg" ? styles.inputLg : styles.inputSm} ${
    editing ? styles.inputEditing : ""
  }`;

  return (
    <span
      className={styles.wrap}
      // Cards wrap the editor in a clickable surface; don't let a rename
      // click bubble into navigation.
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => e.stopPropagation()}
      role="presentation"
    >
      <input
        className={cls}
        value={draft}
        placeholder={`Rat #${ratId}`}
        maxLength={SOFT_MAX_LEN}
        aria-label={`name for rat ${ratId}`}
        title="Click to rename · Enter saves · Esc reverts"
        onFocus={() => setEditing(true)}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => {
          setEditing(false);
          scheduleSave();
        }}
        onKeyDown={onKeyDown}
        spellCheck={false}
      />
      {saving && <span className={styles.status}>saving…</span>}
      {err && (
        <span className={styles.err} title={err}>
          save failed
        </span>
      )}
    </span>
  );
}
