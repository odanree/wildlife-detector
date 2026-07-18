# Launch detector + web sidecar in split mode (Phase 2 of ADR 002).
#
# Both processes run in the current terminal — Ctrl+C to stop both cleanly.
# For a background service, wrap each in Start-Job or convert to a Windows service.
#
# Usage:
#     .\scripts\start-split.ps1                    # normal launch
#     .\scripts\start-split.ps1 -Video path\to.mp4 # replay a file instead
#
# Compare to all-in-one:
#     python -m src.main    # both in one process

param(
    [string]$Video = "",
    [string]$Rtsp = ""
)

$ErrorActionPreference = "Stop"

# Generate a shared bearer token for this run. Both processes read it from env.
# For a persistent deployment set INTERNAL_API_TOKEN in .env instead.
if (-not $env:INTERNAL_API_TOKEN) {
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $env:INTERNAL_API_TOKEN = [Convert]::ToBase64String($bytes)
    Write-Host "Generated ephemeral INTERNAL_API_TOKEN for this run" -ForegroundColor DarkGray
}

# Kill any stale wildlife-detector / web_service processes from a previous
# aborted run — closing the terminal doesn't always cascade-kill on Windows.
Get-WmiObject Win32_Process -Filter "name='python.exe'" |
    Where-Object { $_.CommandLine -match "src\.(detector_service|web_service|main)" } |
    ForEach-Object {
        Write-Host "Killing stale process $($_.ProcessId): $($_.CommandLine)" -ForegroundColor DarkYellow
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

# Build detector arg list
$detectorArgs = @("-m", "src.detector_service")
if ($Video) { $detectorArgs += @("--video", $Video) }
if ($Rtsp)  { $detectorArgs += @("--rtsp", $Rtsp) }

# Start detector as a background job
Write-Host "Starting detector service..." -ForegroundColor Green
$detectorJob = Start-Job -Name "detector" -ArgumentList $detectorArgs, $env:INTERNAL_API_TOKEN -ScriptBlock {
    param($args, $token)
    Set-Location $using:PWD
    $env:INTERNAL_API_TOKEN = $token
    & python $args 2>&1
}

# Give detector ~3s to open its internal HTTP before launching the web sidecar
Start-Sleep -Seconds 3

# Start web sidecar in the foreground so Ctrl+C targets it first
Write-Host "Starting web sidecar (foreground) — Ctrl+C to stop both..." -ForegroundColor Green
try {
    python -m src.web_service
}
finally {
    # When web sidecar exits, stop the detector job too
    Write-Host "Web sidecar exited — stopping detector..." -ForegroundColor Yellow
    if ($detectorJob) {
        Stop-Job -Job $detectorJob -ErrorAction SilentlyContinue
        Receive-Job -Job $detectorJob -Wait -AutoRemoveJob -ErrorAction SilentlyContinue
    }
    # Belt-and-suspenders: kill any stragglers
    Get-WmiObject Win32_Process -Filter "name='python.exe'" |
        Where-Object { $_.CommandLine -match "src\.detector_service" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host "Split-mode shutdown complete." -ForegroundColor DarkGreen
}
