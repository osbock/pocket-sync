# Windows port: plan

Status (2026-11-13): **code ported, partly tested on Windows** (Windows 11,
RTX 3080 Ti 12 GB). Done: `platform_support.py` (decode, USB drive find/eject,
keep-awake, spawn/kill job, trash), faster-whisper backend and CUDA device in
`transcribe.py`, CUDA PyTorch index in its PEP 723 header, explicit UTF-8 for
every text file read/write, `install.ps1`, `make_app.ps1` + `launch.py`
(Start Menu shortcut), README section. See "Test results" at the end for what
has been checked on real hardware and what hasn't.

Goal: one codebase that runs on macOS (Apple Silicon) and Windows 10 1809+/11.
Don't fork. The Mac must keep working exactly as it does now.

## Carries over unchanged

- Recorder protocol and Bluetooth (`pocket_sync.py`, bleak). bleak's WinRT
  backend works on Windows 10 1809+. No per-app Bluetooth permission on
  Windows (the macOS exit-134 problem doesn't exist there). Don't pair the
  recorder in Windows Settings; if services look stale, WinRT may be caching
  the GATT table (bleak's `use_cached_services=False` / remove the device).
  The saved `address` is a MAC address on Windows, a UUID on macOS; `scan`
  re-saves it, and `find_device` falls back to the `PKT01` name prefix.
- Web app (FastAPI, `static/index.html`, queue logic, estimates).
- LM Studio: same server on localhost:1234. Only the `lms` path changes
  (`%USERPROFILE%\.lmstudio\bin\lms.exe`); models are GGUF instead of MLX.
  Ollama and llama.cpp also work (`llm.py`, `llm_url` in app.json).
- pyannote speaker recognition: change device selection from `mps` to
  `cuda` when available, CPU otherwise (`transcribe.py` ~line 115).
- Session key: adb works on Windows (`winget install Google.PlatformTools`).

## Must change

| macOS piece | Where | Replacement |
|---|---|---|
| mlx-whisper (Apple-only) | `transcribe.py` `transcribe()` | faster-whisper, model `large-v3-turbo`, CUDA float16 or CPU int8. Map its segments to the same `{start, end, text}` shape so `render()` and speaker matching are unchanged. Keep mlx-whisper on the Mac. |
| `afconvert` decoding | `transcribe.py` `load_audio()` (~67), `app.py` peaks (~212) | PyAV (installed with faster-whisper, bundles ffmpeg). Could replace afconvert on the Mac too. |
| `/Volumes` + `diskutil` (USB) | `pocket_sync.py` `pocket_volume`, `disk_info`, `remount_read_only`, `eject` (~500–529) | Scan drive letters for `RECORD\device.txt` starting `PKT01`. Eject via PowerShell `Shell.Application` Namespace(17) "Eject" verb or ctypes `DeviceIoControl`. No read-only remount; just never write to the drive. |
| `caffeinate` | `app.py` `update_caffeinate` (~467) | ctypes `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` while busy, `ES_CONTINUOUS` to release. |
| `start_new_session` / `os.killpg` | `app.py` ~541, ~433, ~639 | `creationflags=CREATE_NEW_PROCESS_GROUP`; kill the tree with `taskkill /T /F /PID` or psutil. |
| `~/.Trash` | `app.py` delete (~317) | send2trash (Recycle Bin; also works on the Mac). Loses the "Pocket <id> <stamp>" renamed folder. |
| `os.kill(getpid(), SIGTERM)` | `app.py` shutdown (~648) | set uvicorn `server.should_exit = True` (works on both). |
| `make_app.sh` | | Start Menu shortcut running `uvw run app.py` (no console), created with PowerShell `WScript.Shell`; `.ico` drawn with Pillow instead of `tools/make_icon.swift`. |
| `install.sh` | | `install.ps1`: winget for uv and LM Studio (`ElementLabs.LMStudio`), `nvidia-smi` GPU check, same HF login/pyannote access checks, same settings `--export/--import` (config.json, voices.json, app.json). |

Suggested structure: a small `platform_support.py` holding decode, find/eject
drive, keep-awake, spawn/kill job, trash; plus a Whisper backend switch in
`transcribe.py`.

## Models: copy from the Mac instead of re-downloading

Worked out 2026-11-13 from what is on the Mac:

| On the Mac (`~/.lmstudio/models/`) | Size | On the PC |
|---|---|---|
| `HauhauCS/Qwen3.5-35B-A3B-Uncensored-HauhauCS-Aggressive/` (GGUF Q4_K_M) | 21 GB | **Copy it.** GGUF is cross-platform. |
| `lmstudio-community/Qwen3.8-27B-MLX-4bit/` | 15 GB | Useless (MLX is Apple-only). Get a GGUF build if wanted. |
| `mlx-community/whisper-large-v3-turbo/` | 1.5 GB | Useless. faster-whisper needs the CTranslate2 build (~1.6 GB download either way). |

- Copy the folder to `%USERPROFILE%\.lmstudio\models\HauhauCS\` keeping the
  publisher\model layout, and LM Studio lists it under My Models. The
  `mmproj-…f16.gguf` file (0.9 GB) is only for images; summaries don't need it.
- Transfer: SMB share (macOS File Sharing with Windows File Sharing on, or a
  Windows share opened from Finder with `smb://`), ~6–12 min over Wi-Fi; or a
  USB SSD formatted **exFAT** (FAT32 can't hold files over 4 GB).
- pyannote's model is in `~/.cache/huggingface/hub`; copying it to
  `%USERPROFILE%\.cache\huggingface\hub` avoids that download too (the HF
  login is still needed).
- The 35B model needs ~24 GB VRAM to run fully on the GPU. On an 8–12 GB card
  it partly runs on the CPU and summaries get much slower; an ~8B model is a
  better fit there.
- On the Mac, LM Studio and llama.cpp were within ~10% on this model (LM Studio
  faster), so there's no speed reason to prefer llama.cpp on the PC either.

## Gotchas

- **PyTorch from PyPI on Windows is CPU-only.** Add a `[tool.uv.sources]` /
  `[[tool.uv.index]]` block for the PyTorch CUDA index in the scripts' PEP 723
  headers, or pyannote silently runs on the CPU.
- Inline deps need platform markers: `mlx-whisper; sys_platform == 'darwin'`,
  `faster-whisper; sys_platform == 'win32'`.
- Console encoding: ✓ marks and accented names can crash prints under cp1252;
  set `PYTHONUTF8=1` in the launcher.
- `0o600` / `chmod 600` are no-ops on Windows; the user profile is private,
  which is acceptable.
- Settings path: `Path.home() / ".config/pocket-sync"` works on Windows too;
  keep it so `--export/--import` files are interchangeable.
- Hardware: an NVIDIA GPU with 8 GB+ VRAM is comfortable for Whisper and
  pyannote; summaries need the LLM to fit in VRAM. CPU-only works but a
  25-minute recording would likely take several minutes, and summaries are slow.
- WSL is not an option: no direct Bluetooth or USB-drive access.

## Testing checklist (on the Windows PC)

1. `uv run pocket_sync.py scan`, `info`, `list`, `pull --limit 1` (Bluetooth).
2. `pull --usb`: drive found, copied, ejected, `usb off` sent.
3. `uv run transcribe.py --largest --summarize`: compare the transcript with
   the Mac's for the same recording; check speakers match `voices.json`
   imported from the Mac (embeddings come from the same pyannote model, so
   they should be compatible).
4. Web app: play, waveform, delete to Recycle Bin, queue cancel (process tree
   really dies), keep-awake during jobs, Quit.
5. Re-run everything on the Mac to confirm nothing regressed.

Estimated effort: 2–3 days, mostly testing faster-whisper output and USB eject
on real hardware.

## Test results (Windows 11, RTX 3080 Ti 12 GB, 2026-11-13)

Checked:

- Bluetooth: `scan`, `info`, `list` (bleak WinRT; no pairing, no prompt).
- `pull --usb`: found as a removable exFAT drive (label "Pocket", USB ID
  ZEPHYR USB DISK), 12 recordings / 24.6 MB in 22 s, eject via
  `DeviceIoControl` keeps it gone. **Order matters:** cable unplugged, USB mode
  on, then plug in. Plugging in before USB mode is on gives only charging, and
  unplugging turns USB mode off. Right after the eject the recorder dropped the
  Bluetooth connection once, so `usb off` now retries (3 attempts).
- A real 25-min recording: Whisper 23 s (66x realtime) on the 3080 Ti, and
  both speakers recognized from `voices.json` imported from the Mac (0.84 and
  1.0), so voiceprints carry over.
- Summary + title of that recording with LM Studio and the 35B-A3B GGUF
  copied from the Mac: 51 s (11.4 of 12 GB VRAM used, the rest in RAM).
- faster-whisper on CUDA (float16) and pyannote on CUDA, on a 32 s two-voice
  test clip: exact transcript, both speakers separated. `transcribe.py
  --check-gpu` prints the device.
- PyAV decoding at 16 kHz and 8 kHz (waveform).
- Web app: list, waveform, audio, delete to Recycle Bin, Quit
  (`server.should_exit`), cancel kills the `uv` + python tree (taskkill /T).
- `launch.py` via `uvw`: the server outlives the launcher.
- `install.ps1`: `-Check`, `-Import` of a Mac export.

Not yet checked:

- `pull` over Bluetooth (downloads); a full `pull --usb` run after the retry fix.
- A side-by-side diff with the Mac's transcript of the same recording.
- Keep-awake during a long job (`powercfg /requests` as admin shows it).
- The web app's `lms.exe` load/unload of the summary model.
- The Start Menu shortcut itself (`make_app.ps1`), and the icon.
- Everything again on the Mac (mlx-whisper path, afconvert, Trash folder,
  caffeinate, and the new `should_exit` shutdown).

Found while testing: `uv run app.py --help` started the server instead of
printing help, which hung `install.sh` and `install.ps1` at the packages
step; fixed for both.
