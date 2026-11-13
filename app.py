# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.115", "uvicorn>=0.30", "numpy"]
# ///
"""Pocket Recordings: a local web app for browsing, playing, transcribing,
summarizing and managing Pocket recorder recordings.

    uv run app.py            # then open http://localhost:8765

Start it from Terminal (or the terminal tab in the Claude app) so the
Bluetooth sync it launches is allowed to use Bluetooth. It only listens on
this Mac (127.0.0.1).

Long-running work goes through a persistent queue with two lanes that run in
parallel: "gpu" (transcribe, summarize; one at a time) and "device" (sync,
delete from recorder). Each job runs pocket_sync.py or transcribe.py as a
subprocess, so cancelling stops it for real.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path

import numpy as np
import uvicorn
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

HERE = Path(__file__).resolve().parent
REC_DIR = Path(os.environ.get("POCKET_RECORDINGS") or Path.home() / "PocketRecordings")
QUEUE_FILE = REC_DIR / ".queue.json"
VOICES = Path.home() / ".config" / "pocket-sync" / "voices.json"
APP_CFG = Path.home() / ".config" / "pocket-sync" / "app.json"
LMS = Path.home() / ".lmstudio" / "bin" / "lms"
LMSTUDIO = "http://localhost:1234"
UV = shutil.which("uv") or "/opt/homebrew/bin/uv"
BYTES_PER_SECOND = 4000
SHORT_SECONDS = 3
ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}/[A-Za-z0-9_-]+$")
SIDECARS = [".txt", ".srt", ".json", ".speakers.json", ".summary.md", ".summary-moe.md",
            ".meta.json", ".peaks.json"]

app = FastAPI(title="Pocket Recordings")


# ── helpers ─────────────────────────────────────────────────────────

def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.replace(path)


def mp3_for(rec_id: str) -> Path:
    if not ID_RE.match(rec_id):
        raise HTTPException(400, "bad recording id")
    return REC_DIR / f"{rec_id}.mp3"


def side(mp3: Path, ext: str) -> Path:
    return mp3.with_name(mp3.stem + ext)


MIN_SPEAKER_SECONDS = 2  # pyannote sometimes reports a "speaker" with ~no speech


def display_names(spk: dict | None) -> dict:
    if not spk:
        return {}
    return {lab: s.get("name") or lab for lab, s in spk.get("speakers", {}).items()}


def real_speakers(spk: dict | None) -> dict:
    if not spk:
        return {}
    return {lab: s for lab, s in spk.get("speakers", {}).items()
            if s.get("seconds", 0) >= MIN_SPEAKER_SECONDS or (s.get("manual") and s["seconds"] > 0)}


def app_cfg() -> dict:
    return read_json(APP_CFG, {})


# ── recordings ─────────────────────────────────────────────────────

def summarize_rec(mp3: Path, device: set[str], manifest: dict) -> dict:
    """A recording on this Mac: with its audio, or transcript-only after the
    audio file was deleted to save space."""
    rec_id = f"{mp3.parent.name}/{mp3.stem}"
    has_audio = mp3.exists()
    meta = read_json(side(mp3, ".meta.json"), {})
    size = mp3.stat().st_size if has_audio else None
    seconds = round(size / BYTES_PER_SECOND, 1) if has_audio else meta.get("audio_seconds")
    spk = read_json(side(mp3, ".speakers.json"), None)
    names = sorted({s.get("name") or lab for lab, s in real_speakers(spk).items()})
    return {
        "id": rec_id, "date": mp3.parent.name, "name": mp3.stem,
        "title": meta.get("title"), "title_source": meta.get("title_source"),
        "seconds": seconds, "bytes": size,
        "has_audio": has_audio,
        "transcribed": side(mp3, ".txt").exists(),
        "speakers_done": spk is not None,
        "summarized": side(mp3, ".summary.md").exists(),
        "speakers": names,
        "on_mac": True,
        "on_device": f"{rec_id}.mp3" in device,
    }


def local_recordings() -> list[Path]:
    """mp3 paths (existing or not) for every recording kept on this Mac,
    including transcript-only ones whose audio was deleted."""
    found = {m for m in REC_DIR.glob("*/*.mp3")}
    for j in REC_DIR.glob("*/*.json"):
        if j.name.count(".") == 1:  # <name>.json segments, not .speakers.json etc.
            found.add(j.with_suffix(".mp3"))
    return sorted(found)


@app.get("/api/recordings")
def list_recordings(q: str = "", short: bool = False):
    dev = read_json(REC_DIR / ".device.json", {})
    device = set(dev.get("recordings", []))
    manifest = read_json(REC_DIR / ".pocket-sync.json", {})
    recs = [summarize_rec(m, device, manifest) for m in local_recordings()]
    on_mac = {f"{r['id']}.mp3" for r in recs}
    for rel in device - on_mac:  # on the recorder but not downloaded
        date, name = rel.removesuffix(".mp3").split("/")
        entry = manifest.get(rel, {})
        recs.append({"id": f"{date}/{name}", "date": date, "name": name, "title": None,
                     "seconds": None, "on_mac": False, "on_device": True,
                     "deleted_locally": bool(entry.get("deleted_locally")),
                     "transcribed": False, "speakers_done": False, "summarized": False,
                     "speakers": []})
    if not short:
        recs = [r for r in recs if r["seconds"] is None or r["seconds"] >= SHORT_SECONDS]
    if q:
        ql = q.lower()

        def hit(r):
            if ql in (r.get("title") or "").lower() or ql in r["id"].lower():
                return True
            if any(ql in s.lower() for s in r["speakers"]):
                return True
            txt = REC_DIR / f"{r['id']}.txt"
            return txt.exists() and ql in txt.read_text().lower()
        recs = [r for r in recs if hit(r)]
    recs.sort(key=lambda r: r["id"], reverse=True)
    return {"recordings": recs, "device_updated": dev.get("updated")}


@app.get("/api/recordings/{date}/{name}")
def get_recording(date: str, name: str):
    rec_id = f"{date}/{name}"
    mp3 = mp3_for(rec_id)
    if not mp3.exists() and not side(mp3, ".json").exists():
        raise HTTPException(404, "not on this Mac")
    dev = set(read_json(REC_DIR / ".device.json", {}).get("recordings", []))
    info = summarize_rec(mp3, dev, {})
    data = read_json(side(mp3, ".json"), {})
    spk = read_json(side(mp3, ".speakers.json"), None)
    names = display_names(spk)
    segs = [{"start": s["start"], "end": s["end"], "text": s["text"].strip(),
             "speaker": names.get(s.get("speaker"), s.get("speaker")),
             "label": s.get("speaker"), "moved": bool(s.get("moved"))}
            for s in data.get("segments", [])]
    speakers = []
    if spk:
        for lab, s in real_speakers(spk).items():
            mine = [x for x in data.get("segments", []) if x.get("speaker") == lab]
            clips = sorted(mine, key=lambda x: x["end"] - x["start"], reverse=True)[:3]
            speakers.append({
                "label": lab, "name": s.get("name"), "pinned": bool(s.get("pinned")),
                "score": s.get("score"), "seconds": s["seconds"],
                "clips": [{"start": c["start"], "end": c["end"], "text": c["text"].strip()}
                          for c in sorted(clips, key=lambda c: c["start"])],
            })
    summary = side(mp3, ".summary.md")
    info.update(segments=segs, speaker_details=speakers,
                summary=summary.read_text() if summary.exists() else None)
    return info


@app.get("/api/recordings/{date}/{name}/peaks")
def peaks(date: str, name: str, n: int = 1200):
    mp3 = mp3_for(f"{date}/{name}")
    cache = side(mp3, ".peaks.json")
    cached = read_json(cache, None)
    if cached and cached.get("n") == n:
        return cached
    import tempfile
    import wave
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "a.wav"
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@8000", "-c", "1",
                        str(mp3), str(wav)], check=True, capture_output=True)
        with wave.open(str(wav)) as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    a = np.abs(pcm.astype(np.float32)) / 32768.0
    if len(a) == 0:
        vals = []
    else:
        buckets = np.array_split(a, min(n, len(a)))
        vals = [float(b.max()) for b in buckets]
        top = max(max(vals), 1e-6)
        vals = [round(v / top, 3) for v in vals]
    out = {"n": n, "peaks": vals}
    write_json(cache, out)
    return out


@app.get("/audio/{date}/{name}.mp3")
def audio(date: str, name: str):
    mp3 = mp3_for(f"{date}/{name}")
    if not mp3.exists():
        raise HTTPException(404)
    return FileResponse(mp3, media_type="audio/mpeg")


@app.put("/api/recordings/{date}/{name}/title")
def set_title(date: str, name: str, body: dict = Body(...)):
    mp3 = mp3_for(f"{date}/{name}")
    meta_file = side(mp3, ".meta.json")
    meta = read_json(meta_file, {})
    title = (body.get("title") or "").strip()[:200]
    if title:
        meta.update(title=title, title_source="user")
    else:
        meta.pop("title", None)
        meta.pop("title_source", None)
    write_json(meta_file, meta)
    return {"title": meta.get("title")}


def run_cli(args: list[str], timeout: int = 120) -> str:
    r = subprocess.run([UV, "run", *args], cwd=HERE, capture_output=True, text=True,
                       timeout=timeout, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    if r.returncode != 0:
        msg = (r.stderr.strip().splitlines() or r.stdout.strip().splitlines() or ["failed"])[-1]
        raise HTTPException(500, msg)
    return r.stdout


@app.post("/api/recordings/{date}/{name}/speakers")
def name_speaker(date: str, name: str, body: dict = Body(...)):
    rec_id = f"{date}/{name}"
    mp3_for(rec_id)
    label, person = body.get("label", ""), (body.get("name") or "").strip()
    if not label or not person or len(person) > 80:
        raise HTTPException(400, "label and name required")
    run_cli(["transcribe.py", "--name", str(REC_DIR / f"{rec_id}.mp3"), label, person])
    out = run_cli(["transcribe.py", "--rematch", "--dir", str(REC_DIR)])
    return {"ok": True, "rematch": out.strip().splitlines()}


@app.post("/api/recordings/{date}/{name}/move")
def move_lines(date: str, name: str, body: dict = Body(...)):
    """Reassign transcript lines (segment indexes, "5" or "5-9") to a speaker."""
    rec_id = f"{date}/{name}"
    mp3_for(rec_id)
    lines, person = str(body.get("lines", "")), (body.get("name") or "").strip()
    if not re.fullmatch(r"\d+(-\d+)?", lines) or not person or len(person) > 80:
        raise HTTPException(400, "lines and name required")
    out = run_cli(["transcribe.py", "--move", str(REC_DIR / f"{rec_id}.mp3"), lines, person])
    return {"ok": True, "message": out.strip()}


@app.get("/api/voices")
def voices():
    v = read_json(VOICES, {})
    return {"voices": [{"name": k, "samples": x.get("samples", 1),
                        "minutes": round(x.get("weight", 0) / 60, 1)} for k, x in sorted(v.items())]}


@app.delete("/api/voices/{person}")
def forget_voice(person: str):
    run_cli(["transcribe.py", "--forget", person])
    run_cli(["transcribe.py", "--rematch", "--dir", str(REC_DIR)])
    return {"ok": True}


@app.post("/api/delete")
def delete(body: dict = Body(...)):
    """Mac copies go to the Trash; recorder deletions are queued as a device job."""
    ids = body.get("ids") or []
    for rec_id in ids:
        mp3_for(rec_id)
    trashed = audio_removed = 0
    skipped = []
    if body.get("mac") or body.get("audio_only"):
        manifest_file = REC_DIR / ".pocket-sync.json"
        manifest = read_json(manifest_file, {})
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for rec_id in ids:
            mp3 = mp3_for(rec_id)
            if body.get("mac"):
                files = [f for f in [mp3, *(side(mp3, e) for e in SIDECARS)] if f.exists()]
            else:
                # Audio only: keep transcript, summary, speakers and title.
                if not side(mp3, ".json").exists():
                    skipped.append(rec_id)  # no transcript: would lose everything
                    continue
                files = [f for f in (mp3, side(mp3, ".peaks.json")) if f.exists()]
                if mp3.exists():
                    meta_file = side(mp3, ".meta.json")
                    meta = read_json(meta_file, {})
                    meta.update(audio_seconds=round(mp3.stat().st_size / BYTES_PER_SECOND, 1),
                                audio_removed=time.strftime("%Y-%m-%dT%H:%M:%S"))
                    write_json(meta_file, meta)
            if not files:
                continue
            dest = Path.home() / ".Trash" / f"Pocket {rec_id.replace('/', ' ')} {stamp}"
            dest.mkdir(parents=True, exist_ok=True)
            for f in files:
                shutil.move(str(f), dest / f.name)
            # Remember it was deleted on purpose so sync doesn't download it again.
            manifest.setdefault(f"{rec_id}.mp3", {})["deleted_locally"] = True
            if body.get("mac"):
                trashed += 1
            else:
                audio_removed += 1
        write_json(manifest_file, manifest)
    job = None
    if body.get("device") and ids:
        job = queue.add("delete_device", ids)
    return {"trashed": trashed, "audio_removed": audio_removed, "skipped": skipped, "job": job}


# ── LM Studio ──────────────────────────────────────────────────────

def lm_models() -> list[dict]:
    with urllib.request.urlopen(f"{LMSTUDIO}/api/v0/models", timeout=5) as r:
        data = json.load(r)["data"]
    return [{"id": m["id"], "state": m.get("state"), "arch": m.get("arch")}
            for m in data if m.get("type") in ("llm", "vlm")]


@app.get("/api/models")
def models():
    try:
        ms = lm_models()
    except Exception as e:
        return {"models": [], "error": f"LM Studio not reachable: {e}", "default": None}
    ids = [m["id"] for m in ms]
    default = app_cfg().get("summary_model")
    if default not in ids:
        default = next((i for i in ids if "a3b" in i.lower()), None) or \
            next((m["id"] for m in ms if m["state"] == "loaded"), ids[0] if ids else None)
    return {"models": ms, "default": default}


@app.put("/api/settings")
def settings(body: dict = Body(...)):
    cfg = app_cfg()
    if "summary_model" in body:
        cfg["summary_model"] = body["summary_model"]
    write_json(APP_CFG, cfg)
    return cfg


def ensure_model_loaded(model: str, job: dict) -> None:
    """Load the summary model in LM Studio, unloading other LLMs to make room."""
    ms = lm_models()
    if any(m["id"] == model and m["state"] == "loaded" for m in ms):
        return
    for m in ms:
        if m["state"] == "loaded":
            job["progress"] = f"Unloading {m['id']} in LM Studio"
            subprocess.run([str(LMS), "unload", m["id"]], capture_output=True, timeout=120)
    job["progress"] = f"Loading {model} in LM Studio"
    r = subprocess.run([str(LMS), "load", model, "--context-length", "32768", "-y"],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"Could not load {model}: {(r.stderr or r.stdout).strip()[-200:]}")


# ── job queue ──────────────────────────────────────────────────────

LANES = {"transcribe": "gpu", "summarize": "gpu",
         "sync_ble": "device", "sync_usb": "device", "delete_device": "device"}
NOISE = re.compile(r"warn|Warning|^\s*$|torchaudio|UserWarning|deprecat|^\s+\w+\(|Installed \d+ packages")


class Queue:
    def __init__(self):
        self.lock = threading.RLock()
        self.jobs: list[dict] = read_json(QUEUE_FILE, [])
        for j in self.jobs:  # a restart interrupts whatever was running
            if j["status"] == "running":
                j.update(status="pending", progress="Restarted after the app was closed")
        self.procs: dict[str, subprocess.Popen] = {}
        self.wake = threading.Event()
        self.caffeinate: subprocess.Popen | None = None
        self.stopping = False
        self.save()

    def save(self):
        with self.lock:
            write_json(QUEUE_FILE, self.jobs)

    def add(self, kind: str, recs: list[str], options: dict | None = None) -> dict:
        job = {"id": uuid.uuid4().hex[:10], "kind": kind, "lane": LANES[kind],
               "recs": recs, "options": options or {}, "status": "pending",
               "created": time.time(), "started": None, "finished": None,
               "progress": "", "log": [], "estimate": estimate(kind, recs, options or {})}
        with self.lock:
            self.jobs.append(job)
            self.save()
        self.wake.set()
        return job

    def find(self, job_id: str) -> dict:
        for j in self.jobs:
            if j["id"] == job_id:
                return j
        raise HTTPException(404, "no such job")

    def cancel(self, job_id: str):
        with self.lock:
            j = self.find(job_id)
            if j["status"] == "pending":
                j.update(status="cancelled", finished=time.time())
            elif j["status"] == "running":
                j["cancel"] = True
                p = self.procs.get(job_id)
                if p:
                    try:
                        os.killpg(p.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            self.save()

    def move(self, job_id: str, direction: int):
        with self.lock:
            j = self.find(job_id)
            if j["status"] != "pending":
                return
            same = [x for x in self.jobs if x["status"] == "pending" and x["lane"] == j["lane"]]
            i = same.index(j)
            k = i + direction
            if 0 <= k < len(same):
                a, b = self.jobs.index(same[i]), self.jobs.index(same[k])
                self.jobs[a], self.jobs[b] = self.jobs[b], self.jobs[a]
            self.save()

    def clear_finished(self):
        with self.lock:
            self.jobs = [j for j in self.jobs if j["status"] in ("pending", "running")]
            self.save()

    def next_for(self, lane: str) -> dict | None:
        with self.lock:
            if any(j["status"] == "running" and j["lane"] == lane for j in self.jobs):
                return None
            for j in self.jobs:
                if j["status"] == "pending" and j["lane"] == lane:
                    j.update(status="running", started=time.time(), progress="Starting")
                    self.save()
                    return j
        return None

    def update_caffeinate(self):
        busy = any(j["status"] in ("pending", "running") for j in self.jobs)
        if busy and (self.caffeinate is None or self.caffeinate.poll() is not None):
            # Keep the Mac from idle-sleeping while there is queued work.
            self.caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        elif not busy and self.caffeinate and self.caffeinate.poll() is None:
            self.caffeinate.terminate()
            self.caffeinate = None

    def worker(self, lane: str):
        while True:
            job = self.next_for(lane)
            self.update_caffeinate()
            if job is None:
                self.wake.wait(2)
                self.wake.clear()
                continue
            try:
                run_job(self, job)
                if self.stopping:
                    continue  # killed by shutdown: stays pending for next launch
                if job.get("cancel"):
                    job.update(status="cancelled")
                elif job["status"] == "running":
                    job["status"] = "done"
            except Exception as e:
                if self.stopping:
                    continue
                job.update(status="cancelled" if job.get("cancel") else "failed",
                           error=str(e)[-500:])
            job["finished"] = time.time()
            self.procs.pop(job["id"], None)
            self.save()


def estimate(kind: str, recs: list[str], options: dict) -> float | None:
    """Rough seconds, from this Mac's measurements (M4 Pro)."""
    secs = 0.0
    for rec_id in recs:
        mp3 = REC_DIR / f"{rec_id}.mp3"
        if mp3.exists():
            dur = mp3.stat().st_size / BYTES_PER_SECOND
        else:
            dur = read_json(side(mp3, ".meta.json"), {}).get("audio_seconds") or 0
        transcribed = side(mp3, ".txt").exists()
        if kind == "transcribe" or (kind == "summarize" and not transcribed):
            secs += 15 + dur / 40 + dur / 25          # whisper + speakers + model load
        if kind == "summarize":
            fast = "a3b" in (options.get("model") or "").lower()
            secs += 10 + dur * (0.02 if fast else 0.1)
    return round(secs) if secs else None


