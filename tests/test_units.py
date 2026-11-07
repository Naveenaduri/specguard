import pytest

from specguard import events, gh, spec, testgen


# ---- already built ----
def test_verify_rejects_non_verbatim_quote():
    text = "The \"aud\" claim MUST be validated by the recipient."
    ok, bad = spec.verify([{"id": "R1", "quote": "MUST be validated by the recipient"},
                           {"id": "R2", "quote": "invented sentence not in spec"}], text)
    assert [c["id"] for c in ok] == ["R1"] and [c["id"] for c in bad] == ["R2"]


def test_classify_red_error_and_nondiscriminating():
    clauses = [{"id": "REQ-1"}, {"id": "REQ-2"}, {"id": "REQ-3"}]
    p = testgen.test_path
    results = [
        {"name": p("REQ-1") + "::t", "status": "fail", "output": "AssertionError"},
        {"name": p("REQ-2") + "::t", "status": "pass", "output": ""},
        {"name": p("REQ-3"), "status": "error", "output": "ImportError"},
    ]
    cls = testgen.classify(results, clauses)
    assert cls["REQ-1"]["status"] == "red"
    assert cls["REQ-2"]["status"] == "nondiscriminating"  # passes before implementation -> discarded
    assert cls["REQ-3"]["status"] == "error"


def test_redact_removes_secrets_and_token_shapes(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "supersecretvalue123")
    out = events.redact("x supersecretvalue123 y xoxb-123-abc-def z ghp_abcdefghijklmnop")
    assert "supersecretvalue123" not in out and "xoxb-" not in out and "ghp_" not in out


def test_parse_repo_and_pr():
    assert gh.parse_repo("https://github.com/Naveenaduri/tinyjwt.git") == "Naveenaduri/tinyjwt"
    assert gh.parse_repo("tinyjwt", "Naveenaduri") == "Naveenaduri/tinyjwt"
    assert gh.parse_pr("https://github.com/a/b/pull/12") == ("a/b", 12)
    assert gh.parse_pr("a/b#7") == ("a/b", 7)
    with pytest.raises(ValueError):
        gh.parse_pr("nope")


# ---- to be implemented (red first) ----
def test_scope_check_flags_sensitive_paths():
    from specguard.review import scope_check

    r = scope_check(["src/app.py", ".github/workflows/ci.yml", "config/.env", "deploy/key.pem"], write=True)
    assert r["ok"] is False
    assert set(r["flagged"]) == {".github/workflows/ci.yml", "config/.env", "deploy/key.pem"}
    assert scope_check(["src/app.py", "tests/test_a.py"], write=True)["ok"] is True


def test_scope_check_blocks_when_no_write_access():
    from specguard.review import scope_check

    r = scope_check(["src/app.py"], write=False, need_write=True)
    assert r["ok"] is False and "write" in r["summary"].lower()


def test_parse_file_blocks():
    from specguard.pipeline import parse_file_blocks

    text = "noise\n=== FILE: pkg/a.py ===\nx = 1\n=== END ===\n=== FILE: pkg/b.py ===\ny = 2\n=== END ===\n"
    assert parse_file_blocks(text) == {"pkg/a.py": "x = 1\n", "pkg/b.py": "y = 2\n"}


def test_parse_file_blocks_rejects_path_escape():
    from specguard.pipeline import parse_file_blocks

    assert parse_file_blocks("=== FILE: ../evil.py ===\nx\n=== END ===\n") == {}


def test_evidence_table_has_required_columns():
    from specguard.pipeline import evidence_table

    rows = [{"clause_id": "REQ-1", "source": "RFC 7519 §4.1.3", "test": "t::a", "files": "core.py",
             "before": "AssertionError", "after": "passed"}]
    md = evidence_table(rows)
    for col in ("Clause", "Source", "Test", "Changed files", "Before", "After"):
        assert col in md
    assert "REQ-1" in md and "RFC 7519 §4.1.3" in md
