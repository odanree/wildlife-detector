import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { CameraPane, type ViewMode } from "../components/CameraPane";
import { GlobalHeader } from "../components/GlobalHeader";
import { ManualDetectOverlay } from "../components/ManualDetectOverlay";
import { type MaskMode, MaskOverlay } from "../components/MaskOverlay";
import { SlewPresetPanel } from "../components/SlewPresetPanel";
import { SlewPresetsOverlay } from "../components/SlewPresetsOverlay";
import { type EditMode, ZoneOverlay } from "../components/ZoneOverlay";
import { useCameras } from "../hooks/useCameras";
import { useDetectionSize } from "../hooks/useDetectionSize";
import { useManualDetectMode } from "../hooks/useManualDetectMode";
import { useMaskEditor } from "../hooks/useMaskEditor";
import { usePauseState } from "../hooks/usePauseState";
import { useSecondaryPane } from "../hooks/useSecondaryPane";
import { useSlewPresetEditor } from "../hooks/useSlewPresetEditor";
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
  // Primary camera resolution order:
  //   1. ?camera= in URL (deep-link, share, in-tab nav preserves)
  //   2. localStorage (across-nav persistence — clicking Alerts and
  //      back loses the URL param, so this restores the last choice)
  //   3. server-provided default (yard, per registry)
  // Persist to localStorage inline when the operator picks a new
  // camera — no useEffect subscribing to `primary` changes (avoids
  // the sync-via-effect anti-pattern the audit flagged).
  const primary =
    searchParams.get("camera") ??
    (typeof window !== "undefined" ? localStorage.getItem("previewPrimaryCam") : null) ??
    defaultCam;

  // Single writer for primary camera: any code path that changes it
  // (select onChange, promote-swap in useSecondaryPane, deep-link
  // opener) goes through here. localStorage.setItem lives inline in
  // the mutation, not in a subscribing effect — matches the anti-
  // pattern rule the audit flagged. Wraps both mechanisms so the
  // "swap panes then nav-back-to-preview reverts to old primary" bug
  // stays fixed.
  const setPrimary = (cam: string) => {
    try {
      localStorage.setItem("previewPrimaryCam", cam);
    } catch {
      /* quota / privacy mode — URL update below still works */
    }
    setSearchParams({ camera: cam });
  };

  const pane = useSecondaryPane(cameras, primary, setPrimary);

  // Deep-link seed: if the tab opens via `/preview?camera=rooftop`
  // with an empty localStorage, primary comes from the URL but
  // nothing writes it back. Nav-back-to-preview then reverts to
  // defaultCam. One-shot mount seed writes the URL-resolved value
  // so subsequent navs restore it. NOT the sync-via-effect anti-
  // pattern — mount-only + idempotent, not subscribing to a prop
  // for state coherence.
  // biome-ignore lint/correctness/useExhaustiveDependencies: intentional mount-only seed
  useEffect(() => {
    if (!primary) return;
    try {
      if (!localStorage.getItem("previewPrimaryCam")) {
        localStorage.setItem("previewPrimaryCam", primary);
      }
    } catch {
      /* quota / privacy mode — fine */
    }
  }, []);
  const zone = useZoneEditor(primary);
  const mask = useMaskEditor(primary);
  const slew = useSlewPresetEditor(primary);
  const manualDetect = useManualDetectMode();

  // View mode is keyed by camera (not pane slot) so it follows a
  // camera across a promote-swap. Session-only.
  const [viewModes, setViewModes] = useState<Record<string, ViewMode>>({});
  const setViewModeFor = (camera: string) => (mode: ViewMode) =>
    setViewModes((prev) => ({ ...prev, [camera]: mode }));

  // Slew preset overlay visibility. Off by default — the read-only
  // outlines clutter the main view when the operator isn't editing
  // slew polygons. Persisted to localStorage so the choice survives
  // reloads. Force-shown while editing (see slewOverlaysVisible below)
  // so the operator has context for the polygon they're working on.
  const [slewOverlaysToggle, setSlewOverlaysToggle] = useState<boolean>(
    () => localStorage.getItem("slewOverlaysVisible") === "1",
  );
  const toggleSlewOverlays = () => {
    setSlewOverlaysToggle((v) => {
      const next = !v;
      localStorage.setItem("slewOverlaysVisible", next ? "1" : "0");
      return next;
    });
  };

  // Per-camera + fan-out pause state via usePauseState — sole writer
  // for both this page's "pause all" toolbar button and the per-pane
  // toggle in each CameraPane. Same hook instance drives both so
  // they never drift out of sync.
  const pause = usePauseState();

  // Editors target the primary camera. detW/detH come from primary's
  // status with useDetectionSize's cache filling the gap during a
  // camera-change so overlay viewBox coords don't briefly render at
  // the 1280×720 fallback aspect.
  const { data: primaryStatus } = useStatus(primary || undefined);
  const [detW, detH] = useDetectionSize(primary, primaryStatus?.detection_size);

  // Displayed masks: server value when idle, working value otherwise.
  // Replaces the H4 sync-via-effect the audit flagged. (Displayed
  // polygon is computed below along with the slew-aware overlay
  // routing — see zoneOverlayPolygon.)
  const displayedMasks = mask.mode === "idle" ? mask.serverMasks : mask.workingMasks;

  // Mutual-exclusion enforced at the page layer — before entering an
  // editor mode, cancel the other two. Only one editor active at a
  // time. (Third editor landed → still not extracting useEditorRegistry
  // per YAGNI; hand-wired mutual-cancel is 6 lines.)
  const enterZoneDraw = () => {
    mask.cancel();
    slew.cancel();
    zone.enterDraw();
  };
  const enterZoneTweak = () => {
    mask.cancel();
    slew.cancel();
    zone.enterTweak();
  };
  const enterMaskEdit = () => {
    zone.cancel();
    slew.cancel();
    mask.enterEdit();
  };
  const enterSlewEdit = () => {
    zone.cancel();
    mask.cancel();
  };

  // Which polygon does ZoneOverlay display + edit? Priority:
  //   1. Active slew preset (draw/tweak) — polygon of the editing preset
  //   2. Zone editor working polygon (draw/tweak)
  //   3. Zone server polygon (idle)
  // When slew is active, zone editor is forcibly idle (mutual-exclusion
  // above), so the zone-editor branch collapses to server view.
  const slewActive = slew.mode !== "idle" && slew.activePreset !== null;
  const activeSlewPolygon = slewActive
    ? (slew.workingPresets.find((p) => p.preset === slew.activePreset)?.polygon ?? [])
    : null;
  const zoneOverlayMode: EditMode = slewActive ? slew.mode : zone.mode;
  const zoneOverlayPolygon = slewActive
    ? (activeSlewPolygon ?? [])
    : zone.mode === "idle"
      ? zone.serverPolygon
      : zone.workingPolygon;
  const zoneOverlayOnChange = slewActive ? slew.setActivePolygon : zone.setWorkingPolygon;
  const zoneOverlayOnClose = slewActive ? slew.closeDrawing : zone.closeDrawing;

  // Show slew preset outlines when toggled on OR when actively
  // editing — operator needs context on the other zones to place
  // vertices sensibly.
  const slewOverlaysVisible = slewOverlaysToggle || slewActive;

  return (
    <div className={styles.wrap}>
      <GlobalHeader
        right={
          <>
            <select
              className={styles.select}
              value={primary}
              onChange={(e) => setPrimary(e.target.value)}
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
        <>
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
            <div className={styles.editorGroup}>
              <span className={styles.editorLabel}>manual</span>
              <button
                type="button"
                className={manualDetect.mode !== "off" ? styles.editorBtnActive : styles.editorBtn}
                onClick={manualDetect.toggle}
                disabled={manualDetect.mode === "submitting"}
                title={
                  manualDetect.mode === "off"
                    ? "Arm manual detection: click-drag on the primary pane to box a stationary animal MOG can't reach"
                    : "Click-drag on the primary pane, or click this to cancel"
                }
              >
                {manualDetect.mode === "off"
                  ? "○ draw bbox"
                  : manualDetect.mode === "armed"
                    ? "● click-drag on pane"
                    : "⏳ submitting…"}
              </button>
              {manualDetect.status.kind === "success" && (
                <span className={styles.editorLabel}>✓ track {manualDetect.status.trackId}</span>
              )}
              {manualDetect.status.kind === "error" && (
                <span className={styles.editorErr}>err: {manualDetect.status.message}</span>
              )}
            </div>
            <span className={styles.spacer} />
            <button
              type="button"
              className={pause.allPaused ? styles.pauseBtnActive : styles.pauseBtn}
              onClick={() => {
                // Fan-out toggle: if every camera is currently paused,
                // this resumes all; otherwise pauses all (partial paused
                // state → the click brings you to fully-paused first).
                void pause.toggleAll(!pause.allPaused);
              }}
              title={
                pause.allPaused
                  ? "All cameras paused — click to resume all"
                  : pause.anyPaused
                    ? "Some cameras paused — click to pause the rest too"
                    : "Pause detection on all cameras"
              }
            >
              {pause.allPaused
                ? "▶ resume all"
                : pause.anyPaused
                  ? `⏸ pause all (${Object.values(pause.cameras).filter(Boolean).length}/${Object.keys(pause.cameras).length} paused)`
                  : "⏸ pause all"}
            </button>
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
          <SlewPresetPanel
            editor={slew}
            onEnterEdit={enterSlewEdit}
            overlaysVisible={slewOverlaysToggle}
            onToggleOverlays={toggleSlewOverlays}
          />
        </>
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
            paused={!!pause.cameras[primary]}
            onTogglePause={() => void pause.togglePause(primary)}
          >
            {slewOverlaysVisible && (
              <SlewPresetsOverlay
                baseW={detW}
                baseH={detH}
                presets={slew.workingPresets}
                activePreset={slew.activePreset}
              />
            )}
            <ZoneOverlay
              baseW={detW}
              baseH={detH}
              polygon={zoneOverlayPolygon}
              mode={zoneOverlayMode}
              onChange={zoneOverlayOnChange}
              onClose={zoneOverlayOnClose}
            />
            <MaskOverlay
              baseW={detW}
              baseH={detH}
              masks={displayedMasks}
              mode={mask.mode}
              onChange={mask.setWorkingMasks}
            />
            <ManualDetectOverlay
              baseW={detW}
              baseH={detH}
              mode={manualDetect.mode}
              onSubmit={(bbox) => manualDetect.submit(primary, bbox)}
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
              paused={!!pause.cameras[pane.secondary]}
              onTogglePause={() => pane.secondary && void pause.togglePause(pane.secondary)}
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
