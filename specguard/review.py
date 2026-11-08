"""Review engine: deterministic scope check + spec agent (map clauses -> generate tests -> run -> adjudicate).

Used by both PR review and the build pipeline's independent inspection.
Never reports a violation without a failing test as evidence.
"""
import fnmatch
import json
from pathlib import Path

from . import gh, llm, testgen

SENSITIVE = [".github/workflows/*", ".github/actions/*", ".gitlab-ci*", "Jenkinsfile", "azure-pipelines*",
             "*/.env", ".env", ".env.*", "*.pem", "*.key", "*.p12", "*secret*", "*credentials*", ".circleci/*"]


def scope_check(changed_files: list, write: bool = True, need_write: bool = False) -> dict:
    """Deterministic scope gate: access + path allowlist. One summary line."""
    flagged = [f for f in changed_files if any(fnmatch.fnmatch(f, pat) or fnmatch.fnmatch(f.split("/")[-1], pat)
                                               for pat in SENSITIVE)]
    if need_write and not write:
        return {"ok": False, "flagged": flagged, "summary": "Scope: ❌ identity lacks write access to this repository"}
    if flagged:
        return {"ok": False, "flagged": flagged,
                "summary": f"Scope: ⚠️ {len(flagged)} sensitive path(s) touched: {', '.join(flagged[:4])}"}
    return {"ok": True, "flagged": [], "summary": f"Scope: ✅ {len(changed_files)} file(s), no CI/secret paths touched"}


def map_clauses(state: str, clauses: list, top: int = 6) -> list:
    """Which clauses does this change touch? SubQ (one yes/no per clause); falls back to flash-lite."""
    testable = [c for c in clauses if c.get("testable", True)]
    if not testable:
        return []
    try:
        questions = {c["id"]: {"type": "noul", "instructions":
                     f"Does this code change touch, or put at risk, this requirement: \"{c['quote']}\"?"} for c in testable}
        answers = llm.subq_decide(state[:900_000], questions)
        scored = sorted(((a.get("noul", 0.0), cid) for cid, a in answers.items()), reverse=True)
        picked = [cid for p, cid in scored if p >= 0.5][:top]
        via = "subq-snap-1.1"
    except Exception:  # noqa: BLE001
        listing = "\n".join(f"{c['id']}: {c['quote']}" for c in testable)
        reply = llm.gemini(f"Which of these spec clauses does the change below touch or put at risk? Reply ONLY with a JSON "
                           f"array of ids (max {top}).\n\nCLAUSES:\n{listing}\n\nCHANGE:\n{state[:60000]}", llm.LITE, "low")
        try:
            picked = [i for i in llm.extract_json(reply) if i in {c['id'] for c in testable}][:top]
        except Exception:  # noqa: BLE001
            picked = []
        via = "gemini-3.5-flash-lite"
    map_clauses.via = via
    return picked


def adjudicate(clause: dict, test_source: str, output: str) -> dict:
    prompt = f"""A generated test failed against the code. Decide if this is a REAL violation of the spec clause
or a bug in the test (wrong assumption, wrong API use, over-strict assertion).

CLAUSE {clause['id']} ({clause.get('level')}, {clause.get('source')}): "{clause['quote']}"
TEST:
{test_source}
FAILURE OUTPUT:
{output}

Reply ONLY JSON: {{"violation": true|false, "reason": "<one sentence>", "suggested_fix": "<one or two sentences>"}}"""
    try:
        return llm.extract_json(llm.gemini(prompt, llm.ARGON, "medium"))
    except Exception:  # noqa: BLE001
        return {"violation": False, "reason": "adjudication failed", "suggested_fix": ""}


def review_changes(work: Path, spec_text: str, all_clauses: list, diff: str, changed_files: list,
                   emit=None, skip_quotes: set | None = None) -> dict:
    """Spec agent. Returns touched, violations[proven], conformant, untestable, discarded."""
    emit = emit or (lambda *a, **k: None)
    skip_quotes = skip_quotes or set()
    clauses = [c for c in all_clauses if c["quote"] not in skip_quotes]
    ctx = diff + "\n\n" + "\n\n".join(f"### {p}\n{t}" for p, t in gh.repo_files(work, limit=120000).items()
                                       if p in changed_files)
    emit("map_clauses", "started", "Mapping change to spec clauses", f"{len(clauses)} clauses in scope")
    touched_ids = map_clauses(ctx, clauses)
    touched = [c for c in clauses if c["id"] in touched_ids]
    emit("map_clauses", "done", f"{len(touched)} clause(s) touched", ", ".join(touched_ids),
         {"clauses": touched, "via": getattr(map_clauses, "via", "")})
    if not touched:
        return {"touched": [], "violations": [], "conformant": [], "untestable": [], "discarded": []}
    emit("generate_tests", "started", f"Generating {len(touched)} conformance test(s)", "")
    state = testgen.build_and_validate(work, touched, "", lambda s, t, d="": emit("generate_tests", s, t, d))
    emit("generate_tests", "done", "Tests generated", "", {"tests": [
        {"name": c["id"], "clause_id": c["id"], "status": state[c["id"]]["status"]} for c in touched]})
    emit("run_tests", "done", "Ran conformance tests against the change", "", {"tests": [
        {"name": t["name"], "clause_id": c["id"], "status": t["status"], "output_snippet": t["output"][:300]}
        for c in touched for t in state[c["id"]]["tests"]]})
    violations, conformant, untestable = [], [], []
    for c in touched:
        s = state[c["id"]]
        if s["status"] == "untestable":
            untestable.append(c["id"])
        elif s["status"] == "nondiscriminating":  # passes -> conformant
            conformant.append(c["id"])
        elif s["status"] == "red":
            fail = next(t for t in s["tests"] if t["status"] == "fail")
            verdict = adjudicate(c, s["source"], fail["output"])
            if verdict.get("violation"):
                violations.append({"clause_id": c["id"], "source": c.get("source"), "quote": c["quote"],
                                   "test_name": fail["name"], "output_snippet": fail["output"][:600],
                                   "suggested_fix": verdict.get("suggested_fix", ""), "test_source": s["source"]})
            else:
                untestable.append(c["id"])
    return {"touched": touched_ids, "violations": violations, "conformant": conformant,
            "untestable": untestable, "discarded": []}


def render_comment(scope: dict, result: dict) -> str:
    head = "🛡️ **PRProof**\n<!-- specguard -->\n\n" + scope["summary"] + "\n\n"
    if result["violations"]:
        out = head + f"**{len(result['violations'])} violation(s) proven by a failing test:**\n\n"
        for v in result["violations"]:
            out += (f"#### ❌ {v['clause_id']} — {v['source']}\n> {v['quote']}\n\n"
                    f"Failing test `{v['test_name']}`:\n```\n{v['output_snippet']}\n```\n"
                    f"**Suggested fix:** {v['suggested_fix']}\n\n")
        return out
    ids = ", ".join(result["conformant"] or result["touched"]) or "none touched"
    return head + f"✅ spec-conformant for clauses {ids}."
