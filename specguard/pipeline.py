"""Build + review pipelines. Every stage goes through events.emit()."""
import itertools
import re
import threading
import time
from pathlib import Path

from . import events, gh, llm, review, rubric, sandbox, spec, testgen

WORK = Path(".specguard/work")
RUN_SLOTS = threading.Semaphore(2)  # global queue: max 2 concurrent runs


# ---------- human gates ----------
class Gates:
    def __init__(self):
        self._ev, self._res = {}, {}

    def wait(self, run_id, name, auto=False, timeout=3600):
        if auto:
            return {"decision": "approve", "text": ""}
        key = (run_id, name)
        self._ev[key] = threading.Event()
        if not self._ev[key].wait(timeout):
            return {"decision": "timeout", "text": ""}
        return self._res.pop(key)

    def resolve(self, run_id, name, decision, text=""):
        key = (run_id, name)
        self._res[key] = {"decision": decision, "text": text}
        if key in self._ev:
            self._ev[key].set()
            return True
        return False


GATES = Gates()


# ---------- pure helpers (unit tested) ----------
def parse_file_blocks(text: str) -> dict:
    out = {}
    for m in re.finditer(r"=== FILE: (.+?) ===\n(.*?)=== END ===", text, re.S):
        path = m.group(1).strip().replace("\\", "/")
        if path.startswith("/") or ".." in path.split("/") or re.match(r"^[A-Za-z]:", path):
            continue
        out[path] = m.group(2)
    return out


def evidence_table(rows: list) -> str:
    md = "| Clause | Source | Test | Changed files | Before | After |\n|---|---|---|---|---|---|\n"
    for r in rows:
        cell = lambda s: str(s).replace("|", "\\|").replace("\n", " ")[:160]  # noqa: E731
        md += (f"| {cell(r['clause_id'])} | {cell(r['source'])} | `{cell(r['test'])}` | {cell(r['files'])} "
               f"| ❌ {cell(r['before'])} | ✅ {cell(r['after'])} |\n")
    return md


def slugify(s: str, n=28) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:n].strip("-") or "feature"


def _first_line(output: str) -> str:
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    pick = next((ln for ln in reversed(lines) if ln.startswith("E ")), lines[-1] if lines else "failed")
    return pick[:140]


# ---------- build ----------
def make_plan(feature, clauses, code_ctx) -> dict:
    src = "\n\n".join(f"# {p}\n{t}" for p, t in code_ctx.items())
    prompt = f"""Plan this change. Return ONLY JSON:
{{"bullets": ["<=8 short bullets"], "files": [{{"path": "...", "change": "..."}}],
  "api": "exact public signatures/behaviour tests may rely on, e.g. decode(token, key, audience=None) raises InvalidTokenError ..."}}

FEATURE: {feature}
CLAUSES: {[{k: c[k] for k in ('id', 'level', 'source', 'quote', 'requirement') if k in c} for c in clauses]}
SOURCE:
{src}"""
    return llm.extract_json(llm.gemini(prompt, llm.ARGON, "medium"))


def implement(work: Path, feature, clauses, plan, red_sources: dict, failures: str, thinking: str) -> dict:
    code = gh.repo_files(work)
    existing = gh.repo_files(work, exts=(".py",), skip=("specs", ".git"), limit=30000)
    existing = {p: t for p, t in existing.items() if p.startswith("tests/")}
    src = "\n\n".join(f"=== {p} ===\n{t}" for p, t in code.items())
    tests = "\n\n".join(f"=== {p} ===\n{t}" for p, t in {**existing, **red_sources}.items())
    prompt = f"""Implement the feature so ALL the tests below pass, without breaking existing behaviour.

FEATURE: {feature}
CLAUSES: {[{k: c[k] for k in ('id', 'level', 'source', 'quote', 'requirement') if k in c} for c in clauses]}
PLAN: {plan}

CURRENT SOURCE:
{src}

TESTS (do not modify tests):
{tests}
"""
    if failures:
        prompt += f"\nPREVIOUS ATTEMPT FAILED THESE TESTS:\n{failures}\n"
    prompt += """
Output format: for EVERY file you change or create (never test files), emit the COMPLETE new file content as:
=== FILE: relative/path.py ===
<full file content>
=== END ===
No other prose."""
    blocks = parse_file_blocks(llm.gemini(prompt, llm.ARGON, thinking))
    return {p: t for p, t in blocks.items() if not p.startswith("tests/")}


