"""Spec handling: load spec text from the repo, extract/choose clauses, verify quotes are verbatim."""
import re
from pathlib import Path

from . import llm

CLAUSE_SHAPE = ('[{"id": "REQ-1", "level": "MUST|SHOULD|MUST NOT", "source": "RFC 7519 §4.1.3", '
                '"quote": "<verbatim excerpt from the spec, at most 25 words>", "testable": true, '
                '"requirement": "<one concrete sentence: what the code must do>"}]')


def load_spec(work: Path) -> str:
    parts = []
    for p in sorted((work / "specs").glob("*.md")) if (work / "specs").exists() else []:
        parts.append(f"<!-- file: {p.name} -->\n" + p.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("“", '"').replace("”", '"').replace("’", "'")).strip().lower()


def verify(clauses: list, spec_text: str):
    """Split clauses into (verbatim, rejected). A clause whose quote is not in the spec is never used."""
    spec_n = _norm(spec_text)
    ok, bad = [], []
    for c in clauses:
        q = _norm(str(c.get("quote", "")).strip('"'))
        (ok if q and q in spec_n else bad).append(c)
    return ok, bad


def pick_feature_clauses(feature: str, spec_text: str, feedback: str = "", current: list | None = None) -> list:
    prompt = f"""You are a standards analyst. Below is a normative specification excerpt, then a feature request.
Select every clause from the SPEC that a correct implementation of the feature must satisfy.

Rules:
- Use ONLY clauses that appear in the spec. "quote" MUST be copied verbatim from the spec (max 25 words).
- "source" must be derived from the spec's headings (RFC number and section).
- "requirement" restates what the code must do for this feature, concretely and testably.
- testable=false only if it cannot be checked by running code.
- Ids are REQ-1, REQ-2, ... in order.
- Return ONLY a JSON array shaped like: {CLAUSE_SHAPE}

SPEC:
{spec_text}

FEATURE REQUEST:
{feature}
"""
    if current:
        prompt += f"\nCURRENT CLAUSE LIST (keep ids of unchanged clauses):\n{current}\n"
    if feedback:
        prompt += f"\nREVIEWER FEEDBACK to incorporate (add/modify clauses accordingly):\n{feedback}\n"
    clauses = llm.extract_json(llm.gemini(prompt, llm.ARGON, "medium"))
    ok, bad = verify(clauses, spec_text)
    if bad and not ok:  # one retry asking for strictly verbatim quotes
        clauses = llm.extract_json(llm.gemini(prompt + "\nYour previous quotes were not verbatim. Copy EXACT text.", llm.ARGON, "medium"))
        ok, _ = verify(clauses, spec_text)
    return ok


def extract_all_clauses(spec_text: str) -> list:
    prompt = f"""Extract every normative requirement (MUST / MUST NOT / SHOULD / SHOULD NOT) from this spec excerpt.
Rules: "quote" is verbatim from the spec (max 25 words); "source" from headings (RFC + section);
"requirement" is one concrete sentence about code behaviour; testable=false if not checkable by running code.
Ids S-1, S-2, ... Return ONLY a JSON array shaped like: {CLAUSE_SHAPE}

SPEC:
{spec_text}
"""
    clauses = llm.extract_json(llm.gemini(prompt, llm.ARGON, "low"))
    ok, _ = verify(clauses, spec_text)
    return ok
