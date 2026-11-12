# pocket-sync

Cloud-free tools for the HeyPocket **Pocket** voice recorder (`PKT01_…`):
download recordings over Bluetooth or USB, transcribe them, label who spoke,
and summarize them, all on this Mac. Nothing is sent to Pocket's servers or
any other cloud service.

| File | What it does |
|---|---|
| `app.py` | Local web app: browse, play, title, delete, name speakers, queue transcription and summaries |
| `pocket_sync.py` | Talks to the recorder: download (Bluetooth or USB), list, delete, device info |
| `transcribe.py` | Transcription (Whisper), speaker labelling (pyannote), summaries and titles (LM Studio) |
| `static/index.html` | The web app's page |

Each script declares its own dependencies inline, so `uv run <script>` installs
whatever it needs the first time. No virtualenv to manage.

---

## Quick start

```bash
cd ~/pocket-sync
uv run app.py            # then open http://localhost:8765
```

Or from the command line:

```bash
uv run pocket_sync.py pull           # new recordings over Bluetooth
uv run pocket_sync.py pull --usb     # ...or over a USB-C cable (much faster)
uv run transcribe.py --summarize     # transcribe, label speakers, summarize
```

> **Run these from Terminal.app or the Claude app's terminal tab.** macOS only
> lets processes it has granted Bluetooth access use Bluetooth; other launch
> paths abort with exit code 134 when they touch Bluetooth.

