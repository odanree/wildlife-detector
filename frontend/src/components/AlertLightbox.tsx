import {
  type MouseEvent as ReactMouseEvent,
  type WheelEvent as ReactWheelEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { AlertRow, LabelVerdict } from "../api/alerts";
import { fmtTs } from "../util/time";
import styles from "./AlertLightbox.module.css";
import { LabelPicker } from "./LabelPicker";
import { ReplayButton } from "./ReplayButton";

const RODENT_SPECIES = new Set(["rat", "mouse"]);
const ZOOM_MIN = 1;
const ZOOM_MAX = 8;
const ZOOM_STEP = 0.25;

interface AlertLightboxProps {
  /** All alerts currently visible in the table — the nav list. */
  items: AlertRow[];
  /** ID of the alert currently open, or null when closed. */
  openId: number | null;
  /** Setter — pass null to close, or an alert id to open. */
  setOpenId: (id: number | null) => void;
  /** Local overlay of just-written labels — mirror of AlertsPage state so
   *  the LabelPicker in this modal reflects labels applied via the table's
   *  own picker or the BulkLabelBar between polling ticks. */
  labelOverlay?: Map<number, { verdict: LabelVerdict; species: string | null }>;
  /** IDs currently mid-write — disables LabelPicker buttons for those alerts. */
  busyIds?: Set<number>;
  /** Fully-owned label writer: parent updates overlay optimistically,
   *  fires setAlertLabel, rolls back on error. The lightbox calls this
   *  for both mouse-click votes (via LabelPicker.onChange) and keyboard
   *  votes (Y/N/U handlers). */
  writeLabel?: (id: number, verdict: LabelVerdict, species: string | null) => Promise<void>;
}

/**
 * Full-viewport modal for eyeballing a snapshot at full resolution.
 *
 *  - **ID-based navigation, not index-based.** Anchoring on alert.id
 *    keeps the same crop in view across 5s polls (index-based would
 *    silently shift when a new alert lands at position 0).
 *  - **Keyboard as command dispatch.** One effect owns keydown,
 *    translates keys → intent → delegate.
 *  - **Wheel-to-zoom, click-drag-to-pan** on the snapshot img.
 *    Zoom is cursor-anchored (pixel under cursor stays put); pan
 *    resets when navigating to another snapshot or on double-click.
 *    Lightweight local-state implementation (no useZoom hook because
 *    the lightbox doesn't need per-camera localStorage persistence —
 *    each snapshot is a one-off view).
 *  - **Body scroll lock via cleanup**. Restored on unmount / close.
 */
export function AlertLightbox({
  items,
  openId,
  setOpenId,
  labelOverlay,
  busyIds,
  writeLabel,
}: AlertLightboxProps) {
  // Memoized so `go` + the keydown-effect deps stay stable across parent
  // re-renders (AlertsPage polls every 5s → `items` array reference
  // changes → without useMemo, `navList` was fresh every render, `go`
  // fresh every render, and the window keydown listener churned on each
  // tick. See issue #32.)
  const navList = useMemo(() => items.filter((a) => a.snapshot), [items]);
  const currentIdx = useMemo(
    () => (openId == null ? -1 : navList.findIndex((a) => a.id === openId)),
    [navList, openId],
  );
  const current = currentIdx >= 0 ? navList[currentIdx] : null;

  const close = useCallback(() => setOpenId(null), [setOpenId]);
  const go = useCallback(
    (dir: -1 | 1) => {
      if (currentIdx < 0) return;
      const next = currentIdx + dir;
      if (next < 0 || next >= navList.length) return;
      setOpenId(navList[next].id);
    },
    [currentIdx, navList, setOpenId],
  );

  // ── Zoom + pan state ─────────────────────────────────────────────
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const imgRef = useRef<HTMLImageElement>(null);
  const dragStartRef = useRef<{
    mouseX: number;
    mouseY: number;
    panX: number;
    panY: number;
  } | null>(null);

  // Reset zoom + pan when navigating to another snapshot (or closing).
  // Different image → new content → user shouldn't inherit prior crop.
  // biome-ignore lint/correctness/useExhaustiveDependencies: openId IS the fire trigger; body only calls setters
  useEffect(() => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
  }, [openId]);

  const resetZoom = useCallback(() => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
  }, []);

  // Cursor-anchored wheel zoom: keep the image pixel under the mouse
  // fixed as zoom changes. Same math as the live-preview useZoom hook
  // but self-contained (no CSS-var publishing, no localStorage).
  const onWheel = useCallback((e: ReactWheelEvent<HTMLImageElement>) => {
    e.preventDefault();
    const img = imgRef.current;
    if (!img) return;
    setZoom((oldZoom) => {
      const dir = e.deltaY < 0 ? 1 : -1;
      const newZoom = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, oldZoom + dir * ZOOM_STEP));
      if (newZoom === oldZoom) return oldZoom;
      const rect = img.getBoundingClientRect();
      const fracX = rect.width > 0 ? (e.clientX - rect.left) / rect.width : 0.5;
      const fracY = rect.height > 0 ? (e.clientY - rect.top) / rect.height : 0.5;
      const scale = newZoom / oldZoom;
      const newW = rect.width * scale;
      const newH = rect.height * scale;
      setPan((p) => ({
        x: p.x - fracX * (newW - rect.width),
        y: p.y - fracY * (newH - rect.height),
      }));
      return newZoom;
    });
  }, []);

  const onImgMouseDown = useCallback(
    (e: ReactMouseEvent<HTMLImageElement>) => {
      if (zoom <= 1) return; // no pan at rest scale
      e.preventDefault();
      dragStartRef.current = { mouseX: e.clientX, mouseY: e.clientY, panX: pan.x, panY: pan.y };
    },
    [zoom, pan.x, pan.y],
  );

  useEffect(() => {
    function onMove(e: MouseEvent) {
      const start = dragStartRef.current;
      if (!start) return;
      setPan({
        x: start.panX + (e.clientX - start.mouseX),
        y: start.panY + (e.clientY - start.mouseY),
      });
    }
    function onUp() {
      dragStartRef.current = null;
    }
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, []);

  useEffect(() => {
    if (!current) return;
    const originalOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    // Fast-vote: delegates to parent's writeLabel (single owner of state +
    // side effect — optimistic overlay update, async server write, rollback
    // on error). Auto-advance after dispatch so operator can label a
    // hundred rows in a few minutes without leaving the keyboard.
    //
    // Compound-shortcut variant: pass a species tag alongside the verdict
    // so a SINGLE keystroke applies BOTH — cuts species-tagging in the
    // labeling workflow from mouse-drag-per-row to one key. `null` species
    // preserves the original Y/N/U behavior (no fine-grained tag).
    const vote = (verdict: LabelVerdict, species: string | null = null) => {
      if (!current) return;
      const alertId = current.id;
      // Fire-and-forget — writeLabel handles the optimistic update, so
      // the LabelPicker in the CURRENT frame already re-renders via the
      // overlay before we advance. Guards against wrap-around.
      writeLabel?.(alertId, verdict, species).catch((e) => {
        console.error("keyboard vote failed:", e);
      });
      if (currentIdx < navList.length - 1) go(1);
    };
    // Compound species shortcuts — single-key correct/species combos, and
    // Shift+letter for FP subcategories. Chosen to avoid conflict with the
    // existing Y/N/U verdict keys AND with nav (arrows, Esc). Mnemonics:
    // R=rodent (most common TP), C=cat, D=dog, A=raccoon, S=squirrel,
    // B=bird, P=opossum, O=other. FP: I=insect, H=human, S=shadow (only
    // ambiguous with correct-squirrel — shift disambiguates), L=reflection
    // (R already taken by rodent), X=noise, O=other.
    //
    // Case-sensitive: species keys are LOWERCASE-only for correct combos;
    // Shift+key produces uppercase which routes to the incorrect combos.
    // The check runs after the verdict-only keys below so operator can't
    // accidentally trigger correct+rodent when they meant plain Y.
    const CORRECT_SPECIES_KEYS: Record<string, string> = {
      r: "real_rodent",
      c: "real_cat",
      d: "real_dog",
      a: "real_raccoon",
      s: "real_squirrel",
      b: "real_bird",
      p: "real_opossum",
      o: "real_other",
    };
    const INCORRECT_SPECIES_KEYS: Record<string, string> = {
      I: "FP:insect",
      H: "FP:human",
      S: "FP:shadow",
      L: "FP:reflection",
      X: "FP:noise",
      O: "FP:other",
    };
    function onKey(e: KeyboardEvent): void {
      // Skip when focus is on a form control — otherwise typing in a
      // textarea/select would trigger votes.
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")) {
        return;
      }
      // Compound Shift+letter first — takes precedence over lowercase
      // matches. Uses e.key (which is uppercase when shift is held) to
      // distinguish "S" (FP:shadow) from "s" (correct+real_squirrel).
      if (e.shiftKey) {
        const fp = INCORRECT_SPECIES_KEYS[e.key];
        if (fp) {
          e.preventDefault();
          vote("incorrect", fp);
          return;
        }
      } else {
        const tp = CORRECT_SPECIES_KEYS[e.key.toLowerCase()];
        if (tp && !e.ctrlKey && !e.metaKey && !e.altKey) {
          e.preventDefault();
          vote("correct", tp);
          return;
        }
      }
      switch (e.key) {
        case "Escape":
          close();
          break;
        case "ArrowLeft":
          go(-1);
          break;
        case "ArrowRight":
          go(1);
          break;
        case "0":
        case "Home":
          resetZoom();
          break;
        case "y":
        case "Y":
        case "1":
          vote("correct");
          break;
        case "n":
        case "N":
        case "2":
          vote("incorrect");
          break;
        case "u":
        case "U":
        case "3":
          vote("unclear");
          break;
      }
    }
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = originalOverflow;
    };
  }, [current, close, go, resetZoom, currentIdx, navList.length, writeLabel]);

  if (!current || !current.snapshot) return null;

  const isRodent = RODENT_SPECIES.has(current.species);
  const speciesCls = `${styles.species} ${isRodent ? styles.speciesRodent : styles.speciesOther}`;
  const confPct = current.confidence != null ? `${Math.round(current.confidence * 100)}%` : "—";
  const canPrev = currentIdx > 0;
  const canNext = currentIdx < navList.length - 1;

  return (
    // biome-ignore lint/a11y/useKeyWithClickEvents: primary close paths (× button, Esc) are keyboard-accessible; backdrop-click is a bonus
    <div
      className={styles.backdrop}
      // biome-ignore lint/a11y/useSemanticElements: native <dialog> fights React's declarative model
      role="dialog"
      aria-modal="true"
      aria-label="alert snapshot viewer"
      onClick={(e) => {
        if (e.target === e.currentTarget) close();
      }}
    >
      <button className={styles.closeBtn} onClick={close} aria-label="close" type="button">
        ×
      </button>
      <button
        className={`${styles.chev} ${styles.chevLeft}`}
        onClick={() => go(-1)}
        disabled={!canPrev}
        aria-label="previous"
        type="button"
      >
        ‹
      </button>
      <button
        className={`${styles.chev} ${styles.chevRight}`}
        onClick={() => go(1)}
        disabled={!canNext}
        aria-label="next"
        type="button"
      >
        ›
      </button>
      <div className={styles.inner}>
        <div className={styles.imgViewport}>
          <img
            ref={imgRef}
            className={styles.img}
            src={`/snapshots/${encodeURIComponent(current.snapshot)}`}
            alt="alert snapshot"
            onWheel={onWheel}
            onMouseDown={onImgMouseDown}
            onDoubleClick={resetZoom}
            style={{
              transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
              cursor: zoom > 1 ? (dragStartRef.current ? "grabbing" : "grab") : "zoom-in",
              transformOrigin: "top left",
              userSelect: "none",
            }}
            draggable={false}
          />
        </div>
        <div className={styles.meta}>
          {/* Preview strip — 3 alerts each side + current position.
              Purpose is rapid-vote pacing: operator glances at the
              strip to see when a camera transition (rooftop → yard
              etc.) is coming up and slows down before muscle-memory-
              voting through it on the wrong mental model. 3-slot
              window gives ~3-alert look-ahead lead time. Click any
              thumb to jump.

              Camera-change highlight uses **transition-boundary
              marking**: each slot compares to its NEIGHBOR (not to
              current), so only the actual transition point flashes
              amber instead of every alert after a transition. E.g.
              current=rooftop, next3=[rooftop, yard, yard] → only [+2]
              flashes; [+3] stays calm because it matches its own
              neighbor. Direct answer to "where should I slow down?" */}
          <div className={styles.previewStrip}>
            {[-3, -2, -1].map((offset) => {
              const i = currentIdx + offset;
              const alert = i >= 0 && i < navList.length ? navList[i] : null;
              const neighbor = i + 1 >= 0 && i + 1 < navList.length ? navList[i + 1] : current;
              return (
                <PreviewThumb
                  key={offset}
                  alert={alert}
                  label={`${offset}`}
                  cameraChangeFrom={neighbor?.camera_id}
                  onClick={
                    alert && offset === -1
                      ? () => go(-1)
                      : alert
                        ? () => setOpenId(alert.id)
                        : undefined
                  }
                />
              );
            })}
            <span className={styles.pos}>
              {currentIdx + 1} / {navList.length}
              {zoom > 1 && <span className={styles.zoomBadge}> · {zoom.toFixed(2)}×</span>}
            </span>
            {[1, 2, 3].map((offset) => {
              const i = currentIdx + offset;
              const alert = i >= 0 && i < navList.length ? navList[i] : null;
              const neighbor = i - 1 >= 0 && i - 1 < navList.length ? navList[i - 1] : current;
              return (
                <PreviewThumb
                  key={offset}
                  alert={alert}
                  label={`+${offset}`}
                  cameraChangeFrom={neighbor?.camera_id}
                  onClick={
                    alert && offset === 1
                      ? () => go(1)
                      : alert
                        ? () => setOpenId(alert.id)
                        : undefined
                  }
                />
              );
            })}
          </div>
          <div className={styles.metaRow}>
            <span>
              <span className={speciesCls}>{current.species || "?"}</span>{" "}
              {current.camera_id && <span className={styles.badgeCam}>{current.camera_id}</span>} ·{" "}
              {fmtTs(current.ts)} · conf {confPct} · track #{current.track_id ?? "—"}
            </span>
            <ReplayButton alertId={current.id} size="md" />
          </div>
          <div className={styles.metaRow}>
            {(() => {
              // Prefer overlay (freshly-written) over the row's server-side
              // label — same pattern as the table view. Both sources of
              // truth are parent-owned; LabelPicker is fully controlled so
              // there's no local state to sync when navigation changes the
              // current alert or when the overlay updates from another
              // click (table row, bulk bar, keyboard vote).
              const ov = labelOverlay?.get(current.id);
              const effVerdict: LabelVerdict = ov ? ov.verdict : (current.label_verdict ?? null);
              const effSpecies = ov ? ov.species : (current.label_species ?? null);
              return (
                <LabelPicker
                  verdict={effVerdict}
                  species={effSpecies}
                  busy={busyIds?.has(current.id) ?? false}
                  onChange={(v, s) => writeLabel?.(current.id, v, s)}
                  showSpeciesDefault={true}
                />
              );
            })()}
          </div>
          <div className={styles.desc}>{current.description ?? ""}</div>
          <div className={styles.hintLine}>
            keys: Y correct · N incorrect · U unclear · ← / → nav · Esc close
            {zoom > 1 && ` · zoom ${zoom.toFixed(2)}× (double-click or "0" to reset)`}
          </div>
          <div className={styles.hintLine}>
            species: R rodent · C cat · D dog · A raccoon · S squirrel · B bird · P opossum · O other
          </div>
          <div className={styles.hintLine}>
            FP tag (shift+): I insect · H human · S shadow · L reflection · X noise · O other
          </div>
        </div>
      </div>
    </div>
  );
}

