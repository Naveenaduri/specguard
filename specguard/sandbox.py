"""Sandbox: run pytest against a throwaway copy of a repo and report per-test results."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

IGNORE = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".venv", "node_modules")
TEST_BUG_EXC = {"SyntaxError", "IndentationError", "NameError", "UnboundLocalError", "ImportError", "ModuleNotFoundError"}
SECRET_ENV = ("GEMINI_API_KEY", "SUBQ_API_KEY", "GITHUB_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "MONGODB_URI")


def run_tests(repo_path, test_files: dict | None = None, only: list | None = None, timeout: int = 120):
    """Run pytest in a temp copy of repo_path.

    test_files: {relative_path: source} written into the copy before running.
    only: optional list of relative test paths to run (default: everything under tests/).
    Returns [{name, status: pass|fail|error, output}].
    """
    tmp = Path(tempfile.mkdtemp(prefix="specguard_"))
    try:
        work = tmp / "repo"
        shutil.copytree(repo_path, work, ignore=IGNORE)
        for rel, src in (test_files or {}).items():
            dest = work / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(src, encoding="utf-8")
        report = tmp / "report.json"
        targets = only or (list((test_files or {}).keys()) + ["tests"] if test_files else ["tests"])
        targets = [t for t in dict.fromkeys(targets) if (work / t).exists()]
        env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
        env["PYTHONPATH"] = str(work)
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
               "--continue-on-collection-errors", "--json-report", f"--json-report-file={report}", "--json-report-indent=0", *targets]
        try:
            proc = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return [{"name": "<suite>", "status": "error", "output": f"timeout after {timeout}s"}]
        if not report.exists():
            return [{"name": "<suite>", "status": "error", "output": (proc.stdout + proc.stderr)[-600:]}]
        return _parse(json.loads(report.read_text(encoding="utf-8")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _condense(longrepr: str, limit: int = 520) -> str:
    """Keep what matters from a pytest failure: the failing line with context, plus the E lines."""
    lines = str(longrepr).splitlines()
    idx = next((i for i, ln in enumerate(lines) if ln.startswith(">")), None)
    if idx is None:
        return "\n".join(lines[-8:])[-limit:]
    ctx = [ln for ln in lines[max(0, idx - 2): idx + 1]]
    errs = [ln for ln in lines[idx + 1:] if ln.startswith("E ")][:4]
    return "\n".join(ctx + errs)[:limit]


def _parse(data: dict):
    results = []
    # Collection errors (ImportError, SyntaxError) show up under collectors, not tests.
    for c in data.get("collectors", []):
        if c.get("outcome") == "failed":
            results.append({"name": c.get("nodeid", "<collect>"), "status": "error",
                            "output": str(c.get("longrepr", ""))[-600:]})
    for t in data.get("tests", []):
        outcome = t.get("outcome")
        if outcome == "passed":
            status, out = "pass", ""
        else:
            phase = "setup" if (t.get("setup") or {}).get("outcome") == "failed" else "call"
            call = t.get(phase) or {}
            raw = str(call.get("longrepr") or call.get("crash") or "")
            out = _condense(raw)
            tail = [ln for ln in raw.splitlines() if ln.startswith("E ")]
            exc = re.match(r"E\s+([\w.]+)", tail[-1]).group(1) if tail and re.match(r"E\s+([\w.]+)", tail[-1]) else ""
            # Test bugs (broken test code) are errors. A missing API / wrong behaviour at call time is a real failure (red).
            is_bug = phase == "setup" or exc.split(".")[-1] in TEST_BUG_EXC
            status = "error" if is_bug else "fail"
        results.append({"name": t["nodeid"], "status": status, "output": out})
    return results


if __name__ == "__main__":
    res = run_tests(sys.argv[1] if len(sys.argv) > 1 else "../tinyjwt")
    for r in res:
        print(r["status"].upper(), r["name"])
