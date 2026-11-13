#!/bin/bash
# Set up pocket-sync on a Mac (or check an existing setup).
#
#   ./install.sh                  # set up: checks each requirement, offers to fix it
#   ./install.sh --check          # report only; changes nothing
#   ./install.sh --import DIR     # set up, copying settings exported from another Mac
#   ./install.sh --export DIR     # copy this Mac's settings into DIR (to carry over)
#
# Settings are the recorder's session key (config.json), named voices
# (voices.json) and the app's model choice (app.json), all in
# ~/.config/pocket-sync. They're private: move them by AirDrop or a USB stick,
# not email or a synced cloud folder. Run this from Terminal, not from an
# editor or agent shell: the Bluetooth scan needs Terminal's permission.
#
# Safe to run again; steps that are already done are skipped.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CONF="$HOME/.config/pocket-sync"
SETTINGS=(config.json voices.json app.json)
WHISPER="$HOME/.lmstudio/models/mlx-community/whisper-large-v3-turbo"
LMS="$HOME/.lmstudio/bin/lms"
DIARIZER="pyannote/speaker-diarization-community-1"

MODE=install
IMPORT=""
case "${1:-}" in
  "") ;;
  --check) MODE=check ;;
  --import) IMPORT="${2:?--import needs a folder}" ;;
  --export) MODE=export; EXPORT="${2:?--export needs a folder}" ;;
  -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
esac

if [ -t 1 ]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'; else B= G= Y= R= N=; fi
ok()   { echo "  ${G}✓${N} $*"; }
warn() { echo "  ${Y}!${N} $*"; WARN=$((WARN + 1)); }
bad()  { echo "  ${R}✗${N} $*"; FAIL=$((FAIL + 1)); }
step() { echo; echo "${B}$*${N}"; }
ask()  {
  [ "$MODE" = install ] || return 1
  # No terminal to answer in (e.g. run from an editor or agent shell): skip
  # rather than treat the failed read as "yes".
  if ! { : </dev/tty; } 2>/dev/null; then
    echo "    $1 Skipped: no terminal to answer in; run ./install.sh from Terminal."
    return 1
  fi
  local a; read -r -p "    $1 [Y/n] " a </dev/tty; [[ ! "$a" =~ ^[Nn] ]]
}
WARN=0; FAIL=0

# --- export -----------------------------------------------------------------
if [ "$MODE" = export ]; then
  mkdir -p "$EXPORT" && chmod 700 "$EXPORT"
  n=0
  for f in "${SETTINGS[@]}"; do
    if [ -f "$CONF/$f" ]; then
      cp "$CONF/$f" "$EXPORT/$f" && chmod 600 "$EXPORT/$f" && echo "Copied $f" && n=$((n + 1))
    fi
  done
  [ $n -gt 0 ] || { echo "Nothing to export: no settings in $CONF." >&2; exit 1; }
  echo
  echo "Exported $n file(s) to $EXPORT. They include your recorder's session key"
  echo "and voiceprints: carry them over by AirDrop or USB, then on the other Mac run"
  echo "  ./install.sh --import <that folder>"
  echo "and delete the copy afterwards."
  exit 0
fi

[ "$MODE" = check ] && echo "Checking only; nothing will be changed."

# --- Mac --------------------------------------------------------------------
step "Mac"
if [ "$(uname -m)" = arm64 ]; then
  ok "Apple Silicon ($(sysctl -n machdep.cpu.brand_string 2>/dev/null))"
else
  bad "This needs an Apple Silicon Mac (Whisper runs on MLX)."
fi
mem=$(( $(sysctl -n hw.memsize) / 1073741824 ))
if [ "$mem" -ge 32 ]; then ok "${mem} GB memory"
elif [ "$mem" -ge 16 ]; then warn "${mem} GB memory: transcription is fine; pick a smaller summary model (around 8B) in LM Studio"
else warn "${mem} GB memory: transcription may be slow; summaries need a small model"; fi
free=$(df -g "$HOME" | awk 'NR==2 {print $4}')
if [ "${free:-0}" -ge 30 ]; then ok "${free} GB free disk"
else warn "${free} GB free disk: plan for about 2 GB for the tools plus 15-25 GB per summary model"; fi

# --- Command Line Tools -----------------------------------------------------
step "Developer tools"
if xcode-select -p >/dev/null 2>&1; then
  ok "Xcode Command Line Tools"
else
  bad "Xcode Command Line Tools are missing (git, swift for the app icon)"
  if ask "Install them now? (opens an Apple installer window)"; then
    xcode-select --install 2>/dev/null
    echo "    Finish the installer, then run ./install.sh again."
    exit 1
  fi
fi

