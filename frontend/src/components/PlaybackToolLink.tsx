import { Link } from "react-router-dom";
import styles from "./ReplayButton.module.css";

interface PlaybackToolLinkProps {
  camera: string;
  /** Alert unix timestamp — used to pre-fill the playback tool's start
   *  field. We subtract a few seconds of pre-roll so the operator lands
   *  on context leading up to the event, not the event itself. */
  ts: number;
  size?: "sm" | "md";
  /** Seconds of pre-roll before the alert ts. Defaults to 10s — enough
   *  to see the animal enter frame without wading through empty time. */
  preRollSeconds?: number;
}

/**
 * Deep-links to /react/playback with camera + start pre-filled.
 * Same shape as ReplayButton but a different destination — Replay
 * opens VLC directly; this one drops the operator into the playback
 * tool so they can adjust the window before hitting Open in VLC.
 *
 * Sibling of ReplayButton on the alert row + lightbox — one shared
 * "shortcut to VLC" surface with two flavors:
 *   ▶ Replay in VLC  — immediate, uses the archived clip or NVR range.
 *   🎬 Open in tool  — for when the archived window isn't the right
 *                       range and the operator wants to widen it.
 */
export function PlaybackToolLink({
  camera,
  ts,
  size = "sm",
  preRollSeconds = 10,
}: PlaybackToolLinkProps) {
  const start = new Date((ts - preRollSeconds) * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  const isoLocal =
    `${start.getFullYear()}-${pad(start.getMonth() + 1)}-${pad(start.getDate())}` +
    `T${pad(start.getHours())}:${pad(start.getMinutes())}:${pad(start.getSeconds())}`;
  const to = `/playback?camera=${encodeURIComponent(camera)}&start=${encodeURIComponent(isoLocal)}`;
  return (
    <Link
      to={to}
      target="_blank"
      rel="noreferrer"
      className={`${styles.btn} ${size === "md" ? styles.md : ""}`}
      title="Open in the /playback tool with this alert's camera + time pre-filled — lets you widen the window or switch to direct-live before opening in VLC. Opens a new tab so the alerts view stays put."
    >
      Playback Tool
    </Link>
  );
}
