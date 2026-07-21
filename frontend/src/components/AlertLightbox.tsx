import { useCallback, useEffect } from "react";
import type { AlertRow } from "../api/alerts";
import { fmtTs } from "../util/time";
import styles from "./AlertLightbox.module.css";

const RODENT_SPECIES = new Set(["rat", "mouse"]);

interface AlertLightboxProps {
  /** All alerts currently visible in the table — the nav list. */
  items: AlertRow[];
  /** ID of the alert currently open, or null when closed. */
  openId: number | null;
  /** Setter — pass null to close, or an alert id to open. */
  setOpenId: (id: number | null) => void;
}

/**
 * Full-viewport modal for eyeballing a snapshot at full resolution.
 * Ports the vanilla-JS lightbox from preview.py into a React component
 * with three architectural improvements:
 *
 *  - **ID-based navigation, not index-based.** Old page tracked "current
 *    index in the visible list." When the 5s poll dropped in a new alert
 *    at position 0, the lightbox's index still pointed at the old
 *    position — silently shifting the viewer to a different snapshot.
 *    Anchoring on alert.id keeps the same crop in view across polls.
 *  - **Keyboard as command dispatch, not state mutation.** One effect
 *    owns the keydown handler; it translates keys into intent (close /
 *    prev / next) then delegates. Adding more shortcuts later is one
 *    switch case, not one useEffect per key.
 *  - **Body scroll lock via cleanup**. document.body.overflow is
 *    restored on unmount / close, not left "hidden" if the component
 *    dies mid-open.
 */
export function AlertLightbox({ items, openId, setOpenId }: AlertLightboxProps) {
  // Filter to only items with snapshots — nothing to show without one,
  // and prev/next should skip over them cleanly.
  const navList = items.filter((a) => a.snapshot);
  const currentIdx = openId == null ? -1 : navList.findIndex((a) => a.id === openId);
  const current = currentIdx >= 0 ? navList[currentIdx] : null;

  const close = useCallback(() => setOpenId(null), [setOpenId]);
  const go = useCallback(
    (dir: -1 | 1) => {
      if (currentIdx < 0) return;
      const next = currentIdx + dir;
      if (next < 0 || next >= navList.length) return;
      setOpenId(navList[next].id);
    },
    [currentIdx, navList, setOpenId],
  );

  useEffect(() => {
    if (!current) return;
    const originalOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    function onKey(e: KeyboardEvent): void {
      switch (e.key) {
        case "Escape":
          close();
          break;
        case "ArrowLeft":
          go(-1);
          break;
        case "ArrowRight":
          go(1);
          break;
      }
    }
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = originalOverflow;
    };
  }, [current, close, go]);

  if (!current || !current.snapshot) return null;

  const isRodent = RODENT_SPECIES.has(current.species);
  const speciesCls = `${styles.species} ${isRodent ? styles.speciesRodent : styles.speciesOther}`;
  const confPct = current.confidence != null ? `${Math.round(current.confidence * 100)}%` : "—";
  const canPrev = currentIdx > 0;
  const canNext = currentIdx < navList.length - 1;

  return (
    // Backdrop uses a native <dialog>-style layout but implemented on a div
    // because we need a click-to-close backdrop AND a controlled-open state.
    // Native <dialog> requires imperative .showModal() from a ref, which
    // fights React's declarative model. Backdrop-click-to-close is a bonus
    // affordance; the primary close paths (× button, Esc key) are keyboard-
    // accessible and covered by the useEffect above.
    // biome-ignore lint/a11y/useKeyWithClickEvents: primary close paths (× button, Esc) are keyboard-accessible; backdrop-click is a bonus
    <div
      className={styles.backdrop}
      // biome-ignore lint/a11y/useSemanticElements: native <dialog> fights React's declarative model; see comment above
      role="dialog"
      aria-modal="true"
      aria-label="alert snapshot viewer"
      onClick={(e) => {
        if (e.target === e.currentTarget) close();
      }}
    >
      <button className={styles.closeBtn} onClick={close} aria-label="close" type="button">
        ×
      </button>
      <button
        className={`${styles.chev} ${styles.chevLeft}`}
        onClick={() => go(-1)}
        disabled={!canPrev}
        aria-label="previous"
        type="button"
      >
        ‹
      </button>
      <button
        className={`${styles.chev} ${styles.chevRight}`}
        onClick={() => go(1)}
        disabled={!canNext}
        aria-label="next"
        type="button"
      >
        ›
      </button>
      <div className={styles.inner}>
        <img
          className={styles.img}
          src={`/snapshots/${encodeURIComponent(current.snapshot)}`}
          alt="alert snapshot"
        />
        <div className={styles.meta}>
          <div>
            <span className={speciesCls}>{current.species || "?"}</span>{" "}
            {current.camera_id && <span className={styles.badgeCam}>{current.camera_id}</span>} ·{" "}
            {fmtTs(current.ts)} · conf {confPct} · track #{current.track_id ?? "—"}
          </div>
          <div className={styles.desc}>{current.description ?? ""}</div>
        </div>
        <div className={styles.pos}>
          {currentIdx + 1} / {navList.length}
        </div>
      </div>
    </div>
  );
}
