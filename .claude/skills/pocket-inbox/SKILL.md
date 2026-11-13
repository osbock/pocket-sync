---
name: pocket-inbox
description: Download new Pocket recordings, transcribe and summarize them, then pull out spoken instructions (feature requests, bugs, to-dos for a project) and file them as GitHub issues on the right repo. Use when the user says "check my Pocket", "process my recordings", "pocket inbox", or asks to turn recordings into issues.
---

# Pocket inbox

Works from any directory. Everything goes through the Pocket Recordings web
app's local API at `http://127.0.0.1:8765` (port: `$POCKET_APP_PORT` if
set); it owns Bluetooth, Whisper and LM Studio. Never run `pocket_sync.py`
or `transcribe.py` yourself. Use `jq` to read the JSON.

If `curl` can't connect, tell the user to open the Pocket Recordings
launcher app; don't start it yourself.

## 1. Fetch and process

```
curl -s -X POST http://127.0.0.1:8765/api/inbox/run \
  -H 'Content-Type: application/json' -d '{"sync": "ble"}' | jq '.job.id'
```

This queues an "inbox" job that syncs over Bluetooth, transcribes anything
new and summarizes it. `"sync"` can be `"ble"`, `"usb"` or `"none"` (only
process what's already on the Mac); add `"summarize": false` to skip
summaries. If one is already queued you get that job back
(`"existing": true`).

Wait for it (transcription runs at about 1/10 of real time). Repeat this
until `status` is `done`, `failed` or `cancelled`; each call waits up to
110 s:

```
curl -s 'http://127.0.0.1:8765/api/jobs/<job id>?wait=110' | jq '{status, progress, log, result}'
```

`log` says what happened, including steps that failed. If the sync failed
(recorder out of range), say so and carry on; the recordings already on this
Mac still get processed.

Then list the recordings not yet reviewed (id, title, speakers, transcript
and summary paths):

```
curl -s http://127.0.0.1:8765/api/inbox | jq '.pending'
```

## 2. Read each pending recording

Get the projects:

```
curl -s http://127.0.0.1:8765/api/inbox/projects
```

For each pending recording, read the summary, then the **full transcript**
(`.txt`, lines like `[0:01:23] Kevin: ...`). Summaries drop details.

Find **actionable instructions to Claude or about a project**:
- explicit: "note for Claude", "Claude, ...", "add an issue", "file a bug",
  "add a feature to X", "remind me to change X"
- implicit, from the user only: "pixelkit should be able to ...",
  "I need to fix the ... in spinning top"

Match a project by its `aliases` and `description`; Whisper mangles names,
so match on sound and context, not exact spelling. Skip:
- things other speakers ask for, unless the user agrees to do them
- general musing with no ask, meetings about other people's work
- anything with no identifiable project: list it under "unmatched" instead

One recording can hold several instructions, and some hold none.

## 3. Check for duplicates

For each candidate:

```
gh issue list --repo <repo> --state all --search "<key words>" --limit 10
```

Also check the recording's `<id>.meta.json` `inbox.issues` (already filed
from this recording). If one already covers it, propose a comment on that
issue instead of a new one, or skip it.

## 4. Propose, then file

Unless `confirm = false` in projects.toml, show the user a numbered list
first and wait for approval or edits:

```
1. osbock/pixelkit — "Add hexagon grid option"  (2026-11-13 14:02, [0:03:10])
   > quoted transcript line(s)
```

Then file each approved one:

```
gh label create from-pocket --repo <repo> --color C5DEF5 \
  --description "Filed from a Pocket voice recording" 2>/dev/null || true
gh issue create --repo <repo> --label from-pocket --title "<title>" --body-file <file>
```

Write the body to a file in the scratchpad (not inline; transcripts contain
quotes). Body:

```
<What to do, rewritten as a clear request: 1–4 sentences, with any details
the user gave (constraints, examples, why).>

### From the recording
> [0:03:10] Kevin: <exact transcript lines, lightly trimmed>

Recording `2026-11-13/20261113140200`, "<title>". Transcribed by Whisper,
so names and technical words may be misheard.
```

Titles: imperative, specific, under 70 characters ("Add hexagon grid
option", not "Feature request from recording").

Keep transcript quotes to the instruction itself. Don't put unrelated
conversation, other people's names or personal details in an issue, and
check whether the repo is public (`gh repo view <repo> --json visibility`)
before quoting anything sensitive.

## 5. Mark reviewed

After each recording is done, even ones with nothing to file:

```
curl -s -X POST http://127.0.0.1:8765/api/inbox/mark -H 'Content-Type: application/json' \
  -d '{"id": "<id>", "issues": ["<url>", "<url>"]}'
curl -s -X POST http://127.0.0.1:8765/api/inbox/mark -H 'Content-Type: application/json' \
  -d '{"id": "<id>", "note": "nothing actionable"}'
```

Build the body with `jq -n` if a note contains quotes.

Only mark a recording once its issues have been filed (or the user declined
them), so an interrupted run picks it up again.

## 6. Report

End with a short report: recordings processed (titles), issues filed (as
links), anything skipped, unmatched or failed.

## First run

If `pending` lists a backlog of old recordings the user doesn't want
reviewed, offer `curl -s -X POST http://127.0.0.1:8765/api/inbox/baseline`, which marks every
current recording as reviewed without filing anything.

Private settings are in `~/.config/pocket-sync`; never print or commit
`config.json` or `voices.json`.