def run_job(q: Queue, job: dict) -> None:
    kind, recs = job["kind"], job["recs"]
    if kind == "transcribe":
        cmd = ["transcribe.py", "--force", *[str(REC_DIR / f"{r}.mp3") for r in recs]]
    elif kind == "summarize":
        model = job["options"].get("model")
        if not model:
            raise RuntimeError("No summary model chosen")
        ensure_model_loaded(model, job)
        cmd = ["transcribe.py", "--summary-only", "--force", "--model", model,
               *[str(REC_DIR / f"{r}.mp3") for r in recs]]
    elif kind == "sync_ble":
        cmd = ["pocket_sync.py", "pull"]
    elif kind == "sync_usb":
        cmd = ["pocket_sync.py", "pull", "--usb"]
    elif kind == "delete_device":
        cmd = ["pocket_sync.py", "delete", *recs]
    else:
        raise RuntimeError(f"unknown job kind {kind}")

    p = subprocess.Popen([UV, "run", *cmd], cwd=HERE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, start_new_session=True,
                         env={**os.environ, "PYTHONUNBUFFERED": "1"})
    q.procs[job["id"]] = p
    buf = b""
    while True:
        chunk = p.stdout.read1(4096) if hasattr(p.stdout, "read1") else p.stdout.read(1)
        if not chunk:
            break
        buf += chunk
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for raw in parts:
            line = raw.decode(errors="replace").rstrip()
            if NOISE.search(line):
                continue
            job["progress"] = line.strip()
            if not re.search(r"\d+%\s", line):  # keep percentage redraws out of the log
                job["log"] = (job["log"] + [line])[-60:]
        if buf and not NOISE.search(buf.decode(errors="replace")):
            job["progress"] = buf.decode(errors="replace").strip()
    rc = p.wait()
    if rc != 0 and not job.get("cancel"):
        last = next((l for l in reversed(job["log"]) if l.strip()), f"exit code {rc}")
        raise RuntimeError(last)


