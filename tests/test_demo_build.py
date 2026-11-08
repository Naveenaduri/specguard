"""Guests can build only on the demo account's own repos, with a per-session cap."""
import pytest
from fastapi.testclient import TestClient

from specguard import web


@pytest.fixture()
def client(monkeypatch):
    started = []
    monkeypatch.setattr(web, "DEMO_OWNER", "demo-owner")
    monkeypatch.setattr(web, "PUBLIC_TOKEN", "host-token")
    monkeypatch.setattr(web.pipeline, "run_build", lambda *a, **k: started.append(a))
    web.ANON_BUILDS.clear()
    c = TestClient(web.app)
    c.started = started
    return c


def test_config_advertises_demo_owner(client):
    assert client.get("/auth/config").json()["demo_owner"] == "demo-owner"


def test_guest_builds_on_demo_owner_with_host_token(client):
    r = client.post("/api/build", json={"repo": "demo-owner/tinyjwt", "feature": "add nbf"})
    assert r.status_code == 200
    _, repo, feature, token, acting = client.started[0]
    assert (repo, token, acting) == ("demo-owner/tinyjwt", "host-token", "demo-owner")


def test_guest_cannot_build_on_someone_elses_repo(client):
    r = client.post("/api/build", json={"repo": "someone-else/project", "feature": "x"})
    assert r.status_code == 403 and not client.started


def test_guest_build_cap(client):
    for _ in range(web.ANON_BUILD_LIMIT):
        assert client.post("/api/build", json={"repo": "demo-owner/a", "feature": "x"}).status_code == 200
    assert client.post("/api/build", json={"repo": "demo-owner/a", "feature": "x"}).status_code == 429


def test_no_host_token_means_sign_in_required(client, monkeypatch):
    monkeypatch.setattr(web, "PUBLIC_TOKEN", "")
    assert client.post("/api/build", json={"repo": "demo-owner/a", "feature": "x"}).status_code == 401