# --- Homebrew and uv --------------------------------------------------------
[ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"
export PATH="$HOME/.local/bin:$PATH"
if command -v uv >/dev/null 2>&1; then
  ok "uv $(uv --version | awk '{print $2}')"
else
  bad "uv (Python runner) is missing"
  if command -v brew >/dev/null 2>&1; then
    ask "Install uv with Homebrew?" && brew install uv && ok "uv installed" && FAIL=$((FAIL - 1))
  else
    echo "    Homebrew isn't installed either. Install Homebrew from https://brew.sh"
    echo "    and run ./install.sh again, or install uv alone: https://docs.astral.sh/uv/"
  fi
fi
HAVE_UV=0; command -v uv >/dev/null 2>&1 && HAVE_UV=1
hf() { uvx --quiet --from huggingface_hub hf "$@"; }

# --- Settings ---------------------------------------------------------------
step "Settings (~/.config/pocket-sync)"
if [ -n "$IMPORT" ]; then
  [ -d "$IMPORT" ] || { bad "No such folder: $IMPORT"; exit 1; }
  mkdir -p "$CONF" && chmod 700 "$CONF"
  for f in "${SETTINGS[@]}"; do
    [ -f "$IMPORT/$f" ] || continue
    if [ -f "$CONF/$f" ] && ! cmp -s "$IMPORT/$f" "$CONF/$f"; then
      bak="$CONF/$f.before-import.$(date +%Y%m%d%H%M%S)"
      cp -p "$CONF/$f" "$bak" && chmod 600 "$bak"
      echo "    Kept the existing $f as $(basename "$bak")"
    fi
    cp "$IMPORT/$f" "$CONF/$f" && chmod 600 "$CONF/$f" && ok "Imported $f"
  done
fi
if [ -f "$CONF/config.json" ] && grep -q '"session_key"' "$CONF/config.json"; then
  ok "Session key saved"
else
  warn "No session key yet. Copy it from your other Mac (./install.sh --export DIR there,"
  echo "    then --import DIR here) or find it again as the README describes, then:"
  echo "      uv run pocket_sync.py setkey <16 characters>"
fi
if [ -f "$CONF/voices.json" ]; then
  n=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$CONF/voices.json" 2>/dev/null || echo "?")
  ok "Named voices: $n"
else
  echo "    (No named voices yet; they're created as you name speakers.)"
fi
for f in "${SETTINGS[@]}"; do
  if [ -f "$CONF/$f" ] && [ "$(stat -f %Lp "$CONF/$f")" != 600 ]; then
    if [ "$MODE" = check ]; then warn "$f is readable by other users (run without --check to fix)"
    else chmod 600 "$CONF/$f" && ok "Made $f private"; fi
  fi
done

# --- LM Studio and Whisper --------------------------------------------------
step "LM Studio (summaries and titles)"
if [ -d "/Applications/LM Studio.app" ] || [ -x "$LMS" ]; then
  ok "LM Studio installed"
  if curl -s -m 2 http://localhost:1234/api/v0/models >/dev/null; then
    n=$(curl -s -m 2 http://localhost:1234/api/v0/models | python3 -c 'import json,sys; print(sum(m.get("type") in ("llm","vlm") for m in json.load(sys.stdin)["data"]))' 2>/dev/null || echo 0)
    if [ "$n" -gt 0 ]; then ok "Server running, $n chat model(s) downloaded"
    else warn "Server running but no chat model downloaded; get one in LM Studio's Discover tab (e.g. Qwen3 30B-A3B, or an 8B model on 16 GB Macs)"; fi
  else
    warn "LM Studio's server isn't running. In LM Studio: Developer tab > Start Server,"
    echo "    and in Settings turn on starting the server at login. Without it you get"
    echo "    transcripts but no summaries or titles."
  fi
else
  LLM_URL=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("llm_url",""))' "$CONF/app.json" 2>/dev/null)
  if [ -n "$LLM_URL" ] && curl -s -m 2 "$LLM_URL/v1/models" >/dev/null; then
    ok "Using the LLM server at $LLM_URL"
  else
    warn "LM Studio isn't installed (needed for summaries and titles): https://lmstudio.ai"
    echo "    Ollama or llama.cpp work too; see README, setup step 5."
  fi
fi

step "Whisper model (transcription)"
if [ -f "$WHISPER/config.json" ] && ls "$WHISPER"/*.safetensors >/dev/null 2>&1; then
  ok "whisper-large-v3-turbo ($(du -sh "$WHISPER" | cut -f1))"
else
  bad "whisper-large-v3-turbo not found in $WHISPER"
  echo "    Get it in LM Studio (search mlx-community/whisper-large-v3-turbo) or download here."
  if [ $HAVE_UV = 1 ] && ask "Download it now (about 1.6 GB)?"; then
    mkdir -p "$WHISPER"
    hf download mlx-community/whisper-large-v3-turbo --local-dir "$WHISPER" \
      && ok "Downloaded" && FAIL=$((FAIL - 1))
  fi
fi

# --- Hugging Face / pyannote ------------------------------------------------
step "Speaker recognition (pyannote, via Hugging Face)"
if [ $HAVE_UV = 1 ]; then
  if user=$(hf auth whoami 2>/dev/null | head -1) && [ -n "$user" ] && ! grep -qi "not logged" <<<"$user"; then
    user=${user#user=}; ok "Hugging Face login: ${user#user: }"
  else
    warn "Not logged in to Hugging Face. Speaker labels are skipped until you are."
    if ask "Log in now? (paste a read token from huggingface.co/settings/tokens)"; then
      hf auth login </dev/tty && WARN=$((WARN - 1))
    fi
  fi
  probe=$(mktemp -d)
  if hf download "$DIARIZER" config.yaml --local-dir "$probe" >/dev/null 2>&1; then
    ok "Access to $DIARIZER"
  else
    warn "No access to $DIARIZER. Open https://huggingface.co/$DIARIZER"
    echo "    while logged in and accept the terms (once per account), then run this again."
  fi
  rm -rf "$probe"
else
  warn "Skipped (needs uv)"
fi

# --- Python packages --------------------------------------------------------
step "Python packages"
if [ $HAVE_UV = 1 ]; then
  if [ "$MODE" = check ]; then
    echo "    (Installed on first use; run without --check to install them now.)"
  else
    echo "    Installing (a few minutes the first time; about 1.5 GB)..."
    for s in pocket_sync.py transcribe.py app.py; do
      if (cd "$HERE" && uv run --quiet "$s" --help >/dev/null 2>&1); then ok "$s"
      else bad "$s: packages failed to install. Try: cd $HERE && uv run $s --help"; fi
    done
  fi
fi

# --- Recorder ---------------------------------------------------------------
step "Recorder (Bluetooth)"
if [ -f "$CONF/config.json" ] && grep -q '"address"' "$CONF/config.json"; then
  ok "A recorder address is saved (it's different on every Mac; Sync finds it again if needed)"
fi
if [ $HAVE_UV = 1 ] && [ "$MODE" = install ]; then
  echo "    Close the Pocket phone app (only one Bluetooth connection at a time) and"
  echo "    keep the recorder nearby. macOS may ask Terminal for Bluetooth access: allow it."
  if ask "Scan for the recorder now?"; then
    (cd "$HERE" && uv run pocket_sync.py scan) || warn "Recorder not found; try again later with: uv run pocket_sync.py scan"
  fi
fi

# --- Launcher ---------------------------------------------------------------
step "Launcher app"
APP="$HOME/Applications/Pocket Recordings.app"
if [ -d "$APP" ] && grep -q "PROJECT=\"$HERE\"" "$APP/Contents/MacOS/launch" 2>/dev/null; then
  ok "Pocket Recordings.app points at this folder"
elif [ "$MODE" = install ] && ask "Build Pocket Recordings.app in ~/Applications?"; then
  "$HERE/make_app.sh" && echo "    Open it from Launchpad or Spotlight; allow Bluetooth when asked."
else
  warn "Launcher not built (or built for another folder). Run ./make_app.sh"
fi

# --- Claude skill -----------------------------------------------------------
step "Claude Code skill (/pocket-inbox)"
SKILL_SRC="$HERE/.claude/skills/pocket-inbox"
SKILL_DST="$HOME/.claude/skills/pocket-inbox"
if [ "$(readlink "$SKILL_DST" 2>/dev/null)" = "$SKILL_SRC" ]; then
  ok "/pocket-inbox is available in every folder"
elif [ -e "$SKILL_DST" ] && [ ! -L "$SKILL_DST" ]; then
  warn "$SKILL_DST exists and isn't a link; leaving it alone"
elif [ "$MODE" = install ]; then
  mkdir -p "$HOME/.claude/skills" && ln -sfn "$SKILL_SRC" "$SKILL_DST" \
    && ok "Linked /pocket-inbox into ~/.claude/skills"
else
  warn "/pocket-inbox only works inside this folder. Run ./install.sh to link it globally"
fi

# --- Summary ----------------------------------------------------------------
echo
if [ $FAIL -gt 0 ]; then echo "${R}${FAIL} problem(s)${N} and ${WARN} warning(s). Fix the ✗ items and run this again."; exit 1
elif [ $WARN -gt 0 ]; then echo "${Y}Ready, with ${WARN} warning(s).${N}"
else echo "${G}All set.${N}"; fi
