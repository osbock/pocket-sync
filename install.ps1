# Set up pocket-sync on a Windows PC (or check an existing setup).
# install.sh does the same on a Mac.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1                # set up
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Check         # report only
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Import DIR    # set up, copying settings
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Export DIR    # copy settings into DIR
#
# Settings are the recorder's session key (config.json), named voices
# (voices.json) and the app's model choice (app.json), all in
# ~\.config\pocket-sync. The files are the same on a Mac and a PC, so
# `./install.sh --export DIR` on a Mac and `-Import DIR` here works. They're
# private: move them by USB stick, not email or a synced cloud folder.
#
# Safe to run again; steps that are already done are skipped.
param([switch]$Check, [string]$Import = "", [string]$Export = "", [switch]$Help)

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Conf = Join-Path $HOME ".config\pocket-sync"
$Settings = @("config.json", "voices.json", "app.json")
$Diarizer = "pyannote/speaker-diarization-community-1"
$WhisperRepo = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
$Lms = Join-Path $HOME ".lmstudio\bin\lms.exe"
$script:Warn = 0; $script:Fail = 0
$Mode = if ($Check) { "check" } elseif ($Export) { "export" } else { "install" }

if ($Help) { Get-Content $MyInvocation.MyCommand.Path | Select-Object -Skip 1 -First 14 | ForEach-Object { $_ -replace '^# ?', '' }; exit 0 }

function Ok($m)   { Write-Host "  [ok] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow; $script:Warn++ }
function Bad($m)  { Write-Host "  [x]  $m" -ForegroundColor Red; $script:Fail++ }
function Note($m) { Write-Host "       $m" }
function Step($m) { Write-Host ""; Write-Host $m -ForegroundColor White }
function Ask($q)  { if ($Mode -ne "install") { return $false }; $a = Read-Host "       $q [Y/n]"; return -not ($a -match '^[Nn]') }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Find-Uv {
    $c = Get-Command uv -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $p = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\astral-sh.uv_*\uv.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($p) { return $p.FullName }
    return $null
}

# --- export -----------------------------------------------------------------
if ($Mode -eq "export") {
    New-Item -ItemType Directory -Force $Export | Out-Null
    $n = 0
    foreach ($f in $Settings) {
        $src = Join-Path $Conf $f
        if (Test-Path $src) { Copy-Item $src (Join-Path $Export $f) -Force; Write-Host "Copied $f"; $n++ }
    }
    if ($n -eq 0) { Write-Error "Nothing to export: no settings in $Conf."; exit 1 }
    Write-Host ""
    Write-Host "Exported $n file(s) to $Export. They include your recorder's session key"
    Write-Host "and voiceprints: carry them over by USB, then on the other computer run"
    Write-Host "  install.ps1 -Import <that folder>   (or ./install.sh --import on a Mac)"
    Write-Host "and delete the copy afterwards."
    exit 0
}

if ($Mode -eq "check") {
    if ($Import) { Write-Error "-Check changes nothing, so it can't be combined with -Import."; exit 2 }
    Write-Host "Checking only; nothing will be changed."
}

# --- PC ---------------------------------------------------------------------
Step "PC"
$os = Get-CimInstance Win32_OperatingSystem
if ([int]$os.BuildNumber -ge 17763) { Ok "$($os.Caption) (build $($os.BuildNumber))" }
else { Bad "Windows 10 1809 or later is needed for Bluetooth LE (this is build $($os.BuildNumber))" }
$mem = [math]::Round($os.TotalVisibleMemorySize / 1MB)
if ($mem -ge 32) { Ok "$mem GB memory" } else { Warn "$mem GB memory: pick a smaller summary model (around 8B)" }
$sysDrive = Get-PSDrive ($env:SystemDrive.TrimEnd(':'))
$free = [math]::Round($sysDrive.Free / 1GB)
if ($free -ge 30) { Ok "$free GB free disk" } else { Warn "$free GB free disk: plan for about 6 GB for the tools plus 5-25 GB per summary model" }
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
    $gpu = (& nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits | Select-Object -First 1)
    $name, $vram = $gpu -split ',\s*'
    $vramGb = [math]::Round([int]$vram / 1024)
    if ($vramGb -ge 8) { Ok "$name, $vramGb GB VRAM (Whisper and speakers run on the GPU)" }
    else { Warn "$name, $vramGb GB VRAM: Whisper may run out of GPU memory; it falls back to the CPU" }
    if ($vramGb -lt 20) { Note "Summaries: a 35B model won't fit in $vramGb GB and runs partly on the CPU (slow); an ~8B model is a better fit." }
} else {
    Warn "No NVIDIA GPU found: transcription and speakers run on the CPU (several minutes per recording)"
}

