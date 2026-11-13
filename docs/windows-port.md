# Windows port: plan

Status (2026-11-12): **planned, not started.** Everything here was worked out
on the Mac; nothing has been tried on Windows yet.

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
