import {
  type MouseEvent as ReactMouseEvent,
  type WheelEvent as ReactWheelEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import type { AlertRow } from "../api/alerts";
import { fmtTs } from "../util/time";
import styles from "./AlertLightbox.module.css";

const RODENT_SPECIES = new Set(["rat", "mouse"]);
const ZOOM_MIN = 1;
const ZOOM_MAX = 8;
const ZOOM_STEP = 0.25;

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
 *
 *  - **ID-based navigation, not index-based.** Anchoring on alert.id
 *    keeps the same crop in view across 5s polls (index-based would
 *    silently shift when a new alert lands at position 0).
 *  - **Keyboard as command dispatch.** One effect owns keydown,
 *    translates keys → intent → delegate.
 *  - **Wheel-to-zoom, click-drag-to-pan** on the snapshot img.
 *    Zoom is cursor-anchored (pixel under cursor stays put); pan
 *    resets when navigating to another snapshot or on double-click.
 *    Lightweight local-state implementation (no useZoom hook because
 *    the lightbox doesn't need per-camera localStorage persistence —
 *    each snapshot is a one-off view).
 *  - **Body scroll lock via cleanup**. Restored on unmount / close.
 */
export function AlertLightbox({ items, openId, setOpenId }: AlertLightboxProps) {
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

  // ── Zoom + pan state ─────────────────────────────────────────────
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const imgRef = useRef<HTMLImageElement>(null);
  const dragStartRef = useRef<{
    mouseX: number;
    mouseY: number;
    panX: number;
    panY: number;
  } | null>(null);

  // Reset zoom + pan when navigating to another snapshot (or closing).
  // Different image → new content → user shouldn't inherit prior crop.
  // biome-ignore lint/correctness/useExhaustiveDependencies: openId IS the fire trigger; body only calls setters
  useEffect(() => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
  }, [openId]);

  const resetZoom = useCallback(() => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
  }, []);

  // Cursor-anchored wheel zoom: keep the image pixel under the mouse
  // fixed as zoom changes. Same math as the live-preview useZoom hook
  // but self-contained (no CSS-var publishing, no localStorage).
  const onWheel = useCallback((e: ReactWheelEvent<HTMLImageElement>) => {
    e.preventDefault();
    const img = imgRef.current;
    if (!img) return;
    setZoom((oldZoom) => {
      const dir = e.deltaY < 0 ? 1 : -1;
      const newZoom = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, oldZoom + dir * ZOOM_STEP));
      if (newZoom === oldZoom) return oldZoom;
      const rect = img.getBoundingClientRect();
      const fracX = rect.width > 0 ? (e.clientX - rect.left) / rect.width : 0.5;
      const fracY = rect.height > 0 ? (e.clientY - rect.top) / rect.height : 0.5;
      const scale = newZoom / oldZoom;
      const newW = rect.width * scale;
      const newH = rect.height * scale;
      setPan((p) => ({
        x: p.x - fracX * (newW - rect.width),
        y: p.y - fracY * (newH - rect.height),
      }));
      return newZoom;
    });
  }, []);

  const onImgMouseDown = useCallback(
    (e: ReactMouseEvent<HTMLImageElement>) => {
      if (zoom <= 1) return; // no pan at rest scale
      e.preventDefault();
      dragStartRef.current = { mouseX: e.clientX, mouseY: e.clientY, panX: pan.x, panY: pan.y };
    },
    [zoom, pan.x, pan.y],
  );

  useEffect(() => {
    function onMove(e: MouseEvent) {
      const start = dragStartRef.current;
      if (!start) return;
      setPan({
        x: start.panX + (e.clientX - start.mouseX),
        y: start.panY + (e.clientY - start.mouseY),
      });
    }
    function onUp() {
      dragStartRef.current = null;
    }
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, []);

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
        case "0":
        case "Home":
          resetZoom();
          break;
      }
    }
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = originalOverflow;
    };
  }, [current, close, go, resetZoom]);

  if (!current || !current.snapshot) return null;

  const isRodent = RODENT_SPECIES.has(current.species);
  const speciesCls = `${styles.species} ${isRodent ? styles.speciesRodent : styles.speciesOther}`;
  const confPct = current.confidence != null ? `${Math.round(current.confidence * 100)}%` : "—";
  const canPrev = currentIdx > 0;
  const canNext = currentIdx < navList.length - 1;

  return (
    // biome-ignore lint/a11y/useKeyWithClickEvents: primary close paths (× button, Esc) are keyboard-accessible; backdrop-click is a bonus
    <div
      className={styles.backdrop}
      // biome-ignore lint/a11y/useSemanticElements: native <dialog> fights React's declarative model
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
        {/* biome-ignore lint/a11y/useKeyWithClickEvents: img uses mouse-only affordances (wheel zoom, click-drag pan); reset via keyboard "0"/Home */}
        <div className={styles.imgViewport}>
          <img
            ref={imgRef}
            className={styles.img}
            src={`/snapshots/${encodeURIComponent(current.snapshot)}`}
            alt="alert snapshot"
            onWheel={onWheel}
            onMouseDown={onImgMouseDown}
            onDoubleClick={resetZoom}
            style={{
              transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
              cursor: zoom > 1 ? (dragStartRef.current ? "grabbing" : "grab") : "zoom-in",
              transformOrigin: "top left",
              userSelect: "none",
            }}
            draggable={false}
          />
        </div>
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
          {zoom > 1 && (
            <span className={styles.zoomBadge}>
              {" "}
              · {zoom.toFixed(2)}× — double-click or "0" to reset
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
