"""emit(): single source of truth. Writes to SQLite (always) and MongoDB (if configured), then notifies sinks."""
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DB_PATH = Path(os.getenv("SPECGUARD_DB", ".specguard/events.db"))
_lock = threading.Lock()
_sinks = []
_mongo_db = None
_mongo_tried = False

SECRET_ENVS = ("GEMINI_API_KEY", "SUBQ_API_KEY", "GITHUB_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN",
               "MONGODB_PASSWORD", "GITHUB_CLIENT_SECRET")
_PATTERNS = re.compile(r"(xox[bap]-[\w-]+|xapp-[\w-]+|gh[pousr]_\w+|github_pat_\w+|sk-[\w-]{20,}|AQ\.[\w-]{20,})")


def redact(text) -> str:
    s = str(text)
    for k in SECRET_ENVS:
        v = os.getenv(k)
        if v and len(v) > 6:
            s = s.replace(v, "***")
    return _PATTERNS.sub("***", s)


def clip(text, n=600) -> str:
    s = redact(text)
    return s if len(s) <= n else s[:n] + "…"


def _clip_data(obj):
    if isinstance(obj, dict):
        return {k: _clip_data(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clip_data(v) for v in obj]
    if isinstance(obj, str):
        return clip(obj)
    return obj


def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.execute("create table if not exists runs (run_id text primary key, doc text)")
    c.execute("create table if not exists events (run_id text, seq integer, doc text, primary key (run_id, seq))")
    return c


def _mongo():
    global _mongo_db, _mongo_tried
    if _mongo_tried:
        return _mongo_db
    _mongo_tried = True
    host, user, pw = os.getenv("MONGODB_HOST"), os.getenv("MONGODB_USER"), os.getenv("MONGODB_PASSWORD")
    uri = os.getenv("MONGODB_URI")
    if not uri and host and user and pw:
        uri = f"mongodb+srv://{urllib.parse.quote_plus(user)}:{urllib.parse.quote_plus(pw)}@{host}/?retryWrites=true&w=majority"
    if not uri:
        return None
    try:
        from pymongo import MongoClient

        _mongo_db = MongoClient(uri, serverSelectionTimeoutMS=8000)["specguard"]
        _mongo_db.events.create_index([("run_id", 1), ("seq", 1)])
    except Exception:  # noqa: BLE001
        _mongo_db = None
    return _mongo_db


def _mongo_write(coll, doc, key=None):
    def work():
        try:
            db = _mongo()
            if db is None:
                return
            d = dict(doc)
            if key:
                db[coll].replace_one({key: d[key]}, d, upsert=True)
            else:
                db[coll].insert_one(d)
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=work, daemon=True).start()


def add_sink(fn):
    _sinks.append(fn)


def create_run(kind: str, feature: str = "", repo: str = "", user: str = "", **extra) -> dict:
    now = time.time()
    run = {"run_id": uuid.uuid4().hex[:10], "kind": kind, "feature": feature, "repo": repo, "slack_user": user,
           "slack_thread_url": "", "pr_url": "", "status": "running", "created_at": now, "updated_at": now, "seq": 0}
    run.update(extra)
    _save_run(run)
    return run


def _save_run(run):
    with _lock:
        c = _db()
        c.execute("insert or replace into runs values (?, ?)", (run["run_id"], json.dumps(run)))
        c.commit()
        c.close()
    _mongo_write("runs", run, key="run_id")


def get_run(run_id):
    with _lock:
        c = _db()
        row = c.execute("select doc from runs where run_id=?", (run_id,)).fetchone()
        c.close()
    return json.loads(row[0]) if row else None


def update_run(run_id, **fields):
    run = get_run(run_id) or {"run_id": run_id}
    run.update(fields)
    run["updated_at"] = time.time()
    _save_run(run)
    return run


def list_runs(limit=30):
    with _lock:
        c = _db()
        rows = c.execute("select doc from runs").fetchall()
        c.close()
    runs = sorted((json.loads(r[0]) for r in rows), key=lambda r: -r["created_at"])
    return runs[:limit]


def get_events(run_id, after=0):
    with _lock:
        c = _db()
        rows = c.execute("select doc from events where run_id=? and seq>? order by seq", (run_id, after)).fetchall()
        c.close()
    return [json.loads(r[0]) for r in rows]


def emit(run_id, stage, status, title, detail="", data=None):
    """status: started|progress|done|failed|waiting_human"""
    with _lock:
        c = _db()
        row = c.execute("select max(seq) from events where run_id=?", (run_id,)).fetchone()
        seq = (row[0] or 0) + 1
        ev = {"run_id": run_id, "seq": seq, "ts": time.time(), "kind": "stage", "stage": stage, "status": status,
              "title": clip(title, 200), "detail": clip(detail), "data": _clip_data(data or {})}
        c.execute("insert into events values (?, ?, ?)", (run_id, seq, json.dumps(ev)))
        c.commit()
        c.close()
    _mongo_write("events", ev)
    if status == "failed" and stage != "pr":
        update_run(run_id, status="failed")
    elif status == "waiting_human":
        update_run(run_id, status="waiting_human")
    for sink in list(_sinks):
        try:
            sink(ev)
        except Exception as exc:  # noqa: BLE001
            print("[emit] sink error:", redact(exc), flush=True)
    return ev
