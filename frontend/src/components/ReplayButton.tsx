import styles from "./ReplayButton.module.css";

interface ReplayButtonProps {
  alertId: number;
  /** Visual size — "sm" for table rows, "md" for the lightbox meta area. */
  size?: "sm" | "md";
}

/**
 * Opens the alert's timestamp in an external RTSP player (VLC / mpv).
 * Fetches the NVR playback URL from the backend and hands it to the
 * OS via a plain rtsp:// link. Copy-to-clipboard fallback for when the
 * OS has no registered rtsp:// handler.
 *
 * Requires NVR_CHANNEL_<CAMERA> env on the web container to hit the
 * right channel (see /api/alerts/<id>/playback-url note field).
 *
 * Extracted from AlertsPage.tsx so the lightbox modal can render the
 * same affordance from the enlarged snapshot view — one shared component,
 * two consumption sites, single source of truth for the click behavior.
 */
export function ReplayButton({ alertId, size = "sm" }: ReplayButtonProps) {
  const onClick = async () => {
    // Open a new tab SYNCHRONOUSLY so popup blockers count this as a
    // user gesture. Fetch + navigation happen in the new tab; the
    // labeling page stays put. Prior implementation used
    // window.location.href which navigated the current page — for local
    // mp4 clips, the browser opened them in a bare viewer with no back
    // button, forcing operators to browser-back to return to labeling.
    const newTab = window.open("about:blank", "_blank");
    // Windows has an mpv:// URL scheme handler (docs/mpv-scheme-setup.md)
    // AND typically a rtsp:// handler (VLC or MPV). macOS/Linux browsers
    // usually have neither — clicking mpv:// or rtsp:// silently fails.
    // Ask the backend for the Frigate http:// clip URL instead, which
    // opens inline in a browser tab. Backend falls back to rtsp:// if
    // the camera has no Frigate mapping.
    const isWindows =
      typeof navigator !== "undefined" &&
      (navigator.platform?.startsWith("Win") || navigator.userAgent?.includes("Windows"));
    const query = isWindows ? "" : "?prefer=frigate";
    try {
      const r = await fetch(`/api/alerts/${alertId}/playback-url${query}`);
      if (!r.ok) {
        newTab?.close();
        // 404 + error:"no_recording" = archiver dropped a tombstone
        // (NVR FIFO'd the source footage). Show the archiver's reason
        // instead of a bare "HTTP 404" so the operator knows this
        // isn't a broken URL — the recording is genuinely gone.
        if (r.status === 404) {
          try {
            const j = (await r.json()) as { error?: string; note?: string };
            if (j.error === "no_recording") {
              alert(`No recording available for this alert.\n\n${j.note ?? ""}`.trim());
              return;
            }
          } catch {
            /* body wasn't JSON — fall through to generic message */
          }
        }
        alert(`Playback URL fetch failed: HTTP ${r.status}`);
        return;
      }
      const j = (await r.json()) as { url: string; note?: string; source_label?: string };
      // Belt: try to launch the OS rtsp:// / mpv:// handler. Suspenders:
      // also copy to clipboard so the operator can paste into VLC / MPV
      // manually if the handler isn't registered. Strip the mpv:// wrapper
      // on the clipboard copy so the pasted URL is directly openable in
      // MPV (Ctrl+V into the Open URL dialog).
      const clipboardUrl = j.url.replace(/^mpv:\/\//, "");
      try {
        await navigator.clipboard.writeText(clipboardUrl);
      } catch {
        /* clipboard blocked in insecure context — non-fatal */
      }
      // macOS/Linux don't have a registered mpv:// handler, so opening
      // the wrapped URL silently no-ops. Strip the wrapper on non-Windows
      // so the raw http:// URL opens in a browser tab and inline-plays
      // the MP4 via the built-in video element. (Reuses the isWindows
      // computed above for the ?prefer=frigate query.)
      const navUrl = isWindows ? j.url : clipboardUrl;
      if (newTab) {
        newTab.location.href = navUrl;
      } else {
        // Popup blocked → fall back to same-tab navigation (old behavior).
        window.location.href = navUrl;
      }
      // Silent toast — the source_label used to fire a browser alert()
      // to surface routing (Frigate / NVR / local clip), but the dialog
      // interrupts every playback click. Log to console for debugging;
      // notes surface only when they carry actionable info (they don't
      // right now — the archiver's tombstone message is the sole caller
      // and that path already alerts on the 404 branch above).
      if (j.source_label) console.log(`[playback] via ${j.source_label}`);
      if (j.note) console.log(`[playback] note: ${j.note}`);
    } catch (e) {
      newTab?.close();
      alert(`Playback URL error: ${e instanceof Error ? e.message : String(e)}`);
    }
  };
  return (
    <button
      type="button"
      onClick={onClick}
      className={`${styles.btn} ${size === "md" ? styles.md : ""}`}
      title="Open in VLC / mpv via rtsp:// (URL also copied to clipboard)"
    >
      ▶ Replay in VLC
    </button>
  );
}
