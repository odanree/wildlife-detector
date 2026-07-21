import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { type Rect, saveMasks } from "../api/masks";
import { type Point, saveZone } from "../api/zone";
import { AlertsNavLink } from "../components/AlertsNavLink";
import { CameraPane, type ViewMode } from "../components/CameraPane";
import { type MaskMode, MaskOverlay } from "../components/MaskOverlay";
import { type EditMode, ZoneOverlay } from "../components/ZoneOverlay";
import { useCameras } from "../hooks/useCameras";
import { useDetectionSize } from "../hooks/useDetectionSize";
import { useMasks } from "../hooks/useMasks";
import { useStatus } from "../hooks/useStatus";
import { useZone } from "../hooks/useZone";
import { polygonIsSimple } from "../util/polygon";
import styles from "./LivePreviewPage.module.css";

const SECONDARY_STORAGE_KEY = "livePreview.secondaryCamera";

/**
 * Live streaming preview. This page is now a thin orchestrator on top
 * of two <CameraPane> instances. Editor state (zone + mask) stays here
 * because the editor toolbar always targets the *primary* pane and its
 * mutual-exclusivity invariant lives above both panes.
 *
 * Architecture calls:
 *   - **Component-level bulkhead** — <CameraPane> owns its own zoom,
 *     view-mode, alert-flash, and baseline controls. A bug in one pane
 *     can't corrupt the other's state.
 *   - **URL as source of truth for primary, localStorage for secondary.**
 *     Primary camera is bookmarkable (shared context). Secondary is a
 *     per-viewer preference (opt-in via "+ Add pane" toggle).
 *   - **Promote swap = state re-parenting.** Secondary's "↑ Promote"
 *     swaps the two camera IDs; the editors follow whichever camera
 *     ends up primary. useZone/useMasks re-poll on the id change.
 */
