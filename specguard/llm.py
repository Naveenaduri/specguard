"""LLM layer: Gemini (Interactions API) + SubQ (OpenAI-compatible), with a disk cache."""
import hashlib
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

CACHE_DIR = Path(os.getenv("SPECGUARD_CACHE", ".specguard_cache"))
FRESH = False  # set True by --fresh to bypass the cache

ARGON = "gemini-4-argon"
FLASH = "gemini-3.8-flash"
LITE = "gemini-3.5-flash-lite"


def set_fresh(value: bool) -> None:
    global FRESH
    FRESH = value


def _key(provider: str, model: str, level: str, prompt: str) -> str:
    return hashlib.sha256(f"{provider}|{model}|{level}|{prompt}".encode()).hexdigest()


def _cached(key: str):
    path = CACHE_DIR / f"{key}.json"
    if not FRESH and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))["text"]
    return None


def _store(key: str, text: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{key}.json").write_text(json.dumps({"text": text}), encoding="utf-8")


def gemini(prompt: str, model: str = ARGON, thinking: str = "medium", background: bool = False) -> str:
    """Call Gemini via the Interactions API. No temperature/top_p/top_k/thinking_budget."""
    key = _key("gemini", model, thinking, prompt)
    hit = _cached(key)
    if hit is not None:
        return hit
    from google import genai

    client = genai.Client(http_options={"timeout": 15 * 60 * 1000})
    kwargs = dict(model=model, input=prompt, generation_config={"thinking_level": thinking})
    if background:
        r = client.interactions.create(background=True, **kwargs)
        while r.status in ("in_progress", "queued"):
            time.sleep(3)
            r = client.interactions.get(id=r.id)
    else:
        r = client.interactions.create(**kwargs)
    text = r.output_text
    _store(key, text)
    return text


def subq(prompt: str, model: str | None = None) -> str:
    """SubQ long-context model via an OpenAI-compatible chat endpoint.

    Isolated in this one function so the API details are easy to change.
    Falls back to gemini-4-argon when SUBQ_* is not configured.
    """
    base, api_key = os.getenv("SUBQ_BASE_URL"), os.getenv("SUBQ_API_KEY")
    if not (base and api_key):
        return gemini(prompt, ARGON, "medium")
    model = model or os.getenv("SUBQ_MODEL", "subq-snap-1.1")
    key = _key("subq", model, "", prompt)
    hit = _cached(key)
    if hit is not None:
        return hit
    import requests

    resp = requests.post(
        base.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "messages": [{"role": "user", "content": prompt}]},
        timeout=15 * 60,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    _store(key, text)
    return text


def extract_json(text: str):
    """Pull the first JSON value out of a model reply (handles ``` fences)."""
    t = text.strip()
    if "```" in t:
        t = t.split("```", 2)[1]
        if t.startswith("json"):
            t = t[4:]
    start = min([i for i in (t.find("["), t.find("{")) if i != -1], default=0)
    return json.loads(t[start:].strip().rstrip("`"))


def subq_decide(state: str, questions: dict) -> dict:
    """SubQ Snap 1.1 Decisions API (POST /v1/systemone). Returns {question_name: answer}.

    Raises RuntimeError when SubQ is not configured so callers can fall back.
    """
    base, api_key = os.getenv("SUBQ_BASE_URL"), os.getenv("SUBQ_API_KEY")
    if not (base and api_key):
        raise RuntimeError("SUBQ not configured")
    model = os.getenv("SUBQ_MODEL", "subq-snap-1.1")
    key = _key("subq-decide", model, "", json.dumps([state, questions], sort_keys=True))
    hit = _cached(key)
    if hit is not None:
        return json.loads(hit)
    import requests

    resp = requests.post(
        base.rstrip("/") + "/systemone",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model, "state": state, "questions": questions},
        timeout=120,
    )
    resp.raise_for_status()
    answers = resp.json()["answers"]
    _store(key, json.dumps(answers))
    return answers
