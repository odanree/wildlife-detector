@echo off
REM Wrapper for the mpv:// URL scheme. Strips the mpv:// prefix from %1,
REM re-adds the :// that Chrome's URL handler collapses when passing the
REM wrapped URL through, then hands the result to MPV Player.
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

REM `start ""` detaches the MPV window from the parent cmd so the shell
REM handler can exit immediately without waiting on MPV's playback.
start "" "%MPV_EXE%" "%URL%"
