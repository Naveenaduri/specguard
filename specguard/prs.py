"""Open pull requests of a repo, grouped by what kind of change they are, with a review-priority score.
Both decisions come from the SubQ Snap decision model (one call per PR)."""
from concurrent.futures import ThreadPoolExecutor

from . import llm

PR_TYPES = [
    {"id": "feature", "name": "New feature", "desc": "Adds new capability or behaviour"},
    {"id": "bugfix", "name": "Bug fix", "desc": "Fixes incorrect behaviour or a defect"},
    {"id": "refactor", "name": "Refactor & cleanup", "desc": "Restructures code without changing behaviour"},
    {"id": "security", "name": "Security", "desc": "Touches authentication, validation, crypto, permissions or vulnerabilities"},
    {"id": "perf", "name": "Performance", "desc": "Speeds things up or reduces resource use"},
    {"id": "deps", "name": "Dependencies & build", "desc": "Dependency bumps, build, CI or release configuration"},
    {"id": "tests", "name": "Tests", "desc": "Adds or changes tests only"},
    {"id": "docs", "name": "Docs", "desc": "Documentation, comments or examples only"},
]
NAME = {t["id"]: t["name"] for t in PR_TYPES}
PRIORITY = ["Low", "Medium", "High"]


def _state(p: dict) -> str:
    files = "\n".join(p.get("files", [])[:25]) or "(unknown)"
    return (f"Pull request #{p['number']}: {p['title']}\nAuthor: {p.get('author')}\n"
            f"Size: +{p.get('additions', 0)} / -{p.get('deletions', 0)}, {len(p.get('files', []))} file(s)\n"
            f"Description:\n{(p.get('body') or '')[:800]}\n\nChanged files:\n{files}")


def classify_prs(plist: list) -> dict:
    questions = {
        "type": {"type": "choice", "instructions": "What kind of change is this pull request?",
                 "criteria": {t["id"]: t["desc"] for t in PR_TYPES}},
        "priority": {"type": "score", "instructions": "How much does this pull request need a careful human review before merging?",
                     "criteria": PRIORITY},
    }

    def one(p):
        try:
            a = llm.subq_decide(_state(p), questions)
            ch, pr = a["type"], a["priority"]
            score = float(pr["score"])
            label = (pr.get("legend") or {}).get(str(min(2, max(0, round(score))))) or PRIORITY[min(2, max(0, round(score)))]
            return p["number"], {"type": ch["choice"], "confidence": round(float(ch.get("confidence", 0)), 2),
                                 "priority": label, "priority_score": round(score, 1)}
        except Exception:  # noqa: BLE001
            return p["number"], None

    with ThreadPoolExecutor(max_workers=8) as pool:
        return {n: d for n, d in pool.map(one, plist) if d}


def group_prs(plist: list, decisions: dict) -> list:
    groups: dict = {}
    for p in plist:
        d = decisions.get(p["number"])
        gid = d["type"] if d and d["type"] in NAME else "other"
        groups.setdefault(gid, []).append({**p, "decision": d})
    out = [{"id": gid, "name": NAME.get(gid, "Other"), "prs": sorted(items, key=lambda x: -((x["decision"] or {}).get("priority_score", -1)))}
           for gid, items in groups.items()]
    out.sort(key=lambda g: (g["id"] == "other", -len(g["prs"])))
    return out
