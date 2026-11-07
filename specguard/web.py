"""FastAPI app: GitHub OAuth login, repo graph data, build/review runs, SSE event stream, chat."""
import asyncio
import itertools
import json
import os
import secrets
import subprocess
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from github import GithubException
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import events, gh, graph, llm, pipeline, prs

load_dotenv()
STATIC = Path(__file__).parent / "static"
BASE = os.getenv("UI_BASE_URL", "http://localhost:8000").rstrip("/")
CLIENT_ID, CLIENT_SECRET = os.getenv("GITHUB_CLIENT_ID", ""), os.getenv("GITHUB_CLIENT_SECRET", "")
ALLOW_LOCAL = os.getenv("SPECGUARD_ALLOW_LOCAL_LOGIN") == "1"  # dev only: log in with the local `gh` CLI token
SESSIONS: dict = {}  # sid -> {login, name, avatar, token}  (tokens live in server memory only)

app = FastAPI(title="SpecGuard")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET") or secrets.token_hex(32),
                   same_site="lax", https_only=False, max_age=60 * 60 * 8)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def user(request: Request) -> dict:
    u = SESSIONS.get(request.session.get("sid", ""))
    if not u:
        raise HTTPException(401, "not signed in")
    return u


PUBLIC_TOKEN = os.getenv("PUBLIC_READ_TOKEN", "")  # read-only token used for guests (public repos only)
if not PUBLIC_TOKEN and ALLOW_LOCAL:  # dev convenience: borrow the local gh login so guests aren't capped at 60 req/hour
    PUBLIC_TOKEN = subprocess.run(["gh", "auth", "token", *(["--user", os.getenv("GITHUB_ORG", "")] if os.getenv("GITHUB_ORG") else [])],
                                  capture_output=True, text=True).stdout.strip()
ANON_RUNS: dict = {}  # guest id -> reviews started (free preview quota)
ANON_LIMIT = 3


def viewer(request: Request) -> dict:
    """Signed-in user, or a guest limited to public repos (read + review, no posting)."""
    u = SESSIONS.get(request.session.get("sid", ""))
    if u:
        return u
    gid = request.session.get("guest")
    if not gid:
        gid = request.session["guest"] = secrets.token_hex(6)
    return {"login": f"guest-{gid}", "name": "Guest", "avatar": "", "token": PUBLIC_TOKEN, "anon": True}


def _missing_msg(v: dict, what: str = "repository", exc=None) -> str:
    if exc is not None and "rate limit" in str(exc).lower():
        return "GitHub is rate-limiting requests right now. Please try again in a few minutes."
    if v.get("anon"):
        return f"We couldn't open that {what}. It may not exist, or it's private. Sign in with GitHub to open private ones you have access to."
    return f"Your GitHub account can't open that {what}. It may not exist, or you may not have access to it."


def _public_only(repo, v: dict):
    if v.get("anon") and repo.private:
        raise HTTPException(403, "That repository is private. Sign in with GitHub to use repositories you can access.")


def _has_dir(r, path: str) -> bool:
    try:
        return bool(r.get_contents(path))
    except Exception:  # noqa: BLE001
        return False


def _login(request: Request, token: str, profile: dict):
    sid = secrets.token_hex(16)
    SESSIONS[sid] = {"login": profile["login"], "name": profile.get("name") or profile["login"],
                     "avatar": profile.get("avatar_url", ""), "token": token}
    request.session["sid"] = sid


# ---------- auth ----------
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/auth/config")
def auth_config():
    return {"github": bool(CLIENT_ID and CLIENT_SECRET), "local": ALLOW_LOCAL}


@app.get("/auth/github")
def auth_github(request: Request):
    if not (CLIENT_ID and CLIENT_SECRET):
        return HTMLResponse("<h3>GitHub OAuth is not configured</h3><p>Set GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET.</p>", 503)
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    q = urllib.parse.urlencode({"client_id": CLIENT_ID, "scope": "repo read:user", "state": state,
                                "redirect_uri": f"{BASE}/auth/callback"})
    return RedirectResponse(f"https://github.com/login/oauth/authorize?{q}")


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = "", state: str = ""):
    if not code or state != request.session.get("oauth_state"):
        raise HTTPException(400, "bad oauth state")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post("https://github.com/login/oauth/access_token", headers={"Accept": "application/json"},
                         data={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "code": code,
                               "redirect_uri": f"{BASE}/auth/callback"})
        token = r.json().get("access_token")
        if not token:
            raise HTTPException(400, "GitHub did not return a token")
        prof = (await c.get("https://api.github.com/user", headers={"Authorization": f"Bearer {token}"})).json()
    _login(request, token, prof)
    return RedirectResponse("/")


