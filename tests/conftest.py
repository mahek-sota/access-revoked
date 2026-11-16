import pytest
from fastapi.testclient import TestClient

from app.main import create_app

MAYA = {"X-User": "maya"}
SAM = {"X-User": "sam"}
ADMIN = {"X-User": "admin"}
QUERY = "renewal budget"
FINANCE_DOC = 2
WORKSPACE_DOC = 1


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "test.db")


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def search(client, headers, q=QUERY):
    r = client.get("/search", params={"q": q}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def revoke(client, user="maya", group="finance"):
    r = client.post(
        "/admin/memberships/revoke", json={"user": user, "group": group}, headers=ADMIN
    )
    assert r.status_code == 200, r.text
    return r.json()
