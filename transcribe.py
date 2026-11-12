# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = ["mlx-whisper>=0.4", "numpy", "pyannote.audio>=4.0"]
# ///
"""Transcribe Pocket recordings locally, label speakers, optionally summarize.

Speech-to-text: the MLX Whisper model in LM Studio's model folder.
Speakers: pyannote speaker-diarization-community-1 (Hugging Face login and
accepted model terms needed once, for the download). Voiceprints of people
you name are kept in ~/.config/pocket-sync/voices.json and recognized in
later recordings. Summaries: the LLM loaded in LM Studio. Nothing leaves
this Mac.

    uv run transcribe.py                       # every recording without a transcript
    uv run transcribe.py --largest             # just the longest untranscribed one
    uv run transcribe.py FILE.mp3 ...          # specific files
    uv run transcribe.py --summarize           # also write <name>.summary.md

    uv run transcribe.py --speakers REC        # who spoke, how long, best voice matches
    uv run transcribe.py --name REC "Speaker B" "Alex"   # teach a voice
    uv run transcribe.py --forget "Alex"       # delete a voiceprint
    uv run transcribe.py --rematch             # relabel all transcripts with current voices

REC is a recording name (20261105150712) or a path to its .mp3.
Outputs next to each MP3: .txt (speaker-labelled transcript), .srt, .json
(segments), .speakers.json (diarization and per-speaker voiceprints).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

REC_DIR = Path.home() / "PocketRecordings"
WHISPER = Path.home() / ".lmstudio/models/mlx-community/whisper-large-v3-turbo"
DIARIZER = "pyannote/speaker-diarization-community-1"
VOICES = Path.home() / ".config" / "pocket-sync" / "voices.json"
LMSTUDIO = "http://localhost:1234/v1"
SR = 16000

# Cosine similarity between a speaker's voiceprint and a named voice needed to
# call it a match. Tune with --threshold; --speakers shows the scores. Measured
# on this recorder: same person across recordings 0.83-0.91, different people
# 0.11-0.56 (highest for two people in the same room).
MATCH_THRESHOLD = 0.70


# ── audio ───────────────────────────────────────────────────────────

def load_audio(mp3: Path) -> np.ndarray:
    """Decode to 16 kHz mono float32 with macOS's afconvert (no ffmpeg needed)."""
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "a.wav"
        subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{SR}", "-c", "1",
                        str(mp3), str(wav)], check=True, capture_output=True)
        with wave.open(str(wav)) as w:
            pcm = w.readframes(w.getnframes())
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def ts(sec: float, srt: bool = False) -> str:
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if srt:
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s % 1) * 1000):03d}"
    return f"{int(h)}:{int(m):02d}:{int(s):02d}"


# ── transcription ──────────────────────────────────────────────────

def transcribe(audio: np.ndarray, language: str | None) -> dict:
    import mlx_whisper
    return mlx_whisper.transcribe(
        audio,
        path_or_hf_repo=str(WHISPER),
        language=language,
        condition_on_previous_text=False,  # fewer runaway repetitions on long audio
        verbose=None,
    )


# ── diarization and voices ─────────────────────────────────────────

_pipeline = None


def diarizer():
    global _pipeline
    if _pipeline is None:
        import torch
        from pyannote.audio import Pipeline
        try:
            _pipeline = Pipeline.from_pretrained(DIARIZER)
        except Exception as e:
            sys.exit(
                f"Could not load {DIARIZER}: {type(e).__name__}: {e}\n"
                f"Accept its terms at https://huggingface.co/{DIARIZER} and run:\n"
                "  uvx --from huggingface_hub hf auth login"
            )
        if _pipeline is None:
            sys.exit(f"Could not load {DIARIZER}; check your Hugging Face login and access.")
        if torch.backends.mps.is_available():
            _pipeline.to(torch.device("mps"))
    return _pipeline


def unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def diarize(audio: np.ndarray, num_speakers: int | None) -> dict:
    """Speaker turns (non-overlapping) and one voiceprint per speaker."""
    import torch
    kwargs = {"num_speakers": num_speakers} if num_speakers else {}
    file = {"waveform": torch.from_numpy(audio)[None], "sample_rate": SR}
    try:
        out = diarizer()(file, **kwargs)
    except Exception as e:  # rare MPS kernel gaps: retry on CPU
        print(f" (GPU diarization failed: {type(e).__name__}; retrying on CPU)", end="", flush=True)
        diarizer().to(torch.device("cpu"))
        out = diarizer()(file, **kwargs)
    labels = out.speaker_diarization.labels()
    letters = {lab: f"Speaker {chr(65 + i)}" for i, lab in enumerate(labels)}
    turns = [{"start": round(t.start, 3), "end": round(t.end, 3), "speaker": letters[s]}
             for t, _, s in out.exclusive_speaker_diarization.itertracks(yield_label=True)]
    speakers = {}
    for i, lab in enumerate(labels):
        name = letters[lab]
        secs = sum(t["end"] - t["start"] for t in turns if t["speaker"] == name)
        emb = out.speaker_embeddings[i] if out.speaker_embeddings is not None else None
        ok = emb is not None and np.isfinite(emb).all() and np.linalg.norm(emb) > 0
        speakers[name] = {"seconds": round(secs, 1),
                          "embedding": unit(emb).round(5).tolist() if ok else None}
    return {"model": DIARIZER, "turns": turns, "speakers": speakers}


def load_voices() -> dict:
    try:
        return json.loads(VOICES.read_text())
    except FileNotFoundError:
        return {}


def save_voices(voices: dict) -> None:
    VOICES.parent.mkdir(parents=True, exist_ok=True)
    tmp = VOICES.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(voices, f, indent=1)
    tmp.replace(VOICES)


def scores(spk: dict, voices: dict) -> list[tuple[float, str]]:
    if not spk.get("embedding"):
        return []
    e = unit(spk["embedding"])
    return sorted(((float(e @ unit(v["embedding"])), name) for name, v in voices.items()),
                  reverse=True)


def match(diar: dict, voices: dict, threshold: float) -> dict:
    """Assign named voices to this recording's speakers, best matches first,
    each voice at most once. Manual names (pinned) are kept as they are."""
    pairs = []
    for label, spk in diar["speakers"].items():
        if spk.get("pinned"):
            continue
        spk["name"], spk["score"] = None, None
        pairs += [(s, label, name) for s, name in scores(spk, voices)]
    taken = {s["name"] for s in diar["speakers"].values() if s.get("pinned")}
    done = set()
    for s, label, name in sorted(pairs, reverse=True):
        if s < threshold or label in done or name in taken:
            continue
        diar["speakers"][label].update(name=name, score=round(s, 3))
        done.add(label)
        taken.add(name)
    return diar


def display_names(diar: dict | None) -> dict:
    if not diar:
        return {}
    return {label: s.get("name") or label for label, s in diar["speakers"].items()}


def speaker_for(seg: dict, turns: list[dict]) -> str | None:
    """The speaker with the most overlap with a segment, else the nearest turn."""
    best, overlap = None, 0.0
    for t in turns:
        o = min(seg["end"], t["end"]) - max(seg["start"], t["start"])
        if o > overlap:
            best, overlap = t["speaker"], o
    if best is None and turns:
        mid = (seg["start"] + seg["end"]) / 2
        t = min(turns, key=lambda t: min(abs(mid - t["start"]), abs(mid - t["end"])))
        if min(abs(mid - t["start"]), abs(mid - t["end"])) < 1.5:
            best = t["speaker"]
    return best


# ── outputs ─────────────────────────────────────────────────────────

def paths(mp3: Path) -> dict:
    return {k: mp3.with_suffix(ext) for k, ext in
            [("txt", ".txt"), ("srt", ".srt"), ("json", ".json"),
             ("spk", ".speakers.json"), ("sum", ".summary.md")]}


def render(mp3: Path) -> Path:
    """(Re)write .txt and .srt from .json segments and .speakers.json names."""
    p = paths(mp3)
    data = json.loads(p["json"].read_text())
    diar = json.loads(p["spk"].read_text()) if p["spk"].exists() else None
    names = display_names(diar)
    segs = data["segments"]

    lines, cur, start, buf = [], None, 0.0, []
    for s in segs:
        who = names.get(s.get("speaker"), "Unknown") if diar else None
        if who != cur and buf:
            lines.append(f"[{ts(start)}] " + (f"{cur}: " if cur else "") + " ".join(buf))
            buf = []
        if not buf:
            cur, start = who, s["start"]
        buf.append(s["text"].strip())
        if not diar:  # without speakers, one line per segment
            lines.append(f"[{ts(start)}] " + " ".join(buf))
            buf = []
    if buf:
        lines.append(f"[{ts(start)}] " + (f"{cur}: " if cur else "") + " ".join(buf))
    p["txt"].write_text("\n".join(lines) + "\n")

    p["srt"].write_text("".join(
        f"{i}\n{ts(s['start'], True)} --> {ts(s['end'], True)}\n"
        + (f"{names.get(s.get('speaker'), 'Unknown')}: " if diar else "")
        + f"{s['text'].strip()}\n\n"
        for i, s in enumerate(segs, 1)))
    return p["txt"]