> **Close the Pocket phone app** (or turn off the phone's Bluetooth) before a
> Bluetooth sync. The recorder accepts one Bluetooth connection at a time and
> is invisible to everything else while the phone app holds it.

---

## One-time setup

### 1. Tools

- [uv](https://docs.astral.sh/uv/) (`brew install uv`)
- Apple Silicon Mac (Whisper runs on the GPU through MLX)
- `adb` only to read the session key: `brew install --cask android-platform-tools`

### 2. The recorder's session key

The recorder only answers a client that sends your account's 16-character
session key. With the Pocket Android app installed:

1. Enable USB debugging on the phone and connect it to the Mac.
2. Open the Pocket app, then run `adb logcat | grep -i distinct_id`.
3. The 28-character `distinct_id` is your account ID; **its first 16 characters
   are the session key.**

```bash
uv run pocket_sync.py setkey          # prompts for the key; saved with mode 600
uv run pocket_sync.py scan            # finds the recorder and remembers it
uv run pocket_sync.py info            # battery, firmware, storage
```

Keep the key private: its first 8 characters are also the password of the
recorder's Wi-Fi hotspot. You can also pass it as `POCKET_SESSION_KEY`.

### 3. Whisper (speech to text)

`transcribe.py` uses the MLX Whisper model stored in LM Studio's model folder:

```
~/.lmstudio/models/mlx-community/whisper-large-v3-turbo/
    config.json
    weights.safetensors
```

Download `mlx-community/whisper-large-v3-turbo` in LM Studio (or from Hugging
Face into that folder). LM Studio itself has no speech-to-text endpoint, so the
script runs the model directly. MP3s are decoded with macOS's `afconvert`;
ffmpeg isn't needed.

### 4. pyannote (speaker labelling)

1. On huggingface.co, accept the terms of
   [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1).
2. Create a **Read** token (a fine-grained token needs access to public gated repos).
3. `uvx --from huggingface_hub hf auth login`

The login is only needed to download the model once. Audio never leaves the Mac.
Skip speaker labelling with `--no-speakers` if you don't want this.

### 5. LM Studio (summaries and titles)

Start LM Studio's local server (port 1234). Any loaded chat model works; the
web app loads the one you pick and unloads other LLMs to make room.
Summaries are requested with reasoning turned off, which is much faster and
gives similar results.

Measured on an M4 Pro (48 GB), 25-minute recording:

| Model | Summary time |
|---|---|
| Qwen 35B-A3B (MoE) | ~30 s |
| Qwen 27B dense | ~2.5 min (8.5 min with reasoning on) |

---

## The web app (`app.py`)

```bash
uv run app.py                          # http://localhost:8765 (127.0.0.1 only)
POCKET_APP_PORT=9000 uv run app.py     # another port
```

- **List:** grouped by day (local time), with title, length, speakers and
  badges for transcript, summary, *on recorder*, and queued work. Search covers
  titles, speakers and transcript text. Clips under 3 s (often created by
  plugging in or pressing the button) are hidden unless you tick *short clips*.
- **Player:** clickable waveform, ±10 s, 1–2× speed, Space to play or pause.
  Click any transcript line to jump there; the current line is highlighted.
- **Title:** click it to edit. The LLM suggests a title when it summarizes,
  but never replaces one you typed. Clear the field to go back to LLM titles.
- **Speakers tab:** each speaker's talk time and their three longest clips
  to play. Type a name and Save; every other transcript is relabelled with
  that voice.
- **Sync:** pick Bluetooth or USB cable. For USB, a banner tells you when to
  plug in and when to unplug.
- **Delete…:** asks each time what to delete:
  - *Audio file only (keep transcript and summary)*: the MP3 goes to the
    Trash. The recording stays in the list with its title, transcript,
    summary and speakers, marked *audio deleted*. Playback and
    re-transcribing are no longer possible. Summarizing again and naming
    speakers still work. Only offered for recordings that have a transcript.
  - *Mac copy and transcripts*: everything goes to the Trash.
  - *On the recorder*: queued as a Bluetooth job. Permanent.

  Mac deletions are remembered, so sync won't download the audio again.
  To free space **on the recorder** while keeping everything on the Mac,
  tick only *On the recorder*. For scale, an hour of audio is about 14 MB.
- **Queue:** select recordings and choose Transcribe or Summarize
  (summarizing transcribes first if needed). Jobs survive restarts; you can
  reorder or cancel them. Two lanes run side by side: *gpu* (one transcription
  or summary at a time) and *device* (sync, recorder deletes). The Mac is kept
  awake (`caffeinate`) while anything is queued, so you can queue a batch and
  leave it overnight.

Each job runs `pocket_sync.py` or `transcribe.py` as a subprocess, so
cancelling stops it immediately and a crash can't take the server down.

---

## `pocket_sync.py`: talking to the recorder

```
uv run pocket_sync.py setkey [KEY]       store the session key
uv run pocket_sync.py scan               find the recorder, save its address
uv run pocket_sync.py info               battery, firmware, storage, USB mode
uv run pocket_sync.py list               recordings on the device (✓ = downloaded)
uv run pocket_sync.py pull [options]     download new recordings
      --usb            copy over a USB-C cable instead of Bluetooth
      --since DATE     only dates >= YYYY-MM-DD
      --limit N        only the newest N new recordings
      --retries N      Bluetooth attempts per recording (default 4)
      --out DIR        default ~/PocketRecordings
uv run pocket_sync.py delete DATE/NAME ...   delete from the recorder (not the Mac)
uv run pocket_sync.py usb on|off         switch USB drive mode by hand
```

### Bluetooth vs USB

| | Bluetooth | USB-C cable |
|---|---|---|
| Speed | ~41 KB/s (≈6 min per hour of audio) | ~1.1 MB/s (≈13 s per hour) |
| Cable | no | yes, a **data** cable |
| Quirks | occasional stalls (retried automatically); recorder sometimes stops advertising for a while (the script waits up to 90 s) | the recorder can't see its own storage, and probably can't record, while USB mode is on and the cable is in |

`pull --usb` does the whole sequence:

1. Switch USB mode on over Bluetooth.
2. Wait up to 5 minutes for you to plug in.
3. Remount the drive read-only.
4. Copy new files and check their sizes.
5. Eject, switch USB mode off, and tell you to unplug.

**Always unplug when it says so.** If the recorder ever reports 0 GB or no
recordings, it's still attached to the Mac: eject it, run `usb off`, and
unplug.

Both methods produce byte-identical files and share one download log, so
neither re-downloads what the other already fetched.

---

## `transcribe.py`: transcripts, speakers, summaries

```
uv run transcribe.py                         every recording without a transcript
uv run transcribe.py --largest               just the longest untranscribed one
uv run transcribe.py FILE.mp3 ...            specific files (add --force to redo)
uv run transcribe.py --summarize             also summarize and suggest a title
uv run transcribe.py --summary-only FILE     summarize existing transcripts only
      --model ID        LM Studio model for summaries (default: the loaded one)
      --think           let the model reason first (much slower)
      --no-title        don't suggest a title
      --no-speakers     skip speaker labelling
      --num-speakers N  exact speaker count, if known
      --language en     skip language auto-detection
      --min-seconds 3   skip shorter clips

uv run transcribe.py --speakers REC              speakers, clips and voice matches
uv run transcribe.py --name REC "Speaker B" "Alex"   name a speaker (saves a voiceprint)
uv run transcribe.py --rematch                   relabel all transcripts with current voices
uv run transcribe.py --forget "Alex"             delete a voiceprint
      --threshold 0.70   voice match threshold
```

`REC` is a recording name such as `20261105150712`, or the path to its `.mp3`.

**Speed** (M4 Pro): Whisper runs at about 40× real time, and speaker
labelling at about 25×. A 25-minute recording takes about 1.5 minutes in total.

### How speaker recognition works

pyannote splits each recording into Speaker A, B, … and saves one voiceprint
per speaker. Each voiceprint is compared with the voices you've named; a
cosine similarity of **0.70** or more is a match, and each named voice is used
at most once per recording.

Calibration on this recorder:

| Comparison | Similarity |
|---|---|
| Same person, different recordings | 0.83 – 0.91 |
| Different people | 0.11 – 0.56 |

Voiceprints change only when you name someone. Automatic matches never
update them, so one wrong match can't gradually corrupt a voice.

---

## Where things are stored

```
~/PocketRecordings/
  2026-11-05/
    20261105150712.mp3            the recording (MP3, 16 kHz mono, 32 kbps)
    20261105150712.txt            transcript: "[0:05:07] Alex: ..."
    20261105150712.srt            subtitles
    20261105150712.json           Whisper segments with speaker labels
    20261105150712.speakers.json  speaker turns and voiceprints
    20261105150712.summary.md     summary
    20261105150712.meta.json      title (and length, once the audio is deleted)
    20261105150712.peaks.json     waveform cache for the app
  .pocket-sync.json               download log (also marks deliberate deletions)
  .device.json                    what was on the recorder at the last sync
  .queue.json                     the app's job queue

~/.config/pocket-sync/
  config.json     session key and recorder address (mode 600)
  voices.json     named voiceprints (mode 600)
  app.json        app settings (summary model)
```

Recording names are the UTC start time (`YYYYMMDDHHmmss`). A second form,
`PH` + `YYMMDDHHmmss`, also exists, so treat names as opaque. The app shows
local times.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Pocket not found` | Close the phone app, press the recorder's button, try again. The script already waits up to 90 s. |
| Exit code 134 on any Bluetooth command | Not allowed to use Bluetooth: run from Terminal.app or the terminal tab and allow the prompt. |
| `MCU&SK&ERR` / key rejected | Wrong session key. Re-read it with `adb logcat`. |
| Bluetooth transfer stalls | Retried automatically on a fresh connection; or use `pull --usb`. |
| Recorder shows 0 GB / 0 recordings | USB mode is on with the cable in. Eject, `usb off`, unplug. |
| `Could not load pyannote/...` | Accept the model terms and run `hf auth login` (see setup). |
| Summary: `No LLM is loaded` | Load a model in LM Studio, or pass `--model`. |
| Short clips you didn't record | Plugging in the cable or pressing the button seems to start a short recording. They're harmless and hidden in the app. |

---

## Notes

- **Credit:** the recorder protocol (Bluetooth commands, Wi-Fi and USB modes) was
  reverse-engineered by [pocket-libre](https://github.com/shahcolate/pocket-libre)
  (MIT). See its `PROTOCOL.md`. These scripts are a smaller, independent client.
- **Tested on:** firmware 1.8.9 (Wi-Fi V9), macOS on an M4 Pro, Pocket Android
  app 0.6.103.
- **Privacy:** voiceprints are biometric data about the people you record. They
  stay on this Mac, but some places (Illinois, for example) require consent to
  keep them. `--forget` deletes one.
- **Not implemented:** Wi-Fi transfer (~1 MB/s over the recorder's own hotspot,
  but finicky) and live capture while recording.
