import { useCallback, useState } from "react";
import { type Point, saveZone } from "../api/zone";
import type { EditMode } from "../components/ZoneOverlay";
import { polygonIsSimple } from "../util/polygon";
import { useZone } from "./useZone";

/**
 * Zone-polygon editor FSM — extracted from LivePreviewPage during the
 * god-component refactor (#34). Owns:
 *
 * - Mode state (`idle` | `draw` | `tweak`)
 * - Working polygon while in draw/tweak
 * - Server data source of truth (via useZone)
 * - Save flow with self-intersection guard
 *
 * The mutual-exclusion invariant between zone + mask editors lives ONE
 * LEVEL UP in the page — before calling `enterDraw()` / `enterTweak()`,
 * the page cancels the mask editor. Keeping cross-editor coordination
 * outside individual hooks avoids the tangled dependency graph a
 * shared-registry pattern would introduce (YAGNI for 2 editors; add
 * useEditorRegistry when a 3rd shows up).
 *
 * Pattern named: **owned-FSM hook** — single hook holds all state +
 * transitions for one finite state machine. Consumers see only the
 * verbs (enterDraw, enterTweak, cancel, save, close) and the current
 * mode + working data — no setters exposed.
 *
 * Also fixes the H4 anti-pattern the audit flagged: the previous
 * `useEffect(() => { if (mode === "idle" && data) setWorking(data.polygon); }, ...)`
 * pattern is replaced by an inline computation at the consumer
 * ("displayed polygon = mode === 'idle' ? server : working"). No
 * derived-state-via-effect.
 */
export interface ZoneEditorApi {
  mode: EditMode;
  /** Working polygon in draw/tweak modes. Reset to server value on
   *  every idle transition. Read this ONLY when mode !== "idle". */
  workingPolygon: Point[];
  /** Ambient server-side polygon. Consumers should render this when
   *  mode === "idle" and workingPolygon otherwise. */
  serverPolygon: Point[];
  /** True while polygon is self-intersecting — save is blocked. */
  isSimple: boolean;
  saving: boolean;
  saveErr: string | null;
  setWorkingPolygon: (poly: Point[]) => void;
  enterDraw: () => void;
  enterTweak: () => void;
  cancel: () => void;
  /** ZoneOverlay's draw-mode double-click-to-close handler. Transitions
   *  draw → tweak without discarding the working polygon. */
  closeDrawing: () => void;
  save: () => Promise<void>;
}

export function useZoneEditor(camera: string): ZoneEditorApi {
  const { data: zoneData, refresh: refreshZone } = useZone(camera);
  const [mode, setMode] = useState<EditMode>("idle");
  const [workingPolygon, setWorkingPolygon] = useState<Point[]>([]);
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);

  const serverPolygon = zoneData?.polygon ?? [];

  // Cancel editing on camera change via adjust-state-during-rendering.
  // React docs' "You Might Not Need an Effect" — set the sentinel state
  // during render, then setState fires and React re-renders with the
  // fresh mode. No subscribing effect → no tier-1 anti-pattern from the
  // ESLint plugin.
  const [prevCamera, setPrevCamera] = useState(camera);
  if (camera !== prevCamera) {
    setPrevCamera(camera);
    setMode("idle");
    setSaveErr(null);
  }

  const enterDraw = useCallback(() => {
    setWorkingPolygon([]);
    setMode("draw");
    setSaveErr(null);
  }, []);

  const enterTweak = useCallback(() => {
    setWorkingPolygon(zoneData?.polygon ?? []);
    setMode("tweak");
    setSaveErr(null);
  }, [zoneData]);

  const cancel = useCallback(() => {
    setMode("idle");
    setWorkingPolygon(zoneData?.polygon ?? []);
    setSaveErr(null);
  }, [zoneData]);

  const closeDrawing = useCallback(() => setMode("tweak"), []);

  const save = useCallback(async () => {
    if (workingPolygon.length < 3 || saving) return;
    // Guard against self-intersecting polygons — SVG strokes vertex
    // order, so the closing edge V(n-1)→V0 can slice through the
    // interior when a vertex is placed inside the outline.
    if (!polygonIsSimple(workingPolygon)) {
      setSaveErr(
        "self-intersecting — one edge crosses another. Move vertices so the outline doesn't cross itself.",
      );
      return;
    }
    setSaving(true);
    setSaveErr(null);
    try {
      await saveZone(camera, workingPolygon);
      refreshZone();
      setMode("idle");
    } catch (e) {
      setSaveErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }, [camera, workingPolygon, saving, refreshZone]);

  return {
    mode,
    workingPolygon,
    serverPolygon,
    isSimple: polygonIsSimple(workingPolygon),
    saving,
    saveErr,
    setWorkingPolygon,
    enterDraw,
    enterTweak,
    cancel,
    closeDrawing,
    save,
  };
}
