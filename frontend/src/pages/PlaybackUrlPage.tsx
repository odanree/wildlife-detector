import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
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

const CHANNELS: readonly number[] = [1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13];

// Per-NVR valid channel sets. Annke N98PBK physically has 8 channels;
// Amcrest holds the rest of the fleet. Direct-mode picker still shows
// every CAMERA_RTSP_<N> slot regardless of NVR (no NVR involved).
const NVR_CHANNELS: Record<"amcrest" | "annke", readonly number[]> = {
  amcrest: [1, 3, 4, 5, 6, 7, 8],
  annke: [1, 2, 3, 4, 5, 6, 7, 8],
};

// Known channel → camera-name mapping (from NVR_CHANNEL_* env on the
// web container). Only three channels are mapped today; the rest show
// as "channel N" so the operator can still reach them.
const CHANNEL_LABEL: Record<number, string> = {
  1: "1 (sideyard)",
  3: "3 (crawlspace int)",
  5: "5 (yard)",
  6: "6 (rooftop)",
  7: "7 (crawlspace ext)",
  8: "8 (backyard)",
  11: "11 (Annke plant pathway — direct only)",
  12: "12 (Annke side path .125 — direct only)",
  13: "13 (Annke sideyard .112 — direct only)",
};

// Per-NVR channel labels — same channel number can mean different cameras
// on Amcrest vs Annke, so the label context matters.
const CHANNEL_LABEL_BY_NVR: Record<"amcrest" | "annke", Record<number, string>> = {
  amcrest: CHANNEL_LABEL,
  annke: {
    1: "1 (crawlspace int)",
    2: "2 (rooftop)",
    3: "3 (crawlspace ext)",
    4: "4 (backyard)",
    5: "5 (frontyard)",
    6: "6 (sideyard)",
    7: "7 (corner)",
    8: "8 (plant pathway)",
  },
};

