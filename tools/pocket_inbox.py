# /// script
# requires-python = ">=3.11"
# ///
"""Inbox for Pocket recordings: sync, transcribe, summarize, then track which
recordings have been reviewed for spoken instructions (GitHub issues etc.).

Drives the running web app (app.py) over its local job API, so Bluetooth works
even when this is run from an agent's shell. Start the app first.

    uv run tools/pocket_inbox.py run          # sync + transcribe + summarize, list new
    uv run tools/pocket_inbox.py run --no-sync    # skip the download step
    uv run tools/pocket_inbox.py run --usb        # download over USB instead of Bluetooth
    uv run tools/pocket_inbox.py pending      # recordings not yet reviewed (JSON)
    uv run tools/pocket_inbox.py mark ID [--issue URL ...] [--note TEXT]
    uv run tools/pocket_inbox.py baseline     # mark everything existing as reviewed
    uv run tools/pocket_inbox.py projects     # repos and spoken aliases (JSON)

Review state is kept in each recording's <name>.meta.json under "inbox".
Projects come from ~/.config/pocket-sync/projects.toml
(see docs/projects.example.toml).
"""

import argparse
import json
import os
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

API = f"http://127.0.0.1:{os.environ.get('POCKET_APP_PORT', '8765')}"
REC_DIR = Path.home() / "PocketRecordings"
PROJECTS = Path.home() / ".config/pocket-sync/projects.toml"
MIN_SECONDS = 3
DONE = ("done", "error", "cancelled")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path}: {e.code} {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"Pocket app not reachable at {API} ({e.reason}). "
                 "Start it from the launcher app (or `uv run app.py` in Terminal).")


def wait(job_ids: list[str], label: str) -> dict:
    """Poll until the jobs finish; return {id: job}."""
    if not job_ids:
        return {}
    last = ""
    while True:
        jobs = {j["id"]: j for j in call("GET", "/api/jobs")["jobs"] if j["id"] in job_ids}
        running = [j for j in jobs.values() if j["status"] not in DONE]
        if not running:
            return jobs
        line = f"  {label}: {len(job_ids) - len(running)}/{len(job_ids)} done"
        if running[0].get("progress"):
            line += f" - {running[0]['progress'][:80]}"
        if line != last:
            log(line)
            last = line
        time.sleep(5)


def meta_path(rec_id: str) -> Path:
    return REC_DIR / f"{rec_id}.meta.json"


def load_meta(rec_id: str) -> dict:
    try:
        return json.loads(meta_path(rec_id).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_meta(rec_id: str, meta: dict) -> None:
    p = meta_path(rec_id)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, indent=1))
    tmp.replace(p)


def recordings() -> list[dict]:
    return call("GET", "/api/recordings")["recordings"]


def entry(r: dict) -> dict:
    base = REC_DIR / r["id"]
    summary = base.with_suffix(".summary.md")
    return {"id": r["id"], "title": r.get("title"), "seconds": r.get("seconds"),
            "speakers": r.get("speakers", []),
            "transcript": str(base.with_suffix(".txt")),
            "summary": str(summary) if summary.exists() else None}


def pending_list() -> list[dict]:
    out = [entry(r) for r in recordings()
           if r.get("on_mac") and r.get("transcribed") and "inbox" not in load_meta(r["id"])]
    return sorted(out, key=lambda e: e["id"])


def cmd_run(args) -> None:
    if not args.no_sync:
        kind = "sync_usb" if args.usb else "sync_ble"
        log(f"Downloading new recordings ({'USB' if args.usb else 'Bluetooth'})...")
        try:
            job = call("POST", "/api/jobs", {"kind": kind})["jobs"][0]
            j = wait([job["id"]], "sync")[job["id"]]
            if j["status"] != "done":
                log(f"  sync {j['status']}: {j.get('error') or j.get('progress')} "
                    "(continuing with what's already on this Mac)")
        except RuntimeError as e:
            log(f"  {e} (continuing)")

    recs = [r for r in recordings() if r.get("on_mac") and r.get("has_audio")
            and (r.get("seconds") or 0) >= MIN_SECONDS]
    to_tx = [r["id"] for r in recs if not r["transcribed"]]
    if to_tx:
        log(f"Transcribing {len(to_tx)} recording(s)...")
        added = call("POST", "/api/jobs", {"kind": "transcribe", "ids": to_tx})["jobs"]
        report(wait([j["id"] for j in added], "transcribe"))

    if not args.no_summary:
        recs = {r["id"]: r for r in recordings()}
        to_sum = [i for i, r in recs.items()
                  if r.get("on_mac") and r.get("transcribed") and not r.get("summarized")
                  and "inbox" not in load_meta(i)]
        if to_sum:
            model = args.model or call("GET", "/api/models").get("default")
            if not model:
                log("No LM Studio model available; skipping summaries.")
            else:
                log(f"Summarizing {len(to_sum)} recording(s) with {model}...")
                added = call("POST", "/api/jobs", {"kind": "summarize", "ids": to_sum,
                                                   "options": {"model": model}})["jobs"]
                report(wait([j["id"] for j in added], "summarize"))

    print(json.dumps({"pending": pending_list()}, indent=1))


def report(jobs: dict) -> None:
    for j in jobs.values():
        if j["status"] != "done":
            log(f"  {j['kind']} {', '.join(j['recs'])}: {j['status']} "
                f"{j.get('error') or j.get('progress') or ''}")


def cmd_pending(args) -> None:
    print(json.dumps({"pending": pending_list()}, indent=1))


def cmd_mark(args) -> None:
    rec_id = args.id.removesuffix(".mp3")
    if not (REC_DIR / f"{rec_id}.txt").exists():
        sys.exit(f"no transcript for {rec_id}")
    meta = load_meta(rec_id)
    inbox = meta.get("inbox", {})
    inbox["reviewed"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    inbox["issues"] = sorted(set(inbox.get("issues", []) + (args.issue or [])))
    if args.note:
        inbox["note"] = args.note
    meta["inbox"] = inbox
    save_meta(rec_id, meta)
    print(json.dumps({rec_id: inbox}, indent=1))


def cmd_baseline(args) -> None:
    n = 0
    for e in pending_list():
        meta = load_meta(e["id"])
        meta["inbox"] = {"reviewed": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "issues": [], "note": "baseline"}
        save_meta(e["id"], meta)
        n += 1
    print(f"marked {n} recording(s) as reviewed")


def cmd_projects(args) -> None:
    if not PROJECTS.exists():
        sys.exit(f"{PROJECTS} not found; copy docs/projects.example.toml there and edit it")
    print(json.dumps(tomllib.loads(PROJECTS.read_text()), indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="sync, transcribe, summarize; list unreviewed")
    r.add_argument("--no-sync", action="store_true")
    r.add_argument("--usb", action="store_true")
    r.add_argument("--no-summary", action="store_true")
    r.add_argument("--model", help="LM Studio model (default: the app's)")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("pending", help="list unreviewed recordings").set_defaults(fn=cmd_pending)
    m = sub.add_parser("mark", help="mark a recording reviewed")
    m.add_argument("id", help="e.g. 2026-11-13/20261113030137")
    m.add_argument("--issue", action="append", help="URL of an issue filed from it")
    m.add_argument("--note")
    m.set_defaults(fn=cmd_mark)
    sub.add_parser("baseline", help="mark all current recordings reviewed").set_defaults(fn=cmd_baseline)
    sub.add_parser("projects", help="print projects.toml as JSON").set_defaults(fn=cmd_projects)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
