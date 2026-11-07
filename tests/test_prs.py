from specguard import prs


def P(n, title="t", add=10, dele=2, files=("a.py",)):
    return {"number": n, "title": title, "author": "me", "additions": add, "deletions": dele, "files": list(files), "body": "", "created_at": "2026-11-01"}


def D(t, prio="Medium", conf=.9):
    return {"type": t, "confidence": conf, "priority": prio, "priority_score": {"Low": 0, "Medium": 1, "High": 2}[prio]}


def test_group_prs_groups_by_type_and_sorts_high_priority_first():
    plist = [P(1), P(2), P(3), P(4)]
    dec = {1: D("bugfix", "Low"), 2: D("bugfix", "High"), 3: D("docs", "Low"), 4: D("bugfix", "Medium")}
    groups = prs.group_prs(plist, dec)
    by = {g["id"]: g for g in groups}
    assert [p["number"] for p in by["bugfix"]["prs"]] == [2, 4, 1]
    assert by["docs"]["prs"][0]["number"] == 3
    assert groups[0]["id"] == "bugfix"            # biggest group first


def test_undecided_prs_land_in_other_and_nothing_is_lost():
    groups = prs.group_prs([P(1), P(2)], {1: D("feature")})
    assert sorted(p["number"] for g in groups for p in g["prs"]) == [1, 2]
    assert any(g["id"] == "other" and g["prs"][0]["number"] == 2 for g in groups)


def test_every_group_has_a_readable_name():
    groups = prs.group_prs([P(1)], {1: D("deps")})
    assert groups[0]["name"] == "Dependencies & build"


def test_classify_prs_parses_decision_answers(monkeypatch):
    def fake(state, questions):
        assert set(questions) == {"type", "priority"}
        assert set(questions["type"]["criteria"]) == {t["id"] for t in prs.PR_TYPES}
        return {"type": {"type": "choice", "choice": "security", "confidence": .91, "probabilities": {"security": .91}},
                "priority": {"type": "score", "score": 1.7, "legend": {"0": "Low", "1": "Medium", "2": "High"}}}
    monkeypatch.setattr(prs.llm, "subq_decide", fake)
    out = prs.classify_prs([P(7, "Tighten token checks")])
    assert out[7] == {"type": "security", "confidence": .91, "priority": "High", "priority_score": 1.7}


def test_classify_prs_tolerates_individual_failures(monkeypatch):
    calls = {"n": 0}

    def flaky(state, questions):
        calls["n"] += 1
        if "boom" in state:
            raise RuntimeError("down")
        return {"type": {"type": "choice", "choice": "docs", "confidence": .8, "probabilities": {}},
                "priority": {"type": "score", "score": 0.2, "legend": {"0": "Low", "1": "Medium", "2": "High"}}}
    monkeypatch.setattr(prs.llm, "subq_decide", flaky)
    out = prs.classify_prs([P(1, "boom"), P(2, "fine")])
    assert 1 not in out and out[2]["type"] == "docs"