# ── summary ─────────────────────────────────────────────────────────

def lmstudio_model() -> str:
    with urllib.request.urlopen("http://localhost:1234/api/v0/models", timeout=5) as r:
        models = json.load(r)["data"]
    loaded = [m["id"] for m in models
              if m.get("state") == "loaded" and m.get("type") in ("llm", "vlm")]
    if not loaded:
        sys.exit("No LLM is loaded in LM Studio; load one or pass --model.")
    return loaded[0]


def summarize(txt: Path, model: str, think: bool = False) -> Path:
    transcript = txt.read_text()
    prompt = (
        "Below is a transcript of a voice recording made with a pocket recorder. "
        "Lines are labelled with the speaker where known ('Speaker A' etc. are "
        "unidentified people; speaker labels can occasionally be wrong on very short "
        "replies). Write a concise Markdown summary with these sections: **Summary** "
        "(3-6 sentences), **Key points** (bullets), **Action items** (bullets with "
        "owner if stated; 'None' if none), **Notable quotes** (up to 3, with speaker "
        "and timestamp). Use only what is in the transcript; do not add concepts "
        "that were not said.\n\n"
        f"Transcript:\n{transcript}"
    )
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        # Reasoning made a 25-min summary take 8.5 min on a 27B model; a
        # summary doesn't need it. Use --think to turn it back on.
        **({} if think else {"reasoning_effort": "none"}),
    }).encode()
    req = urllib.request.Request(f"{LMSTUDIO}/chat/completions", body,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        text = json.load(r)["choices"][0]["message"]["content"]
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.S).strip()
    out = txt.with_suffix(".summary.md")
    out.write_text(f"# {txt.stem}\n\n_Summarized locally with {model}_\n\n{text}\n")
    return out