def _apply(work: Path, files: dict):
    for p, t in files.items():
        dest = work / p
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(t if t.endswith("\n") else t + "\n", encoding="utf-8")


def _suite(work: Path):
    res = sandbox.run_tests(work, None)
    bad = [r for r in res if r["status"] != "pass"]
    return res, bad


def _tdd_cycle(E, work, feature, clauses, plan, round_no, extra_tests=None, extra_failures=""):
    """red -> commit tests -> implement -> green. Returns (ok, evidence_rows, files_changed, discarded)."""
    E("tests_red", "started", f"Writing tests first (round {round_no})", f"{len(clauses)} clause(s)")
    state = testgen.build_and_validate(work, clauses, plan.get("api", ""), lambda s, t, d="": E("tests_red", s, t, d))
    red = [c for c in clauses if state[c["id"]]["status"] == "red"]
    discarded = [{"clause_id": c["id"], "why": ("passes before implementation (non-discriminating)"
                  if state[c["id"]]["status"] == "nondiscriminating" else "test errors (untestable)")}
                 for c in clauses if state[c["id"]]["status"] != "red"]
    E("tests_red", "done", f"{len(red)} red", ("discarded: " + ", ".join(f"{d['clause_id']} ({d['why']})" for d in discarded)
                                              if discarded else "all tests failing as expected"),
      {"tests": [{"name": t["name"], "clause_id": c["id"], "status": "fail", "output_snippet": t["output"][:300]}
                 for c in red for t in state[c["id"]]["tests"] if t["status"] == "fail"], "discarded": discarded})
    if not red:
        return False, [], [], discarded
    red_sources = {testgen.test_path(c["id"]): state[c["id"]]["source"] for c in red}
    for p, s in {**red_sources, **(extra_tests or {})}.items():
        (work / p).parent.mkdir(parents=True, exist_ok=True)
        (work / p).write_text(s, encoding="utf-8")
    gh.git(work, "add", "-A")
    gh.git(work, "commit", "-q", "-m", f"test: failing spec tests for {', '.join(c['id'] for c in red)} (round {round_no})")
    red_commit = gh.git(work, "rev-parse", "HEAD").strip()

    failures, changed = extra_failures, {}
    for attempt in range(3):
        E("implement", "started", f"Implementing (attempt {attempt + 1})", "")
        changed = implement(work, feature, clauses, plan, red_sources, failures, "medium" if attempt == 0 else "high")
        if not changed:
            failures = "no files were produced; output complete files in the required format"
            continue
        _apply(work, changed)
        diff = gh.git(work, "diff", "-U2")
        E("implement", "progress", f"Applied {len(changed)} file(s)", ", ".join(changed),
          {"diff_preview": "\n".join(diff.splitlines()[:30]), "files_changed": list(changed)})
        res, bad = _suite(work)
        if not bad:
            break
        failures = "\n".join(f"{r['name']}: {r['output'][:400]}" for r in bad[:6])
        E("implement", "progress", f"{len(bad)} test(s) still failing", _first_line(bad[0]["output"]))
    else:
        E("implement", "failed", "Could not reach green after 3 attempts", failures[:300])
        return False, [], list(changed), discarded
    E("implement", "done", "Implementation complete", ", ".join(changed))

    rows = []
    for c in red:
        for t in state[c["id"]]["tests"]:
            if t["status"] != "fail":
                continue
            after = next((r for r in res if r["name"] == t["name"]), None)
            rows.append({"clause_id": c["id"], "source": c.get("source", ""), "test": t["name"].split("::")[-1],
                         "files": ", ".join(changed), "before": _first_line(t["output"]),
                         "after": "passed" if after and after["status"] == "pass" else "FAILED"})
    E("tests_green", "done", f"{len(rows)} test(s) flipped red → green; {len(res)} total passing",
      "existing tests still pass", {"tests": [{"name": r["test"], "clause_id": r["clause_id"], "status": "pass",
                                                "output_snippet": r["before"]} for r in rows], "evidence": rows})
    return True, rows, list(changed), discarded


