"""CLI: python -m specguard build|review|serve."""
import argparse
import os
import subprocess
import sys

from . import events, gh, llm, pipeline


def get_token(user: str = "") -> str:
    tok = os.getenv("GITHUB_TOKEN")
    if tok:
        return tok
    try:
        who = ["--user", user] if user else []
        return subprocess.run(["gh", "auth", "token", *who], capture_output=True, text=True, check=True).stdout.strip()
    except Exception:  # noqa: BLE001
        sys.exit("No GitHub token: set GITHUB_TOKEN or run `gh auth login`")


def printer(ev):
    icon = {"started": "…", "progress": "·", "done": "✔", "failed": "✘", "waiting_human": "⏸"}.get(ev["status"], "?")
    print(f"[{ev['seq']:>3}] {icon} {ev['stage']:<14} {ev['title']}" + (f"\n        {ev['detail'][:300]}" if ev["detail"] else ""), flush=True)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(prog="specguard")
    ap.add_argument("--fresh", action="store_true", help="bypass the LLM cache")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("--repo", required=True); b.add_argument("--feature", required=True)
    b.add_argument("--yes", action="store_true", help="auto-approve gates"); b.add_argument("--dry", action="store_true", help="stop before pushing/PR")
    b.add_argument("--merge", action="store_true", help="with --yes: also merge"); b.add_argument("--gh-user", default=os.getenv("GITHUB_ORG", ""))
    r = sub.add_parser("review"); r.add_argument("--repo", required=True); r.add_argument("--pr", type=int, required=True)
    r.add_argument("--comment", action="store_true"); r.add_argument("--gh-user", default=os.getenv("GITHUB_ORG", ""))
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=8000)
    sub.add_parser("bench", help="score the review rubric on labelled diffs")
    a = ap.parse_args()
    llm.set_fresh(a.fresh)
    if a.cmd == "bench":
        from . import bench
        r = bench.main()
        print(bench.to_markdown(r))
        return
    if a.cmd == "serve":
        import uvicorn
        uvicorn.run("specguard.web:app", host="127.0.0.1", port=a.port)
        return
    events.add_sink(printer)
    token = get_token(a.gh_user)
    repo = gh.parse_repo(a.repo, a.gh_user)
    if a.cmd == "build":
        run = events.create_run("build", a.feature, repo, "cli")
        pipeline.run_build(run["run_id"], repo, a.feature, token, "cli", auto=a.yes, merge=a.merge, dry=a.dry)
    else:
        run = events.create_run("review", f"PR #{a.pr}", repo, "cli")
        pipeline.run_review(run["run_id"], repo, a.pr, token, comment=a.comment)
    print("run:", run["run_id"], "->", (events.get_run(run["run_id"]) or {}).get("status"))


if __name__ == "__main__":
    main()
