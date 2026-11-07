"""Call every configured model once and print pass/fail. Run: python scripts/smoke_models.py"""
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()

GEMINI_MODELS = ["gemini-4-argon", "gemini-3.8-flash", "gemini-3.5-flash-lite"]
PROMPT = "Reply with the single word: ok"


def smoke_gemini(model: str) -> str:
    from google import genai

    client = genai.Client()
    r = client.interactions.create(
        model=model, input=PROMPT, generation_config={"thinking_level": "low"}
    )
    return r.output_text.strip()


def smoke_subq() -> str:
    import requests

    base, key = os.getenv("SUBQ_BASE_URL"), os.getenv("SUBQ_API_KEY")
    if not (base and key):
        raise RuntimeError("SUBQ_BASE_URL / SUBQ_API_KEY not set")
    resp = requests.post(
        base.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": os.getenv("SUBQ_MODEL", "subq-snap-1.1"),
            "messages": [{"role": "user", "content": PROMPT}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def main() -> int:
    failed = 0
    checks = [(m, lambda m=m: smoke_gemini(m)) for m in GEMINI_MODELS]
    checks.append(("subq", smoke_subq))
    for name, fn in checks:
        t = time.time()
        try:
            out = fn()
            print(f"PASS {name:24s} {time.time() - t:5.1f}s  -> {out[:40]!r}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name:24s} {type(exc).__name__}: {str(exc)[:160]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
