"""Benchmark for the review rubric: labelled diffs, each with a known issue category (or none).

Measures what the decision model actually does: does it flag the right category (detection) and stay quiet on clean
changes (specificity)? Run: python -m specguard bench
"""
import json
from pathlib import Path

from . import rubric

CASES = [
    {"id": "sec-signature", "expect": "security", "title": "Speed up token verification",
     "diff": "--- auth/tokens.py\n@@ def verify(token, key):\n-    expected = sign(token.payload, key)\n-    if not hmac.compare_digest(expected, token.signature):\n-        raise InvalidSignature()\n+    # signature is verified upstream by the gateway\n+    pass\n     return token.payload\n"},
    {"id": "err-swallow", "expect": "error_handling", "title": "Make sync job more resilient",
     "diff": "--- jobs/sync.py\n@@ def run(items):\n     for item in items:\n-        try:\n-            push(item)\n-        except NetworkError as exc:\n-            log.error('push failed', exc)\n-            raise\n+        try:\n+            push(item)\n+        except Exception:\n+            pass\n"},
    {"id": "perf-quadratic", "expect": "performance", "title": "Deduplicate events before sending",
     "diff": "--- events/dedupe.py\n@@ def dedupe(events):\n-    seen = set()\n-    out = []\n-    for e in events:\n-        if e.id not in seen:\n-            seen.add(e.id)\n-            out.append(e)\n-    return out\n+    out = []\n+    for e in events:\n+        if e.id not in [o.id for o in out]:\n+            out.append(e)\n+    return out\n"},
    {"id": "api-rename", "expect": "api", "title": "Clean up parameter names",
     "diff": "--- client/api.py\n@@\n-def fetch_user(user_id, include_deleted=False):\n+def fetch_user(uid):\n     ...\n--- README.md\n (no changes)\n"},
    {"id": "tests-removed", "expect": "tests", "title": "Fix flaky CI",
     "diff": "--- tests/test_billing.py\n@@\n-def test_refund_is_capped_at_original_amount():\n-    assert refund(order, 500) == order.total\n-\n-def test_refund_negative_rejected():\n-    with pytest.raises(ValueError):\n-        refund(order, -1)\n--- billing/refund.py\n@@\n-    if amount > order.total:\n-        amount = order.total\n+    pass\n"},
    {"id": "config-ci", "expect": "config", "title": "Faster builds",
     "diff": "--- .github/workflows/ci.yml\n@@\n-      - run: pip install -r requirements.lock\n+      - run: curl -sSL https://example.com/install.sh | sudo bash\n+      - run: pip install -r requirements.txt --pre\n--- requirements.txt\n-requests==2.31.0\n+requests\n"},
    {"id": "stab-retry", "expect": "stability", "title": "Retry on failure",
     "diff": "--- http/fetch.py\n@@ def fetch(url):\n-    return requests.get(url, timeout=5)\n+    while True:\n+        try:\n+            return requests.get(url)\n+        except requests.RequestException:\n+            continue\n"},
    {"id": "clean-docstring", "expect": None, "title": "Fix typos in docstrings",
     "diff": "--- utils/text.py\n@@ def slugify(text):\n-    \"\"\"Convert a string to a slug. Retruns lowercase text.\"\"\"\n+    \"\"\"Convert a string to a slug. Returns lowercase text.\"\"\"\n     return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')\n"},
    {"id": "clean-rename-local", "expect": None, "title": "Rename a local variable for clarity",
     "diff": "--- reports/summary.py\n@@ def total(rows):\n-    t = 0\n-    for r in rows:\n-        t += r.amount\n-    return t\n+    total_amount = 0\n+    for row in rows:\n+        total_amount += row.amount\n+    return total_amount\n"},
]


def run(threshold: float = .5) -> dict:
    rows, hit, clean_ok, clean_n, detect_n, alarms, top1 = [], 0, 0, 0, 0, 0, 0
    for c in CASES:
        sc = rubric.score_rubric(f"PR title: {c['title']}\n\nDIFF:\n{c['diff']}")
        probs = sc["probs"]
        flagged = [k for k, p in probs.items() if p >= threshold]
        if c["expect"]:
            detect_n += 1
            ok = probs.get(c["expect"], 0) >= threshold
            hit += ok
            top1 += sc['primary']['choice'] == c['expect']
            alarms += len([k for k in flagged if k != c["expect"]])
        else:
            clean_n += 1
            ok = not flagged
            clean_ok += ok
            top1 += sc['primary']['choice'] == 'none'
        rows.append({"case": c["id"], "expected": c["expect"] or "none (clean)", "ok": bool(ok), "risk": sc["risk"]["label"],
                     "flagged": flagged, "primary": sc["primary"]["choice"], "p_expected": round(probs.get(c["expect"], 0), 2) if c["expect"] else None})
    out = {"top1_primary_concern": f"{top1}/{len(CASES)}", "detection": f"{hit}/{detect_n}", "clean_specificity": f"{clean_ok}/{clean_n}",
           "avg_extra_categories_on_bad_cases": round(alarms / max(detect_n, 1), 2), "via": sc["via"], "rows": rows}
    return out


def to_markdown(r: dict) -> str:
    md = (f"# Review rubric benchmark\n\nDecision model: `{r['via']}`  \n"
          f"**Main concern correct** (single-choice question): {r['top1_primary_concern']}  \n"
          f"**Detection** (yes/no question for the right category): {r['detection']}  \n"
          f"**Clean changes left unflagged**: {r['clean_specificity']}  \n"
          f"**Extra categories flagged by yes/no on bad cases** (avg): {r['avg_extra_categories_on_bad_cases']} "
          "(noisy on its own, which is why findings are investigated and proven before they are reported)\n\n"
          "| case | expected | result | risk | main concern | flagged (yes/no) |\n|---|---|---|---|---|---|\n")
    for x in r["rows"]:
        md += f"| {x['case']} | {x['expected']} | {'✅' if x['ok'] else '❌'} | {x['risk']} | {x['primary']} | {', '.join(x['flagged']) or '-'} |\n"
    return md


def main(path: str = "benchmarks/RESULTS.md") -> dict:
    r = run()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(to_markdown(r), encoding="utf-8")
    Path(path).with_suffix(".json").write_text(json.dumps(r, indent=1), encoding="utf-8")
    return r
