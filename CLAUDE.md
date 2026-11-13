# pocket-sync

Cloud-free tools for the HeyPocket "Pocket" voice recorder: download
recordings over Bluetooth or USB (`pocket_sync.py`), transcribe with Whisper,
label speakers with pyannote and summarize with LM Studio (`transcribe.py`),
and a local web app with a job queue (`app.py`, `static/index.html`).
`README.md` is the full user documentation.

- Scripts are uv PEP 723 scripts: `uv run <script>`; no virtualenv.
- Private settings live in `~/.config/pocket-sync` (session key, voiceprints).
  Never print, commit or upload them.
- Recordings live in `~/PocketRecordings`; deletes go to the Trash, never `rm`.
- Currently macOS / Apple Silicon only. On macOS, Bluetooth only works from
  Terminal or the built launcher app, not an agent's shell (exit 134).

## Windows port

Planned, not started. If this session is on Windows, read
`docs/windows-port.md` first: it lists what carries over, every macOS-only
call with its replacement and line numbers, gotchas (CUDA PyTorch index,
console encoding) and a testing checklist. Keep one codebase; the Mac must
keep working.
