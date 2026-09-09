import { useCallback, useMemo, useState } from "react";
import { GlobalHeader } from "../components/GlobalHeader";
import styles from "./PlaybackUrlPage.module.css";

/**
 * Ad-hoc NVR playback URL builder — operator picks channel + time
 * range, backend composes the rtsp:// URL (creds stay server-side).
 * Two output actions:
 *   - **Open in VLC** — navigates the newly-opened tab to the rtsp:// URL,
 *     triggering the OS RTSP handler (VLC on Windows).
 *   - **Copy URL** — puts the URL on the clipboard for paste into VLC's
 *     "Open Network Stream" dialog when the handler isn't registered.
 *
 * Same shape as ReplayButton (which does per-alert lookups) but decoupled
 * from any alert row — the operator supplies channel + time directly.
 * Persists the last-picked channel + duration to localStorage so the
 * common "same channel, another moment" flow doesn't re-input every time.
 */

const CHANNELS: readonly number[] = [1, 3, 4, 5, 6, 7, 8];

// Known channel → camera-name mapping (from NVR_CHANNEL_* env on the
// web container). Only three channels are mapped today; the rest show
// as "channel N" so the operator can still reach them.
const CHANNEL_LABEL: Record<number, string> = {
  5: "5 (yard)",
  6: "6 (rooftop)",
  8: "8 (backyard)",
};

const DURATION_OPTIONS: readonly { label: string; seconds: number }[] = [
  { label: "30 sec", seconds: 30 },
  { label: "1 min", seconds: 60 },
  { label: "2 min", seconds: 120 },
  { label: "5 min", seconds: 300 },
  { label: "10 min", seconds: 600 },
  { label: "30 min", seconds: 1800 },
];

interface PlaybackUrlResponse {
  url?: string;
  channel?: number;
  camera?: string;
  start?: string;
  end?: string;
  error?: string;
}

/** Format a Date's local-time components as `YYYY-MM-DDTHH:MM:SS` — the
 *  API expects PST/PDT, and the browser is already in that zone. */
function isoLocal(dt: Date): string {
  const pad = (n: number, w = 2) => String(n).padStart(w, "0");
  return (
    `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}` +
    `T${pad(dt.getHours())}:${pad(dt.getMinutes())}:${pad(dt.getSeconds())}`
  );
}

