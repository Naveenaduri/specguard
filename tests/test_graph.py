from specguard import graph


def R(full, lang="Python", topics=(), fork=False, parent=None, created="2026-01-01T00:00:00", desc=""):
    return {"full_name": full, "name": full.split("/")[1], "owner": full.split("/")[0], "language": lang,
            "topics": list(topics), "fork": fork, "parent": parent, "created_at": created, "description": desc}


def kinds(edges, a, b):
    return {e["type"] for e in edges if {e["a"], e["b"]} == {a, b}}


def test_same_name_different_owner_is_same_project():
    e = graph.deterministic_edges([R("a/renewal-negotiator"), R("b/Renewal-Negotiator")])
    assert "same" in kinds(e, "a/renewal-negotiator", "b/Renewal-Negotiator")


def test_fork_links_to_its_parent():
    e = graph.deterministic_edges([R("me/lib", fork=True, parent="up/lib"), R("up/lib")])
    assert "fork" in kinds(e, "me/lib", "up/lib")


def test_similar_names_with_prefix_match():
    e = graph.deterministic_edges([R("me/hack-with-mistral-ai"), R("me/Mistral-AI-Hackathon")])
    assert "name" in kinds(e, "me/hack-with-mistral-ai", "me/Mistral-AI-Hackathon")


def test_unrelated_names_not_linked():
    e = graph.deterministic_edges([R("me/pizza-pilot"), R("me/rootlens")])
    assert e == []


def test_shared_topic_gives_reason():
    e = graph.deterministic_edges([R("me/a", topics=["llm", "agents"]), R("me/b", topics=["agents"])])
    t = [x for x in e if x["type"] == "topic"]
    assert t and "agents" in t[0]["why"]


def test_fallback_clusters_group_by_language_and_cover_every_repo():
    repos = [R("me/a", "Python"), R("me/b", "Python"), R("me/c", "TypeScript"), R("me/d", None)]
    cl = graph.fallback_clusters(repos)
    assigned = {r for c in cl for r in c["repos"]}
    assert assigned == {"me/a", "me/b", "me/c", "me/d"}
    assert any(set(c["repos"]) == {"me/a", "me/b"} for c in cl)


def test_build_graph_survives_llm_failure(monkeypatch):
    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("llm down"))  # noqa: E731
    monkeypatch.setattr(graph, "propose_taxonomy", boom)
    monkeypatch.setattr(graph, "ai_structure", boom)
    g = graph.build_graph([R("me/a"), R("me/b")])
    assert g["clusters"] and g["assign"]["me/a"] in {c["id"] for c in g["clusters"]}


def D(cat, conf=.9, maturity="Prototype", attention=.1, probs=None):
    return {"category": cat, "confidence": conf, "probs": probs or {cat: conf}, "maturity": maturity, "maturity_score": 1.0, "attention": attention}


def test_validate_taxonomy_dedupes_caps_and_drops_blanks():
    raw = [{"name": "AI Agents", "description": "LLM agents"}, {"name": "ai agents", "description": "dup"},
           {"name": "", "description": "x"}, {"name": "Web Apps", "description": "sites"}] +           [{"name": f"C{i}", "description": "d"} for i in range(12)]
    t = graph.validate_taxonomy(raw)
    names = [c["name"].lower() for c in t]
    assert 2 <= len(t) <= 8 and len(set(names)) == len(names)


def test_validate_taxonomy_needs_at_least_two_categories():
    import pytest
    with pytest.raises(ValueError):
        graph.validate_taxonomy([{"name": "Only", "description": "one"}])


def test_build_graph_uses_dynamic_categories_from_decision_model(monkeypatch):
    repos = [R("me/a"), R("me/b"), R("me/c")]
    monkeypatch.setattr(graph, "propose_taxonomy", lambda rs: [{"name": "Agents", "description": "x"}, {"name": "Web", "description": "y"}])
    monkeypatch.setattr(graph, "classify_repos", lambda rs, tax: {"me/a": D("Agents"), "me/b": D("Agents"), "me/c": D("Web", maturity="Idea or stub")})
    g = graph.build_graph(repos)
    members = {c["name"]: set(c["repos"]) for c in g["clusters"]}
    assert members == {"Agents": {"me/a", "me/b"}, "Web": {"me/c"}}
    assert g["method"] == "decision-model" and g["decisions"]["me/c"]["maturity"] == "Idea or stub"
    assert g["assign"]["me/a"] == next(c["id"] for c in g["clusters"] if c["name"] == "Agents")


