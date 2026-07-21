import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { type Point, saveZone } from "../api/zone";
import { BaselineControls } from "../components/BaselineControls";
import { StatusBar } from "../components/StatusBar";
import { type EditMode, ZoneOverlay } from "../components/ZoneOverlay";
import { useCameras } from "../hooks/useCameras";
import { useStatus } from "../hooks/useStatus";
import { useZone } from "../hooks/useZone";
import { useZoom } from "../hooks/useZoom";
import styles from "./LivePreviewPage.module.css";

/**
 * Live streaming preview with the zone editor overlay (PR 12b).
 *
 * DOM structure inside the canvas:
 *   .scrollHost         — bounded viewport, clips overflow
 *     .canvas           — carries CSS vars for zoom/pan (useZoom)
 *       <img>           — MJPEG stream, sized 100% of canvas
 *       <ZoneOverlay>   — SVG covering the stream, zone-edit-aware
 *
 * The canvas's CSS variables (--rendered-w/h, --pan-x/y) are read by
 * both .canvas (for its own width/height and transform) and any child
 * via inherit. The overlay SVG sits at 100% of canvas dimensions with
 * a viewBox in image-pixel coords, so polygon points stay in image
 * space regardless of zoom.
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

  const { data: zoneData, refresh: refreshZone } = useZone(camera);
  const [editMode, setEditMode] = useState<EditMode>("idle");
  const [workingPolygon, setWorkingPolygon] = useState<Point[]>([]);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Sync working polygon with server whenever we're idle. Draw/tweak
  // sessions keep the working copy locally until the user saves.
  useEffect(() => {
    if (editMode === "idle" && zoneData) setWorkingPolygon(zoneData.polygon);
  }, [zoneData, editMode]);

  function enterDraw() {
    setWorkingPolygon([]);
    setEditMode("draw");
    setSaveErr(null);
  }
  function enterTweak() {
    setWorkingPolygon(zoneData?.polygon ?? []);
    setEditMode("tweak");
    setSaveErr(null);
  }
  function cancelEdit() {
    setEditMode("idle");
    setWorkingPolygon(zoneData?.polygon ?? []);
    setSaveErr(null);
  }
  function onDrawClose() {
    // Closing the draw loop transitions to tweak so the user can
    // fine-tune vertex positions before saving.
    setEditMode("tweak");
  }
  async function doSave() {
    if (workingPolygon.length < 3 || saving) return;
    setSaving(true);
    setSaveErr(null);
    try {
      await saveZone(camera, workingPolygon);
      refreshZone();
      setEditMode("idle");
    } catch (e) {
      setSaveErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  const [streamError, setStreamError] = useState(false);
  const [streamKey, setStreamKey] = useState(0);
  const currentSrc = camera ? `/stream?camera=${encodeURIComponent(camera)}&t=${streamKey}` : "";

  const displayedPolygon = editMode === "idle" ? (zoneData?.polygon ?? []) : workingPolygon;

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
            setEditMode("idle");
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
          <ZoneEditorButtons
            mode={editMode}
            vertexCount={workingPolygon.length}
            saving={saving}
            saveErr={saveErr}
            onDraw={enterDraw}
            onTweak={enterTweak}
            onSave={doSave}
            onCancel={cancelEdit}
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
          {streamError ? (
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
            <div ref={canvasRef} className={styles.canvas}>
              <img
                key={streamKey}
                className={styles.stream}
                src={currentSrc}
                alt={`live stream ${camera}`}
                onWheel={onWheel}
                onError={() => setStreamError(true)}
              />
              <ZoneOverlay
                baseW={detW}
                baseH={detH}
                polygon={displayedPolygon}
                mode={editMode}
                onChange={setWorkingPolygon}
                onClose={onDrawClose}
              />
            </div>
          )}
        </div>
      )}
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
        {mode === "draw" ? "drawing" : "tweaking"} · {vertexCount} pts
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