queue = Queue()


@app.get("/api/jobs")
def jobs():
    with queue.lock:
        now = time.time()
        out = []
        for j in queue.jobs:
            x = {k: v for k, v in j.items() if k != "log"}
            x["log_tail"] = j["log"][-8:]
            if j["status"] == "running" and j["started"]:
                x["elapsed"] = round(now - j["started"])
            out.append(x)
    pending = [j for j in out if j["status"] in ("pending", "running")]
    gpu_left = sum(max((j.get("estimate") or 0) - j.get("elapsed", 0), 0)
                   for j in pending if j["lane"] == "gpu")
    return {"jobs": out, "gpu_seconds_left": gpu_left}


@app.post("/api/jobs")
def add_jobs(body: dict = Body(...)):
    kind = body.get("kind")
    ids = body.get("ids") or []
    options = body.get("options") or {}
    if kind not in ("transcribe", "summarize", "sync_ble", "sync_usb"):
        raise HTTPException(400, "bad job kind")
    if kind.startswith("sync"):
        if any(j["kind"].startswith("sync") and j["status"] in ("pending", "running")
               for j in queue.jobs):
            raise HTTPException(409, "A sync is already queued")
        return {"jobs": [queue.add(kind, [])]}
    added = []
    for rec_id in ids:
        mp3 = mp3_for(rec_id)
        if not mp3.exists() and not (kind == "summarize" and side(mp3, ".txt").exists()):
            continue  # transcribing needs the audio; summarizing only the transcript
        if any(j["kind"] == kind and rec_id in j["recs"] and j["status"] in ("pending", "running")
               for j in queue.jobs):
            continue  # already queued
        added.append(queue.add(kind, [rec_id], options))
    return {"jobs": added}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    queue.cancel(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/move")
def move_job(job_id: str, body: dict = Body(...)):
    queue.move(job_id, -1 if body.get("direction") == "up" else 1)
    return {"ok": True}


@app.post("/api/jobs/clear")
def clear_jobs():
    queue.clear_finished()
    return {"ok": True}


@app.post("/api/shutdown")
def shutdown():
    """Stop the server (the launcher app runs it with no window). A running
    job is stopped and goes back to pending, so it restarts on next launch."""
    def stop():
        time.sleep(0.5)
        with queue.lock:
            queue.stopping = True
            for job_id, p in list(queue.procs.items()):
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            for j in queue.jobs:
                if j["status"] == "running":
                    j.update(status="pending", progress="Stopped when the app quit")
            queue.save()
        if queue.caffeinate and queue.caffeinate.poll() is None:
            queue.caffeinate.terminate()
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=stop, daemon=True).start()
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index():
    # no-store: always fetch the current page, so app updates show after a reload
    return HTMLResponse((HERE / "static" / "index.html").read_text(),
                        headers={"Cache-Control": "no-store"})


def main():
    REC_DIR.mkdir(exist_ok=True)
    for lane in ("gpu", "device"):
        threading.Thread(target=queue.worker, args=(lane,), daemon=True).start()
    port = int(os.environ.get("POCKET_APP_PORT", "8765"))
    print(f"Pocket Recordings: http://localhost:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
