import { useState } from "react";
import type { SlewPresetEditorApi } from "../hooks/useSlewPresetEditor";
import styles from "./SlewPresetPanel.module.css";

interface SlewPresetPanelProps {
  editor: SlewPresetEditorApi;
  /** Called when the operator enters edit mode — parent should cancel
   *  zone + mask editors to maintain the mutual-exclusion invariant. */
  onEnterEdit: () => void;
}

/**
 * Compact toolbar-adjacent panel for the slew preset editor. Lists
 * every preset in the current working set + provides:
 *
 *   - "Go here" per preset — fires GotoPreset via /api/ptz/preset so
 *     the operator can align the polygon with what the preset views.
 *   - "Edit" per preset — enters draw/tweak mode on that preset;
 *     ZoneOverlay in the primary CameraPane picks up the polygon.
 *   - "+ Add preset" — new-preset flow. Operator picks a preset
 *     number (matching what's saved on the camera), gives it a name,
 *     draws the polygon.
 *   - "Save all" / "Cancel" — flushes the working list to /api/slew/
 *     presets. Save-all because the backend replaces the list
 *     wholesale.
 *
 * The list is scrollable when N presets exceeds the panel height so
 * cameras with 10+ presets don't blow out the toolbar row.
 */
export function SlewPresetPanel({ editor, onEnterEdit }: SlewPresetPanelProps) {
  const [newPreset, setNewPreset] = useState("");
  const [newName, setNewName] = useState("");
  const editing = editor.mode !== "idle";

  const startAdd = () => {
    const preset = Number.parseInt(newPreset, 10);
    if (!Number.isFinite(preset) || preset < 1 || preset > 255) return;
    if (!newName.trim()) return;
    onEnterEdit();
    editor.enterDraw(preset, newName.trim());
    setNewPreset("");
    setNewName("");
  };

  return (
    <div className={styles.wrap}>
      <div className={styles.header}>
        <span className={styles.title}>Slew presets</span>
        {editor.enabled ? (
          <span className={styles.badgeOn} title="Self-slew is enabled in detection.yaml">
            enabled
          </span>
        ) : (
          <span
            className={styles.badgeOff}
            title="Self-slew disabled — flip enabled=true in yaml to activate"
          >
            disabled
          </span>
        )}
        <span className={styles.spacer} />
        <button
          type="button"
          className={styles.smallBtn}
          onClick={() => editor.goto(editor.homePreset)}
          title="Snap camera back to home preset (does not change any polygon)"
        >
          ⌂ home
        </button>
        <span className={styles.metaBit}>P{editor.homePreset}</span>
        <span className={styles.metaBit}>PTZ cam: {editor.ptzCameraId}</span>
      </div>

      {editor.workingPresets.length === 0 && !editing && (
        <div className={styles.empty}>
          No presets configured. Add one below — you'll draw its zone polygon on the preview.
        </div>
      )}

      <ul className={styles.list}>
        {editor.workingPresets
          .slice()
          .sort((a, b) => a.preset - b.preset)
          .map((p) => {
            const isActive = editor.activePreset === p.preset;
            return (
              <li key={p.preset} className={isActive ? styles.rowActive : styles.row}>
                <span className={styles.preset}>P{p.preset}</span>
                <span className={styles.name} title={`${p.polygon.length} vertex polygon`}>
                  {p.name}
                </span>
                <span className={styles.vertexCount}>{p.polygon.length}v</span>
                <span className={styles.actionSpacer} />
                <button
                  type="button"
                  className={styles.smallBtn}
                  onClick={() => editor.goto(p.preset)}
                  title="Fire GotoPreset on the PTZ camera"
                >
                  go here
                </button>
                <button
                  type="button"
                  className={styles.smallBtn}
                  onClick={() => {
                    onEnterEdit();
                    editor.enterTweak(p.preset);
                  }}
                  disabled={editing && !isActive}
                  title="Move vertices on the preview"
                >
                  edit
                </button>
                {isActive && (
                  <button
                    type="button"
                    className={styles.smallBtnDanger}
                    onClick={editor.removeActive}
                    title="Remove this preset from the working list (needs Save to persist)"
                  >
                    del
                  </button>
                )}
              </li>
            );
          })}
      </ul>

      {!editing && (
        <div className={styles.addRow}>
          <input
            className={styles.numInput}
            type="number"
            min="1"
            max="255"
            placeholder="preset #"
            value={newPreset}
            onChange={(e) => setNewPreset(e.target.value)}
          />
          <input
            className={styles.nameInput}
            type="text"
            placeholder="name (gate_corner, feeder, ...)"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") startAdd();
            }}
          />
          <button
            type="button"
            className={styles.addBtn}
            onClick={startAdd}
            disabled={!newPreset || !newName.trim()}
          >
            + add + draw
          </button>
        </div>
      )}

      {editing && editor.activePreset !== null && (
        <div className={styles.editingRow}>
          <span className={styles.editingLabel}>
            editing P{editor.activePreset}:{" "}
            {editor.workingPresets.find((p) => p.preset === editor.activePreset)?.name}
          </span>
          {editor.mode === "draw" && (
            <span className={styles.hint}>
              click preview to add vertices · click first vertex to close (need 3+)
            </span>
          )}
          {editor.mode === "tweak" && (
            <span className={styles.hint}>
              drag vertices to move · right-click to remove · click edges to insert
            </span>
          )}
          <span className={styles.spacer} />
          <button
            type="button"
            className={styles.smallBtn}
            onClick={editor.closeDrawing}
            disabled={editor.mode === "tweak"}
          >
            close draw
          </button>
          <button
            type="button"
            className={styles.saveBtn}
            onClick={editor.save}
            disabled={editor.saving || !editor.isSimple}
            title={!editor.isSimple ? "self-intersecting polygon" : "Save all presets"}
          >
            {editor.saving ? "saving…" : "save all"}
          </button>
          <button type="button" className={styles.smallBtn} onClick={editor.cancel}>
            cancel
          </button>
        </div>
      )}

      {editor.saveErr && <div className={styles.err}>{editor.saveErr}</div>}
    </div>
  );
}
