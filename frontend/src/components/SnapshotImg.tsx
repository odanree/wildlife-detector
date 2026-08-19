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
}

export function SnapshotImg({
  snapshot,
  alt,
  className,
  loading = "lazy",
  maxRetries = 2,
  retryDelayMs = 500,
}: SnapshotImgProps): JSX.Element {
  const [attempt, setAttempt] = useState(0);
  const [gaveUp, setGaveUp] = useState(false);

  const base = snapshotUrl(snapshot);
  // Add cache-bust ONLY on retries; the first request uses the canonical
  // URL so the browser cache still helps most-of-the-time.
  const src = attempt === 0 ? base : `${base}?v=${attempt}`;

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