# --- uv ---------------------------------------------------------------------
Step "uv (Python runner)"
$Uv = Find-Uv
if ($Uv) { Ok "uv $((& $Uv --version) -replace '^uv\s+(\S+).*', '$1')" }
else {
    Bad "uv is missing"
    if (Ask "Install uv with winget?") {
        winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements
        Refresh-Path; $Uv = Find-Uv
        if ($Uv) { Ok "uv installed"; $script:Fail-- }
    }
}
function Hf { & $Uv tool run --quiet --from huggingface_hub hf @args }

# --- Settings ---------------------------------------------------------------
Step "Settings (~\.config\pocket-sync)"
if ($Import) {
    if (-not (Test-Path $Import -PathType Container)) { Bad "No such folder: $Import"; exit 1 }
    New-Item -ItemType Directory -Force $Conf | Out-Null
    foreach ($f in $Settings) {
        $src = Join-Path $Import $f; $dst = Join-Path $Conf $f
        if (-not (Test-Path $src)) { continue }
        if ((Test-Path $dst) -and ((Get-FileHash $src).Hash -ne (Get-FileHash $dst).Hash)) {
            $bak = "$dst.before-import.$(Get-Date -Format yyyyMMddHHmmss)"
            Copy-Item $dst $bak
            Note "Kept the existing $f as $(Split-Path -Leaf $bak)"
        }
        Copy-Item $src $dst -Force; Ok "Imported $f"
    }
}
$cfgFile = Join-Path $Conf "config.json"
if ((Test-Path $cfgFile) -and (Select-String -Quiet '"session_key"' $cfgFile)) { Ok "Session key saved" }
else {
    Warn "No session key yet. Copy it from your Mac (./install.sh --export DIR there, then"
    Note "-Import DIR here) or find it again as the README describes (adb works on Windows:"
    Note "winget install Google.PlatformTools), then: uv run pocket_sync.py setkey <16 characters>"
}
$voicesFile = Join-Path $Conf "voices.json"
if (Test-Path $voicesFile) {
    $n = (Get-Content -Raw -Encoding UTF8 $voicesFile | ConvertFrom-Json).PSObject.Properties.Name.Count
    Ok "Named voices: $n"
} else { Note "(No named voices yet; they're created as you name speakers.)" }

# --- LLM server -------------------------------------------------------------
Step "LM Studio (summaries and titles)"
function Get-Json($url) { try { Invoke-RestMethod -TimeoutSec 2 $url } catch { $null } }
if (Test-Path $Lms) {
    Ok "LM Studio installed"
    $m = Get-Json "http://localhost:1234/api/v0/models"
    if ($m) {
        $n = @($m.data | Where-Object { $_.type -in @("llm", "vlm") }).Count
        if ($n -gt 0) { Ok "Server running, $n chat model(s) downloaded" }
        else { Warn "Server running but no chat model downloaded; get one in LM Studio's Discover tab (GGUF)" }
    } else {
        Warn "LM Studio's server isn't running. In LM Studio: Developer tab > Start Server,"
        Note "and in Settings turn on starting the server at login. Without it you get"
        Note "transcripts but no summaries or titles."
    }
} else {
    $llmUrl = $null
    $appCfg = Join-Path $Conf "app.json"
    if (Test-Path $appCfg) { $llmUrl = (Get-Content -Raw -Encoding UTF8 $appCfg | ConvertFrom-Json).llm_url }
    if ($llmUrl -and (Get-Json "$llmUrl/v1/models")) { Ok "Using the LLM server at $llmUrl" }
    else {
        Warn "LM Studio isn't installed (needed for summaries and titles)"
        Note "Ollama or llama.cpp work too; see README, setup step 5."
        if (Ask "Install LM Studio with winget?") {
            winget install --id ElementLabs.LMStudio -e --accept-source-agreements --accept-package-agreements
            Note "Open LM Studio once, download a GGUF chat model, and start its server."
        }
    }
}

# --- Whisper ----------------------------------------------------------------
Step "Whisper model (transcription)"
$hfHub = if ($env:HF_HOME) { Join-Path $env:HF_HOME "hub" } else { Join-Path $HOME ".cache\huggingface\hub" }
$whisperDir = Join-Path $hfHub ("models--" + ($WhisperRepo -replace '/', '--'))
if (Test-Path (Join-Path $whisperDir "snapshots\*\model.bin")) { Ok "faster-whisper large-v3-turbo is downloaded" }
elseif (-not $Uv) { Warn "Skipped (needs uv)" }
else {
    Warn "faster-whisper large-v3-turbo isn't downloaded yet (about 1.6 GB; also fetched on first use)"
    if (Ask "Download it now?") { Hf download $WhisperRepo | Out-Null; if ($LASTEXITCODE -eq 0) { Ok "Downloaded"; $script:Warn-- } }
}

