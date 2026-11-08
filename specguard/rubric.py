"""PR review rubric: works on any PR, no specs required.

Shape inspired by published AI-review practice (CodeRabbit comments read: "Severity · Category · Effort",
a bold one-line problem, a short concrete explanation, a proposed fix, plus a PR walkthrough summary).
PRProof adds what those lack: a Proof row. A finding is only 'proven' if an executed test failed.
"""
import re
from pathlib import Path

from . import gh, llm, sandbox

RUBRIC = [
    {"id": "correctness", "name": "Correctness", "ask": "Does this change introduce wrong behaviour, a logic error, or an unhandled edge case?"},
    {"id": "security", "name": "Security", "ask": "Does this change weaken authentication, validation, cryptography or permissions, or add injection or secret-exposure risk?"},
    {"id": "error_handling", "name": "Error handling", "ask": "Does this change remove, swallow or weaken error handling or input validation?"},
    {"id": "api", "name": "API & compatibility", "ask": "Does this change break or silently alter public behaviour, signatures or data formats?"},
    {"id": "stability", "name": "Stability & availability", "ask": "Could this change cause crashes, hangs, resource leaks, race conditions or outages?"},
    {"id": "performance", "name": "Performance", "ask": "Does this change add expensive work, worse complexity or avoidable I/O?"},
    {"id": "tests", "name": "Tests", "ask": "Does this change alter behaviour without tests, or delete or weaken existing tests?"},
    {"id": "config", "name": "Config & dependencies", "ask": "Does this change add risky dependencies, or alter CI, build or deployment configuration?"},
    {"id": "maintainability", "name": "Maintainability", "ask": "Does this change add duplication, confusing structure or hard-to-follow logic?"},
]
SEVERITY = ["Critical", "Major", "Minor", "Trivial"]
CATEGORY_NAMES = {c["name"] for c in RUBRIC} | {"Spec conformance"}
BY_ID = {c["id"]: c for c in RUBRIC}
NON_EXECUTABLE = {"Tests", "Maintainability"}  # a failing behaviour test can't prove these, so they stay heads-ups
MAX_FINDINGS = 4
SANDBOX_MAX_KB = 150_000  # don't copy huge repos into the test sandbox


def pick_hot(probs: dict, top: int = 3, thr: float = .5) -> list:
    return [k for k, p in sorted(probs.items(), key=lambda kv: -kv[1]) if p >= thr][:top]


def verdict_for(findings: list) -> dict:
    if any(f["status"] == "proven" for f in findings):
        return {"label": "Needs changes", "tone": "bad"}
    if any(f["status"] == "headsup" for f in findings):
        return {"label": "Looks fine, check the heads-up", "tone": "warn"}
    return {"label": "Looks good", "tone": "good"}


def status_for(verdict: dict, tests: dict) -> tuple:
    """GitHub commit status for the verdict: a proven defect fails the check, so branch protection can block the merge."""
    if verdict["tone"] == "bad":
        return "failure", f"Needs changes: {tests.get('failed', 0)} of {tests.get('run', 0)} proof test(s) failed"[:140]
    if verdict["tone"] == "warn":
        return "success", "No failing proof; heads-up items to check"
    return "success", "No problems proven" + (f" ({tests['run']} test(s) passed)" if tests.get("run") else "")


def clean_findings(raw) -> list:
    names = {n.lower(): n for n in CATEGORY_NAMES}
    out = []
    for f in raw if isinstance(raw, list) else []:
        cat = names.get(str(f.get("category", "")).strip().lower())
        sev = str(f.get("severity", "")).strip().title()
        title, where = str(f.get("title", "")).strip(), str(f.get("where", "")).strip()
        if not (cat and sev in SEVERITY and title and where):
            continue
        out.append({"category": cat, "severity": sev, "title": title[:140], "where": where[:120],
                    "why": str(f.get("why", "")).strip()[:600], "fix": str(f.get("fix", "")).strip()[:400],
                    "effort": f.get("effort") if f.get("effort") in ("Quick win", "Larger change") else "Quick win",
                    "testable": bool(f.get("testable", True)) and cat not in NON_EXECUTABLE, "status": "headsup", "proof": ""})
    out.sort(key=lambda f: SEVERITY.index(f["severity"]))
    out = out[:MAX_FINDINGS]
    for i, f in enumerate(out, 1):
        f["id"] = f"F{i}"
    return out


