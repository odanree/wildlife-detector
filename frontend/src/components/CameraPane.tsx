import {
  type CSSProperties,
  type ReactNode,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { baselineImageUrl } from "../api/baseline";
import { useBaselineMeta } from "../hooks/useBaselineMeta";
import { useDetectionSize } from "../hooks/useDetectionSize";
import { useStatus } from "../hooks/useStatus";
import { useZoom } from "../hooks/useZoom";
import { BaselineControls } from "./BaselineControls";
import styles from "./CameraPane.module.css";
import { StatusBar } from "./StatusBar";

export type ViewMode = "live" | "day-baseline" | "night-baseline";

interface CameraPaneProps {
  camera: string;
  isPrimary: boolean;
  /** Full camera roster; used by the secondary pane's own dropdown. */
  cameras: string[];
  /** Camera occupying the other pane — disabled in this pane's dropdown
   *  so both panes can't converge on the same camera. Not used on
   *  primary (primary's dropdown lives in the page header). */
  otherPaneCamera?: string;
  /** Secondary-only: user picked a different camera for this pane. */
  onSelectCamera?: (c: string) => void;
  /** Secondary-only: swap this pane's camera up to primary. */
  onPromote?: () => void;
  /** Secondary-only: close the pane. */
  onRemove?: () => void;
  /** Current view mode for this pane's camera — hoisted to the parent
   *  as a Record<camera, ViewMode> so it follows the camera across a
   *  promote-swap. Same for onViewModeChange. */
  viewMode: ViewMode;
  onViewModeChange: (m: ViewMode) => void;
  /** Rendered inside the canvas div (on top of the stream img). Used by
   *  the primary pane to slot in Zone/Mask overlays. Secondary passes
   *  nothing — editors live on primary only. */
  children?: ReactNode;
}

/**
 * One camera's live view. Owns its own zoom, view-mode toggle, alert
 * flash, and BaselineControls. Composed by LivePreviewPage — primary
 * pane gets the editor overlays as `children`; secondary gets a
 * dropdown + Promote/Remove actions.
 *
 * Pattern: component-level bulkhead. Panes share nothing but the
 * camera roster prop — a zoom mishap or view-mode change in one pane
 * cannot leak into the other. The parent page keeps only the
 * primary-editor state and orchestration (add/promote/remove).
 */
export function CameraPane({
  camera,
  isPrimary,
  cameras,
  otherPaneCamera,
  onSelectCamera,
  onPromote,
  onRemove,
  viewMode,
  onViewModeChange,
  children,
}: CameraPaneProps) {
  const { data: status } = useStatus(camera || undefined);
  const [detW, detH] = useDetectionSize(camera, status?.detection_size);

  const canvasRef = useRef<HTMLDivElement | null>(null);
  const scrollHostRef = useRef<HTMLDivElement | null>(null);
  // Canvas dimensions computed in JS to guarantee an object-fit-contain
  // fit within scrollHost regardless of container aspect. Pure-CSS
  // aspect-ratio + max-w/max-h broke when max-width clamped: `height:
  // 100%` remained explicit, so both dimensions were definite and
  // aspect-ratio was ignored — visible aspect loss on single-pane
  // when the pane was taller than the image aspect implied.
  const [canvasBox, setCanvasBox] = useState<{ w: number; h: number } | null>(null);
  useLayoutEffect(() => {
    const el = scrollHostRef.current;
    if (!el || !detW || !detH) return;
    const imgAspect = detW / detH;
    function measure() {
      if (!el) return;
      const cs = getComputedStyle(el);
      const padX = Number.parseFloat(cs.paddingLeft) + Number.parseFloat(cs.paddingRight);
      const padY = Number.parseFloat(cs.paddingTop) + Number.parseFloat(cs.paddingBottom);
      const inW = el.clientWidth - padX;
      const inH = el.clientHeight - padY;
      if (inW <= 0 || inH <= 0) return;
      const containerAspect = inW / inH;
      const [w, h] =
        containerAspect > imgAspect
          ? [inH * imgAspect, inH] // container wider than image → fit by height
          : [inW, inW / imgAspect]; // container taller than image → fit by width
      setCanvasBox((prev) =>
        prev && Math.abs(prev.w - w) < 0.5 && Math.abs(prev.h - h) < 0.5 ? prev : { w, h },
      );
    }
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [detW, detH]);
  // Zoom is keyed by camera (not by pane slot) so a camera's zoom
  // preference follows it across a promote-swap. useZoom appends
  // `:<cameraId>` to storageKey, so passing the same prefix from both
  // panes gives us separate per-camera localStorage entries.
  const { zoom, adjustBy, setZoomTo, onWheel } = useZoom(camera, {
    storageKey: "livePreviewZoom",
    min: 0.25,
    max: 3.0,
    step: 0.1,
    canvasRef,
  });

  const { data: baselineMeta } = useBaselineMeta(camera);

  // ── Alert flash — watch this pane's camera's last_alert.ts ──
  const [flashKey, setFlashKey] = useState(0);
  const lastSeenAlertTs = useRef<number>(0);
  // biome-ignore lint/correctness/useExhaustiveDependencies: camera IS the fire trigger; body only touches a ref + setter
  useEffect(() => {
    // Camera change: reset baseline so we don't flash on the new
    // camera's historical alert.
    lastSeenAlertTs.current = 0;
    setFlashKey(0);
  }, [camera]);
  useEffect(() => {
    const ts = status?.last_alert?.ts;
    if (!ts) return;
    if (lastSeenAlertTs.current === 0) {
      lastSeenAlertTs.current = ts;
      return;
    }
    if (ts > lastSeenAlertTs.current) {
      lastSeenAlertTs.current = ts;
      setFlashKey((k) => k + 1);
    }
  }, [status?.last_alert?.ts]);

  const [streamError, setStreamError] = useState(false);
  // streamKey exists only for the user-triggered Retry button (same URL,
  // want a fresh fetch). On camera change the URL already differs — the
  // ?camera= query param carries the id — so the browser cancels the old
  // MJPEG connection and opens the new one when React updates src. Bumping
  // the key here would ALSO force an <img> unmount+remount, giving up to
  // 6 concurrent /stream connections during a promote-swap (2 old draining
  // + 2 short-lived + 2 final) and briefly stalling the backend.
  const [streamKey, setStreamKey] = useState(0);

  // frameReady: false = show skeleton over the canvas. Flipped true by
  // the img's onLoad (first frame decoded) or by a 2500ms grace timer
  // in case the stream never yields a frame (detector down, etc.) so
  // the user isn't stuck on the skeleton indefinitely.
  const [frameReady, setFrameReady] = useState(false);
  // biome-ignore lint/correctness/useExhaustiveDependencies: (camera, viewMode) ARE the fire triggers; body only calls setters/setTimeout
  useEffect(() => {
    setStreamError(false);
    setFrameReady(false);
    const t = window.setTimeout(() => setFrameReady(true), 2500);
    return () => window.clearTimeout(t);
  }, [camera, viewMode]);

  const liveSrc = camera ? `/stream?camera=${encodeURIComponent(camera)}&t=${streamKey}` : "";
  const baselineSrc = (() => {
    if (!baselineMeta || !camera) return "";
    if (viewMode === "day-baseline") return baselineImageUrl(camera, "day", baselineMeta.version);
    if (viewMode === "night-baseline")
      return baselineImageUrl(camera, "night", baselineMeta.version);
    return "";
  })();
  const currentSrc = viewMode === "live" ? liveSrc : baselineSrc;

  return (
    <section className={`${styles.pane} ${isPrimary ? styles.panePrimary : styles.paneSecondary}`}>
      <div className={styles.paneHeader}>
        <span className={styles.paneLabel}>{isPrimary ? "primary" : "secondary"}</span>
        {!isPrimary && (
          <>
            <select
              className={styles.select}
              value={camera}
              onChange={(e) => onSelectCamera?.(e.target.value)}
              aria-label="Secondary camera"
            >
              {cameras.map((c) => (
                <option key={c} value={c} disabled={c === otherPaneCamera}>
                  {c}
                  {c === otherPaneCamera ? " (primary)" : ""}
                </option>
              ))}
            </select>
            <button
              type="button"
              className={styles.linkBtn}
              onClick={onPromote}
              title="Swap this camera into the primary slot (editors follow the primary)"
            >
              ↑ Promote
            </button>
            <button
              type="button"
              className={styles.linkBtn}
              onClick={onRemove}
              title="Close the secondary pane"
            >
              × Remove
            </button>
          </>
        )}
      </div>

      <div className={styles.paneToolbar}>
        <StatusBar camera={camera} />
        <BaselineControls camera={camera} />
        <ViewModeButtons
          viewMode={viewMode}
          onSet={onViewModeChange}
          dayExists={!!baselineMeta?.day.exists}
          nightExists={!!baselineMeta?.night.exists}
        />
        <div className={styles.zoomBtns}>
          <button type="button" onClick={() => adjustBy(-0.1)} title="Zoom out">
            −
          </button>
          <span className={styles.zoomVal}>{zoom.toFixed(2)}×</span>
          <button type="button" onClick={() => adjustBy(0.1)} title="Zoom in">
            +
          </button>
          <button type="button" onClick={() => setZoomTo(1.0)} title="Reset zoom to 1×">
            1×
          </button>
        </div>
      </div>

      <div className={styles.scrollHost} ref={scrollHostRef}>
        {streamError && viewMode === "live" ? (
          <div className={styles.empty}>
            Stream unavailable. Detector may still be starting up — retry in a few seconds.
            <div style={{ marginTop: 12 }}>
              <button
                type="button"
                className={styles.linkBtn}
                onClick={() => {
                  setStreamKey((k) => k + 1);
                  setStreamError(false);
                }}
              >
                Retry
              </button>
            </div>
          </div>
        ) : (
          // Re-mount the canvas on each flashKey bump so the CSS
          // keyframe animation restarts. Same pattern as the single-pane
          // implementation before this extraction.
          <div
            ref={canvasRef}
            key={`canvas-${flashKey}`}
            className={`${styles.canvas} ${flashKey > 0 ? styles.canvasFlash : ""}`}
            // Feed the image aspect ratio to CSS so the canvas sizes
            // itself to fit the container while preserving aspect.
            // Any change is picked up automatically by aspect-ratio.
            style={
              {
                width: canvasBox ? `${canvasBox.w}px` : 0,
                height: canvasBox ? `${canvasBox.h}px` : 0,
              } as CSSProperties
            }
          >
            <img
              key={streamKey}
              className={styles.stream}
              src={currentSrc}
              alt={
                viewMode === "live"
                  ? `live stream ${camera}`
                  : `${viewMode.split("-")[0]} baseline ${camera}`
              }
              onWheel={onWheel}
              onLoad={() => setFrameReady(true)}
              onError={() => {
                if (viewMode === "live") setStreamError(true);
                // Still reveal the canvas — otherwise the error UI
                // hides behind the skeleton.
                setFrameReady(true);
              }}
            />
            {!frameReady && <div className={styles.skeleton} aria-hidden="true" />}
            {children}
          </div>
        )}
      </div>
    </section>
  );
}

function ViewModeButtons({
  viewMode,
  onSet,
  dayExists,
  nightExists,
}: {
  viewMode: ViewMode;
  onSet: (m: ViewMode) => void;
  dayExists: boolean;
  nightExists: boolean;
}) {
  return (
    <div className={styles.viewGroup}>
      <span className={styles.viewLabel}>view</span>
      <button
        type="button"
        className={`${styles.viewBtn} ${viewMode === "live" ? styles.viewBtnActive : ""}`}
        onClick={() => onSet("live")}
        title="Show the live MJPEG stream"
      >
        Live
      </button>
      <button
        type="button"
        className={`${styles.viewBtn} ${viewMode === "day-baseline" ? styles.viewBtnActive : ""}`}
        onClick={() => onSet("day-baseline")}
        disabled={!dayExists}
        title={dayExists ? "Show the day baseline JPG" : "No day baseline captured yet"}
      >
        Day baseline
      </button>
      <button
        type="button"
        className={`${styles.viewBtn} ${viewMode === "night-baseline" ? styles.viewBtnActive : ""}`}
        onClick={() => onSet("night-baseline")}
        disabled={!nightExists}
        title={nightExists ? "Show the night baseline JPG" : "No night baseline captured yet"}
      >
        Night baseline
      </button>
    </div>
  );
}