def test_unclassified_repo_lands_in_uncategorised_not_lost(monkeypatch):
    repos = [R("me/a"), R("me/b")]
    monkeypatch.setattr(graph, "propose_taxonomy", lambda rs: [{"name": "Agents", "description": "x"}, {"name": "Web", "description": "y"}])
    monkeypatch.setattr(graph, "classify_repos", lambda rs, tax: {"me/a": D("Agents")})
    g = graph.build_graph(repos)
    assert set(g["assign"]) == {"me/a", "me/b"}


def test_build_graph_marks_fallback_method(monkeypatch):
    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))  # noqa: E731
    monkeypatch.setattr(graph, "propose_taxonomy", boom)
    monkeypatch.setattr(graph, "ai_structure", boom)
    assert graph.build_graph([R("me/a"), R("me/b")])["method"] == "fallback"


def test_classify_repos_parses_decision_answers(monkeypatch):
    """Exercises classify_repos for real (only the SubQ call is faked), so import/parsing bugs can't hide behind mocks."""
    def fake(state, questions):
        assert set(questions) == {"category", "maturity", "attention"}
        return {"category": {"type": "choice", "choice": "Web", "probabilities": {"Web": .83, "Agents": .17}, "confidence": .83},
                "maturity": {"type": "score", "score": 1.8, "legend": {"0": "Idea or stub", "1": "Prototype", "2": "Working project", "3": "Maintained product"}},
                "attention": {"type": "noul", "noul": .72}}
    monkeypatch.setattr(graph.llm, "subq_decide", fake)
    out = graph.classify_repos([R("me/a"), R("me/b")], [{"name": "Web", "description": "sites"}, {"name": "Agents", "description": "llm"}])
    assert out["me/a"] == {"category": "Web", "confidence": .83, "probs": {"Web": .83, "Agents": .17},
                           "maturity": "Working project", "maturity_score": 1.8, "attention": .72}


def test_classify_repos_raises_if_everything_fails(monkeypatch):
    import pytest
    monkeypatch.setattr(graph.llm, "subq_decide", lambda s, q: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(RuntimeError):
        graph.classify_repos([R("me/a")], [{"name": "Web", "description": "sites"}, {"name": "Agents", "description": "llm"}])


def test_a_word_shared_by_most_repos_does_not_create_name_links():
    """Every OpenTelemetry repo contains 'opentelemetry'; that must not link them all together."""
    repos = [R(f"o/opentelemetry-{x}") for x in ("go", "java", "php", "ruby", "erlang", "dotnet")] + [R("o/community"), R("o/weaver")]
    e = graph.deterministic_edges(repos)
    assert [x for x in e if x["type"] == "name"] == []


def test_a_rare_shared_word_still_links_repos():
    repos = [R("o/hack-stripe"), R("o/hack-auth")] + [R(f"o/misc{i}") for i in range(10)]
    e = graph.deterministic_edges(repos)
    assert any(x["type"] == "name" and {x["a"], x["b"]} == {"o/hack-stripe", "o/hack-auth"} for x in e)


def test_a_topic_carried_by_most_repos_does_not_link_them():
    repos = [R(f"o/lib{i}x", topics=["opentelemetry"]) for i in range(9)]
    assert [x for x in graph.deterministic_edges(repos) if x["type"] == "topic"] == []


def test_a_rare_topic_still_links_repos():
    repos = [R("o/aaa", topics=["observability"]), R("o/bbb", topics=["observability"])] + [R(f"o/zz{i}k") for i in range(10)]
    assert any(x["type"] == "topic" for x in graph.deterministic_edges(repos))