def _inspect(E, work, spec_text, all_clauses, feature_clauses, base_commit):
    scope = review.scope_check([p for p in gh.git(work, "diff", "--name-only", base_commit).split()])
    diff = gh.git(work, "diff", base_commit)
    changed = [p for p in gh.git(work, "diff", "--name-only", base_commit).split() if not p.startswith("tests/")]
    E("inspect", "started", "Independent inspection", scope["summary"])
    result = review.review_changes(work, spec_text, all_clauses, diff, changed,
                                   lambda stage, status, title, detail="", data=None: E("inspect", "progress", title, detail, data),
                                   skip_quotes={c["quote"] for c in feature_clauses})
    return scope, result


def run_build(run_id, repo, feature, token, user="", auto=False, merge=True, dry=False):
    E = lambda stage, status, title, detail="", data=None: events.emit(run_id, stage, status, title, detail, data)  # noqa: E731
    try:
        with RUN_SLOTS:
            _run_build(E, run_id, repo, feature, token, user, auto, merge, dry)
    except Exception as exc:  # noqa: BLE001
        E("pr", "failed", "Run failed", f"{type(exc).__name__}: {exc}")
        events.update_run(run_id, status="failed")


def _run_build(E, run_id, repo, feature, token, user, auto, merge, dry=False):
    g = gh.client(token)
    E("ticket", "done", "Request received", feature, {"repo": repo})
    acc = gh.access(g, repo)
    if not acc["read"]:
        E("ticket", "failed", "Scope: ❌ your GitHub account cannot read this repository")
        return
    forked = not acc["write"]  # no write access: work in a fork and open the PR upstream
    push_repo, head_owner = repo, None
    if forked:
        E("ticket", "progress", "No write access here, so PRProof will fork it to your account", "The PR opens from your fork")
        me = g.get_user()
        fk = me.create_fork(g.get_repo(repo))
        for _ in range(40):  # forks are created asynchronously
            try:
                if list(itertools.islice(fk.get_commits(), 1)):
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(3)
        push_repo, head_owner = fk.full_name, me.login
    work = gh.clone(repo, token, WORK / run_id / "repo")
    base_commit = gh.git(work, "rev-parse", "HEAD").strip()
    branch = f"feat/{slugify(user or 'user', 12)}-{slugify(feature)}"
    gh.git(work, "checkout", "-q", "-b", branch)
    if not any(f for f in work.rglob("*.py") if ".git" not in f.parts):  # tests are executed with pytest, so a non-Python repo cannot be proven
        E("spec", "failed", "PRProof builds with Python tests today", "This repository has no Python code, so it cannot run the tests it writes. "
          "Try a Python repository such as tinyjwt.")
        events.update_run(run_id, status="failed")
        return
    spec_text = spec.load_spec(work)
    has_specs = bool(spec_text)
    if not has_specs:  # requirements come from the user's own words instead of a standard
        spec_text = f"## Feature request\n{feature}\n"
        E("spec", "progress", "No specs/ folder in this repo", "Requirements will be derived from your request instead of a standard")

    # --- spec (human gate) ---
    E("spec", "started", "Reading spec, selecting clauses", "")
    clauses = spec.pick_feature_clauses(feature, spec_text)
    while True:
        E("spec", "waiting_human", f"{len(clauses)} clauses — approve spec?", "", {"clauses": clauses})
        d = GATES.wait(run_id, "spec", auto)
        if d["decision"] == "approve":
            break
        if d["decision"] == "timeout":
            E("spec", "failed", "Timed out waiting for approval")
            return
        E("spec", "started", "Revising clauses from your edit", d["text"])
        clauses = spec.pick_feature_clauses(feature, spec_text, d["text"], clauses)
    E("spec", "done", f"Spec approved: {len(clauses)} clauses", "", {"clauses": clauses})

    # --- plan ---
    E("plan", "started", "Planning", "")
    plan = make_plan(feature, clauses, gh.repo_files(work))
    E("plan", "done", "Plan ready", "\n".join(f"• {b}" for b in plan.get("bullets", [])[:8]), {"files": plan.get("files", [])})

    # --- round 1 ---
    all_clauses = spec.extract_all_clauses(spec_text) if has_specs else []
    ok, rows, files, _ = _tdd_cycle(E, work, feature, clauses, plan, 1)
    if not ok:
        E("pr", "failed", "No green implementation; stopping",
          "Either no failing test could be produced for this request (the feature may already work), or the implementation never passed the tests.")
        events.update_run(run_id, status="failed")
        return
    all_rows = list(rows)
    scope = review.scope_check(gh.git(work, "diff", "--name-only", base_commit).split())
    if not has_specs:
        E("inspect", "done", "Inspection skipped (no specs/ folder)", "Open the PR in PRProof's review to get the rubric review")
    for rnd in range(2 if has_specs else 0):  # inspect -> feed violations back (max 2 rounds)
        scope, result = _inspect(E, work, spec_text, all_clauses, clauses, base_commit)
        E("inspect", "done" if not result["violations"] else "failed",
          "Inspection clean" if not result["violations"] else f"{len(result['violations'])} violation(s) proven",
          scope["summary"], {"violations": [{k: v[k] for k in v if k != "test_source"} for v in result["violations"]]})
        if not result["violations"]:
            break
        extra = {testgen.test_path(v["clause_id"]): v["test_source"] for v in result["violations"]}
        for p, s in extra.items():
            (work / p).write_text(s, encoding="utf-8")
        fb = "\n".join(f"{v['clause_id']} {v['quote']}\n{v['output_snippet']}" for v in result["violations"])
        gh.git(work, "add", "-A")
        gh.git(work, "commit", "-q", "-m", "test: failing tests proving violations found by inspection")
        changed = implement(work, feature, clauses, plan, extra, fb, "high")
        _apply(work, changed)
        res, bad = _suite(work)
        for v in result["violations"]:
            all_rows.append({"clause_id": v["clause_id"], "source": v["source"], "test": v["test_name"].split("::")[-1],
                             "files": ", ".join(changed), "before": _first_line(v["output_snippet"]),
                             "after": "passed" if not bad else "FAILED"})
        E("tests_green", "done", "Inspection findings fixed" if not bad else "Fix attempt failed", "", {"evidence": all_rows})
    gh.git(work, "add", "-A")
    gh.git(work, "commit", "-q", "-m", f"feat: {feature[:60]}\n\nClauses: {', '.join(c['id'] for c in clauses)}")

    if dry:
        E("pr", "done", "Dry run: stopped before pushing / opening a PR", "", {"evidence": all_rows})
        events.update_run(run_id, status="done")
        return
    # --- PR ---
    E("pr", "started", "Opening pull request", branch)
    gh.push(work, push_repo, token, branch, force=True)
    run = events.get_run(run_id) or {}
    body = (f"## PRProof evidence — {feature}\n\n{evidence_table(all_rows)}\n"
            f"{scope['summary']}\n\nRun: {run_id}\n")
    gr = g.get_repo(repo)
    head_ref = f"{head_owner}:{branch}" if forked else branch
    existing = list(gr.get_pulls(state="open", head=f"{head_owner or gr.owner.login}:{branch}"))
    if existing:  # a retry of the same feature: update the open PR instead of failing
        pr = existing[0]
        pr.edit(body=body)
    else:
        pr = gr.create_pull(title=f"feat: {feature[:70]}", body=body, head=head_ref, base=acc["default_branch"])
    events.update_run(run_id, pr_url=pr.html_url)
    E("pr", "done", f"PR #{pr.number} opened", pr.html_url, {"pr_number": pr.number, "pr_url": pr.html_url})

    # --- human review loop ---
    rnd = 1
    while True:
        E("human_review", "waiting_human", "Approve or request changes", pr.html_url)
        d = GATES.wait(run_id, "review", auto)
        E("human_review", "done", f"Decision: {d['decision']}", d["text"], {"decision": d["decision"], "text": d["text"]})
        if d["decision"] == "approve":
            if forked:
                E("merged", "done", "Approved. The PR is open upstream and the maintainers decide on merging", pr.html_url)
            elif merge:
                pr.merge(merge_method="squash")
                E("merged", "done", "Squash-merged ✅", pr.html_url)
            events.update_run(run_id, status="done")
            return
        if d["decision"] == "timeout":
            events.update_run(run_id, status="done")
            return
        rnd += 1
        E("revise", "started", f"Round {rnd}: revising from feedback", d["text"])
        new = spec.pick_feature_clauses(feature, spec_text, d["text"], clauses)
        old_req = {c["id"]: c.get("requirement") for c in clauses}
        added = [c for c in new if c["id"] not in old_req or c.get("requirement") != old_req[c["id"]]] or new
        clauses = new
        E("spec", "done", f"Round {rnd}: {len(added)} new/changed clause(s)", "", {"clauses": clauses})
        plan = make_plan(feature, clauses, gh.repo_files(work))
        ok, rows, files, discarded = _tdd_cycle(E, work, feature, added, plan, rnd)
        if not ok:
            if discarded and all("non-discriminating" in d["why"] for d in discarded):
                msg = (f"Round {rnd}: the feedback is already satisfied by the current implementation — "
                       f"{len(discarded)} new test(s) passed before any change, so they were discarded as non-discriminating. No code change needed.")
                pr.create_issue_comment("### PRProof\n\n" + msg)
                E("pr", "done", f"Round {rnd}: already satisfied, nothing to change", msg, {"pr_number": pr.number, "pr_url": pr.html_url})
                continue
            E("pr", "failed", f"Round {rnd} did not reach green", "")
            return
        gh.git(work, "add", "-A")
        gh.git(work, "commit", "-q", "-m", f"feat: address review feedback (round {rnd})")
        gh.push(work, push_repo, token, branch)
        all_rows += rows
        pr.edit(body=f"## PRProof evidence — {feature}\n\n{evidence_table(all_rows)}\nRun: {run_id}\n")
        pr.create_issue_comment(f"### Round {rnd} evidence\n\n{evidence_table(rows)}")
        E("pr", "done", f"Round {rnd} pushed to PR #{pr.number}", pr.html_url, {"pr_number": pr.number, "pr_url": pr.html_url})


