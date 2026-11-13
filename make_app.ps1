# Create a "Pocket Recordings" Start Menu shortcut for the web app (Windows;
# make_app.sh does the same on a Mac).
#
#   powershell -ExecutionPolicy Bypass -File make_app.ps1            # Start Menu
#   powershell -ExecutionPolicy Bypass -File make_app.ps1 -Desktop   # also on the desktop
#
# The shortcut runs launch.py with uvw (no console window): it starts
# `uv run app.py` in the background if it isn't already running and opens
# http://localhost:8765. Use the Quit button in the web page to stop it.
# Log: %LOCALAPPDATA%\Pocket Recordings\app.log. Run this again after moving
# this folder (the shortcut records its path).
param([switch]$Desktop)
$ErrorActionPreference = "Stop"

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    $uv = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\astral-sh.uv_*\uv.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $uv) { Write-Error "uv isn't installed. Run install.ps1 first."; exit 1 }
    $uvPath = $uv.FullName
} else { $uvPath = $uv.Source }
$uvw = Join-Path (Split-Path $uvPath) "uvw.exe"
if (-not (Test-Path $uvw)) { Write-Error "uvw.exe not found next to $uvPath"; exit 1 }

# Icon, drawn with Pillow (same design as the Mac app).
$IconDir = Join-Path $env:LOCALAPPDATA "Pocket Recordings"
New-Item -ItemType Directory -Force $IconDir | Out-Null
$Icon = Join-Path $IconDir "pocket.ico"
& $uvPath run --quiet (Join-Path $Here "tools\make_icon.py") $Icon | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Warning "Couldn't draw the icon; the shortcut gets uv's icon."; $Icon = $null }

$targets = @([Environment]::GetFolderPath("Programs"))
if ($Desktop) { $targets += [Environment]::GetFolderPath("Desktop") }
$shell = New-Object -ComObject WScript.Shell
foreach ($dir in $targets) {
    $lnk = $shell.CreateShortcut((Join-Path $dir "Pocket Recordings.lnk"))
    $lnk.TargetPath = $uvw
    $lnk.Arguments = "run --script `"$(Join-Path $Here 'launch.py')`""
    $lnk.WorkingDirectory = $Here
    $lnk.Description = "Browse, transcribe and summarize Pocket recordings"
    if ($Icon) { $lnk.IconLocation = "$Icon,0" }
    $lnk.Save()
    Write-Host "Created $($lnk.FullName)"
}