@app.get("/auth/local")
def auth_local(request: Request):
    if not ALLOW_LOCAL:
        raise HTTPException(404)
    gh_user = os.getenv("GITHUB_ORG", "")
    token = subprocess.run(["gh", "auth", "token", *(["--user", gh_user] if gh_user else [])],
                           capture_output=True, text=True).stdout.strip()
    prof = gh.client(token).get_user()
    _login(request, token, {"login": prof.login, "name": prof.name, "avatar_url": prof.avatar_url})
    return RedirectResponse("/" + request.query_params.get("to", ""))


@app.post("/auth/logout")
def logout(request: Request):
    SESSIONS.pop(request.session.get("sid", ""), None)
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    u = SESSIONS.get(request.session.get("sid", ""))
    return {"login": u["login"], "name": u["name"], "avatar": u["avatar"]} if u else JSONResponse({"login": None}, 401)


# ---------- repos ----------
_GRAPH_CACHE: dict = {}


def _row(r) -> dict:
    p = r.permissions
    parent = None
    if r.fork:
        try:
            parent = r.parent.full_name if r.parent else None
        except Exception:  # noqa: BLE001
            parent = None
    return {"full_name": r.full_name, "name": r.name, "owner": r.owner.login, "language": r.language,
            "stars": r.stargazers_count, "forks": r.forks_count, "size": r.size, "private": r.private,
            "issues": r.open_issues_count, "pushed_at": r.pushed_at.isoformat() if r.pushed_at else "",
            "created_at": r.created_at.isoformat() if r.created_at else "", "topics": list(r.topics or []),
            "description": r.description or "", "write": bool(p and (p.push or p.admin)),
            "fork": r.fork, "parent": parent, "archived": r.archived, "html_url": r.html_url,
            "default_branch": r.default_branch, "license": (r.license.spdx_id if r.license else ""), "homepage": r.homepage or ""}


def _repo_rows(g, owner: str = "", me: str = "") -> list:
    """Repos of the signed-in user (incl. private) when owner is empty or is the user; otherwise the owner's public repos."""
    if not owner or owner.lower() == me.lower():
        it = g.get_user().get_repos(sort="pushed", direction="desc")
    else:
        try:
            it = g.get_user(owner).get_repos(type="owner", sort="pushed", direction="desc")
            next(iter(it), None)  # force the first request so a bad name fails here
        except GithubException as exc:
            raise HTTPException(404, "We couldn't find that GitHub user or organisation. Check the spelling.") from exc
    return [_row(r) for r in itertools.islice(it, 60)]


@app.get("/api/repos")
def repos(u: dict = Depends(user)):
    return _repo_rows(gh.client(u["token"]))


@app.get("/api/repo/{owner}/{name}/issues")
def repo_issues(owner: str, name: str, v: dict = Depends(viewer)):
    """Open issues (not PRs), so a build can start from one."""
    g = gh.client(v["token"])
    try:
        r = g.get_repo(f"{owner}/{name}")
    except GithubException as exc:
        raise HTTPException(404, _missing_msg(v, exc=exc)) from exc
    _public_only(r, v)
    out = []
    for i in itertools.islice(r.get_issues(state="open", sort="updated", direction="desc"), 30):
        if i.pull_request:
            continue
        out.append({"number": i.number, "title": i.title, "body": (i.body or "")[:1200], "url": i.html_url,
                    "author": i.user.login if i.user else "", "labels": [l.name for l in i.labels][:4]})
        if len(out) >= 12:
            break
    return {"repo": f"{owner}/{name}", "issues": out}


