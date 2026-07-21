import { useCallback, useEffect } from "react";
import type { AlertRow } from "../api/alerts";
import { fmtTs } from "../util/time";

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

  // Keyboard handler + body scroll lock. Both scoped to "lightbox is
  // open" so background page keeps its own keybindings when closed.
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
  const speciesColor = isRodent ? "#f66" : "#9c6";
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
      style={styles.backdrop}
      // biome-ignore lint/a11y/useSemanticElements: native <dialog> fights React's declarative model; see comment above
      role="dialog"
      aria-modal="true"
      aria-label="alert snapshot viewer"
      onClick={(e) => {
        // Click backdrop (not children) to close.
        if (e.target === e.currentTarget) close();
      }}
    >
      <button style={styles.closeBtn} onClick={close} aria-label="close" type="button">
        ×
      </button>
      <button
        style={{ ...styles.chev, ...styles.chevLeft, opacity: canPrev ? 1 : 0.3 }}
        onClick={() => go(-1)}
        disabled={!canPrev}
        aria-label="previous"
        type="button"
      >
        ‹
      </button>
      <button
        style={{ ...styles.chev, ...styles.chevRight, opacity: canNext ? 1 : 0.3 }}
        onClick={() => go(1)}
        disabled={!canNext}
        aria-label="next"
        type="button"
      >
        ›
      </button>
      <div style={styles.inner}>
        <img
          style={styles.img}
          src={`/snapshots/${encodeURIComponent(current.snapshot)}`}
          alt="alert snapshot"
        />
        <div style={styles.meta}>
          <div>
            <span style={{ ...styles.species, color: speciesColor }}>{current.species || "?"}</span>{" "}
            {current.camera_id && <span style={styles.badgeCam}>{current.camera_id}</span>} ·{" "}
            {fmtTs(current.ts)} · conf {confPct} · track #{current.track_id ?? "—"}
          </div>
          <div style={styles.desc}>{current.description ?? ""}</div>
        </div>
        <div style={styles.pos}>
          {currentIdx + 1} / {navList.length}
        </div>
      </div>
    </div>
  );
}

const styles = {
  backdrop: {
    position: "fixed" as const,
    inset: 0,
    zIndex: 9999,
    background: "rgba(0,0,0,0.92)",
    display: "flex",
    alignItems: "flex-start",
    justifyContent: "center",
    padding: "24px 80px",
    overflowY: "auto" as const,
  },
  inner: {
    display: "flex",
    flexDirection: "column" as const,
    alignItems: "center",
    gap: 12,
    maxWidth: "100%",
  },
  img: {
    display: "block",
    maxWidth: "100%",
    maxHeight: "calc(100vh - 200px)",
    objectFit: "contain" as const,
    borderRadius: 4,
    background: "#000",
  },
  meta: {
    color: "#ddd",
    fontSize: 13,
    textAlign: "center" as const,
    lineHeight: 1.4,
    maxWidth: 900,
  },
  species: { fontWeight: 600 },
  desc: {
    color: "#aab",
    marginTop: 4,
    fontSize: 12,
    display: "-webkit-box" as const,
    WebkitLineClamp: 2 as const,
    WebkitBoxOrient: "vertical" as const,
    overflow: "hidden" as const,
    textOverflow: "ellipsis" as const,
  },
  pos: { color: "#667", fontSize: 12, fontVariantNumeric: "tabular-nums" as const },
  closeBtn: {
    position: "absolute" as const,
    top: 16,
    right: 20,
    color: "#ddd",
    fontSize: 32,
    background: "transparent",
    border: "none",
    cursor: "pointer",
    width: 40,
    height: 40,
    lineHeight: 1,
  },
  chev: {
    position: "absolute" as const,
    top: "50%",
    transform: "translateY(-50%)",
    background: "rgba(38, 38, 44, 0.7)",
    color: "#ddd",
    border: "1px solid #3a3a40",
    width: 48,
    height: 64,
    fontSize: 24,
    cursor: "pointer",
    borderRadius: 4,
  },
  chevLeft: { left: 20 },
  chevRight: { right: 20 },
  badgeCam: {
    display: "inline-block",
    background: "#26262c",
    color: "#9cf",
    fontSize: 10,
    padding: "1px 5px",
    borderRadius: 3,
    marginLeft: 6,
    verticalAlign: "middle" as const,
  },
} as const;
