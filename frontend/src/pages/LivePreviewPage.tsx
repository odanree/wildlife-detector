import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { CameraPane, type ViewMode } from "../components/CameraPane";
import { GlobalHeader } from "../components/GlobalHeader";
import { type MaskMode, MaskOverlay } from "../components/MaskOverlay";
import { type EditMode, ZoneOverlay } from "../components/ZoneOverlay";
import { useCameras } from "../hooks/useCameras";
import { useDetectionSize } from "../hooks/useDetectionSize";
import { useMaskEditor } from "../hooks/useMaskEditor";
import { useSecondaryPane } from "../hooks/useSecondaryPane";
import { useStatus } from "../hooks/useStatus";
import { useZoneEditor } from "../hooks/useZoneEditor";
import styles from "./LivePreviewPage.module.css";

/**
 * Live streaming preview — layout + <CameraPane> composition. State
 * ownership is delegated to three focused hooks:
 *
 *   - useSecondaryPane: opt-in dual-pane state + persistence.
 *   - useZoneEditor(primary): zone-polygon FSM + save.
 *   - useMaskEditor(primary): OSD-mask FSM + save.
 *
 * Was 460 LOC + 10 useState + 5 useEffect before #34; now ~180 LOC
 * + 1 useState + 0 useEffect (excluding the hooks' own internals).
 *
 * Mutual-exclusion invariant between zone + mask editors lives HERE
 * (one level up from both hooks) — before calling `zone.enterDraw()`,
 * the page calls `mask.cancel()` and vice versa. Keeps cross-hook
 * coordination out of the individual hooks and centralised in the
 * layer that already owns both. If a third editor ever lands, extract
 * useEditorRegistry then; for now YAGNI.
 *
 * Architecture calls (unchanged from before):
 *   - **Component-level bulkhead** — <CameraPane> owns its own zoom,
 *     view-mode, alert-flash, and baseline controls. Isolated failures.
 *   - **URL as source of truth for primary, localStorage for secondary.**
 *   - **Promote swap = state re-parenting.** The URL update flips
 *     primary; useZoneEditor / useMaskEditor re-init on the id change.
 */
export function LivePreviewPage() {
  const { data: camerasData } = useCameras();
  const cameras = camerasData?.cameras ?? [];
  const defaultCam = camerasData?.default ?? "";
  const [searchParams, setSearchParams] = useSearchParams();
  const primary = searchParams.get("camera") ?? defaultCam;

  const pane = useSecondaryPane(cameras, primary);
  const zone = useZoneEditor(primary);
  const mask = useMaskEditor(primary);

  // View mode is keyed by camera (not pane slot) so it follows a
  // camera across a promote-swap. Session-only.
  const [viewModes, setViewModes] = useState<Record<string, ViewMode>>({});
  const setViewModeFor = (camera: string) => (mode: ViewMode) =>
    setViewModes((prev) => ({ ...prev, [camera]: mode }));

  // Editors target the primary camera. detW/detH come from primary's
  // status with useDetectionSize's cache filling the gap during a
  // camera-change so overlay viewBox coords don't briefly render at
  // the 1280×720 fallback aspect.
  const { data: primaryStatus } = useStatus(primary || undefined);
  const [detW, detH] = useDetectionSize(primary, primaryStatus?.detection_size);

  // Displayed polygons/masks: server value when idle, working value
  // otherwise. Replaces the H4 sync-via-effect the audit flagged.
  const displayedPolygon = zone.mode === "idle" ? zone.serverPolygon : zone.workingPolygon;
  const displayedMasks = mask.mode === "idle" ? mask.serverMasks : mask.workingMasks;

  // Mutual-exclusion enforced at the page layer — before entering an
  // editor mode, cancel the other. Only one editor active at a time.
  const enterZoneDraw = () => {
    mask.cancel();
    zone.enterDraw();
  };
  const enterZoneTweak = () => {
    mask.cancel();
    zone.enterTweak();
  };
  const enterMaskEdit = () => {
    zone.cancel();
    mask.enterEdit();
  };

  return (
    <div className={styles.wrap}>
      <GlobalHeader
        right={
          <>
            <select
              className={styles.select}
              value={primary}
              onChange={(e) => setSearchParams({ camera: e.target.value })}
              aria-label="Primary camera"
            >
              {cameras.length === 0 && <option value="">(loading)</option>}
              {cameras.map((c) => (
                <option key={c} value={c} disabled={c === pane.secondary}>
                  {c}
                  {c === pane.secondary ? " (secondary)" : ""}
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
          </>
        }
      />

      {primary && (
        <div className={styles.editorToolbar}>
          <span className={styles.editorScope}>editing: primary</span>
          <ZoneEditorButtons
            mode={zone.mode}
            vertexCount={zone.workingPolygon.length}
            isSimple={zone.isSimple}
            saving={zone.saving}
            saveErr={zone.saveErr}
            onDraw={enterZoneDraw}
            onTweak={enterZoneTweak}
            onSave={zone.save}
            onCancel={zone.cancel}
          />
          <MaskEditorButtons
            mode={mask.mode}
            count={mask.workingMasks.length}
            saving={mask.saving}
            saveErr={mask.saveErr}
            onEdit={enterMaskEdit}
            onSave={mask.save}
            onCancel={mask.cancel}
          />
          <span className={styles.spacer} />
          {pane.secondary ? null : (
            <button
              type="button"
              className={styles.linkBtn}
              onClick={pane.add}
              disabled={!pane.canAdd}
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
            otherPaneCamera={pane.secondary ?? undefined}
            viewMode={viewModes[primary] ?? "live"}
            onViewModeChange={setViewModeFor(primary)}
          >
            <ZoneOverlay
              baseW={detW}
              baseH={detH}
              polygon={displayedPolygon}
              mode={zone.mode}
              onChange={zone.setWorkingPolygon}
              onClose={zone.closeDrawing}
            />
            <MaskOverlay
              baseW={detW}
              baseH={detH}
              masks={displayedMasks}
              mode={mask.mode}
              onChange={mask.setWorkingMasks}
            />
          </CameraPane>
          {pane.secondary && (
            <CameraPane
              camera={pane.secondary}
              isPrimary={false}
              cameras={cameras}
              otherPaneCamera={primary}
              onSelectCamera={pane.select}
              onPromote={pane.promote}
              onRemove={pane.remove}
              viewMode={viewModes[pane.secondary] ?? "live"}
              onViewModeChange={setViewModeFor(pane.secondary)}
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
