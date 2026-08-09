import { useCallback, useState } from "react";
import {
  type Point,
  type SlewPresetEntry,
  commandPtzPreset,
  saveSlewPresets,
} from "../api/slewPresets";
import type { EditMode } from "../components/ZoneOverlay";
import { polygonIsSimple } from "../util/polygon";
import { useSlewPresets } from "./useSlewPresets";

/**
 * Multi-polygon editor FSM — one active preset at a time.
 *
 * Extends the single-polygon useZoneEditor pattern (owned-FSM hook) to
 * handle N presets. Only one preset is "active" for editing; the
 * others render as read-only overlays for context. Save flushes the
 * ENTIRE preset list (backend replaces wholesale), so we keep a full
 * working copy in state.
 *
 * Pattern: **owned-FSM with active-item pointer** — same shape as
 * useZoneEditor but with an activePresetId sentinel. Consumers show
 * the active preset's polygon as editable, all others as static
 * outlines.
 */
export interface SlewPresetEditorApi {
  mode: EditMode;
  /** Active preset (idle=view all, draw/tweak=edit this one). Null in idle mode. */
  activePreset: number | null;
  /** Full working list. Includes the active preset's in-flight polygon. */
  workingPresets: SlewPresetEntry[];
  /** Server truth (last fetched). Used to render inactive presets. */
  serverPresets: SlewPresetEntry[];
  ptzCameraId: number;
  homePreset: number;
  enabled: boolean;
  frameWidth: number;
  frameHeight: number;
  isSimple: boolean;
  saving: boolean;
  saveErr: string | null;
  /** ZoneOverlay setter for the active preset's polygon. */
  setActivePolygon: (poly: Point[]) => void;
  /** Start drawing a NEW preset from scratch — polygon empty. */
  enterDraw: (preset: number, name: string) => void;
  /** Start tweaking an EXISTING preset — polygon primed from server. */
  enterTweak: (preset: number) => void;
  /** Delete the active preset from the working list. Must save to persist. */
  removeActive: () => void;
  /** Rename the active preset. */
  renameActive: (name: string) => void;
  /** Back to view-all mode without discarding the working polygon. */
  closeDrawing: () => void;
  cancel: () => void;
  save: () => Promise<void>;
  /** Fire GotoPreset via /api/ptz/preset. Non-editing action — for
   *  aligning polygon with the actual zoomed view. */
  goto: (preset: number) => Promise<void>;
}

export function useSlewPresetEditor(camera: string): SlewPresetEditorApi {
  const { data, refresh } = useSlewPresets(camera);
  const [mode, setMode] = useState<EditMode>("idle");
  const [activePreset, setActivePreset] = useState<number | null>(null);
  const [workingPresets, setWorkingPresets] = useState<SlewPresetEntry[]>([]);
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);

  const serverPresets = data?.presets ?? [];

  // Cancel editing on camera change — same pattern as useZoneEditor
  // (adjust-state-during-rendering; no useEffect subscribing to camera).
  const [prevCamera, setPrevCamera] = useState(camera);
  if (camera !== prevCamera) {
    setPrevCamera(camera);
    setMode("idle");
    setActivePreset(null);
    setWorkingPresets(data?.presets ?? []);
    setSaveErr(null);
  }

  // Server data drift: if a poll brings in a fresh preset list while
  // we're idle, adopt it as working. In draw/tweak mode leave the
  // in-flight working set alone (operator would lose work otherwise).
  const [prevServerVersion, setPrevServerVersion] = useState<string>("");
  const serverKey = JSON.stringify(serverPresets);
  if (mode === "idle" && serverKey !== prevServerVersion) {
    setPrevServerVersion(serverKey);
    setWorkingPresets(serverPresets);
  }

  const findActive = (): SlewPresetEntry | undefined =>
    activePreset === null ? undefined : workingPresets.find((p) => p.preset === activePreset);

  const setActivePolygon = useCallback(
    (poly: Point[]) => {
      if (activePreset === null) return;
      setWorkingPresets((prev) =>
        prev.map((p) => (p.preset === activePreset ? { ...p, polygon: poly } : p)),
      );
    },
    [activePreset],
  );

  const enterDraw = useCallback((preset: number, name: string) => {
    // Insert-or-replace: if a preset with this number exists, its
    // polygon is cleared for redraw. Otherwise a new entry is added.
    setWorkingPresets((prev) => {
      const exists = prev.some((p) => p.preset === preset);
      if (exists) {
        return prev.map((p) => (p.preset === preset ? { ...p, polygon: [], name } : p));
      }
      return [...prev, { preset, name, polygon: [] }];
    });
    setActivePreset(preset);
    setMode("draw");
    setSaveErr(null);
  }, []);

  const enterTweak = useCallback(
    (preset: number) => {
      // Prime working polygon from server (in case operator opened a
      // preset they hadn't touched yet).
      const server = serverPresets.find((p) => p.preset === preset);
      if (server) {
        setWorkingPresets((prev) => {
          const exists = prev.some((p) => p.preset === preset);
          return exists
            ? prev.map((p) => (p.preset === preset ? { ...server } : p))
            : [...prev, { ...server }];
        });
      }
      setActivePreset(preset);
      setMode("tweak");
      setSaveErr(null);
    },
    [serverPresets],
  );

  const removeActive = useCallback(() => {
    if (activePreset === null) return;
    setWorkingPresets((prev) => prev.filter((p) => p.preset !== activePreset));
    setActivePreset(null);
    setMode("idle");
  }, [activePreset]);

  const renameActive = useCallback(
    (name: string) => {
      if (activePreset === null) return;
      setWorkingPresets((prev) =>
        prev.map((p) => (p.preset === activePreset ? { ...p, name } : p)),
      );
    },
    [activePreset],
  );

  const closeDrawing = useCallback(() => setMode("tweak"), []);

  const cancel = useCallback(() => {
    setMode("idle");
    setActivePreset(null);
    setWorkingPresets(serverPresets);
    setSaveErr(null);
  }, [serverPresets]);

  const save = useCallback(async () => {
    if (saving) return;
    // Guard: reject any polygon that's self-intersecting OR under-sized.
    for (const p of workingPresets) {
      if (p.polygon.length < 3) {
        setSaveErr(`preset ${p.preset} (${p.name}): polygon needs 3+ points`);
        return;
      }
      if (!polygonIsSimple(p.polygon)) {
        setSaveErr(`preset ${p.preset} (${p.name}): self-intersecting`);
        return;
      }
    }
    setSaving(true);
    setSaveErr(null);
    try {
      await saveSlewPresets(camera, workingPresets);
      refresh();
      setMode("idle");
      setActivePreset(null);
    } catch (e) {
      setSaveErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }, [camera, workingPresets, saving, refresh]);

  const goto = useCallback(
    async (preset: number) => {
      try {
        await commandPtzPreset(camera, preset);
      } catch (e) {
        setSaveErr(e instanceof Error ? e.message : String(e));
      }
    },
    [camera],
  );

  const active = findActive();
  return {
    mode,
    activePreset,
    workingPresets,
    serverPresets,
    ptzCameraId: data?.ptz_camera_id ?? 2,
    homePreset: data?.home_preset ?? 1,
    enabled: data?.enabled ?? false,
    frameWidth: data?.frame_width ?? 1280,
    frameHeight: data?.frame_height ?? 720,
    isSimple: active ? polygonIsSimple(active.polygon) : true,
    saving,
    saveErr,
    setActivePolygon,
    enterDraw,
    enterTweak,
    removeActive,
    renameActive,
    closeDrawing,
    cancel,
    save,
    goto,
  };
}
