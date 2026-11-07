"""Repo relationship graph: deterministic edges (fork / same project / similar name / shared topic)
plus AI-inferred clusters and links. The AI part is optional; the graph always builds without it."""
import itertools
import json
import re
from concurrent.futures import ThreadPoolExecutor

from . import llm

STOP = {"the", "and", "for", "with", "app", "new", "test", "demo", "repo", "project", "my", "api", "web", "io", "js", "py"}


def _tokens(name: str) -> set:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return {t.lower() for t in re.split(r"[^A-Za-z0-9]+", spaced) if len(t) >= 3 and t.lower() not in STOP}


def _tok_match(a: str, b: str) -> bool:
    return a == b or (min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)))


def deterministic_edges(repos: list) -> list:
    by_name = {r["full_name"]: r for r in repos}
    edges = []

    def add(a, b, typ, why, w):
        edges.append({"a": a, "b": b, "type": typ, "why": why, "w": w})

    for x, y in itertools.combinations(repos, 2):
        a, b = x["full_name"], y["full_name"]
        if x["fork"] and x.get("parent") == b or y["fork"] and y.get("parent") == a:
            add(a, b, "fork", "fork of", 1.0)
            continue
        if x["name"].lower() == y["name"].lower():
            add(a, b, "same", "same project name", .9)
            continue
        tx, ty = _tokens(x["name"]), _tokens(y["name"])
        shared = [t for t in tx for u in ty if _tok_match(t, u)]
        if shared:
            add(a, b, "name", f"similar name: {shared[0]}", .55)
            continue
        topics = set(x.get("topics") or []) & set(y.get("topics") or [])
        if topics:
            add(a, b, "topic", "shared topic: " + ", ".join(sorted(topics)[:2]), .5)
    return edges


CLUSTER_COLORS = 8


def fallback_clusters(repos: list) -> list:
    groups: dict = {}
    for r in repos:
        groups.setdefault(r.get("language") or "Other", []).append(r["full_name"])
    return [{"id": f"c{i}", "name": lang, "blurb": f"{len(names)} repo(s) written mostly in {lang}" if lang != "Other" else "Everything else",
             "repos": names} for i, (lang, names) in enumerate(sorted(groups.items(), key=lambda kv: -len(kv[1])))]


def ai_structure(repos: list) -> dict:
    lines = "\n".join(f"- {r['full_name']} | {r.get('language') or '-'} | topics: {','.join(r.get('topics') or []) or '-'} | "
                      f"{(r.get('description') or '')[:90]}" for r in repos)
    prompt = f"""Group these GitHub repositories into 3-7 meaningful themes (e.g. "AI agents", "Hackathon projects", "Web apps",
"Security & standards") and list pairs of repos that are clearly related (same project, shared purpose, one builds on the other).
Use ONLY full names from the list. Every repo must be in exactly one cluster.
Return ONLY JSON: {{"clusters":[{{"name":"...","blurb":"<=10 words","repos":["owner/name"]}}],
"links":[{{"a":"owner/name","b":"owner/name","why":"<=6 words"}}]}}

REPOS:
{lines}"""
    return llm.extract_json(llm.gemini(prompt, llm.FLASH, "low"))


MATURITY = ["Idea or stub", "Prototype", "Working project", "Maintained product"]


def validate_taxonomy(raw) -> list:
    out, seen = [], set()
    for c in raw if isinstance(raw, list) else []:
        name, desc = str(c.get("name", "")).strip()[:32], str(c.get("description", "")).strip()[:90]
        if not name or not desc or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({"name": name, "description": desc})
    if len(out) < 2:
        raise ValueError("taxonomy needs at least two categories")
    return out[:8]


def propose_taxonomy(repos: list) -> list:
    """Dynamic, per-user categories. Derived only from this user's repos, named the way developers browse catalogs."""
    lines = "\n".join(f"- {r['name']} | {r.get('language') or '-'} | topics: {','.join(r.get('topics') or []) or '-'} | "
                      f"{(r.get('description') or '')[:100]}" for r in repos)
    prompt = f"""Design a category scheme for THIS developer's GitHub repositories (listed below).
Make 4 to 8 categories based on what the projects are FOR (domain or purpose), not on programming language.
Name them the way well-known catalogs do (GitHub Topics, awesome-lists, the CNCF landscape): short, concrete, human.
Derive them only from the repos below; do not use generic filler categories.
Return ONLY JSON: [{{"name": "<=3 words", "description": "<=14 words saying what belongs here"}}]

REPOS:
{lines}"""
    return validate_taxonomy(llm.extract_json(llm.gemini(prompt, llm.FLASH, "low")))