@app.get("/api/owner/{owner}/repos")
def owner_repos(owner: str, v: dict = Depends(viewer)):
    """Light repo list for pickers (no categorisation). '@me' means the signed-in user, including private repos."""
    me = "" if v.get("anon") else v["login"]
    if owner == "@me":
        if not me:
            raise HTTPException(400, "Sign in with GitHub to list your repositories.")
        owner = ""
    elif not owner.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise HTTPException(400, "That doesn't look like a GitHub user or organisation name.")
    key = (v["login"], owner.lower() or me.lower(), "list")
    hit = _GRAPH_CACHE.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    rows = _repo_rows(gh.client(v["token"]), owner, me)
    if v.get("anon"):
        for r in rows:
            r["write"] = False
    out = {"owner": owner or me, "repos": rows}
    _GRAPH_CACHE[key] = (time.time(), out)
    return out


@app.get("/api/graph")
def repo_graph(refresh: int = 0, owner: str = "", v: dict = Depends(viewer)):
    owner = owner.strip()
    if owner and not owner.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise HTTPException(400, "That doesn't look like a GitHub user or organisation name.")
    if v.get("anon") and not owner:
        raise HTTPException(400, "Enter a GitHub user or organisation to explore, or sign in to see your own repositories.")
    me = "" if v.get("anon") else v["login"]
    key = (v["login"], owner.lower() or me.lower())
    hit = _GRAPH_CACHE.get(key)
    if hit and not refresh and time.time() - hit[0] < 600:
        return hit[1]
    g = gh.client(v["token"])
    rows = _repo_rows(g, owner, me)
    if not rows:
        raise HTTPException(404, "That account has no public repositories to show.")

    def readme(row):
        try:
            row["readme"] = g.get_repo(row["full_name"], lazy=True).get_readme().decoded_content.decode("utf-8", "replace")[:1500]
        except Exception:  # noqa: BLE001
            row["readme"] = ""
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(readme, rows))
    if v.get("anon"):  # guests borrow a read token: never present its permissions as theirs
        for r in rows:
            r["write"] = False
    data = {"owner": owner or me, "mine": (owner or me).lower() == me.lower() and bool(me),
            "repos": [{k: x for k, x in r.items() if k != "readme"} for r in rows], **graph.build_graph(rows)}
    _GRAPH_CACHE[key] = (time.time(), data)
    return data