// Alerts-page deep-link camera_id → (channel, NVR) mapping. The alerts
// page hands us ?camera=<id> and we preselect the picker so the operator
// lands on the right NVR + channel for THAT camera's recording home.
//
// Rooftop and backyard reverted to Amcrest 2026-09-16 after Annke's RTSP
// serving pipe wedged post-firmware config surgery — the recordings still
// land on Annke's disk but generic RTSP pulls stall. Cameras dual-stream
// to Amcrest anyway, and Amcrest handles 4K fine. Both stayed on Annke
// originally (PR #220) to gain 12MP handling; neither camera is >4K so
// that reason no longer applies.
const CAMERA_TO_CHANNEL: Record<string, { channel: number; nvr: "amcrest" | "annke" }> = {
  yard: { channel: 5, nvr: "amcrest" },
  rooftop: { channel: 6, nvr: "amcrest" },
  backyard: { channel: 8, nvr: "amcrest" },
  crawlspace: { channel: 7, nvr: "amcrest" },
  crawlspace_inside: { channel: 3, nvr: "amcrest" },
  sideyard: { channel: 1, nvr: "amcrest" },
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
  // Deep-link params: /playback?camera=yard&start=YYYY-MM-DDTHH:MM:SS
  // (fired from the alerts page). Both are optional — bare visits use
  // localStorage + "1 minute ago" as before. URL params win over
  // localStorage for one-shot deep-links but don't overwrite the
  // sticky value (the user's "usual channel" survives).
  const [urlParams] = useSearchParams();
  // Deep-link resolves to a single (channel, NVR) pair or null. When
  // present, this forces the picker onto exactly one channel and the
  // right NVR — sticky multi-selection is ignored so the operator's
  // starting point matches the alert they clicked from.
  const paramPreset = (() => {
    const cam = (urlParams.get("camera") ?? "").toLowerCase();
    if (cam && cam in CAMERA_TO_CHANNEL) return CAMERA_TO_CHANNEL[cam];
    const c = Number.parseInt(urlParams.get("channel") ?? "", 10);
    if (CHANNELS.includes(c)) return { channel: c, nvr: "amcrest" as const };
    return null;
  })();
  const paramStart = urlParams.get("start");

  // Channels: multi-select so operators can fan a single event out to
  // multiple cameras (same time window, different angles). Stored as
  // comma-separated list in localStorage; single-int legacy value is
  // still honored on load so no config reset for existing users.
  const [channels, setChannelsRaw] = useState<number[]>(() => {
    if (paramPreset != null) return [paramPreset.channel];
    const saved =
      localStorage.getItem("playbackUrlChannels") ??
      localStorage.getItem("playbackUrlChannel") ??
      "";
    const parsed = saved
      .split(",")
      .map((s) => Number.parseInt(s, 10))
      .filter((n) => CHANNELS.includes(n));
    return parsed.length > 0 ? parsed : [5];
  });
  const setChannels = useCallback((cs: number[]) => {
    // Empty selection would produce zero URLs — force at least one.
    const next = cs.length > 0 ? cs : [5];
    setChannelsRaw(next);
    localStorage.setItem("playbackUrlChannels", next.join(","));
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

  // Which NVR to route source=nvr playback through. Amcrest is the fleet
  // default; Annke (Hikvision family) holds a separate camera set with a
  // different URL shape (/Streaming/tracks/… + Pacific-as-fake-Z). See
  // src/web_service.py::api_playback_url for the vendor branch.
  const [nvr, setNvrRaw] = useState<"amcrest" | "annke">(() => {
    // Deep-link overrides sticky NVR — same rationale as the channel
    // reset above (align the picker to the alert's playback home).
    if (paramPreset != null) return paramPreset.nvr;
    const saved = localStorage.getItem("playbackUrlNvr");
    return saved === "annke" ? "annke" : "amcrest";
  });
  const setNvr = useCallback((n: "amcrest" | "annke") => {
    setNvrRaw(n);
    localStorage.setItem("playbackUrlNvr", n);
  }, []);

  // When the operator flips NVR (or source) and the sticky channel is
  // not valid on the new NVR (e.g. Amcrest ch11 does not exist on Annke's
  // 8-channel N98PBK), snap the picker to the first valid channel so the
  // URL builder cannot produce an invalid /Streaming/tracks/1101/ path.
  useEffect(() => {
    if (source !== "nvr") return;
    const valid = NVR_CHANNELS[nvr];
    const filtered = channels.filter((c) => valid.includes(c));
    if (filtered.length !== channels.length) {
      setChannels(filtered.length > 0 ? filtered : [valid[0]]);
    }
  }, [source, nvr, channels, setChannels]);

  // Start defaults to "1 minute ago" so the operator can drop in a
  // near-live view without touching the picker. `?start=` from a deep
  // link wins over that default — the alerts-page shortcut hands us
  // the exact alert timestamp already offset for a bit of pre-roll.
  const [startStr, setStartStr] = useState<string>(() => {
    if (paramStart && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(paramStart)) return paramStart;
    return isoLocal(new Date(Date.now() - 60_000));
  });

  // Deep-link re-application: initial-state seeding only runs on MOUNT.
  // If the operator is already on /playback and clicks another alert's
  // Playback Tool link, react-router updates urlParams but the picker
  // state stays sticky. Watch the deep-link tuple and reset channel/NVR
  // (and startStr) whenever it changes so the preset always wins.
  // Declared after the startStr state so setStartStr is not referenced
  // before its declaration (worked at runtime — effects run post-render —
  // but read as a TDZ hazard).
  //
  // paramPreset is a fresh object every render, so the effect captures
  // its primitives instead: depending on the object itself would refire
  // this reset on every render and clobber the operator's manual picks.
  const presetChannel = paramPreset?.channel ?? null;
  const presetNvr = paramPreset?.nvr ?? null;
  useEffect(() => {
    if (presetChannel == null || presetNvr == null) return;
    setChannels([presetChannel]);
    setNvr(presetNvr);
    if (paramStart && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(paramStart)) {
      setStartStr(paramStart);
    }
  }, [presetChannel, presetNvr, paramStart, setChannels, setNvr]);

  const [results, setResults] = useState<PlaybackUrlResponse[]>([]);
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
      // Open one tab per channel synchronously BEFORE the fetches —
      // popup blockers require the open() call to happen inside the
      // click handler; deferred opens after `await` are blocked. Store
      // the tabs in the channel order so we can navigate them once the
      // corresponding URL comes back.
      const targetTabs: (Window | null)[] = openInVlc
        ? channels.map(() => window.open("about:blank", "_blank"))
        : [];

      try {
        // Fan-out: one API request per channel, all in parallel. Same
        // start/end/nvr — different channel. Backend is 1 ms per call
        // so serializing would only save one round-trip; parallel keeps
        // the UI snappy even with 8 channels.
        const responses = await Promise.all(
          channels.map(async (ch) => {
            const params = new URLSearchParams({
              channel: String(ch),
              source,
            });
            if (source === "nvr") {
              params.set("start", startStr);
              params.set("end", endStr);
              params.set("nvr", nvr);
            }
            const r = await fetch(`/api/playback-url?${params.toString()}`);
            const body = (await r.json()) as PlaybackUrlResponse;
            return { ok: r.ok, status: r.status, body };
          }),
        );

        const errs = responses.filter((r) => !r.ok || !r.body.url);
        const oks = responses.filter((r) => r.ok && r.body.url).map((r) => r.body);

        if (oks.length === 0) {
          setStatus("err");
          setStatusMsg(errs[0]?.body?.error ?? `HTTP ${errs[0]?.status ?? "?"}`);
          for (const t of targetTabs) t?.close();
          return;
        }

        setResults(oks);
        setStatus(errs.length > 0 ? "err" : "ok");

        // Belt: copy all URLs to clipboard newline-separated for VLC's
        // "Open Network Stream" paste when the OS handler isn't hooked
        // up. With multi-select this becomes a multi-line paste; VLC
        // adds each URL as a playlist item.
        const allUrls = oks.map((r) => r.url as string).join("\n");
        try {
          await navigator.clipboard.writeText(allUrls);
          setStatusMsg(
            errs.length > 0
              ? `${oks.length}/${channels.length} URLs copied — ${errs.length} channel(s) failed`
              : oks.length > 1
                ? `${oks.length} URLs copied to clipboard`
                : "URL copied to clipboard",
          );
        } catch {
          setStatusMsg(
            errs.length > 0
              ? `${oks.length}/${channels.length} URLs ready — ${errs.length} channel(s) failed`
              : oks.length > 1
                ? `${oks.length} URLs ready (clipboard blocked)`
                : "URL ready (clipboard blocked)",
          );
        }

        if (openInVlc) {
          // Navigate the pre-opened tabs in order. Close any leftover
          // tabs whose fetch failed (their channel produced no URL).
          for (let i = 0; i < channels.length; i++) {
            const tab = targetTabs[i];
            const res = responses[i];
            if (tab && res.ok && res.body.url) {
              tab.location.href = res.body.url;
            } else if (tab) {
              tab.close();
            }
          }
        }
      } catch (e) {
        setStatus("err");
        setStatusMsg(e instanceof Error ? e.message : String(e));
        for (const t of targetTabs) t?.close();
      }
    },
    [channels, source, startStr, endStr, nvr],
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

          {source === "nvr" && (
            <label className={styles.label}>
              NVR
              <select
                className={styles.select}
                value={nvr}
                onChange={(e) => setNvr(e.target.value as "amcrest" | "annke")}
                title="amcrest = Dahua /cam/playback + local wallclock; annke = Hikvision /Streaming/tracks + Pacific-as-fake-Z"
              >
                <option value="amcrest">Amcrest (.148)</option>
                <option value="annke">Annke (.130)</option>
              </select>
            </label>
          )}

          <label className={styles.label}>
            channels{" "}
            <span style={{ opacity: 0.6, fontSize: 11 }}>(Ctrl/Cmd + click for multi)</span>
            <select
              className={styles.select}
              multiple
              size={Math.min(8, source === "nvr" ? NVR_CHANNELS[nvr].length : CHANNELS.length)}
              value={channels.map(String)}
              onChange={(e) => {
                const picked = Array.from(e.target.selectedOptions, (o) =>
                  Number.parseInt(o.value, 10),
                );
                setChannels(picked);
              }}
            >
              {(source === "nvr" ? NVR_CHANNELS[nvr] : CHANNELS).map((c) => {
                const labels = source === "nvr" ? CHANNEL_LABEL_BY_NVR[nvr] : CHANNEL_LABEL;
                // Numeric `value` is fine here: React's <select multiple>
                // matches `value` entries against option.value via string
                // coercion ('$' + v) in ReactDOMSelect.updateOptions, so
                // [3] vs "3" is never the problem. If the highlight ever
                // "disappears" again, check option.selected in the DOM
                // first — last time it was a CSS cascade issue, not React
                // (see PlaybackUrlPage.module.css `.select option`).
                return (
                  <option key={c} value={c}>
                    {labels[c] ?? `channel ${c}`}
                  </option>
                );
              })}
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
            {status === "loading"
              ? "Building…"
              : channels.length > 1
                ? `▶ Open ${channels.length} in VLC`
                : "▶ Open in VLC"}
          </button>
          <button
            type="button"
            className={styles.btn}
            onClick={() => void build(false)}
            disabled={status === "loading"}
          >
            {channels.length > 1 ? `Copy ${channels.length} URLs` : "Copy URL"}
          </button>
        </div>

        {statusMsg && <div className={status === "err" ? styles.err : styles.ok}>{statusMsg}</div>}

        {results.length > 0 && (
          <div className={styles.urlBox}>
            {results.length > 1 && (
              <p className={styles.hint} style={{ marginTop: 0 }}>
                Browsers block multiple popups from a single click, and VLC may route later rtsp://
                launches into its existing instance as playlist items. Click each row to open its
                own VLC window (or in VLC → Tools → Preferences → Interface, uncheck "Allow only one
                instance"):
              </p>
            )}
            {results.map((r) => (
              <div
                key={r.url ?? `${r.channel}-${r.start ?? ""}`}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  marginBottom: 6,
                }}
              >
                <button
                  type="button"
                  className={`${styles.btn} ${styles.btnPrimary}`}
                  onClick={() => {
                    if (r.url) window.open(r.url, "_blank");
                  }}
                  title={`Open channel ${r.channel} in VLC`}
                >
                  ▶ ch{r.channel}
                </button>
                <code className={styles.url} style={{ flex: 1 }}>
                  {r.url}
                </code>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