/** Small thumbnail with camera badge + timestamp used in the prev/next
 *  preview strip. Highlights when the adjacent alert is on a DIFFERENT
 *  camera than the current one — that's the moment operator should
 *  slow down (mental model of "what a real detection looks like"
 *  differs between cameras). Null-safe: renders a placeholder when
 *  there's no adjacent alert (start/end of list). */
function PreviewThumb({
  alert,
  label,
  cameraChangeFrom,
  onClick,
}: {
  alert: AlertRow | null;
  /** Free-form offset label — "prev"/"next" or "-3"/"+2" etc. Shown in
   *  the corner so operator can tell which slot is where at a glance. */
  label: string;
  /** camera_id to compare THIS slot's alert against. Different →
   *  amber transition-boundary highlight. In the multi-slot strip,
   *  this is the neighbor (n±1), not `current`, so only the actual
   *  transition point flashes rather than every alert after it. */
  cameraChangeFrom: string | undefined;
  onClick?: () => void;
}): JSX.Element {
  if (!alert || !alert.snapshot) {
    return (
      <div className={`${styles.previewSlot} ${styles.previewSlotEmpty}`}>
        <div className={styles.previewLabel}>{label}</div>
        <div className={styles.previewPlaceholder}>—</div>
      </div>
    );
  }
  const cameraChanged = cameraChangeFrom && alert.camera_id !== cameraChangeFrom;
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={!onClick}
      className={`${styles.previewSlot} ${cameraChanged ? styles.previewSlotCameraChange : ""}`}
      title={
        cameraChanged
          ? `${label}: ${alert.camera_id} (camera change!)`
          : `${label}: ${alert.camera_id}`
      }
    >
      <div className={styles.previewLabel}>
        {label}
        {alert.camera_id && (
          <span className={cameraChanged ? styles.previewCamChange : styles.previewCam}>
            {alert.camera_id}
          </span>
        )}
      </div>
      <img
        className={styles.previewThumb}
        src={`/snapshots/${encodeURIComponent(alert.snapshot)}`}
        alt={`${label} snapshot`}
        loading="lazy"
      />
    </button>
  );
}