@app.get("/api/repo/{owner}/{name}")
def repo_highlights(owner: str, name: str, v: dict = Depends(viewer)):
    g = gh.client(v["token"])
    full = f"{owner}/{name}"
    try:
        r = g.get_repo(full)
    except GithubException as exc:
        raise HTTPException(404 if exc.status in (403, 404) else 502, _missing_msg(v, exc=exc)) from exc
    _public_only(r, v)
    acc = gh.access(g, full)
    if v.get("anon"):
        acc["write"] = False  # guests borrow a read token: never present its permissions as theirs
    try:
        langs = {k: b for k, b in r.get_languages().items() if isinstance(b, int)}  # GitHub may return a status message instead
    except Exception:  # noqa: BLE001
        langs = {}
    total = sum(langs.values()) or 1
    lang_pct = [{"name": k, "pct": round(100 * b / total, 1)} for k, b in sorted(langs.items(), key=lambda kv: -kv[1])[:5]]
    try:
        readme = r.get_readme().decoded_content.decode("utf-8", "replace")[:3000]
    except Exception:  # noqa: BLE001
        readme = ""
    try:
        top = [c.path + ("/" if c.type == "dir" else "") for c in r.get_contents("")][:60]
    except Exception:  # noqa: BLE001
        top = []
    commits = [{"sha": c.sha[:7], "msg": c.commit.message.splitlines()[0][:90], "author": c.commit.author.name,
                "date": c.commit.author.date.isoformat()} for c in itertools.islice(r.get_commits(), 6)]
    prs = [{"number": p.number, "title": p.title[:90], "user": p.user.login, "head": p.head.ref}
           for p in itertools.islice(r.get_pulls(state="open"), 10)]
    has_specs = any(t.rstrip("/") == "specs" for t in top)
    has_tests = any(t.rstrip("/") in ("tests", "test", "__tests__", "spec") for t in top)
    has_ci = any(t.rstrip("/") == ".github" for t in top) and _has_dir(r, ".github/workflows")
    days = (time.time() - r.pushed_at.timestamp()) / 86400 if r.pushed_at else 9999
    health = [
        {"label": "Activity", "ok": days < 90, "detail": "pushed today" if days < 1 else f"last push {int(days)} days ago"},
        {"label": "Tests", "ok": has_tests, "detail": "tests folder found" if has_tests else "no tests folder found"},
        {"label": "CI", "ok": has_ci, "detail": "GitHub Actions workflows" if has_ci else "no workflows found"},
        {"label": "License", "ok": bool(r.license), "detail": r.license.name if r.license else "none declared"},
        {"label": "Specs", "ok": True if has_specs else None, "detail": "specs/ folder found: reviews also check spec conformance" if has_specs
         else "none (optional): reviews still run with the benchmark rubric"},
        {"label": "Open PRs", "ok": None, "detail": f"{len(prs)}{'+' if len(prs) == 10 else ''} open"},
    ]
    prompt = f"""Describe this GitHub repository for a developer deciding whether to use or contribute to it.
Return ONLY JSON, plain English, no markdown, no asterisks:
{{"tagline": "<=12 words", "what": "two short sentences: what it is and who it is for",
 "stack": ["up to 6 short technology names"], "notable": ["2-3 short observations about structure or activity"],
 "watch": ["0-3 short risks or gaps worth knowing"]}}

Repo {full}: {r.description}
Languages: {lang_pct}
Top-level files: {top}
Recent commits: {commits}
README:
{readme}"""
    try:
        report = llm.extract_json(llm.gemini(prompt, llm.FLASH, "low"))
    except Exception:  # noqa: BLE001
        report = {"tagline": r.description or "", "what": (readme[:200] or "No README."), "stack": [], "notable": [], "watch": []}
    return {"repo": full, "access": acc, "languages": langs, "lang_pct": lang_pct, "top": top, "commits": commits, "prs": prs,
            "has_specs": has_specs, "has_tests": has_tests, "report": report, "health": health, "stars": r.stargazers_count,
            "description": r.description or "", "html_url": r.html_url, "private": r.private}


_PR_CACHE: dict = {}


@app.get("/api/repo/{owner}/{name}/prs")
def repo_prs(owner: str, name: str, v: dict = Depends(viewer)):
    """Open PRs grouped by change type, each with a review-priority score (SubQ decision model)."""
    full = f"{owner}/{name}"
    key = (v["login"], full)
    hit = _PR_CACHE.get(key)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    g = gh.client(v["token"])
    try:
        r = g.get_repo(full)
    except GithubException as exc:
        raise HTTPException(404, _missing_msg(v, exc=exc)) from exc
    _public_only(r, v)
    raw = list(itertools.islice(r.get_pulls(state="open", sort="updated", direction="desc"), 20))

    def one(p):
        try:
            files = [f.filename for f in itertools.islice(p.get_files(), 25)]
        except Exception:  # noqa: BLE001
            files = []
        return {"number": p.number, "title": p.title, "author": p.user.login if p.user else "", "additions": p.additions,
                "deletions": p.deletions, "files": files, "body": p.body or "", "created_at": p.created_at.isoformat(),
                "url": p.html_url, "draft": bool(p.draft)}
    with ThreadPoolExecutor(max_workers=8) as pool:
        plist = list(pool.map(one, raw))
    decisions = prs.classify_prs(plist)
    groups = prs.group_prs([{k: x for k, x in p.items() if k != "body"} | {"body": ""} for p in plist], decisions)
    out = {"repo": full, "total": len(plist), "groups": groups, "method": "decision-model" if decisions else "none"}
    _PR_CACHE[key] = (time.time(), out)
    return out


