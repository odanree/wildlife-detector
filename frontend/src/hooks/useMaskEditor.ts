import { useCallback, useEffect, useState } from "react";
import { type Rect, saveMasks } from "../api/masks";
import type { MaskMode } from "../components/MaskOverlay";
import { useMasks } from "./useMasks";

/**
 * OSD mask editor FSM — extracted from LivePreviewPage during the
 * god-component refactor (#34). Owns:
 *
 * - Mode state (`idle` | `edit`) — simpler than the zone editor
 *   because masks don't have a distinct draw-vs-tweak split.
 * - Working masks array while in edit
 * - Server data source of truth (via useMasks)
 * - Save flow
 *
 * Same **owned-FSM hook** pattern as useZoneEditor. Mutual-exclusion
 * with the zone editor is coordinated one level up (the page cancels
 * the zone editor before calling `enterEdit()` here).
 *
 * Also fixes the H4 anti-pattern the audit flagged for the mask side:
 * previous `useEffect(() => { if (mode === "idle" && data) setWorking(data.masks); }, ...)`
 * is replaced by inline computation at the consumer.
 */
export interface MaskEditorApi {
  mode: MaskMode;
  workingMasks: Rect[];
  serverMasks: Rect[];
  saving: boolean;
  saveErr: string | null;
  setWorkingMasks: (masks: Rect[]) => void;
  enterEdit: () => void;
  cancel: () => void;
  save: () => Promise<void>;
}

export function useMaskEditor(camera: string): MaskEditorApi {
  const { data: masksData, refresh: refreshMasks } = useMasks(camera);
  const [mode, setMode] = useState<MaskMode>("idle");
  const [workingMasks, setWorkingMasks] = useState<Rect[]>([]);
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);

  const serverMasks = masksData?.masks ?? [];

  // Cancel editing on camera change — otherwise operator would silently
  // be editing a stale mask set. Reset saveErr too.
  // biome-ignore lint/correctness/useExhaustiveDependencies: camera IS the trigger; body only calls setters.
  useEffect(() => {
    setMode("idle");
    setSaveErr(null);
  }, [camera]);

  const enterEdit = useCallback(() => {
    setWorkingMasks(masksData?.masks ?? []);
    setMode("edit");
    setSaveErr(null);
  }, [masksData]);

  const cancel = useCallback(() => {
    setMode("idle");
    setWorkingMasks(masksData?.masks ?? []);
    setSaveErr(null);
  }, [masksData]);

  const save = useCallback(async () => {
    if (saving) return;
    setSaving(true);
    setSaveErr(null);
    try {
      await saveMasks(camera, workingMasks);
      refreshMasks();
      setMode("idle");
    } catch (e) {
      setSaveErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }, [camera, workingMasks, saving, refreshMasks]);

  return {
    mode,
    workingMasks,
    serverMasks,
    saving,
    saveErr,
    setWorkingMasks,
    enterEdit,
    cancel,
    save,
  };
}