export function LivePreviewPage() {
  const { data: camerasData } = useCameras();
  const cameras = camerasData?.cameras ?? [];
  const defaultCam = camerasData?.default ?? "";
  const [searchParams, setSearchParams] = useSearchParams();
  const primary = searchParams.get("camera") ?? defaultCam;

  // Secondary camera opt-in — persisted per-viewer. null = pane closed.
  const [secondary, setSecondary] = useState<string | null>(() => {
    try {
      return localStorage.getItem(SECONDARY_STORAGE_KEY);
    } catch {
      return null;
    }
  });
  useEffect(() => {
    try {
      if (secondary) localStorage.setItem(SECONDARY_STORAGE_KEY, secondary);
      else localStorage.removeItem(SECONDARY_STORAGE_KEY);
    } catch {
      // localStorage quota or disabled — the pane still works this session.
    }
  }, [secondary]);
  // If the persisted secondary collides with primary (e.g. user swapped
  // in another tab), close it — same camera on both panes is a UX bug.
  useEffect(() => {
    if (secondary && secondary === primary) setSecondary(null);
  }, [primary, secondary]);

  // View mode is keyed by camera (not pane slot) so it follows a
  // camera across a promote-swap. Session-only — not persisted across
  // reloads so re-opening the page always starts on the Live stream.
  const [viewModes, setViewModes] = useState<Record<string, ViewMode>>({});
  const setViewModeFor = (camera: string) => (mode: ViewMode) =>
    setViewModes((prev) => ({ ...prev, [camera]: mode }));

  // Editors target the primary camera. detW/detH come from primary's status,
  // with useDetectionSize's cache filling the gap during a camera-change so
  // ZoneOverlay/MaskOverlay viewBox coords don't briefly render at the
  // 1280×720 fallback aspect.
  const { data: primaryStatus } = useStatus(primary || undefined);
  const [detW, detH] = useDetectionSize(primary, primaryStatus?.detection_size);

  // ── Zone editor state ──
  const { data: zoneData, refresh: refreshZone } = useZone(primary);
  const [zoneMode, setZoneMode] = useState<EditMode>("idle");
  const [workingPolygon, setWorkingPolygon] = useState<Point[]>([]);
  const [zoneSaving, setZoneSaving] = useState(false);
  const [zoneErr, setZoneErr] = useState<string | null>(null);
  useEffect(() => {
    if (zoneMode === "idle" && zoneData) setWorkingPolygon(zoneData.polygon);
  }, [zoneData, zoneMode]);

  // ── Mask editor state (vanilla-parity: single edit mode) ──
  const { data: masksData, refresh: refreshMasks } = useMasks(primary);
  const [maskMode, setMaskMode] = useState<MaskMode>("idle");
  const [workingMasks, setWorkingMasks] = useState<Rect[]>([]);
  const [maskSaving, setMaskSaving] = useState(false);
  const [maskErr, setMaskErr] = useState<string | null>(null);
  useEffect(() => {
    if (maskMode === "idle" && masksData) setWorkingMasks(masksData.masks);
  }, [masksData, maskMode]);

  // Any change to the primary camera cancels both editors — otherwise
  // the operator would silently be editing a stale polygon/rect set.
  // biome-ignore lint/correctness/useExhaustiveDependencies: primary IS the fire trigger; body only calls setters
  useEffect(() => {
    setZoneMode("idle");
    setMaskMode("idle");
  }, [primary]);

  const displayedPolygon = zoneMode === "idle" ? (zoneData?.polygon ?? []) : workingPolygon;
  const displayedMasks = maskMode === "idle" ? (masksData?.masks ?? []) : workingMasks;

  // ── Editor mutual exclusivity ──
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
    // Guard against self-intersecting polygons: SVG strokes vertex order,
    // so the closing edge V(n-1)→V0 can slice through the interior when
    // a vertex is placed inside the outline, and the shape reads as "two
    // zones" in the editor. Cheaper to block the save than to teach every
    // user to think about winding order.
    if (!polygonIsSimple(workingPolygon)) {
      setZoneErr(
        "self-intersecting — one edge crosses another. Move vertices so the outline doesn't cross itself.",
      );
      return;
    }
    setZoneSaving(true);
    setZoneErr(null);
    try {
      await saveZone(primary, workingPolygon);
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
      await saveMasks(primary, workingMasks);
      refreshMasks();
      setMaskMode("idle");
    } catch (e) {
      setMaskErr(e instanceof Error ? e.message : String(e));
    } finally {
      setMaskSaving(false);
    }
  }

  // ── Secondary-pane orchestration ──
  const canAddSecondary = cameras.length >= 2 && !secondary;
  function addSecondaryPane() {
    const next = cameras.find((c) => c !== primary);
    if (next) setSecondary(next);
  }
  function removeSecondaryPane() {
    setSecondary(null);
  }
  function selectSecondaryCamera(c: string) {
    if (c === primary) return;
    setSecondary(c);
  }
  function promoteSecondary() {
    if (!secondary) return;
    const oldPrimary = primary;
    setSearchParams({ camera: secondary });
    setSecondary(oldPrimary);
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
          value={primary}
          onChange={(e) => setSearchParams({ camera: e.target.value })}
          aria-label="Primary camera"
        >
          {cameras.length === 0 && <option value="">(loading)</option>}
          {cameras.map((c) => (
            <option key={c} value={c} disabled={c === secondary}>
              {c}
              {c === secondary ? " (secondary)" : ""}
            </option>
          ))}
        </select>
        {primary && (
          <a
            className={styles.linkBtn}
            href={`/snapshot?camera=${encodeURIComponent(primary)}`}
            download={`${primary}-snapshot.jpg`}
            title="Download the current annotated frame as JPEG"
          >
            Snapshot
          </a>
        )}
        <AlertsNavLink className={styles.linkBtn} />
        <Link to="/baselines" className={styles.linkBtn}>
          Baselines →
        </Link>
        <Link to="/status" className={styles.linkBtn}>
          Dashboard →
        </Link>
      </header>

      {primary && (
        <div className={styles.editorToolbar}>
          <span className={styles.editorScope}>editing: primary</span>
          <ZoneEditorButtons
            mode={zoneMode}
            vertexCount={workingPolygon.length}
            isSimple={polygonIsSimple(workingPolygon)}
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
          <span className={styles.spacer} />
          {secondary ? null : (
            <button
              type="button"
              className={styles.linkBtn}
              onClick={addSecondaryPane}
              disabled={!canAddSecondary}
              title={
                cameras.length < 2
                  ? "Need at least two cameras to open a secondary pane"
                  : "Show a second camera below the primary"
              }
            >
              + Add camera pane
            </button>
          )}
        </div>
      )}

      {!primary ? (
        <div className={styles.empty}>Waiting for camera roster…</div>
      ) : (
        <div className={styles.panes}>
          <CameraPane
            camera={primary}
            isPrimary
            cameras={cameras}
            otherPaneCamera={secondary ?? undefined}
            viewMode={viewModes[primary] ?? "live"}
            onViewModeChange={setViewModeFor(primary)}
          >
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
          </CameraPane>
          {secondary && (
            <CameraPane
              camera={secondary}
              isPrimary={false}
              cameras={cameras}
              otherPaneCamera={primary}
              onSelectCamera={selectSecondaryCamera}
              onPromote={promoteSecondary}
              onRemove={removeSecondaryPane}
              viewMode={viewModes[secondary] ?? "live"}
              onViewModeChange={setViewModeFor(secondary)}
            />
          )}
        </div>
      )}
    </div>
  );
}

