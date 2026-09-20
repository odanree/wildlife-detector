@echo off
REM Wrapper for the mpv:// URL scheme. Strips the mpv:// prefix from %1,
REM re-adds the :// that Chrome's URL handler collapses when passing the
REM wrapped URL through, infers a source-label from the URL pattern, and
REM hands the result to MPV Player with a --title that surfaces where
REM the clip came from (LOCAL / FRIGATE / NVR).
REM
REM Registered from mpv-scheme.reg - see mpv-scheme-setup.md.
REM If MPV lives somewhere other than the default path, edit MPV_EXE below.

set "MPV_EXE=C:\Program Files\MPV Player\mpv.exe"

set "URL=%~1"
set "URL=%URL:mpv://=%"
REM Chrome collapses the wrapped scheme's :// to // when passing through
REM the outer mpv:// handler. Re-add for the schemes we actually emit.
set "URL=%URL:http//=http://%"
set "URL=%URL:https//=https://%"
set "URL=%URL:rtsp//=rtsp://%"

REM Infer source label from URL pattern so operators can see in the MPV
REM window title (visible in the taskbar / Alt+Tab) which pipeline the
REM clip came out of. Cheap forensic breadcrumb — no server round-trip.
set "SRC=UNKNOWN"
echo %URL% | findstr /R "^rtsp://" >nul && set "SRC=NVR"
echo %URL% | findstr /C:"/clips/" >nul && set "SRC=LOCAL"
echo %URL% | findstr /R "/api/[a-z_]*/start/[0-9]*/end/[0-9]*/clip\.mp4" >nul && set "SRC=FRIGATE"

REM `start ""` detaches the MPV window from the parent cmd so the shell
REM handler can exit immediately without waiting on MPV's playback.
start "" "%MPV_EXE%" --title="wildlife %SRC%: %URL%" "%URL%"