# --- Hugging Face / pyannote ------------------------------------------------
Step "Speaker recognition (pyannote, via Hugging Face)"
if ($Uv) {
    $who = (Hf auth whoami 2>$null | Select-Object -First 1)
    if ($LASTEXITCODE -eq 0 -and $who -and $who -notmatch "not logged") {
        Ok "Hugging Face login: $(($who -replace '^user[=:]\s*', ''))"
    } else {
        Warn "Not logged in to Hugging Face. Speaker labels are skipped until you are."
        if (Ask "Log in now? (paste a read token from huggingface.co/settings/tokens)") {
            Hf auth login; if ($LASTEXITCODE -eq 0) { $script:Warn-- }
        }
    }
    $probe = Join-Path $env:TEMP "pocket-probe-$PID"
    Hf download $Diarizer config.yaml --local-dir $probe 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Ok "Access to $Diarizer" }
    else {
        Warn "No access to $Diarizer. Open https://huggingface.co/$Diarizer"
        Note "while logged in and accept the terms (once per account), then run this again."
    }
    Remove-Item -Recurse -Force $probe -ErrorAction SilentlyContinue
} else { Warn "Skipped (needs uv)" }

# --- Python packages --------------------------------------------------------
Step "Python packages"
if ($Uv) {
    if ($Mode -eq "check") { Note "(Installed on first use; run without -Check to install them now.)" }
    else {
        Note "Installing (several minutes the first time; about 5 GB with CUDA PyTorch)..."
        Push-Location $Here
        foreach ($s in @("pocket_sync.py", "transcribe.py", "app.py")) {
            & $Uv run --quiet $s --help *> $null
            if ($LASTEXITCODE -eq 0) { Ok $s } else { Bad "${s}: packages failed to install. Try: cd $Here; uv run $s --help" }
        }
        $dev = & $Uv run --quiet transcribe.py --check-gpu 2>$null | Select-Object -Last 1
        if ($dev -match "^cuda") { Ok "PyTorch sees the GPU ($dev)" }
        elseif ($smi) { Warn "PyTorch doesn't see the GPU; speakers will be found on the CPU. Update the NVIDIA driver." }
        Pop-Location
    }
}

# --- Recorder ---------------------------------------------------------------
Step "Recorder (Bluetooth)"
if ((Test-Path $cfgFile) -and (Select-String -Quiet '"address"' $cfgFile)) {
    Ok "A recorder address is saved (it's different on every computer; Sync finds it again if needed)"
}
$bt = Get-PnpDevice -Class Bluetooth -Status OK -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $bt) { Warn "No working Bluetooth adapter found" }
if ($Uv -and $Mode -eq "install") {
    Note "Close the Pocket phone app (only one Bluetooth connection at a time) and keep"
    Note "the recorder nearby. Don't pair it in Windows Settings; this program connects directly."
    if (Ask "Scan for the recorder now?") {
        Push-Location $Here; & $Uv run pocket_sync.py scan; Pop-Location
        if ($LASTEXITCODE -ne 0) { Warn "Recorder not found; try again later with: uv run pocket_sync.py scan" }
    }
}

# --- Launcher ---------------------------------------------------------------
Step "Start Menu shortcut"
$lnkPath = Join-Path ([Environment]::GetFolderPath("Programs")) "Pocket Recordings.lnk"
$lnkOk = $false
if (Test-Path $lnkPath) {
    $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut($lnkPath)
    $lnkOk = $lnk.WorkingDirectory -eq $Here
}
if ($lnkOk) { Ok "Pocket Recordings shortcut points at this folder" }
elseif ($Mode -eq "install" -and (Ask "Create the Pocket Recordings Start Menu shortcut?")) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Here "make_app.ps1")
} else { Warn "Shortcut not created (or made for another folder). Run make_app.ps1" }

# --- Claude skill -----------------------------------------------------------
Step "Claude Code skill (/pocket-inbox)"
$skillSrc = Join-Path $Here ".claude\skills\pocket-inbox"
$skillDst = Join-Path $HOME ".claude\skills\pocket-inbox"
$item = Get-Item $skillDst -ErrorAction SilentlyContinue
if ($item -and $item.LinkType -and (($item.Target | Select-Object -First 1) -eq $skillSrc)) { Ok "/pocket-inbox is available in every folder" }
elseif ($item -and -not $item.LinkType) { Warn "$skillDst exists and isn't a link; leaving it alone" }
elseif ($Mode -eq "install") {
    New-Item -ItemType Directory -Force (Split-Path $skillDst) | Out-Null
    if ($item) { $item.Delete() }
    # A junction needs no admin rights, unlike a symbolic link.
    New-Item -ItemType Junction -Path $skillDst -Target $skillSrc | Out-Null
    Ok "Linked /pocket-inbox into ~\.claude\skills"
} else { Warn "/pocket-inbox only works inside this folder. Run install.ps1 to link it globally" }

# --- Summary ----------------------------------------------------------------
Write-Host ""
if ($script:Fail -gt 0) { Write-Host "$($script:Fail) problem(s) and $($script:Warn) warning(s). Fix the [x] items and run this again." -ForegroundColor Red; exit 1 }
elseif ($script:Warn -gt 0) { Write-Host "Ready, with $($script:Warn) warning(s)." -ForegroundColor Yellow }
else { Write-Host "All set." -ForegroundColor Green }
