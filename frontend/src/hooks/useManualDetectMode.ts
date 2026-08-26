import { useCallback, useRef, useState } from "react";
import { postManualDetect } from "../api/manualDetect";

/**
 * State machine for the manual-detect draw flow, split out of
 * LivePreviewPage so the page owns only the toggle button and the
 * hook owns armed-state + POST + toast lifecycle.
 *
 * States:
 * - `off`        — no interaction; overlay is idle.
 * - `armed`      — waiting for the operator's first click-drag.
 * - `submitting` — POST in flight; overlay is frozen (no re-arms).
 *
 * Pattern: **single-writer state machine at the mutation boundary**.
 * All submits go through the same handler that owns POST + toast +
 * disarm. Overlay component is presentation-only; hook holds the
 * dispatch policy.
 */

export type ManualDetectStatus =
  | { kind: "idle" }
  | { kind: "success"; trackId: number; camera: string; at: number }
  | { kind: "error"; message: string; camera: string; at: number };

export interface ManualDetectApi {
  mode: "off" | "armed" | "submitting";
  status: ManualDetectStatus;
  toggle: () => void; // off ↔ armed
  cancel: () => void; // force back to off
  submit: (camera: string, bbox: [number, number, number, number]) => Promise<void>;
  clearStatus: () => void;
}

export function useManualDetectMode(): ManualDetectApi {
  const [mode, setMode] = useState<"off" | "armed" | "submitting">("off");
  const [status, setStatus] = useState<ManualDetectStatus>({ kind: "idle" });
  // Serialize submits across rapid clicks — if a POST is in flight,
  // ignore new ones. AbortController protects against race on unmount
  // (called by cancel()).
  const inflight = useRef<AbortController | null>(null);

  const toggle = useCallback(() => {
    setMode((m) => (m === "off" ? "armed" : "off"));
    setStatus({ kind: "idle" });
  }, []);

  const cancel = useCallback(() => {
    inflight.current?.abort();
    inflight.current = null;
    setMode("off");
  }, []);

  const clearStatus = useCallback(() => setStatus({ kind: "idle" }), []);

  const submit = useCallback(
    async (camera: string, bbox: [number, number, number, number]) => {
      if (mode === "submitting") return;
      setMode("submitting");
      const ctrl = new AbortController();
      inflight.current = ctrl;
      try {
        const resp = await postManualDetect(camera, bbox, ctrl.signal);
        if (ctrl.signal.aborted) return;
        if (resp.error || !resp.track_id) {
          setStatus({
            kind: "error",
            message: resp.error ?? "unknown error",
            camera,
            at: Date.now(),
          });
        } else {
          setStatus({
            kind: "success",
            trackId: resp.track_id,
            camera,
            at: Date.now(),
          });
        }
      } catch (e) {
        if (ctrl.signal.aborted) return;
        if (e instanceof DOMException && e.name === "AbortError") return;
        setStatus({
          kind: "error",
          message: e instanceof Error ? e.message : String(e),
          camera,
          at: Date.now(),
        });
      } finally {
        if (!ctrl.signal.aborted) {
          setMode("off"); // one-shot: submit disarms so operator opts in each time
          inflight.current = null;
        }
      }
    },
    [mode],
  );

  return { mode, status, toggle, cancel, submit, clearStatus };
}