# ---------- LLM / decision-model steps ----------
def summarize(meta: dict, diff: str) -> str:
    prompt = f"""In two plain sentences, say what this pull request changes and why (use its title and description if helpful).
No markdown, no bullet points.

TITLE: {meta.get('title')}
DESCRIPTION: {(meta.get('body') or '')[:1200]}
FILES: {meta.get('files')}
DIFF:
{diff[:30000]}"""
    return llm.gemini(prompt, llm.FLASH, "low").strip()


def score_rubric(state: str) -> dict:
    """SubQ Snap decision model: one yes/no per rubric category plus an overall risk score, in a single call."""
    questions = {c["id"]: {"type": "noul", "instructions": c["ask"]} for c in RUBRIC}
    questions["risk"] = {"type": "score", "instructions": "Overall, how risky is this change to merge as it is?",
                         "criteria": ["Low", "Moderate", "High", "Severe"]}
    questions["primary"] = {"type": "choice", "instructions": "Which single area is the most significant concern in this change, if any?",
                            "criteria": {**{c["id"]: f"{c['name']}: {c['ask']}" for c in RUBRIC}, "none": "No significant concern"}}
    try:
        ans = llm.subq_decide(state[:900_000], questions)
        probs = {c["id"]: float(ans[c["id"]]["noul"]) for c in RUBRIC if c["id"] in ans}
        r = ans.get("risk", {})
        score = float(r.get("score", 0))
        legend = r.get("legend", {})
        label = legend.get(str(min(3, max(0, round(score))))) or ["Low", "Moderate", "High", "Severe"][min(3, max(0, round(score)))]
        prim = ans.get("primary", {})
        return {"probs": probs, "risk": {"label": label, "score": round(score, 1)}, "via": "subq-snap-1.1",
                "primary": {"choice": prim.get("choice", "none"), "probs": {k: float(v) for k, v in (prim.get("probabilities") or {}).items()}}}
    except Exception:  # noqa: BLE001
        listing = "\n".join(f"{c['id']}: {c['ask']}" for c in RUBRIC)
        reply = llm.gemini(f"For this pull request, give the probability (0 to 1) that each question is YES, and an overall risk "
                           f"(0 low .. 3 severe). Return ONLY JSON {{\"probs\": {{\"id\": 0.0}}, \"risk\": 0}}.\n\n{listing}\n\n{state[:60000]}",
                           llm.LITE, "low")
        j = llm.extract_json(reply)
        score = float(j.get("risk", 0))
        return {"probs": {k: float(v) for k, v in j.get("probs", {}).items() if k in BY_ID},
                "risk": {"label": ["Low", "Moderate", "High", "Severe"][min(3, max(0, round(score)))], "score": round(score, 1)},
                "via": "gemini-3.5-flash-lite", "primary": {"choice": (pick_hot(j.get("probs", {}), 1) or ["none"])[0], "probs": {}}}


def investigate(meta: dict, diff: str, hot: list, probs: dict) -> list:
    focus = "\n".join(f"- {BY_ID[h]['name']} (signal {probs.get(h, 0):.0%}): {BY_ID[h]['ask']}" for h in hot)
    prompt = f"""You are a careful senior reviewer. The decision model flagged these areas for this pull request:
{focus}

Report only real problems you can point to in the diff. If an area is fine, say nothing about it. At most {MAX_FINDINGS} findings.
Each finding must name a concrete place (file and function or line) that appears in the diff.
Write for a busy human: plain words, no jargon, one concrete failing example in "why".
Return ONLY a JSON array:
[{{"category": "<one of: {', '.join(c['name'] for c in RUBRIC)}>", "severity": "Critical|Major|Minor|Trivial",
  "title": "<one-line problem, <=12 words>", "where": "<file · function>",
  "why": "<2 short sentences: how it fails, with an example>", "fix": "<1-2 sentences>",
  "effort": "Quick win|Larger change", "testable": true or false}}]

PR TITLE: {meta.get('title')}
PR DESCRIPTION: {(meta.get('body') or '')[:800]}
DIFF:
{diff[:60000]}"""
    return clean_findings(llm.extract_json(llm.gemini(prompt, llm.ARGON, "medium")))


