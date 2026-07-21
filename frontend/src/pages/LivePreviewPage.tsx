import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { baselineImageUrl } from "../api/baseline";
import { type Rect, saveMasks } from "../api/masks";
import { type Point, saveZone } from "../api/zone";
import { BaselineControls } from "../components/BaselineControls";
import { type MaskMode, MaskOverlay } from "../components/MaskOverlay";
import { StatusBar } from "../components/StatusBar";
import { type EditMode, ZoneOverlay } from "../components/ZoneOverlay";
import { useBaselineMeta } from "../hooks/useBaselineMeta";
import { useCameras } from "../hooks/useCameras";
import { useMasks } from "../hooks/useMasks";
import { useStatus } from "../hooks/useStatus";
import { useZone } from "../hooks/useZone";
import { useZoom } from "../hooks/useZoom";
import styles from "./LivePreviewPage.module.css";

type ViewMode = "live" | "day-baseline" | "night-baseline";

/**
 * Live streaming preview + editors. PR 13 additions:
 *   - Mask editor (rectangle overlays, draw/tweak/save/cancel)
 *   - Baseline view toggle — replaces the live stream image with the
 *     stored day or night baseline JPG so the operator can compare
 *     "what the pipeline uses as reference" against the current scene.
 *   - Alert flash — red border pulse around the canvas when a new
 *     alert fires for the currently-displayed camera. Watches
 *     status.last_alert.ts and animates for 3s on change.
 *
 * Editor mutual exclusivity: only one of {zoneMode, maskMode} can be
 * !== "idle" at a time. Entering one from the other's editing state
 * auto-cancels the other. Simpler mental model than "two editors
 * running in parallel," matches the vanilla UI.
 */
