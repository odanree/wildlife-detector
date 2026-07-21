import { useState } from "react";
import { type BaselineMode, captureBaseline, clearBaseline } from "../api/baseline";
import { useBaselineMeta } from "../hooks/useBaselineMeta";
import { fmtAgo } from "../util/time";
import styles from "./BaselineControls.module.css";

interface BaselineControlsProps {
  camera: string;
}

/**
 * Capture / clear controls for both day and night baselines of a
 * camera. Ported from the vanilla-JS live-preview page's baseline
 * button strip. Reads meta via useBaselineMeta (already introduced
 * in PR 5) and calls refresh() after mutations so the status text
 * updates immediately.
 *
 * Design notes:
 * - **Explicit-mode capture, not auto-picker.** The pipeline has a
 *   brightness auto-picker that misclassifies IR-lit night as "day"
 *   on the overhead camera. The two Cap buttons force the mode so
 *   the operator gets a deterministic outcome instead of relying on
 *   the heuristic.
 * - **Clear needs a confirm click.** Baselines are cheap to
 *   re-capture but nuking one mid-operation is annoying. A single
 *   soft-confirm (button label becomes "Confirm?" for 2s) is enough
 *   friction to catch fat-fingers without a modal.
 * - **Flash feedback**, no toast. Green "captured" for 2s next to
 *   the button — attention where the eye already is, no separate
 *   notification area to build/maintain.
 */
export function BaselineControls({ camera }: BaselineControlsProps) {
  const { data, error: metaError, refresh } = useBaselineMeta(camera);

  return (
    <div className={styles.wrap}>
      <span className={styles.label}>baseline</span>
      <ModeControls mode="day" camera={camera} slot={data?.day} refresh={refresh} />
      <ModeControls mode="night" camera={camera} slot={data?.night} refresh={refresh} />
      {metaError && <span className={styles.err}>meta: {metaError.message}</span>}
    </div>
  );
}

function ModeControls({
  mode,
  camera,
  slot,
  refresh,
}: {
  mode: BaselineMode;
  camera: string;
  slot: { exists: boolean; ts: number; bytes: number } | undefined;
  refresh: () => void;
}) {
  const [busy, setBusy] = useState<null | "capture" | "clear">(null);
  const [flash, setFlash] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);

  async function doCapture(): Promise<void> {
    setBusy("capture");
    setError(null);
    try {
      await captureBaseline(camera, mode);
      refresh();
      setFlash(`captured ${mode}`);
      window.setTimeout(() => setFlash(null), 2000);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function doClear(): Promise<void> {
    if (!confirmClear) {
      setConfirmClear(true);
      window.setTimeout(() => setConfirmClear(false), 2000);
      return;
    }
    setBusy("clear");
    setError(null);
    setConfirmClear(false);
    try {
      await clearBaseline(camera, mode);
      refresh();
      setFlash(`cleared ${mode}`);
      window.setTimeout(() => setFlash(null), 2000);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const ageSec = slot?.exists ? Date.now() / 1000 - slot.ts : null;

  return (
    <span className={styles.group}>
      <button
        type="button"
        className={`${styles.btn} ${styles.btnCap}`}
        onClick={doCapture}
        disabled={busy !== null}
        title={`Capture the current frame as ${mode} baseline`}
      >
        Cap {mode}
      </button>
      <button
        type="button"
        className={`${styles.btn} ${styles.btnClear}`}
        onClick={doClear}
        disabled={!slot?.exists || busy !== null}
        title={
          slot?.exists
            ? confirmClear
              ? `Click again to confirm deleting the ${mode} baseline`
              : `Delete the ${mode} baseline`
            : `No ${mode} baseline to clear`
        }
      >
        {confirmClear ? "Confirm?" : `Clr ${mode}`}
      </button>
      {flash ? (
        <span className={styles.flash}>✓ {flash}</span>
      ) : error ? (
        <span className={styles.err}>err: {error}</span>
      ) : ageSec !== null ? (
        <span className={styles.status}>{fmtAgo(ageSec)} ago</span>
      ) : (
        <span className={styles.status}>—</span>
      )}
    </span>
  );
}
