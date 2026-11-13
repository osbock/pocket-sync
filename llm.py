"""Client for the local LLM server that writes summaries and titles.

Imported by transcribe.py and app.py; standard library only. Works with any
server that speaks the OpenAI Chat Completions API, with extras for:

  LM Studio   http://localhost:1234    lists loaded models; app loads/unloads via `lms`
  Ollama      http://localhost:11434   native API, so the context window can be set
  llama.cpp   http://localhost:8080    `llama-server`; also any other OpenAI-style server

The server is set by "llm_url" in ~/.config/pocket-sync/app.json (or
transcribe.py --llm-url) and its kind is detected automatically.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

APP_CFG = Path.home() / ".config" / "pocket-sync" / "app.json"
DEFAULT_URL = "http://localhost:1234"
CONTEXT = 32768          # tokens; a 25-minute transcript is about 6k
NAMES = {"lmstudio": "LM Studio", "ollama": "Ollama", "llamacpp": "llama.cpp",
         "openai": "OpenAI-compatible server"}

_kinds: dict[str, str] = {}


def server_url(override: str | None = None) -> str:
    """The server root, without a trailing /v1."""
    url = override
    if not url:
        try:
            url = json.loads(APP_CFG.read_text()).get("llm_url")
        except (FileNotFoundError, ValueError):
            url = None
    url = (url or DEFAULT_URL).strip().rstrip("/")
    if "://" not in url:
        url = "http://" + url
    return re.sub(r"/v1$", "", url)


def _get(url: str, timeout: float = 5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def _post(url: str, body: dict, timeout: float):
    req = urllib.request.Request(url, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def kind(url: str) -> str:
    """'lmstudio', 'ollama', 'llamacpp' or 'openai' (anything else).
    Raises OSError if nothing answers at url."""
    if url in _kinds:
        return _kinds[url]
    k = None
    try:
        if "data" in _get(f"{url}/api/v0/models", 3):
            k = "lmstudio"
    except (OSError, ValueError):
        pass
    if not k:
        try:
            if "version" in _get(f"{url}/api/version", 3):
                k = "ollama"
        except (OSError, ValueError):
            pass
    if not k:
        data = _get(f"{url}/v1/models", 5).get("data", [])   # raises if unreachable
        k = "llamacpp" if any(m.get("owned_by") == "llamacpp" for m in data) else "openai"
    _kinds[url] = k
    return k


def models(url: str) -> list[dict]:
    """Chat models on the server: [{"id", "state"}], state 'loaded',
    'not-loaded' or None when the server doesn't say."""
    k = kind(url)
    if k == "lmstudio":
        return [{"id": m["id"], "state": m.get("state")}
                for m in _get(f"{url}/api/v0/models")["data"]
                if m.get("type") in ("llm", "vlm") and "whisper" not in m["id"].lower()]
    if k == "ollama":
        loaded = {m["name"] for m in _get(f"{url}/api/ps").get("models", [])}
        return [{"id": m["name"], "state": "loaded" if m["name"] in loaded else "not-loaded"}
                for m in _get(f"{url}/api/tags").get("models", []) if "embed" not in m["name"]]
    out = []
    for m in _get(f"{url}/v1/models").get("data", []):
        st = m.get("status")   # llama-server in router mode: {"value": "loaded"|"unloaded"|...}
        st = st.get("value") if isinstance(st, dict) else None
        out.append({"id": m["id"], "state": None if st is None else
                    ("loaded" if st == "loaded" else "not-loaded")})
    return out


def default_model(url: str) -> str | None:
    """The loaded model; else, except on LM Studio (which must load it
    first), the first one listed (Ollama loads models on request)."""
    ms = models(url)
    loaded = next((m["id"] for m in ms if m["state"] == "loaded"), None)
    if loaded or kind(url) == "lmstudio":
        return loaded
    return ms[0]["id"] if ms else None


def short(model: str) -> str:
    """A display name: llama.cpp reports the model file's full path."""
    name = model.rsplit("/", 1)[-1] if model.startswith("/") else model
    return name.removesuffix(".gguf")


def chat(url: str, model: str, prompt: str, think: bool = False, timeout: float = 1800) -> str:
    """One user message in, the reply out (reasoning stripped)."""
    msgs = [{"role": "user", "content": prompt}]
    k = kind(url)
    if k == "ollama":
        # Native API: the OpenAI one can't set the context, and Ollama's
        # default would silently cut long transcripts.
        body = {"model": model, "messages": msgs, "stream": False, "think": think,
                "options": {"temperature": 0.3, "num_ctx": CONTEXT}}
        path, pick = "/api/chat", lambda r: r["message"]["content"]
        optional = ["think"]          # older Ollama / non-thinking models may refuse it
    else:
        body = {"model": model, "messages": msgs, "temperature": 0.3}
        if not think:
            body["reasoning_effort"] = "none"                        # LM Studio, OpenAI
            if k in ("llamacpp", "openai"):
                body["chat_template_kwargs"] = {"enable_thinking": False}  # llama.cpp (Qwen3 etc.)
        path, pick = "/v1/chat/completions", lambda r: r["choices"][0]["message"]["content"]
        optional = ["reasoning_effort", "chat_template_kwargs"]
    try:
        r = _post(url + path, body, timeout)
    except urllib.error.HTTPError as e:
        if e.code != 400 or not any(o in body for o in optional):
            raise RuntimeError(f"{NAMES[k]} at {url}: {e.code} {e.read()[:300]!r}") from e
        for o in optional:            # a strict server rejected an extra; retry plainly
            body.pop(o, None)
        r = _post(url + path, body, timeout)
    text = pick(r) or ""
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.S).strip()
