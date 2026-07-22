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
    try {
      const r = await fetch(`/api/alerts/${alertId}/playback-url`);
      if (!r.ok) {
        alert(`Playback URL fetch failed: HTTP ${r.status}`);
        return;
      }
      const j = (await r.json()) as { url: string; note?: string };
      // Belt: try to launch the OS rtsp:// handler (VLC/mpv on Windows/Mac
      // register themselves as handlers by default). Suspenders: also copy
      // to clipboard so operator can paste into VLC → "Open Network Stream"
      // if the handler isn't registered.
      try {
        await navigator.clipboard.writeText(j.url);
      } catch {
        /* clipboard blocked in insecure context — non-fatal */
      }
      window.location.href = j.url;
      if (j.note) {
        // Slight delay so the rtsp:// navigation is already dispatched.
        setTimeout(() => alert(`Playback URL copied to clipboard.\n${j.note}`), 200);
      }
    } catch (e) {
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