def _gen_test(f: dict, code_ctx: dict, error: str = "", previous: str = "") -> str:
    src = "\n\n".join(f"# {p}\n{t}" for p, t in code_ctx.items())
    prompt = f"""Write a pytest file that FAILS (an assertion or missing exception) if this review finding is real in the code below,
and passes if the code is actually correct.

FINDING: {f['title']}
WHERE: {f['where']}
WHY: {f['why']}

CODE UNDER TEST:
{src}

Rules: output ONLY Python code, no fences. Import only from the package under test and the standard library. Deterministic, no network.
Assert observable behaviour. 1 to 3 focused tests."""
    if error:
        prompt += f"\nYour previous attempt had a TEST BUG. Fix it.\nPREVIOUS:\n{previous}\nERROR:\n{error}\n"
    code = llm.gemini(prompt, llm.FLASH, "low")
    m = re.search(r"```(?:python)?\n(.*?)```", code, re.S)
    return (m.group(1) if m else code).strip() + "\n"


def prove(work: Path, findings: list, adjudicate, emit=None) -> dict:
    """Turn findings into executed evidence. proven = failing assertion; cleared = test passes (finding refuted); else headsup."""
    emit = emit or (lambda *a, **k: None)
    ctx = gh.repo_files(work)
    run = failed = 0
    for f in findings:
        if not f.get("testable", True):
            f["proof"] = "Not testable by running code"
            continue
        path, src, err = f"tests/test_review_{f['id'].lower()}.py", "", ""
        for attempt in range(2):
            src = _gen_test(f, ctx, err, src)
            res = [r for r in sandbox.run_tests(work, {path: src}, only=[path]) if r["name"].startswith(path) or r["name"] == "<suite>"]
            bad = [r for r in res if r["status"] == "error"]
            if bad and attempt == 0:
                err = bad[0]["output"]
                continue
            break
        run += 1
        fails = [r for r in res if r["status"] == "fail"]
        if fails:
            verdict = adjudicate({"id": f["id"], "level": f["severity"], "source": f["category"], "quote": f["title"]}, src, fails[0]["output"])
            if verdict.get("violation"):
                f["status"], f["proof"], f["test_name"] = "proven", fails[0]["output"][:520], fails[0]["name"]
                failed += 1
            else:
                f["proof"] = "A test failed, but it looked like a test bug, so this stays a heads-up"
        elif res and all(r["status"] == "pass" for r in res):
            f["status"], f["proof"] = "cleared", "A test for this passed, so the code behaves correctly"
        else:
            f["proof"] = "Could not turn this into a working test"
        emit("prove", "progress", f"{f['id']}: {f['status']}", f["title"])
    return {"run": run, "failed": failed}


# ---------- rendering ----------
def _line(k: str, v: str) -> str:
    return f"**{k} :** {v}  \n"


def render_comment(card: dict) -> str:
    md = "🛡️ **PRProof review**\n<!-- specguard -->\n\n"
    md += _line("Verdict", card["verdict"]["label"]) + _line("Risk", card["risk"]["label"])
    md += _line("What changed", card.get("summary", ""))
    t = card.get("tests", {})
    md += _line("Tests executed", f"{t.get('run', 0)} ({t.get('failed', 0)} failed)") + _line("Scope", card.get("scope", "").replace("Scope: ", ""))
    for f in card.get("findings", []):
        if f["status"] == "cleared":
            continue
        md += "\n---\n\n" + _line("Problem", f["title"]) + _line("Severity", f"{f['severity']} · {f['category']} · {f.get('effort', '')}")
        md += _line("Where", f["where"]) + _line("Why it matters", f.get("why", ""))
        md += _line("Proof", ("Failing test: " + f["proof"][:300]) if f["status"] == "proven" else f.get("proof") or "Not executed")
        if f.get("spec_refs"):
            md += _line("Spec", "; ".join(f"{r['source']}: “{r['quote']}”" for r in f["spec_refs"]))
        md += _line("Fix", f.get("fix", ""))
    cleared = [f for f in card.get("findings", []) if f["status"] == "cleared"]
    if cleared:
        md += "\n---\n\n" + _line("Checked and cleared", "; ".join(f["title"] for f in cleared))
    return md
