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
    monkeypatch.setattr(graph, "ai_structure", lambda repos: (_ for _ in ()).throw(RuntimeError("llm down")))
    g = graph.build_graph([R("me/a"), R("me/b")])
    assert g["clusters"] and g["assign"]["me/a"] in {c["id"] for c in g["clusters"]}