def _repo_state(r: dict) -> str:
    return (f"Repository: {r['full_name']}\nDescription: {r.get('description') or '-'}\nLanguage: {r.get('language') or '-'}\n"
            f"Topics: {', '.join(r.get('topics') or []) or '-'}\nStars: {r.get('stars', 0)}  Forks: {r.get('forks', 0)}  "
            f"Open issues: {r.get('issues', 0)}\nCreated: {r.get('created_at', '')}  Last push: {r.get('pushed_at', '')}\n"
            f"Fork: {r.get('fork')}  Archived: {r.get('archived')}\nREADME:\n{(r.get('readme') or '(none)')[:1500]}")


def classify_repos(repos: list, taxonomy: list) -> dict:
    """SubQ Snap decision model, one call per repo: purpose (choice), maturity (score), abandoned-or-unfinished (yes/no)."""
    questions = {
        "category": {"type": "choice", "instructions": "Which category best describes what this repository is for?",
                     "criteria": {c["name"]: c["description"] for c in taxonomy}},
        "maturity": {"type": "score", "instructions": "How mature is this project?", "criteria": MATURITY},
        "attention": {"type": "noul", "instructions": "Is this repository likely abandoned or unfinished, so its owner should decide to finish, archive or delete it?"},
    }

    def one(r):
        try:
            a = llm.subq_decide(_repo_state(r), questions)
            ch, mt = a["category"], a["maturity"]
            label = mt.get("legend", {}).get(str(min(3, max(0, round(mt["score"]))))) or MATURITY[min(3, max(0, round(mt["score"])))]
            return r["full_name"], {"category": ch["choice"], "confidence": round(float(ch.get("confidence", 0)), 2),
                                    "probs": {k: round(float(v), 2) for k, v in (ch.get("probabilities") or {}).items()},
                                    "maturity": label, "maturity_score": round(float(mt["score"]), 1),
                                    "attention": round(float(a["attention"]["noul"]), 2)}
        except Exception:  # noqa: BLE001
            return r["full_name"], None

    with ThreadPoolExecutor(max_workers=8) as pool:
        got = dict(pool.map(one, repos))
    out = {k: v for k, v in got.items() if v}
    if not out:
        raise RuntimeError("decision model produced no classifications")
    return out


def _clusters_from_decisions(repos: list, taxonomy: list, decisions: dict) -> list:
    members = {c["name"]: [] for c in taxonomy}
    rest = []
    for r in repos:
        d = decisions.get(r["full_name"])
        (members[d["category"]] if d and d["category"] in members else rest).append(r["full_name"])
    clusters = [{"id": f"c{i}", "name": c["name"], "blurb": c["description"], "repos": members[c["name"]]}
                for i, c in enumerate(taxonomy) if members[c["name"]]]
    if rest:
        clusters.append({"id": f"c{len(clusters)}", "name": "Uncategorised", "blurb": "Not enough signal to decide", "repos": rest})
    return clusters


def build_graph(repos: list) -> dict:
    names = {r["full_name"] for r in repos}
    edges = deterministic_edges(repos)
    clusters, decisions, taxonomy, method = None, {}, [], "fallback"
    try:  # 1) dynamic taxonomy from this user's repos, decided per repo by the SubQ decision model
        taxonomy = propose_taxonomy(repos)
        decisions = classify_repos(repos, taxonomy)
        clusters, method = _clusters_from_decisions(repos, taxonomy, decisions), "decision-model"
    except Exception:  # noqa: BLE001
        clusters, decisions, taxonomy = None, {}, []
    if not clusters:
        try:  # 2) LLM-only grouping
            ai = ai_structure(repos)
            seen, clusters = set(), []
            for i, c in enumerate(ai.get("clusters", [])):
                members = [n for n in c.get("repos", []) if n in names and n not in seen]
                seen.update(members)
                if members:
                    clusters.append({"id": f"c{i}", "name": str(c.get("name", "Group"))[:40], "blurb": str(c.get("blurb", ""))[:80], "repos": members})
            rest = [n for n in names if n not in seen]
            if rest:
                clusters.append({"id": f"c{len(clusters)}", "name": "Other", "blurb": "Not grouped", "repos": rest})
            have = {frozenset((e["a"], e["b"])) for e in edges}
            for l in ai.get("links", []):
                a, b = l.get("a"), l.get("b")
                if a in names and b in names and a != b and frozenset((a, b)) not in have:
                    edges.append({"a": a, "b": b, "type": "ai", "why": str(l.get("why", "related"))[:48], "w": .45})
                    have.add(frozenset((a, b)))
            method = "ai"
        except Exception:  # noqa: BLE001
            clusters = None
    if not clusters:  # 3) always works
        clusters, method = fallback_clusters(repos), "fallback"
    assign = {n: c["id"] for c in clusters for n in c["repos"]}
    return {"clusters": clusters, "edges": edges, "assign": assign, "decisions": decisions, "method": method, "taxonomy": taxonomy}
