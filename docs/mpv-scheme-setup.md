# Register `mpv://` URL scheme on Windows

The Frigate branch of the `/playback` picker emits `mpv://<http-url>` links
so clicking **Open in Frigate** launches MPV Player instead of playing the
clip inline in the browser tab. This is the same OS-handler-hands-off
pattern as the `rtsp://` flow the picker already uses for Amcrest / Annke.

Windows doesn't ship an `mpv://` scheme by default — need one-time setup.

## Prereqs

- MPV Player installed. Default install path assumed:
  `C:\Program Files\MPV Player\mpv.exe`
- If your install path differs, edit `mpv-scheme-open.bat` accordingly.

## Steps

### 1. Copy the wrapper batch to a stable location

Save `mpv-scheme-open.bat` (in this same directory) to somewhere permanent.
Suggested: `C:\Tools\mpv-scheme-open.bat`. The batch:

- receives the full `mpv://<http-url>` string as `%1`
- strips the `mpv://` prefix
- invokes MPV with the resulting plain http:// URL

### 2. Register the scheme

Open `mpv-scheme.reg` (in this same directory) and edit the `command` line
to match wherever you saved the batch, e.g.:

```
@="\"C:\\Tools\\mpv-scheme-open.bat\" \"%1\""
```

Then double-click the .reg file → confirm the UAC prompt. That's it — every
future `mpv://` link Windows sees will fire the batch, which fires MPV.

### 3. Verify

Open a browser to any page and click a link like:
`mpv://http://192.168.1.147:5000/api/plant_pathway/start/1789924397/end/1789924997/clip.mp4`

MPV Player should launch and start playback of the clip.

## Fallback if not registered

If the scheme isn't registered, Windows shows its usual "no app associated"
dialog. Copy URL still works — the clipboard gets the raw http:// URL
(stripped of the mpv:// prefix), which you can paste into MPV's Open URL
dialog (Ctrl+V) or MPV's command line.

## Uninstall

Delete the `mpv` key under `HKEY_CLASSES_ROOT` via `regedit`.