@app.get("/api/owner/{owner}/prs")
def owner_prs(owner: str, v: dict = Depends(viewer)):
    """Open PRs across everything this owner has, grouped by repository, each typed and scored by the decision model."""
    key = (v["login"], owner.lower(), "owner")
    hit = _PR_CACHE.get(key)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    g = gh.client(v["token"])
    try:
        found = list(itertools.islice(g.search_issues(f"is:pr is:open user:{owner} archived:false", sort="updated", order="desc"), 30))
    except GithubException as exc:
        raise HTTPException(404, _missing_msg(v, "user or organisation", exc)) from exc

    def one(i):
        try:
            p = i.as_pull_request()
            files = [f.filename for f in itertools.islice(p.get_files(), 25)]
            full = i.repository.full_name
            return {"key": f"{full}#{i.number}", "repo": full, "number": i.number, "title": i.title,
                    "author": i.user.login if i.user else "", "additions": p.additions, "deletions": p.deletions, "files": files,
                    "body": (i.body or "")[:800], "url": i.html_url, "draft": bool(p.draft), "private": i.repository.private}
        except Exception:  # noqa: BLE001
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        plist = [p for p in pool.map(one, found) if p and not (v.get("anon") and p["private"])]
    decisions = prs.classify_prs(plist)
    for p in plist:
        p["type"] = prs.NAME.get((decisions.get(p["key"]) or {}).get("type"), "Other")
    out = {"owner": owner, "total": len(plist), "groups": prs.group_by_repo([{**p, "body": "", "files": []} for p in plist], decisions),
           "method": "decision-model" if decisions else "none"}
    _PR_CACHE[key] = (time.time(), out)
    return out


@app.get("/api/pr/check")
def pr_check(url: str, v: dict = Depends(viewer)):
    """Validate a PR link and say clearly why it can't be reviewed."""
    try:
        full, number = gh.parse_pr(url)
    except ValueError as exc:
        raise HTTPException(400, "That doesn't look like a PR link. Use https://github.com/owner/repo/pull/123") from exc
    g = gh.client(v["token"])
    try:
        r = g.get_repo(full)
        pr = r.get_pull(number)
    except GithubException as exc:
        raise HTTPException(404, _missing_msg(v, "pull request", exc)) from exc
    _public_only(r, v)
    if pr.state != "open":
        raise HTTPException(409, f"PR #{number} is {'merged' if pr.merged else 'closed'}. SpecGuard reviews open PRs. Paste one that is still open.")
    return {"ok": True, "repo": full, "number": number, "title": pr.title, "author": pr.user.login, "files": pr.changed_files, "size": r.size}


# ---------- runs ----------
def _start(target, *args, **kw):
    threading.Thread(target=target, args=args, kwargs=kw, daemon=True).start()


@app.post("/api/build")
async def build(request: Request, u: dict = Depends(user)):
    b = await request.json()
    repo = gh.parse_repo(b["repo"], u["login"])
    feature = b["feature"].strip()
    if not feature:
        raise HTTPException(400, "describe the feature")
    run = events.create_run("build", feature, repo, u["login"])
    _start(pipeline.run_build, run["run_id"], repo, feature, u["token"], u["login"])
    return {"run_id": run["run_id"]}


@app.post("/api/review")
async def review(request: Request, v: dict = Depends(viewer)):
    b = await request.json()
    repo = gh.parse_repo(b["repo"], "" if v.get("anon") else v["login"])
    comment = bool(b.get("comment"))
    if v.get("anon"):
        if ANON_RUNS.get(v["login"], 0) >= ANON_LIMIT:
            raise HTTPException(429, "Free preview limit reached (3 reviews). Sign in with GitHub to keep going.")
        try:
            _public_only(gh.client(v["token"]).get_repo(repo), v)
        except GithubException as exc:
            raise HTTPException(404, _missing_msg(v)) from exc
        ANON_RUNS[v["login"]] = ANON_RUNS.get(v["login"], 0) + 1
        comment = False
    run = events.create_run("review", f"PR #{b['pr']}", repo, v["login"])
    _start(pipeline.run_review, run["run_id"], repo, int(b["pr"]), v["token"], comment=comment)
    return {"run_id": run["run_id"]}


def _own_run(run_id: str, u: dict) -> dict:
    run = events.get_run(run_id)
    if not run or run.get("slack_user") != u["login"]:
        raise HTTPException(404, "run not found")
    return run


@app.get("/api/runs")
def runs(u: dict = Depends(viewer)):
    return [r for r in events.list_runs(50) if r.get("slack_user") == u["login"]][:20]