def chat(model: str, prompt: str, timeout: int = 300) -> str:
    body = json.dumps({"model": model, "temperature": 0.3, "reasoning_effort": "none",
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(f"{LMSTUDIO}/chat/completions", body,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        text = json.load(r)["choices"][0]["message"]["content"]
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.S).strip()


def meta_path(mp3: Path) -> Path:
    return mp3.with_suffix(".meta.json")


def load_meta(mp3: Path) -> dict:
    try:
        return json.loads(meta_path(mp3).read_text())
    except FileNotFoundError:
        return {}


def save_meta(mp3: Path, meta: dict) -> None:
    meta_path(mp3).write_text(json.dumps(meta, indent=1, ensure_ascii=False))


def suggest_title(mp3: Path, summary: Path, model: str) -> str | None:
    """Give the recording an LLM title unless the user has set one."""
    meta = load_meta(mp3)
    if meta.get("title_source") == "user":
        return None
    title = chat(model, "Give a short, specific title (at most 8 words) for the "
                        "recording summarized below. Reply with the title only, no quotes.\n\n"
                 + summary.read_text())
    title = title.splitlines()[0].strip().strip('"*#').strip()[:100] if title else ""
    if title:
        meta.update(title=title, title_source="llm")
        save_meta(mp3, meta)
    return title or None


# ── commands ────────────────────────────────────────────────────────

def find_recording(ref: str, rec_dir: Path) -> Path:
    p = Path(ref).expanduser()
    if p.suffix == ".mp3" and p.exists():
        return p
    hits = sorted(rec_dir.glob(f"*/{Path(ref).stem.split('.')[0]}.mp3"))
    if not hits:
        sys.exit(f"No recording named {ref} under {rec_dir}")
    return hits[0]


def process(mp3: Path, args, voices: dict, model: str | None) -> None:
    p = paths(mp3)
    print(f"{mp3.parent.name}/{mp3.name} (~{mp3.stat().st_size / 4000 / 60:.1f} min)", flush=True)
    if args.summary_only and p["txt"].exists() and p["json"].exists():
        return summarize_step(mp3, p["txt"], args, model)
    audio = load_audio(mp3)
    secs = len(audio) / SR

    t0 = time.monotonic()
    print("    transcribing...", end="", flush=True)
    result = transcribe(audio, args.language)
    segs = [{"start": round(s["start"], 2), "end": round(s["end"], 2), "text": s["text"]}
            for s in result["segments"] if s["text"].strip()]
    print(f" {time.monotonic() - t0:.0f}s ({secs / max(time.monotonic() - t0, .1):.0f}x realtime,"
          f" lang={result.get('language')})")

    diar = None
    if not args.no_speakers:
        t0 = time.monotonic()
        print("    finding speakers...", end="", flush=True)
        diar = match(diarize(audio, args.num_speakers), voices, args.threshold)
        for s in segs:
            s["speaker"] = speaker_for(s, diar["turns"])
        p["spk"].write_text(json.dumps(diar, indent=1))
        found = ", ".join(f"{s.get('name') or lab} {s['seconds'] / 60:.1f} min"
                          + (f" (match {s['score']:.2f})" if s.get("score") else "")
                          for lab, s in diar["speakers"].items())
        print(f" {time.monotonic() - t0:.0f}s: {found or 'no speech found'}")

    p["json"].write_text(json.dumps(
        {"language": result.get("language"), "segments": segs}, indent=1, ensure_ascii=False))
    txt = render(mp3)
    print(f"    -> {txt.name}", flush=True)
    summarize_step(mp3, txt, args, model)


def summarize_step(mp3: Path, txt: Path, args, model: str | None) -> None:
    if not model:
        return
    t0 = time.monotonic()
    print(f"    summarizing with {model}...", end="", flush=True)
    out = summarize(txt, model, args.think)
    print(f" {time.monotonic() - t0:.0f}s -> {out.name}", flush=True)
    if not args.no_title:
        title = suggest_title(mp3, out, model)
        if title:
            print(f"    title: {title}", flush=True)


def cmd_speakers(args) -> None:
    mp3 = find_recording(args.speakers, args.dir)
    p = paths(mp3)
    if not p["spk"].exists():
        sys.exit(f"No speaker data for {mp3.name}; transcribe it first (with --force if needed).")
    diar = json.loads(p["spk"].read_text())
    data = json.loads(p["json"].read_text())
    voices = load_voices()
    print(f"{mp3.parent.name}/{mp3.name}")
    for label, s in diar["speakers"].items():
        tag = f" = {s['name']}" + (" (named by you)" if s.get("pinned") else
                                   f" (match {s['score']:.2f})") if s.get("name") else ""
        print(f"\n  {label}{tag}: {s['seconds'] / 60:.1f} min")
        best = scores(s, voices)[:3]
        if best:
            print("    closest voices: " + ", ".join(f"{n} {sc:.2f}" for sc, n in best))
        mine = [x for x in data["segments"] if x.get("speaker") == label]
        for x in sorted(mine, key=lambda x: x["end"] - x["start"], reverse=True)[:3]:
            print(f"    [{ts(x['start'])}] {x['text'].strip()[:90]}")
    print(f"\nName one with: uv run transcribe.py --name {mp3.stem} \"Speaker A\" \"Their Name\"")


def cmd_name(args) -> None:
    ref, label, name = args.name
    mp3 = find_recording(ref, args.dir)
    p = paths(mp3)
    diar = json.loads(p["spk"].read_text())
    label = next((k for k in diar["speakers"] if k.lower() == label.lower()), label)
    if label not in diar["speakers"]:
        sys.exit(f"{label} not in {mp3.name}; it has: {', '.join(diar['speakers'])}")
    spk = diar["speakers"][label]
    if not spk.get("embedding"):
        sys.exit(f"{label} has no usable voiceprint (too little speech).")

    voices = load_voices()
    for other in diar["speakers"].values():  # a name belongs to one speaker per recording
        if other is not spk and other.get("name") == name:
            other.update(name=None, score=None, pinned=False)
    v = voices.get(name)
    w = min(spk["seconds"], 600.0)  # weight by speech time, capped
    if v:
        n = v["weight"]
        v["embedding"] = unit(unit(v["embedding"]) * n + unit(spk["embedding"]) * w).round(5).tolist()
        v["weight"] = n + w
        v["samples"] = v.get("samples", 1) + 1
    else:
        voices[name] = {"embedding": spk["embedding"], "weight": w, "samples": 1}
    voices[name]["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_voices(voices)

    spk.update(name=name, score=None, pinned=True)
    p["spk"].write_text(json.dumps(diar, indent=1))
    render(mp3)
    print(f"{label} in {mp3.stem} is now {name} "
          f"({voices[name]['samples']} sample(s), {voices[name]['weight'] / 60:.1f} min of speech).")
    print("Run --rematch to relabel your other transcripts with this voice.")


def cmd_forget(args) -> None:
    voices = load_voices()
    if voices.pop(args.forget, None) is None:
        sys.exit(f"No voice named {args.forget}. Known: {', '.join(voices) or 'none'}")
    save_voices(voices)
    print(f"Deleted the voiceprint for {args.forget}. Run --rematch to update transcripts.")


def cmd_rematch(args) -> None:
    voices = load_voices()
    for spk_file in sorted(args.dir.glob("*/*.speakers.json")):
        mp3 = spk_file.with_name(spk_file.name.replace(".speakers.json", ".mp3"))
        diar = json.loads(spk_file.read_text())
        for s in diar["speakers"].values():  # drop pins on voices that were deleted
            if s.get("pinned") and s.get("name") not in voices:
                s.update(name=None, pinned=False)
        diar = match(diar, voices, args.threshold)
        spk_file.write_text(json.dumps(diar, indent=1))
        render(mp3)
        names = [s.get("name") or lab for lab, s in diar["speakers"].items()]
        print(f"{mp3.stem}: {', '.join(names)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--dir", type=Path, default=REC_DIR)
    ap.add_argument("--largest", action="store_true", help="only the longest untranscribed file")
    ap.add_argument("--force", action="store_true", help="redo files that have a transcript")
    ap.add_argument("--min-seconds", type=float, default=3, help="skip shorter clips")
    ap.add_argument("--language", help="e.g. en (default: auto-detect)")
    ap.add_argument("--no-speakers", action="store_true", help="skip speaker labelling")
    ap.add_argument("--num-speakers", type=int, help="exact speaker count, if known")
    ap.add_argument("--threshold", type=float, default=MATCH_THRESHOLD,
                    help=f"voice match threshold (default {MATCH_THRESHOLD})")
    ap.add_argument("--summarize", action="store_true", help="summarize with LM Studio's LLM")
    ap.add_argument("--think", action="store_true", help="let the LLM reason first (much slower)")
    ap.add_argument("--summary-only", action="store_true",
                    help="summarize existing transcripts without re-transcribing")
    ap.add_argument("--no-title", action="store_true", help="don't suggest a title")
    ap.add_argument("--model", help="LM Studio model id for summaries (default: the loaded one)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--speakers", metavar="REC", help="show a recording's speakers and matches")
    g.add_argument("--name", nargs=3, metavar=("REC", "LABEL", "NAME"), help="name a speaker")
    g.add_argument("--forget", metavar="NAME", help="delete a saved voiceprint")
    g.add_argument("--rematch", action="store_true", help="relabel all transcripts")
    args = ap.parse_args()

    if args.speakers:
        return cmd_speakers(args)
    if args.name:
        return cmd_name(args)
    if args.forget:
        return cmd_forget(args)
    if args.rematch:
        return cmd_rematch(args)

    if not (WHISPER / "config.json").exists():
        sys.exit(f"Whisper model not found at {WHISPER}")
    files = args.files or sorted(args.dir.glob("*/*.mp3"))
    if args.summary_only:
        args.summarize = True
    if not args.force:
        done = ".summary.md" if args.summary_only else ".txt"
        files = [f for f in files if not f.with_suffix(done).exists()]
    files = [f for f in files if f.stat().st_size >= args.min_seconds * 4000]
    if args.largest and files:
        files = [max(files, key=lambda f: f.stat().st_size)]
    if not files:
        print("Nothing to transcribe.")
        return

    voices = load_voices()
    needs_audio = not args.summary_only or any(not f.with_suffix(".txt").exists() for f in files)
    if needs_audio and not args.no_speakers:
        diarizer()  # fail early on a missing Hugging Face login
    model = (args.model or lmstudio_model()) if args.summarize else None
    for i, mp3 in enumerate(files, 1):
        print(f"[{i}/{len(files)}] ", end="")
        process(mp3, args, voices, model)


if __name__ == "__main__":
    main()