function ZoneEditorButtons({
  mode,
  vertexCount,
  isSimple,
  saving,
  saveErr,
  onDraw,
  onTweak,
  onSave,
  onCancel,
}: {
  mode: EditMode;
  vertexCount: number;
  isSimple: boolean;
  saving: boolean;
  saveErr: string | null;
  onDraw: () => void;
  onTweak: () => void;
  onSave: () => void;
  onCancel: () => void;
}) {
  if (mode === "idle") {
    return (
      <div className={styles.editorGroup}>
        <span className={styles.editorLabel}>zone</span>
        <button
          type="button"
          className={styles.editorBtn}
          onClick={onDraw}
          title="Draw a new zone polygon from scratch"
        >
          Draw
        </button>
        <button
          type="button"
          className={styles.editorBtn}
          onClick={onTweak}
          title="Edit vertices of the current polygon"
          disabled={vertexCount < 3}
        >
          Tweak
        </button>
      </div>
    );
  }
  const canSave = vertexCount >= 3 && !saving && isSimple;
  const saveTitle = !isSimple
    ? "Polygon self-intersects — one edge crosses another. Adjust vertices before saving."
    : vertexCount < 3
      ? "Need at least 3 vertices"
      : "Save polygon to config";
  return (
    <div className={styles.editorGroup}>
      <span className={styles.editorLabel}>
        {mode === "draw" ? "drawing" : "tweaking"} zone · {vertexCount} pts
      </span>
      <button
        type="button"
        className={`${styles.editorBtn} ${styles.editorBtnSave}`}
        onClick={onSave}
        disabled={!canSave}
        title={saveTitle}
      >
        {saving ? "Saving…" : "Save"}
      </button>
      <button
        type="button"
        className={styles.editorBtn}
        onClick={onCancel}
        title="Discard unsaved changes"
      >
        Cancel
      </button>
      {!isSimple && vertexCount >= 4 && <span className={styles.editorErr}>self-intersecting</span>}
      {saveErr && <span className={styles.editorErr}>err: {saveErr}</span>}
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
      <div className={styles.editorGroup}>
        <span className={styles.editorLabel}>mask</span>
        <button
          type="button"
          className={styles.editorBtn}
          onClick={onEdit}
          title="Draw OSD mask rectangles (drag on canvas). Click × on any mask to remove it."
        >
          Draw OSD mask
        </button>
      </div>
    );
  }
  return (
    <div className={styles.editorGroup}>
      <span className={styles.editorLabel}>editing masks · {count}</span>
      <button
        type="button"
        className={`${styles.editorBtn} ${styles.editorBtnSave}`}
        onClick={onSave}
        disabled={saving}
        title="Save masks to config"
      >
        {saving ? "Saving…" : "Save"}
      </button>
      <button
        type="button"
        className={styles.editorBtn}
        onClick={onCancel}
        title="Discard unsaved changes"
      >
        Cancel
      </button>
      {saveErr && <span className={styles.editorErr}>err: {saveErr}</span>}
    </div>
  );
}
