from specguard import rubric


def F(status="proven", sev="Major", cat="Security", title="Audience mismatch accepted", where="core.py · _validate_aud"):
    return {"id": "F1", "category": cat, "severity": sev, "title": title, "where": where, "why": "Tokens for other services pass.",
            "fix": "Raise on mismatch.", "effort": "Quick win", "status": status, "proof": "DID NOT RAISE"}


def test_pick_hot_orders_by_probability_and_applies_threshold():
    probs = {"security": .91, "correctness": .62, "tests": .2, "performance": .55}
    assert rubric.pick_hot(probs, top=2) == ["security", "correctness"]
    assert rubric.pick_hot({"security": .2, "tests": .1}) == []


def test_verdict_proven_problem_needs_changes():
    assert rubric.verdict_for([F("proven")])["label"] == "Needs changes"


def test_verdict_only_headsups_is_cautious_not_blocking():
    v = rubric.verdict_for([F("headsup")])
    assert v["label"] == "Looks fine, check the heads-up"


def test_verdict_cleared_or_nothing_is_good():
    assert rubric.verdict_for([F("cleared")])["label"] == "Looks good"
    assert rubric.verdict_for([])["label"] == "Looks good"


def test_clean_findings_validates_and_caps():
    raw = [{"category": "Security", "severity": "Major", "title": "A", "where": "x.py:3", "why": "w", "fix": "f"},
           {"category": "Nonsense", "severity": "Huge", "title": "B", "where": "y.py", "why": "w", "fix": "f"},
           {"category": "Security", "severity": "Minor", "title": "", "where": "z.py", "why": "w", "fix": "f"},
           {"category": "Tests", "severity": "Minor", "title": "D", "where": "", "why": "w", "fix": "f"}] + \
          [{"category": "Correctness", "severity": "Minor", "title": f"T{i}", "where": "a.py", "why": "w", "fix": "f"} for i in range(9)]
    out = rubric.clean_findings(raw)
    assert len(out) <= 4
    assert out[0]["severity"] == "Major"                      # sorted most severe first
    assert all(f["title"] and f["where"] for f in out)        # no title or no location -> dropped
    assert all(f["severity"] in rubric.SEVERITY and f["category"] in rubric.CATEGORY_NAMES for f in out)


def test_render_comment_is_heading_value_and_marked():
    card = {"verdict": rubric.verdict_for([F()]), "risk": {"label": "High"}, "summary": "Removes the audience comparison.",
            "findings": [F()], "tests": {"run": 1, "failed": 1}, "scope": "Scope: ok"}
    md = rubric.render_comment(card)
    assert "<!-- specguard -->" in md
    for head in ("Verdict :", "Risk :", "What changed :", "Problem :", "Severity :", "Proof :", "Fix :"):
        assert head in md
    assert md.index("Verdict :") < md.index("Problem :")


def test_rubric_has_unique_ids_and_a_question_for_each():
    ids = [c["id"] for c in rubric.RUBRIC]
    assert len(ids) == len(set(ids)) >= 8
    assert all(c["ask"].endswith("?") for c in rubric.RUBRIC)


def test_non_executable_categories_are_never_testable():
    raw = [{"category": c, "severity": "Major", "title": f"T {c}", "where": "a.py", "why": "w", "fix": "f", "testable": True}
           for c in ("Tests", "Maintainability", "Security")]
    out = {f["category"]: f for f in rubric.clean_findings(raw)}
    assert out["Tests"]["testable"] is False and out["Maintainability"]["testable"] is False
    assert out["Security"]["testable"] is True


def test_render_comment_shows_spec_backing_on_a_finding():
    f = F()
    f["spec_refs"] = [{"source": "RFC 7519 §4.1.3", "quote": "the JWT MUST be rejected"}]
    card = {"verdict": rubric.verdict_for([f]), "risk": {"label": "High"}, "summary": "s", "findings": [f],
            "tests": {"run": 1, "failed": 1}, "scope": "Scope: ok"}
    md = rubric.render_comment(card)
    assert "Spec :" in md and "RFC 7519 §4.1.3" in md


def test_status_for_blocks_only_proven_defects():
    from specguard.rubric import status_for
    assert status_for({"tone": "bad"}, {"run": 3, "failed": 1})[0] == "failure"
    assert status_for({"tone": "warn"}, {})[0] == "success"
    st, desc = status_for({"tone": "good"}, {"run": 2, "failed": 0})
    assert st == "success" and "2 test" in desc
