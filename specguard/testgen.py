"""Per-clause test generation (parallel, flash) + validation + red classification."""
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import gh, llm, sandbox


def _slug(cid: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", cid.lower()).strip("_")


def test_path(cid: str) -> str:
    return f"tests/test_spec_{_slug(cid)}.py"


def _strip_fences(code: str) -> str:
    m = re.search(r"```(?:python)?\n(.*?)```", code, re.S)
    return (m.group(1) if m else code).strip() + "\n"


def gen_test(clause: dict, api: str, code_ctx: dict, error: str = "", previous: str = "") -> str:
    src = "\n\n".join(f"# {p}\n{t}" for p, t in code_ctx.items())
    prompt = f"""Write a pytest conformance test file for ONE spec clause.

CLAUSE {clause['id']} ({clause.get('level')}, {clause.get('source')}):
  quote: "{clause['quote']}"
  requirement: {clause.get('requirement', '')}

PUBLIC API CONTRACT (tests must use exactly this interface; it may not exist in the code yet):
{api or '(use the existing public API)'}

EXISTING SOURCE CODE:
{src}

Rules:
- Output ONLY Python code for one file. No prose, no markdown fences.
- Start the module docstring with: "Clause {clause['id']} ({clause.get('source')}): {clause['quote']}"
- Import only from the package under test and the standard library. No network, no sleeping, deterministic.
- Each test asserts observable behaviour that a CORRECT implementation satisfies and a NON-conforming one violates.
- Use assert statements (AssertionError on violation) or pytest.raises for required rejections.
- 1 to 4 focused tests. Include at least one test that FAILS if the clause is not enforced.
"""
    if error:
        prompt += f"\nYour previous attempt had a TEST BUG (not an implementation failure). Fix it.\nPREVIOUS:\n{previous}\nERROR:\n{error}\n"
    return _strip_fences(llm.gemini(prompt, llm.FLASH, "low"))


def generate(clauses: list, api: str, code_ctx: dict, errors: dict | None = None, previous: dict | None = None) -> dict:
    errors, previous = errors or {}, previous or {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {c["id"]: pool.submit(gen_test, c, api, code_ctx, errors.get(c["id"], ""), previous.get(c["id"], ""))
                for c in clauses}
    return {cid: f.result() for cid, f in futs.items()}


def classify(results: list, clauses: list) -> dict:
    """cid -> {status: red|nondiscriminating|error, output, tests:[...]}"""
    out = {}
    for c in clauses:
        path = test_path(c["id"])
        mine = [r for r in results if r["name"].startswith(path)]
        if not mine:
            out[c["id"]] = {"status": "error", "output": "no tests collected", "tests": []}
        elif any(r["status"] == "error" for r in mine):
            bad = next(r for r in mine if r["status"] == "error")
            out[c["id"]] = {"status": "error", "output": bad["output"], "tests": mine}
        elif any(r["status"] == "fail" for r in mine):
            bad = next(r for r in mine if r["status"] == "fail")
            out[c["id"]] = {"status": "red", "output": bad["output"], "tests": mine}
        else:
            out[c["id"]] = {"status": "nondiscriminating", "output": "passed before implementation", "tests": mine}
    return out


def build_and_validate(work: Path, clauses: list, api: str, emit=None, run_existing=False) -> dict:
    """Generate tests, run them against current code, regenerate errored ones once.

    Returns cid -> {status: red|nondiscriminating|untestable, source, output, tests}.
    """
    code_ctx = gh.repo_files(work)
    sources = generate(clauses, api, code_ctx)
    state = {}
    for attempt in range(2):
        files = {test_path(cid): src for cid, src in sources.items() if cid not in state}
        pending = [c for c in clauses if c["id"] not in state]
        if not pending:
            break
        results = sandbox.run_tests(work, files, only=list(files.keys()))
        cls = classify(results, pending)
        redo = {}
        for c in pending:
            r = cls[c["id"]]
            if r["status"] == "error" and attempt == 0:
                redo[c["id"]] = r["output"]
            elif r["status"] == "error":
                state[c["id"]] = {"status": "untestable", "source": sources[c["id"]], "output": r["output"], "tests": r["tests"]}
            else:
                state[c["id"]] = {"status": r["status"], "source": sources[c["id"]], "output": r["output"], "tests": r["tests"]}
        if redo:
            if emit:
                emit("progress", f"regenerating {len(redo)} test(s) with test bugs", ", ".join(redo))
            sources.update(generate([c for c in clauses if c["id"] in redo], api, code_ctx, redo,
                                    {cid: sources[cid] for cid in redo}))
    return state
