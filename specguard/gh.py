"""GitHub helpers: repo parsing, access checks, clone/push, PRs. Tokens are passed in, never stored."""
import os
import re
import shutil
import subprocess
from pathlib import Path

from github import Auth, Github

from .events import redact

GIT_ID = ["-c", "user.name=SpecGuard", "-c", "user.email=specguard@users.noreply.github.com"]


def client(token: str) -> Github:
    return Github(auth=Auth.Token(token), per_page=100)


def parse_repo(text: str, default_owner: str = "") -> str:
    t = text.strip().rstrip("/")
    m = re.search(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/|$)", t)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    if re.fullmatch(r"[\w.-]+/[\w.-]+", t):
        return t
    if re.fullmatch(r"[\w.-]+", t) and default_owner:
        return f"{default_owner}/{t}"
    raise ValueError(f"cannot parse repo from {text!r}")


def parse_pr(text: str):
    m = re.search(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)", text)
    if m:
        return m.group(1), int(m.group(2))
    m = re.fullmatch(r"([\w.-]+/[\w.-]+)#(\d+)", text.strip())
    if m:
        return m.group(1), int(m.group(2))
    raise ValueError(f"cannot parse PR from {text!r}")


def access(g: Github, full_name: str) -> dict:
    """Scope gate: what can this identity do on the repo?"""
    repo = g.get_repo(full_name)
    p = repo.permissions
    return {"repo": full_name, "read": bool(p.pull or p.push or p.admin), "write": bool(p.push or p.admin),
            "admin": bool(p.admin), "private": repo.private, "default_branch": repo.default_branch}


def _run(args, cwd=None, check=True):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(redact((r.stderr or r.stdout))[-500:])
    return r


def clone(full_name: str, token: str, dest: Path, branch: str | None = None) -> Path:
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://x-access-token:{token}@github.com/{full_name}.git"
    cmd = ["git", "clone", "--quiet", "--depth", "50"] + (["--branch", branch] if branch else []) + [url, str(dest)]
    _run(cmd)
    _run(["git", "remote", "set-url", "origin", f"https://github.com/{full_name}.git"], cwd=dest)
    return dest


def git(dest: Path, *args, check=True) -> str:
    return _run(["git", *GIT_ID, *args], cwd=dest, check=check).stdout


def push(dest: Path, full_name: str, token: str, branch: str, force=False):
    url = f"https://x-access-token:{token}@github.com/{full_name}.git"
    _run(["git", "push", *(["--force"] if force else []), url, f"HEAD:refs/heads/{branch}"], cwd=dest)


def repo_files(dest: Path, exts=(".py",), skip=("tests", "test", "specs", ".git"), limit=40000) -> dict:
    """Source files (path -> text) for prompts, capped to `limit` chars total."""
    out, used = {}, 0
    for p in sorted(dest.rglob("*")):
        if not p.is_file() or p.suffix not in exts:
            continue
        rel = p.relative_to(dest).as_posix()
        if any(part in skip for part in rel.split("/")[:-1]) or rel.startswith(".git/"):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if used + len(text) > limit:
            break
        out[rel] = text
        used += len(text)
    return out