export function PlaybackUrlPage() {
  const [channel, setChannelRaw] = useState<number>(() => {
    const saved = Number.parseInt(localStorage.getItem("playbackUrlChannel") ?? "", 10);
    return CHANNELS.includes(saved) ? saved : 5;
  });
  const setChannel = useCallback((c: number) => {
    setChannelRaw(c);
    localStorage.setItem("playbackUrlChannel", String(c));
  }, []);

  const [durationSec, setDurationSecRaw] = useState<number>(() => {
    const saved = Number.parseInt(localStorage.getItem("playbackUrlDuration") ?? "", 10);
    return DURATION_OPTIONS.some((o) => o.seconds === saved) ? saved : 60;
  });
  const setDurationSec = useCallback((s: number) => {
    setDurationSecRaw(s);
    localStorage.setItem("playbackUrlDuration", String(s));
  }, []);

  const [source, setSourceRaw] = useState<"nvr" | "direct">(() => {
    const saved = localStorage.getItem("playbackUrlSource");
    return saved === "direct" ? "direct" : "nvr";
  });
  const setSource = useCallback((s: "nvr" | "direct") => {
    setSourceRaw(s);
    localStorage.setItem("playbackUrlSource", s);
  }, []);

  // Start defaults to "1 minute ago" so the operator can drop in a
  // near-live view without touching the picker.
  const [startStr, setStartStr] = useState<string>(() => isoLocal(new Date(Date.now() - 60_000)));

  const [result, setResult] = useState<PlaybackUrlResponse | null>(null);
  const [status, setStatus] = useState<"idle" | "loading" | "ok" | "err">("idle");
  const [statusMsg, setStatusMsg] = useState<string>("");

  const endStr = useMemo(() => {
    // Additive computation from start + duration — always in sync,
    // no separate end-time state to drift.
    try {
      const d = new Date(startStr);
      if (Number.isNaN(d.getTime())) return "";
      return isoLocal(new Date(d.getTime() + durationSec * 1000));
    } catch {
      return "";
    }
  }, [startStr, durationSec]);

  const build = useCallback(
    async (openInVlc: boolean): Promise<void> => {
      if (source === "nvr" && (!startStr || !endStr)) {
        setStatus("err");
        setStatusMsg("start time invalid");
        return;
      }
      setStatus("loading");
      setStatusMsg("");
      // Open the target tab synchronously BEFORE the fetch when the
      // user clicked "Open in VLC" — popup blockers require the open()
      // call to happen inside the click handler. Navigating happens
      // once the URL is back.
      const targetTab = openInVlc ? window.open("about:blank", "_blank") : null;
      try {
        const params = new URLSearchParams({
          channel: String(channel),
          source,
        });
        if (source === "nvr") {
          params.set("start", startStr);
          params.set("end", endStr);
        }
        const r = await fetch(`/api/playback-url?${params.toString()}`);
        const body = (await r.json()) as PlaybackUrlResponse;
        if (!r.ok || !body.url) {
          setStatus("err");
          setStatusMsg(body.error ?? `HTTP ${r.status}`);
          targetTab?.close();
          return;
        }
        setResult(body);
        setStatus("ok");
        // Belt: copy to clipboard so VLC "Open Network Stream" paste works
        // even when the OS rtsp:// handler isn't registered.
        try {
          await navigator.clipboard.writeText(body.url);
          setStatusMsg("URL copied to clipboard");
        } catch {
          setStatusMsg("URL ready (clipboard blocked)");
        }
        if (openInVlc && targetTab) {
          targetTab.location.href = body.url;
        }
      } catch (e) {
        setStatus("err");
        setStatusMsg(e instanceof Error ? e.message : String(e));
        targetTab?.close();
      }
    },
    [channel, source, startStr, endStr],
  );

  return (
    <div className={styles.wrap}>
      <GlobalHeader right={null} />
      <div className={styles.body}>
        <h2 className={styles.h2}>NVR playback URL</h2>
        <p className={styles.hint}>
          Pick a channel, start time (America/Los_Angeles), and duration. Opens the RTSP URL in VLC
          via the OS handler, and copies it to the clipboard as a fallback.
        </p>

        <div className={styles.row}>
          <label className={styles.label}>
            source
            <select
              className={styles.select}
              value={source}
              onChange={(e) => setSource(e.target.value as "nvr" | "direct")}
              title="nvr = past recording via NVR /cam/playback (time range required); direct = live stream from the camera's own IP (time range ignored)"
            >
              <option value="nvr">NVR (past recording)</option>
              <option value="direct">Direct (live)</option>
            </select>
          </label>

          <label className={styles.label}>
            channel
            <select
              className={styles.select}
              value={channel}
              onChange={(e) => setChannel(Number.parseInt(e.target.value, 10))}
            >
              {CHANNELS.map((c) => (
                <option key={c} value={c}>
                  {CHANNEL_LABEL[c] ?? `channel ${c}`}
                </option>
              ))}
            </select>
          </label>

          {source === "nvr" && (
            <>
              <label className={styles.label}>
                start (PST)
                <input
                  type="datetime-local"
                  step="1"
                  className={styles.input}
                  value={startStr}
                  onChange={(e) => setStartStr(e.target.value)}
                />
              </label>

              <label className={styles.label}>
                duration
                <select
                  className={styles.select}
                  value={durationSec}
                  onChange={(e) => setDurationSec(Number.parseInt(e.target.value, 10))}
                >
                  {DURATION_OPTIONS.map((o) => (
                    <option key={o.seconds} value={o.seconds}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </label>
            </>
          )}
        </div>

        {source === "nvr" && (
          <div className={styles.row}>
            <span className={styles.readout}>
              end: <b>{endStr || "—"}</b>
            </span>
          </div>
        )}

        <div className={styles.actions}>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnPrimary}`}
            onClick={() => void build(true)}
            disabled={status === "loading"}
          >
            {status === "loading" ? "Building…" : "▶ Open in VLC"}
          </button>
          <button
            type="button"
            className={styles.btn}
            onClick={() => void build(false)}
            disabled={status === "loading"}
          >
            Copy URL
          </button>
        </div>

        {statusMsg && <div className={status === "err" ? styles.err : styles.ok}>{statusMsg}</div>}

        {result?.url && (
          <div className={styles.urlBox}>
            <code className={styles.url}>{result.url}</code>
          </div>
        )}
      </div>
    </div>
  );
}
