"""GitHub OAuth sign-in, tested against a simulated GitHub (no real client id needed)."""
import pytest
from fastapi.testclient import TestClient

from specguard import web


class FakeResp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


class FakeGitHub:
    """Stands in for httpx.AsyncClient: token exchange + /user."""
    token_calls = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        FakeGitHub.token_calls.append((url, kw.get("data")))
        return FakeResp({"access_token": "gho_fake_token_123"} if kw["data"]["code"] == "good-code" else {"error": "bad_verification_code"})

    async def get(self, url, **kw):
        assert kw["headers"]["Authorization"] == "Bearer gho_fake_token_123"
        return FakeResp({"login": "octo", "name": "Octo Cat", "avatar_url": "https://example/a.png"})


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(web, "CLIENT_ID", "cid123")
    monkeypatch.setattr(web, "CLIENT_SECRET", "secret456")
    monkeypatch.setattr(web.httpx, "AsyncClient", FakeGitHub)
    FakeGitHub.token_calls.clear()
    web.SESSIONS.clear()
    return TestClient(web.app, follow_redirects=False)


def test_auth_config_reports_github_ready(client):
    assert client.get("/auth/config").json()["github"] is True


def test_login_redirects_to_github_with_state_and_scope(client):
    r = client.get("/auth/github")
    assert r.status_code in (302, 307)
    loc = r.headers["location"]
    assert loc.startswith("https://github.com/login/oauth/authorize?")
    for part in ("client_id=cid123", "scope=repo+read%3Auser", "state=", "redirect_uri="):
        assert part in loc


def test_callback_with_valid_state_signs_user_in_without_leaking_token(client):
    loc = client.get("/auth/github").headers["location"]
    state = loc.split("state=")[1].split("&")[0]
    r = client.get(f"/auth/callback?code=good-code&state={state}")
    assert r.status_code in (302, 307) and r.headers["location"] == "/"
    me = client.get("/api/me").json()
    assert me == {"login": "octo", "name": "Octo Cat", "avatar": "https://example/a.png"}
    assert "token" not in me and "gho_" not in str(me)
    assert FakeGitHub.token_calls[0][1]["client_secret"] == "secret456"


def test_callback_with_wrong_state_is_rejected(client):
    client.get("/auth/github")
    r = client.get("/auth/callback?code=good-code&state=forged")
    assert r.status_code == 400
    assert client.get("/api/me").status_code == 401


def test_callback_with_bad_code_is_rejected(client):
    state = client.get("/auth/github").headers["location"].split("state=")[1].split("&")[0]
    r = client.get(f"/auth/callback?code=bad&state={state}")
    assert r.status_code == 400


def test_logout_ends_the_session(client):
    state = client.get("/auth/github").headers["location"].split("state=")[1].split("&")[0]
    client.get(f"/auth/callback?code=good-code&state={state}")
    assert client.get("/api/me").status_code == 200
    client.post("/auth/logout")
    assert client.get("/api/me").status_code == 401
