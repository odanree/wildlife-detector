import { useState } from "react";
import { snapshotUrl } from "../api/alerts";

/**
 * Wrapper around `<img>` for snapshot thumbnails that survives transient
 * fetch failures. The bare `<img>` in a React table is fire-and-forget —
 * if the browser drops the request (burst limit, page mid-transition,
 * momentary WSL9P bind-mount latency between detector writing the JPEG
 * and web container serving it), the element stays blank forever because
 * React re-renders don't re-trigger a fetch when the `src` string is
 * unchanged.
 *
 * Fix: onError → retry with `?v=<attempt>` cache-buster after a short
 * delay. Bounded retries (default 2) so a genuinely-missing file
 * eventually gives up and shows the fallback. Pattern name: **retry with
 * exponential backoff at the trust boundary** (browser fetch vs. static
 * file server).
 */
export interface SnapshotImgProps {
  snapshot: string;
  alt: string;
  className?: string;
  loading?: "eager" | "lazy";
  maxRetries?: number;
  retryDelayMs?: number;
  /** Prefer the `.thumb.jpg` variant (rewritten by the notifier with a
   *  solid red block over the bbox — visible even at rapid-labeling
   *  thumbnail scale). On 404 falls back to the base snapshot so old
   *  alerts still render. Default false. */
  preferThumb?: boolean;
}

export function SnapshotImg({
  snapshot,
  alt,
  className,
  loading = "lazy",
  maxRetries = 2,
  retryDelayMs = 500,
  preferThumb = false,
}: SnapshotImgProps): JSX.Element {
  const [attempt, setAttempt] = useState(0);
  const [gaveUp, setGaveUp] = useState(false);
  // preferThumb → try `<name>.thumb.jpg` first, drop to base on any
  // error before entering the normal cache-bust retry loop. Old alerts
  // from before the notifier started writing thumb siblings still
  // render this way.
  const [fellBackToBase, setFellBackToBase] = useState(false);

  const base = snapshotUrl(snapshot);
  const useThumb = preferThumb && !fellBackToBase;
  const url = useThumb ? base.replace(/\.jpg(\?|$)/, ".thumb.jpg$1") : base;
  // Add cache-bust ONLY on retries; the first request uses the canonical
  // URL so the browser cache still helps most-of-the-time.
  const src = attempt === 0 ? url : `${url}?v=${attempt}`;

  if (gaveUp) {
    return (
      <span className={className} aria-label={alt}>
        —
      </span>
    );
  }

  return (
    <img
      className={className}
      src={src}
      alt={alt}
      loading={loading}
      onError={() => {
        // First failure on the thumb variant → drop to base immediately,
        // no cache-bust yet (thumb-not-yet-generated is the expected
        // path for older alerts, not a transient fetch flake).
        if (useThumb) {
          setFellBackToBase(true);
          return;
        }
        if (attempt >= maxRetries) {
          setGaveUp(true);
          return;
        }
        // Backoff schedule: 500ms, 1000ms, 2000ms, ...
        const delay = retryDelayMs * 2 ** attempt;
        setTimeout(() => setAttempt((a) => a + 1), delay);
      }}
    />
  );
}
