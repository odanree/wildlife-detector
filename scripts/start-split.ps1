# Launch detector + web sidecar in split mode (Phase 2 of ADR 002).
#
# Both processes run in the current terminal -- Ctrl+C to stop both cleanly.
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

# Pin CWD to the project root regardless of where the user launched the script.
# `python -m src.*` requires the project root on sys.path.
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot

# Generate a shared bearer token for this run. Both processes read it from env.
# For a persistent deployment set INTERNAL_API_TOKEN in .env instead.
if (-not $env:INTERNAL_API_TOKEN) {
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $env:INTERNAL_API_TOKEN = [Convert]::ToBase64String($bytes)
    Write-Host "Generated ephemeral INTERNAL_API_TOKEN for this run" -ForegroundColor DarkGray
}

# Kill any stale detector / web_service / main processes from a previous
# aborted run -- closing the terminal doesn't always cascade-kill on Windows.
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

# Start detector as a background job.
# Note: renamed the scriptblock param from $args (PowerShell automatic variable) to $pyArgs.
Write-Host "Starting detector service..." -ForegroundColor Green
$detectorJob = Start-Job -Name "detector" -ArgumentList $detectorArgs, $env:INTERNAL_API_TOKEN -ScriptBlock {
    param($pyArgs, $token)
    Set-Location $using:PWD
    $env:INTERNAL_API_TOKEN = $token
    & python $pyArgs 2>&1
}

# Give detector ~3s to open its internal HTTP before launching the web sidecar
Start-Sleep -Seconds 3

# Start web sidecar in the foreground so Ctrl+C targets it first
Write-Host "Starting web sidecar (foreground) -- Ctrl+C to stop both..." -ForegroundColor Green
try {
    python -m src.web_service
}
finally {
    # When web sidecar exits, stop the detector too. Order matters on Windows:
    # Stop-Job only signals the PS pipeline — it does NOT kill the native
    # python.exe child of the job's runspace. If we call Receive-Job -Wait
    # before killing python.exe, the runspace never exits and we hang.
    # So: kill python.exe FIRST, then reap the job.
    Write-Host "Web sidecar exited -- stopping detector..." -ForegroundColor Yellow
    Get-WmiObject Win32_Process -Filter "name='python.exe'" |
        Where-Object { $_.CommandLine -match "src\.detector_service" } |
        ForEach-Object {
            Write-Host "  Killing detector pid $($_.ProcessId)" -ForegroundColor DarkGray
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    if ($detectorJob) {
        # Bounded wait — python is dead so the runspace exits fast; the -Timeout
        # is just a backstop in case a Stop-Job hook stalls.
        Stop-Job -Job $detectorJob -ErrorAction SilentlyContinue
        Wait-Job -Job $detectorJob -Timeout 5 -ErrorAction SilentlyContinue | Out-Null
        Remove-Job -Job $detectorJob -Force -ErrorAction SilentlyContinue
    }
    Write-Host "Split-mode shutdown complete." -ForegroundColor DarkGreen
}