export function LivePreviewPage() {
  const { data: camerasData } = useCameras();
  const cameras = camerasData?.cameras ?? [];
  const defaultCam = camerasData?.default ?? "";
  const [searchParams, setSearchParams] = useSearchParams();
  const camera = searchParams.get("camera") ?? defaultCam;

  const { data: status } = useStatus(camera || undefined);
  const detW = status?.detection_size?.[0] ?? 1280;
  const detH = status?.detection_size?.[1] ?? 720;

  const canvasRef = useRef<HTMLDivElement | null>(null);
  const { zoom, adjustBy, setZoomTo, onWheel } = useZoom(camera, {
    storageKey: "livePreviewZoom",
    min: 0.25,
    max: 3.0,
    step: 0.1,
    baseW: detW,
    baseH: detH,
    canvasRef,
  });

  // ── Zone editor state ──
  const { data: zoneData, refresh: refreshZone } = useZone(camera);
  const [zoneMode, setZoneMode] = useState<EditMode>("idle");
  const [workingPolygon, setWorkingPolygon] = useState<Point[]>([]);
  const [zoneSaving, setZoneSaving] = useState(false);
  const [zoneErr, setZoneErr] = useState<string | null>(null);
  useEffect(() => {
    if (zoneMode === "idle" && zoneData) setWorkingPolygon(zoneData.polygon);
  }, [zoneData, zoneMode]);

  // ── Mask editor state ──
  // Vanilla parity: single edit mode. Existing masks aren't repositioned
  // or resized — wrong ones get deleted (× handle) and re-drawn.
  const { data: masksData, refresh: refreshMasks } = useMasks(camera);
  const [maskMode, setMaskMode] = useState<MaskMode>("idle");
  const [workingMasks, setWorkingMasks] = useState<Rect[]>([]);
  const [maskSaving, setMaskSaving] = useState(false);
  const [maskErr, setMaskErr] = useState<string | null>(null);
  useEffect(() => {
    if (maskMode === "idle" && masksData) setWorkingMasks(masksData.masks);
  }, [masksData, maskMode]);

  // ── Baseline view ──
  const { data: baselineMeta } = useBaselineMeta(camera);
  const [viewMode, setViewMode] = useState<ViewMode>("live");

  // ── Alert flash — watch last_alert.ts, trigger animation on change ──
  const [flashKey, setFlashKey] = useState(0);
  const lastSeenAlertTs = useRef<number>(0);
  useEffect(() => {
    const ts = status?.last_alert?.ts;
    if (!ts) return;
    // First-load: adopt the current ts without flashing (otherwise every
    // page load would flash for whatever the last historical alert was).
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
  const [streamKey, setStreamKey] = useState(0);
  const liveSrc = camera ? `/stream?camera=${encodeURIComponent(camera)}&t=${streamKey}` : "";
  const baselineSrc = (() => {
    if (!baselineMeta || !camera) return "";
    if (viewMode === "day-baseline") return baselineImageUrl(camera, "day", baselineMeta.version);
    if (viewMode === "night-baseline")
      return baselineImageUrl(camera, "night", baselineMeta.version);
    return "";
  })();
  const currentSrc = viewMode === "live" ? liveSrc : baselineSrc;

  const displayedPolygon = zoneMode === "idle" ? (zoneData?.polygon ?? []) : workingPolygon;
  const displayedMasks = maskMode === "idle" ? (masksData?.masks ?? []) : workingMasks;

  // ── Editor mutual exclusivity — entering one auto-cancels the other ──
  function enterZoneDraw() {
    if (maskMode !== "idle") cancelMaskEdit();
    setWorkingPolygon([]);
    setZoneMode("draw");
    setZoneErr(null);
  }
  function enterZoneTweak() {
    if (maskMode !== "idle") cancelMaskEdit();
    setWorkingPolygon(zoneData?.polygon ?? []);
    setZoneMode("tweak");
    setZoneErr(null);
  }
  function cancelZoneEdit() {
    setZoneMode("idle");
    setWorkingPolygon(zoneData?.polygon ?? []);
    setZoneErr(null);
  }
  function onZoneDrawClose() {
    setZoneMode("tweak");
  }
  async function doZoneSave() {
    if (workingPolygon.length < 3 || zoneSaving) return;
    setZoneSaving(true);
    setZoneErr(null);
    try {
      await saveZone(camera, workingPolygon);
      refreshZone();
      setZoneMode("idle");
    } catch (e) {
      setZoneErr(e instanceof Error ? e.message : String(e));
    } finally {
      setZoneSaving(false);
    }
  }

  function enterMaskEdit() {
    if (zoneMode !== "idle") cancelZoneEdit();
    setWorkingMasks(masksData?.masks ?? []);
    setMaskMode("edit");
    setMaskErr(null);
  }
  function cancelMaskEdit() {
    setMaskMode("idle");
    setWorkingMasks(masksData?.masks ?? []);
    setMaskErr(null);
  }
  async function doMaskSave() {
    if (maskSaving) return;
    setMaskSaving(true);
    setMaskErr(null);
    try {
      await saveMasks(camera, workingMasks);
      refreshMasks();
      setMaskMode("idle");
    } catch (e) {
      setMaskErr(e instanceof Error ? e.message : String(e));
    } finally {
      setMaskSaving(false);
    }
  }

  return (
    <div className={styles.wrap}>
      <header className={styles.header}>
        <Link to="/" className={styles.title}>
          wildlife-detector — live
        </Link>
        <span className={styles.spacer} />
        <select
          className={styles.select}
          value={camera}
          onChange={(e) => {
            setSearchParams({ camera: e.target.value });
            setStreamKey((k) => k + 1);
            setStreamError(false);
            setZoneMode("idle");
            setMaskMode("idle");
            setViewMode("live");
            lastSeenAlertTs.current = 0;
          }}
        >
          {cameras.length === 0 && <option value="">(loading)</option>}
          {cameras.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
        {camera && (
          <a
            className={styles.linkBtn}
            href={`/snapshot?camera=${encodeURIComponent(camera)}`}
            download={`${camera}-snapshot.jpg`}
            title="Download the current annotated frame as JPEG"
          >
            Snapshot
          </a>
        )}
        <Link to="/alerts" className={styles.linkBtn}>
          Alerts →
        </Link>
        <Link to="/baselines" className={styles.linkBtn}>
          Baselines →
        </Link>
        <Link to="/status" className={styles.linkBtn}>
          Dashboard →
        </Link>
      </header>

      {camera && (
        <div className={styles.toolbar}>
          <StatusBar camera={camera} />
          <BaselineControls camera={camera} />
          <ViewModeButtons
            viewMode={viewMode}
            onSet={setViewMode}
            dayExists={!!baselineMeta?.day.exists}
            nightExists={!!baselineMeta?.night.exists}
          />
          <ZoneEditorButtons
            mode={zoneMode}
            vertexCount={workingPolygon.length}
            saving={zoneSaving}
            saveErr={zoneErr}
            onDraw={enterZoneDraw}
            onTweak={enterZoneTweak}
            onSave={doZoneSave}
            onCancel={cancelZoneEdit}
          />
          <MaskEditorButtons
            mode={maskMode}
            count={workingMasks.length}
            saving={maskSaving}
            saveErr={maskErr}
            onEdit={enterMaskEdit}
            onSave={doMaskSave}
            onCancel={cancelMaskEdit}
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
      )}

      {!camera ? (
        <div className={styles.empty}>Waiting for camera roster…</div>
      ) : (
        <div id="live-scroll-host" className={styles.scrollHost}>
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
            // key on flashKey re-triggers the CSS animation for each
            // new alert; without it the animation would only play once
            // per mount even if last_alert.ts kept changing.
            <div
              ref={canvasRef}
              key={`canvas-${flashKey}`}
              className={`${styles.canvas} ${flashKey > 0 ? styles.canvasFlash : ""}`}
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
                onError={() => viewMode === "live" && setStreamError(true)}
              />
              <ZoneOverlay
                baseW={detW}
                baseH={detH}
                polygon={displayedPolygon}
                mode={zoneMode}
                onChange={setWorkingPolygon}
                onClose={onZoneDrawClose}
              />
              <MaskOverlay
                baseW={detW}
                baseH={detH}
                masks={displayedMasks}
                mode={maskMode}
                onChange={setWorkingMasks}
              />
            </div>
          )}
        </div>
      )}
    </div>
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
    <div className={styles.zoneGroup}>
      <span className={styles.zoneLabel}>view</span>
      <button
        type="button"
        className={`${styles.zoneBtn} ${viewMode === "live" ? styles.zoneBtnActive : ""}`}
        onClick={() => onSet("live")}
        title="Show the live MJPEG stream"
      >
        Live
      </button>
      <button
        type="button"
        className={`${styles.zoneBtn} ${viewMode === "day-baseline" ? styles.zoneBtnActive : ""}`}
        onClick={() => onSet("day-baseline")}
        disabled={!dayExists}
        title={dayExists ? "Show the day baseline JPG" : "No day baseline captured yet"}
      >
        Day baseline
      </button>
      <button
        type="button"
        className={`${styles.zoneBtn} ${viewMode === "night-baseline" ? styles.zoneBtnActive : ""}`}
        onClick={() => onSet("night-baseline")}
        disabled={!nightExists}
        title={nightExists ? "Show the night baseline JPG" : "No night baseline captured yet"}
      >
        Night baseline
      </button>
    </div>
  );
}

function ZoneEditorButtons({
  mode,
  vertexCount,
  saving,
  saveErr,
  onDraw,
  onTweak,
  onSave,
  onCancel,
}: {
  mode: EditMode;
  vertexCount: number;
  saving: boolean;
  saveErr: string | null;
  onDraw: () => void;
  onTweak: () => void;
  onSave: () => void;
  onCancel: () => void;
}) {
  if (mode === "idle") {
    return (
      <div className={styles.zoneGroup}>
        <span className={styles.zoneLabel}>zone</span>
        <button
          type="button"
          className={styles.zoneBtn}
          onClick={onDraw}
          title="Draw a new zone polygon from scratch"
        >
          Draw
        </button>
        <button
          type="button"
          className={styles.zoneBtn}
          onClick={onTweak}
          title="Edit vertices of the current polygon"
          disabled={vertexCount < 3}
        >
          Tweak
        </button>
      </div>
    );
  }
  const canSave = vertexCount >= 3 && !saving;
  return (
    <div className={styles.zoneGroup}>
      <span className={styles.zoneLabel}>
        {mode === "draw" ? "drawing" : "tweaking"} zone · {vertexCount} pts
      </span>
      <button
        type="button"
        className={`${styles.zoneBtn} ${styles.zoneBtnSave}`}
        onClick={onSave}
        disabled={!canSave}
        title={vertexCount < 3 ? "Need at least 3 vertices" : "Save polygon to config"}
      >
        {saving ? "Saving…" : "Save"}
      </button>
      <button
        type="button"
        className={styles.zoneBtn}
        onClick={onCancel}
        title="Discard unsaved changes"
      >
        Cancel
      </button>
      {saveErr && <span className={styles.zoneErr}>err: {saveErr}</span>}
    </div>
  );
}

function MaskEditorButtons({
  mode,
  count,
  saving,
  saveErr,
  onEdit,
  onSave,
  onCancel,
}: {
  mode: MaskMode;
  count: number;
  saving: boolean;
  saveErr: string | null;
  onEdit: () => void;
  onSave: () => void;
  onCancel: () => void;
}) {
  if (mode === "idle") {
    return (
      <div className={styles.zoneGroup}>
        <span className={styles.zoneLabel}>mask</span>
        <button
          type="button"
          className={styles.zoneBtn}
          onClick={onEdit}
          title="Draw OSD mask rectangles (drag on canvas). Click × on any mask to remove it."
        >
          Draw OSD mask
        </button>
      </div>
    );
  }
  return (
    <div className={styles.zoneGroup}>
      <span className={styles.zoneLabel}>editing masks · {count}</span>
      <button
        type="button"
        className={`${styles.zoneBtn} ${styles.zoneBtnSave}`}
        onClick={onSave}
        disabled={saving}
        title="Save masks to config"
      >
        {saving ? "Saving…" : "Save"}
      </button>
      <button
        type="button"
        className={styles.zoneBtn}
        onClick={onCancel}
        title="Discard unsaved changes"
      >
        Cancel
      </button>
      {saveErr && <span className={styles.zoneErr}>err: {saveErr}</span>}
    </div>
  );
}