# ---------- PR review ----------
def run_review(run_id, repo, pr_number, token, comment=False):
    E = lambda stage, status, title, detail="", data=None: events.emit(run_id, stage, status, title, detail, data)  # noqa: E731
    try:
        with RUN_SLOTS:
            _run_review(E, run_id, repo, pr_number, token, comment)
    except Exception as exc:  # noqa: BLE001
        E("verdict", "failed", "Review failed", f"{type(exc).__name__}: {exc}")
        events.update_run(run_id, status="failed")


def _has_specs(gr) -> bool:
    try:
        return bool(gr.get_contents("specs"))
    except Exception:  # noqa: BLE001
        return False


def _run_review(E, run_id, repo, pr_number, token, comment):
    """Review any PR: no specs required. Specs, when present, add a conformance check on top."""
    g = gh.client(token)
    acc = gh.access(g, repo)
    if not acc["read"]:
        E("received", "failed", "Scope: ❌ identity cannot read this repository")
        return
    gr = g.get_repo(repo)
    pr = gr.get_pull(pr_number)
    files = list(pr.get_files())
    changed = [f.filename for f in files]
    diff = "\n".join(f"--- {f.filename}\n{f.patch or ''}" for f in files)
    scope = review.scope_check(changed, write=acc["write"])
    meta = {"title": pr.title, "body": pr.body or "", "files": len(files), "additions": pr.additions,
            "deletions": pr.deletions, "author": pr.user.login}
    E("received", "done", f"PR #{pr_number}: {pr.title}", scope["summary"],
      {**{k: meta[k] for k in ("title", "files", "additions", "deletions", "author")}, "pr_number": pr_number, "pr_url": pr.html_url,
       "changed": changed[:40]})
    events.update_run(run_id, pr_url=pr.html_url)

    E("understand", "started", "Reading the change", "")
    summary = rubric.summarize(meta, diff)
    E("understand", "done", "What changed", summary, {"summary": summary})

    E("rubric", "started", f"Scoring the change against {len(rubric.RUBRIC)} review categories", "SubQ decision model")
    state = f"PR title: {meta['title']}\nPR description: {meta['body'][:1500]}\n\nDIFF:\n{diff}"
    sc = rubric.score_rubric(state)
    hot = rubric.pick_hot(sc["probs"])
    E("rubric", "done", f"Risk: {sc['risk']['label']}",
      ", ".join(rubric.BY_ID[h]["name"] for h in hot) or "No category above the threshold",
      {"probs": [{"id": c["id"], "name": c["name"], "p": round(sc["probs"].get(c["id"], 0), 2)} for c in rubric.RUBRIC],
       "risk": sc["risk"], "via": sc["via"], "hot": hot, "primary": sc.get("primary")})

    findings = []
    if hot:
        E("investigate", "started", "Looking for concrete problems in the flagged areas", "")
        findings = rubric.investigate(meta, diff, hot, sc["probs"])
        E("investigate", "done", f"{len(findings)} possible problem(s) found" if findings else "Nothing concrete to report", "")
    else:
        E("investigate", "done", "Nothing stood out", "")

    py = any(c.endswith(".py") for c in changed)
    specs = _has_specs(gr)
    can_run = py and gr.size <= rubric.SANDBOX_MAX_KB
    tests = {"run": 0, "failed": 0}
    work = None
    if (findings and can_run) or specs:
        E("prove", "started", "Preparing a sandbox to execute tests", "")
        work = gh.clone(repo, token, WORK / run_id / "repo")
        gh.git(work, "fetch", "-q", "origin", f"pull/{pr_number}/head:pr-{pr_number}")
        gh.git(work, "checkout", "-q", f"pr-{pr_number}")
    if findings and work and can_run:
        t = rubric.prove(work, findings, review.adjudicate, lambda stage, status, title, detail="", data=None: E("prove", status, title, detail, data))
        tests.update(t)
    else:
        why = ("this repository is too large for the test sandbox" if py and not can_run else
               "PRProof executes tests for Python projects today" if not py else "nothing to test")
        for f in findings:
            f["proof"] = f"Not executed: {why}"
    if specs and work:
        spec_text = spec.load_spec(work)
        if spec_text:
            res = review.review_changes(work, spec_text, spec.extract_all_clauses(spec_text), diff,
                                        [c for c in changed if not c.startswith("tests/")], lambda *a, **k: None)
            anchor = next((f for f in findings if f["status"] == "proven"), None)
            for v in res["violations"]:
                tests["run"] += 1
                tests["failed"] += 1
                if anchor:  # same defect already reported: the spec clause backs it up instead of becoming a second problem
                    anchor.setdefault("spec_refs", []).append({"source": v["source"], "quote": v["quote"]})
                else:
                    findings.append({"id": f"F{len(findings) + 1}", "category": "Spec conformance", "severity": "Major",
                                     "title": f"Breaks {v['source']}", "where": v["clause_id"], "why": f"The spec says: {v['quote']}",
                                     "fix": v.get("suggested_fix", ""), "effort": "Quick win", "testable": True, "status": "proven",
                                     "proof": v["output_snippet"], "test_name": v["test_name"]})
    E("prove", "done", f"{tests['run']} test(s) executed, {tests['failed']} failed" if tests["run"] else "No tests executed",
      "" if tests["run"] else (findings[0]["proof"] if findings else ""), {"tests": tests})

    verdict = rubric.verdict_for(findings)
    card = {"verdict": verdict, "risk": sc["risk"], "summary": summary, "findings": findings, "tests": tests,
            "scope": scope["summary"], "hot": hot, "specs": specs, "meta": {k: meta[k] for k in ("title", "files", "additions", "deletions", "author")}}
    E("verdict", "failed" if verdict["tone"] == "bad" else "done", verdict["label"], scope["summary"], card)
    body = rubric.render_comment(card)
    if comment:
        pr.create_issue_comment(body)
        state, desc = rubric.status_for(verdict, tests)
        try:  # a commit status lets branch protection block the merge when a defect is proven
            gr.get_commit(pr.head.sha).create_status(state, target_url=pr.html_url, description=desc, context="prproof/verify")
            E("comment", "running", f"Set the prproof/verify check to {state}", desc)
        except Exception as exc:  # noqa: BLE001
            E("comment", "running", "Could not set the merge check", str(exc)[:160])
        E("comment", "done", "Posted review to the PR", pr.html_url, {"pr_number": pr_number, "pr_url": pr.html_url})
    else:
        E("comment", "done", "Review ready (not posted to GitHub)", "", {"body": body})
    events.update_run(run_id, status="done")
