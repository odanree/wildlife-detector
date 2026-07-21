import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { StatusBar } from "../components/StatusBar";
import { useCameras } from "../hooks/useCameras";
import { useStatus } from "../hooks/useStatus";
import { useZoom } from "../hooks/useZoom";
import styles from "./LivePreviewPage.module.css";

/**
 * Live streaming preview — first of the C-phase migration.
 *
 * Scope for this PR: read-only viewer with camera dropdown, StatusBar,
 * cursor-anchored zoom, and snapshot download. NO editors, NO baseline
 * capture buttons, NO secondary pane. Those land in PRs 12 + 13.
 *
 * Streams MJPEG from Flask's /stream endpoint (multipart/x-mixed-
 * replace) by pointing an <img src> at it — browsers handle the
 * boundary parsing natively; no client-side codec needed. Cache-bust
 * key on the src URL forces a fresh stream connection when the
 * camera changes (browsers otherwise cling to the previous stream).
 *
 * Pattern: strangler-fig continuation. Serves at /react/preview
 * alongside the untouched vanilla-JS /. Cutover after PRs 12 + 13
 * ship the editors.
 */
export function LivePreviewPage() {
  const { data: camerasData } = useCameras();
  const cameras = camerasData?.cameras ?? [];
  const defaultCam = camerasData?.default ?? "";
  const [searchParams, setSearchParams] = useSearchParams();
  const camera = searchParams.get("camera") ?? defaultCam;

  const { data: status } = useStatus(camera || undefined);
  const detW = status?.detection_size?.[0] ?? 1280;
  const detH = status?.detection_size?.[1] ?? 720;

  const { zoom, adjustBy, setZoomTo, onWheel } = useZoom(camera, {
    storageKey: "livePreviewZoom",
    min: 0.25,
    max: 3.0,
    step: 0.1,
    baseW: detW,
    baseH: detH,
  });

  const [streamError, setStreamError] = useState(false);

  // Cache-bust key changes when the camera changes so the browser
  // opens a fresh MJPEG connection instead of reusing the previous
  // stream (which would otherwise show frames from the WRONG camera).
  const [streamKey, setStreamKey] = useState(0);
  const currentSrc = camera ? `/stream?camera=${encodeURIComponent(camera)}&t=${streamKey}` : "";

  return (
    <div className={styles.wrap}>
      <header className={styles.header}>
        <Link to="/" className={styles.title}>
          wildlife-detector — live
        </Link>
        <span className={styles.spacer} />
        <select
          className={styles.select}
          value={camera}
          onChange={(e) => {
            setSearchParams({ camera: e.target.value });
            setStreamKey((k) => k + 1);
            setStreamError(false);
          }}
        >
          {cameras.length === 0 && <option value="">(loading)</option>}
          {cameras.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
        {camera && (
          <a
            className={styles.linkBtn}
            href={`/snapshot?camera=${encodeURIComponent(camera)}`}
            download={`${camera}-snapshot.jpg`}
            title="Download the current annotated frame as JPEG"
          >
            Snapshot
          </a>
        )}
        <Link to="/alerts" className={styles.linkBtn}>
          Alerts →
        </Link>
        <Link to="/baselines" className={styles.linkBtn}>
          Baselines →
        </Link>
        <Link to="/status" className={styles.linkBtn}>
          Dashboard →
        </Link>
      </header>

      {camera && (
        <div className={styles.toolbar}>
          <StatusBar camera={camera} />
          <div className={styles.zoomBtns}>
            <button type="button" onClick={() => adjustBy(-0.1)} title="Zoom out">
              −
            </button>
            <span className={styles.zoomVal}>{zoom.toFixed(2)}×</span>
            <button type="button" onClick={() => adjustBy(0.1)} title="Zoom in">
              +
            </button>
            <button type="button" onClick={() => setZoomTo(1.0)} title="Reset zoom to 1×">
              1×
            </button>
          </div>
        </div>
      )}

      {!camera ? (
        <div className={styles.empty}>Waiting for camera roster…</div>
      ) : (
        <div id="live-scroll-host" className={styles.scrollHost}>
          {streamError ? (
            <div className={styles.empty}>
              Stream unavailable. Detector may still be starting up — retry in a few seconds.
              <div style={{ marginTop: 12 }}>
                <button
                  type="button"
                  className={styles.linkBtn}
                  onClick={() => {
                    setStreamKey((k) => k + 1);
                    setStreamError(false);
                  }}
                >
                  Retry
                </button>
              </div>
            </div>
          ) : (
            <img
              key={streamKey}
              className={styles.stream}
              src={currentSrc}
              alt={`live stream ${camera}`}
              width={Math.round(detW * zoom)}
              height={Math.round(detH * zoom)}
              onWheel={onWheel}
              onError={() => setStreamError(true)}
            />
          )}
        </div>
      )}
    </div>
  );
}
