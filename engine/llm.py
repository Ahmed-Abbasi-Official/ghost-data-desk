"""Minimal LLM client (stdlib only) with on-disk caching and token accounting.

Providers:
  ollama    (default) local Ollama server, model from GHOST_LLM_MODEL (default qwen3.5:9b)
  anthropic Claude Messages API, needs ANTHROPIC_API_KEY; model from GHOST_LLM_MODEL
  none      disable LLM calls; callers fall back to deterministic logic
"""
import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

PROVIDER = os.environ.get("GHOST_LLM_PROVIDER", "ollama").lower()
DEFAULT_MODELS = {"ollama": "qwen3.5:9b", "anthropic": "claude-haiku-4-5-20251001"}
MODEL = os.environ.get("GHOST_LLM_MODEL", DEFAULT_MODELS.get(PROVIDER, ""))
OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.environ.get("GHOST_CACHE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "llm"))
USE_CACHE = os.environ.get("GHOST_LLM_CACHE", "1") != "0"

# Published list prices in USD per million tokens, used only for cost estimates.
PRICES = {
    "qwen3.5:9b": (0.0, 0.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-5-5": (3.0, 15.0),
}


class Usage:
    def __init__(self):
        self.calls = []

    def record(self, purpose, model, provider, input_tokens, output_tokens, seconds, cached):
        self.calls.append({
            "purpose": purpose, "provider": provider, "model": model,
            "input_tokens": int(input_tokens or 0), "output_tokens": int(output_tokens or 0),
            "seconds": round(seconds, 3), "cached": cached,
        })

    def summary(self):
        out = {}
        for c in self.calls:
            key = (c["provider"], c["model"], c["purpose"])
            s = out.setdefault(key, {"calls": 0, "cached_calls": 0, "input_tokens": 0, "output_tokens": 0, "seconds": 0.0})
            s["calls"] += 1
            s["cached_calls"] += 1 if c["cached"] else 0
            s["input_tokens"] += c["input_tokens"]
            s["output_tokens"] += c["output_tokens"]
            s["seconds"] += c["seconds"]
        return out


USAGE = Usage()


def available():
    if PROVIDER == "none":
        return False
    if PROVIDER == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    try:
        urllib.request.urlopen(OLLAMA_URL + "/api/version", timeout=3).read()
        return True
    except Exception:
        return False


def _cache_path(key):
    return os.path.join(CACHE_DIR, key[:2], key + ".json")


def _cache_key(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def chat_json(purpose, system, user, images=None, max_tokens=4096):
    """Send one prompt, return the parsed JSON object the model produced.

    images: optional list of file paths (PNG) for vision-capable models.
    """
    image_digests = []
    for p in images or []:
        with open(p, "rb") as fh:
            image_digests.append(hashlib.sha256(fh.read()).hexdigest())
    key = _cache_key({"provider": PROVIDER, "model": MODEL, "system": system, "user": user, "images": image_digests})
    path = _cache_path(key)
    if USE_CACHE and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            hit = json.load(fh)
        USAGE.record(purpose, MODEL, PROVIDER, hit["input_tokens"], hit["output_tokens"], 0.0, True)
        return hit["result"]

    started = time.time()
    if PROVIDER == "anthropic":
        text, tin, tout = _anthropic(system, user, images, max_tokens)
    else:
        text, tin, tout = _ollama(system, user, images, max_tokens)
    elapsed = time.time() - started
    result = _parse_json(text)
    USAGE.record(purpose, MODEL, PROVIDER, tin, tout, elapsed, False)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"result": result, "input_tokens": tin, "output_tokens": tout, "raw": text}, fh, ensure_ascii=False, indent=1)
    return result


def _parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        return json.loads(text[start:end + 1])


def _post(url, body, headers, timeout=900):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def _ollama(system, user, images, max_tokens):
    msg = {"role": "user", "content": user}
    if images:
        msg["images"] = [base64.b64encode(open(p, "rb").read()).decode("ascii") for p in images]
    body = {
        "model": MODEL, "stream": False, "think": False, "format": "json",
        "options": {"temperature": 0, "seed": 7, "num_predict": max_tokens, "num_ctx": 32768},
        "messages": [{"role": "system", "content": system}, msg],
    }
    r = _post(OLLAMA_URL + "/api/chat", body, {"Content-Type": "application/json"})
    return r["message"]["content"], r.get("prompt_eval_count", 0), r.get("eval_count", 0)


def _anthropic(system, user, images, max_tokens):
    content = []
    for p in images or []:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                     "data": base64.b64encode(open(p, "rb").read()).decode("ascii")}})
    content.append({"type": "text", "text": user})
    body = {"model": MODEL, "max_tokens": max_tokens, "temperature": 0, "system": system,
            "messages": [{"role": "user", "content": content}]}
    headers = {"Content-Type": "application/json", "x-api-key": os.environ["ANTHROPIC_API_KEY"],
               "anthropic-version": "2023-06-01"}
    r = _post("https://api.anthropic.com/v1/messages", body, headers)
    text = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")
    u = r.get("usage", {})
    return text, u.get("input_tokens", 0), u.get("output_tokens", 0)