@app.get("/api/runs/{run_id}")
def run_info(run_id: str, u: dict = Depends(viewer)):
    return {"run": _own_run(run_id, u), "events": events.get_events(run_id)}


@app.post("/api/runs/{run_id}/gate")
async def gate(run_id: str, request: Request, u: dict = Depends(viewer)):
    _own_run(run_id, u)
    b = await request.json()
    ok = pipeline.GATES.resolve(run_id, b["name"], b["decision"], b.get("text", ""))
    return {"ok": ok}


@app.post("/api/runs/{run_id}/comment")
async def post_comment(run_id: str, u: dict = Depends(user)):
    """Post a finished review to the GitHub PR (explicit user action)."""
    run = _own_run(run_id, u)
    evs = [e for e in events.get_events(run_id) if e["stage"] == "comment" and (e["data"] or {}).get("body")]
    if run.get("kind") != "review" or not evs:
        raise HTTPException(400, "no review to post")
    pr_number = int(run["feature"].split("#")[-1])
    pr = gh.client(u["token"]).get_repo(run["repo"]).get_pull(pr_number)
    c = pr.create_issue_comment(evs[-1]["data"]["body"])
    events.emit(run_id, "comment", "done", "Posted review to the PR", c.html_url, {"pr_number": pr_number, "pr_url": c.html_url})
    return {"url": c.html_url}


@app.get("/api/runs/{run_id}/stream")
async def stream(run_id: str, request: Request, u: dict = Depends(viewer)):
    _own_run(run_id, u)
    after = int(request.headers.get("last-event-id") or request.query_params.get("after") or 0)

    async def gen():
        nonlocal after
        idle = 0
        while True:
            evs = events.get_events(run_id, after)
            for e in evs:
                after = e["seq"]
                yield f"id: {e['seq']}\ndata: {json.dumps(e)}\n\n"
            run = events.get_run(run_id) or {}
            idle = 0 if evs else idle + 1
            if run.get("status") in ("done", "failed") and idle >= 2:
                yield "event: end\ndata: {}\n\n"
                return
            if await request.is_disconnected():
                return
            await asyncio.sleep(0.6)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# ---------- chat ----------
ABOUT = """SpecGuard is an agent that builds features and reviews PRs against written specs (RFCs, standards, API contracts).
Build: request -> spec clauses cited from the repo's specs/ folder (you approve) -> plan -> tests written FIRST and shown red ->
implementation -> same tests green + existing tests still pass -> independent inspection (SubQ routes which clauses the change
touches, generated tests prove violations) -> PR with an evidence table -> you approve or request changes (new round on the same PR).
Review: pick a PR; deterministic scope check (access + sensitive paths like .github/workflows) plus a spec agent that comments
ONLY on violations proven by a failing test, otherwise says 'spec-conformant for clauses X, Y'.
Rules: a test that passes before implementation is discarded as non-discriminating; tests that error are regenerated once, then dropped as untestable;
never a violation without a failing test. Repos need markdown specs under specs/."""


@app.post("/api/chat")
async def chat(request: Request, u: dict = Depends(user)):
    b = await request.json()
    ctx = ""
    if b.get("repo"):
        try:
            h = repo_highlights(*b["repo"].split("/", 1), v=u)
            ctx = f"\nSelected repo {h['repo']}: access={h['access']}, health={h['health']}, open PRs={h['prs']}\nReport: {h['report']}"
        except Exception as exc:  # noqa: BLE001
            ctx = f"\n(could not read selected repo: {exc})"
    history = "\n".join(f"{m['role']}: {m['text']}" for m in b.get("history", [])[-6:])
    prompt = (f"You are the SpecGuard assistant inside its web app. Signed-in GitHub user: {u['login']}.\n{ABOUT}{ctx}\n\n"
              f"Conversation so far:\n{history}\n\nUser: {b['message']}\n\nAnswer briefly and concretely. If they want to build or "
              f"review something, tell them which button to use (Create a feature / Read a repo / Explore).")
    text = await asyncio.to_thread(llm.gemini, prompt, llm.FLASH, "low")
    return {"reply": text}
