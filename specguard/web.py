"""FastAPI app: GitHub OAuth login, repo graph data, build/review runs, SSE event stream, chat."""
import asyncio
import itertools
import json
import os
import secrets
import subprocess
import threading
import urllib.parse
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import events, gh, llm, pipeline

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
    return RedirectResponse("/")


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
@app.get("/api/repos")
def repos(u: dict = Depends(user)):
    g = gh.client(u["token"])
    out = []
    for r in itertools.islice(g.get_user().get_repos(sort="pushed", direction="desc"), 60):
        p = r.permissions
        out.append({"full_name": r.full_name, "name": r.name, "owner": r.owner.login, "language": r.language,
                    "stars": r.stargazers_count, "forks": r.forks_count, "size": r.size, "private": r.private,
                    "issues": r.open_issues_count, "pushed_at": r.pushed_at.isoformat() if r.pushed_at else "",
                    "description": r.description or "", "write": bool(p and (p.push or p.admin)), "fork": r.fork})
    return out


@app.get("/api/repo/{owner}/{name}")
def repo_highlights(owner: str, name: str, u: dict = Depends(user)):
    g = gh.client(u["token"])
    full = f"{owner}/{name}"
    acc = gh.access(g, full)
    if not acc["read"]:
        raise HTTPException(403, "your GitHub identity cannot read this repository")
    r = g.get_repo(full)
    langs = r.get_languages()
    try:
        readme = r.get_readme().decoded_content.decode("utf-8", "replace")[:3000]
    except Exception:  # noqa: BLE001
        readme = ""
    try:
        top = [c.path + ("/" if c.type == "dir" else "") for c in r.get_contents("")][:40]
    except Exception:  # noqa: BLE001
        top = []
    commits = [{"sha": c.sha[:7], "msg": c.commit.message.splitlines()[0][:90], "author": c.commit.author.name,
                "date": c.commit.author.date.isoformat()} for c in itertools.islice(r.get_commits(), 6)]
    prs = [{"number": p.number, "title": p.title[:90], "user": p.user.login, "head": p.head.ref}
           for p in itertools.islice(r.get_pulls(state="open"), 10)]
    has_specs = any(t.startswith("specs/") for t in top)
    has_tests = any(t.rstrip("/") in ("tests", "test") for t in top)
    summary = llm.gemini(
        f"Give 4 short bullet highlights (purpose, structure, health, risks) for this GitHub repo. Be concrete, no fluff.\n"
        f"Repo {full}: {r.description}\nLanguages: {langs}\nTop-level: {top}\nRecent commits: {commits}\n"
        f"Open PRs: {prs}\nREADME:\n{readme}", llm.FLASH, "low")
    return {"repo": full, "access": acc, "languages": langs, "top": top, "commits": commits, "prs": prs,
            "has_specs": has_specs, "has_tests": has_tests, "summary": summary, "stars": r.stargazers_count}


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
async def review(request: Request, u: dict = Depends(user)):
    b = await request.json()
    repo = gh.parse_repo(b["repo"], u["login"])
    run = events.create_run("review", f"PR #{b['pr']}", repo, u["login"])
    _start(pipeline.run_review, run["run_id"], repo, int(b["pr"]), u["token"], comment=bool(b.get("comment")))
    return {"run_id": run["run_id"]}


def _own_run(run_id: str, u: dict) -> dict:
    run = events.get_run(run_id)
    if not run or run.get("slack_user") != u["login"]:
        raise HTTPException(404, "run not found")
    return run


@app.get("/api/runs")
def runs(u: dict = Depends(user)):
    return [r for r in events.list_runs(50) if r.get("slack_user") == u["login"]][:20]


@app.get("/api/runs/{run_id}")
def run_info(run_id: str, u: dict = Depends(user)):
    return {"run": _own_run(run_id, u), "events": events.get_events(run_id)}


@app.post("/api/runs/{run_id}/gate")
async def gate(run_id: str, request: Request, u: dict = Depends(user)):
    _own_run(run_id, u)
    b = await request.json()
    ok = pipeline.GATES.resolve(run_id, b["name"], b["decision"], b.get("text", ""))
    return {"ok": ok}


@app.get("/api/runs/{run_id}/stream")
async def stream(run_id: str, request: Request, u: dict = Depends(user)):
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
            h = repo_highlights(*b["repo"].split("/", 1), u=u)
            ctx = f"\nSelected repo {h['repo']}: access={h['access']}, specs={h['has_specs']}, tests={h['has_tests']}, open PRs={h['prs']}\nSummary: {h['summary']}"
        except Exception as exc:  # noqa: BLE001
            ctx = f"\n(could not read selected repo: {exc})"
    history = "\n".join(f"{m['role']}: {m['text']}" for m in b.get("history", [])[-6:])
    prompt = (f"You are the SpecGuard assistant inside its web app. Signed-in GitHub user: {u['login']}.\n{ABOUT}{ctx}\n\n"
              f"Conversation so far:\n{history}\n\nUser: {b['message']}\n\nAnswer briefly and concretely. If they want to build or "
              f"review something, tell them which button to use (Create a feature / Read a repo / Explore).")
    text = await asyncio.to_thread(llm.gemini, prompt, llm.FLASH, "low")
    return {"reply": text}
