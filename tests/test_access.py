"""Authorization with a warm candidate cache."""
import json

from app import db, search as s
from conftest import (
    ADMIN, FINANCE_DOC, MAYA, QUERY, SAM, WORKSPACE_DOC, revoke, search,
)

FINANCE_TITLE = "Renewal budget: vendor line items (Finance only)"
FINANCE_BODY_MARKER = "walk-away number"


def ids(result):
    return [r["id"] for r in result["results"]]


def test_first_search_misses_second_hits(client):
    first = search(client, MAYA)
    second = search(client, MAYA)
    assert first["cache"] == "miss"
    assert second["cache"] == "hit"
    assert ids(first) == ids(second)
    assert set(ids(second)) == {WORKSPACE_DOC, FINANCE_DOC}


def test_cache_header_matches_body(client):
    r1 = client.get("/search", params={"q": QUERY}, headers=MAYA)
    r2 = client.get("/search", params={"q": QUERY}, headers=MAYA)
    assert r1.headers["X-Cache"] == "miss"
    assert r2.headers["X-Cache"] == "hit"


def test_revocation_with_warm_cache_hides_finance_doc(client):
    search(client, MAYA)
    assert set(ids(search(client, MAYA))) == {WORKSPACE_DOC, FINANCE_DOC}

    assert revoke(client)["revoked"] is True
    after = search(client, MAYA)

    assert after["cache"] == "hit"  # same cached candidates, no invalidation
    assert ids(after) == [WORKSPACE_DOC]
    assert after["permitted_count"] == 1


def test_old_link_returns_404_after_revocation(client):
    search(client, MAYA)
    assert client.get(f"/docs/{FINANCE_DOC}", headers=MAYA).status_code == 200
    revoke(client)
    r = client.get(f"/docs/{FINANCE_DOC}", headers=MAYA)
    assert r.status_code == 404
    assert client.get(f"/docs/{WORKSPACE_DOC}", headers=MAYA).status_code == 200


def test_forbidden_and_missing_docs_look_the_same(client):
    revoke(client)
    forbidden = client.get(f"/docs/{FINANCE_DOC}", headers=MAYA)
    missing = client.get("/docs/9999", headers=MAYA)
    assert forbidden.status_code == missing.status_code == 404
    assert forbidden.json() == missing.json()


def test_post_revocation_response_has_no_restricted_fields(client):
    search(client, MAYA)
    revoke(client)
    r = client.get("/search", params={"q": QUERY}, headers=MAYA)
    raw = r.text
    assert FINANCE_TITLE not in raw
    assert FINANCE_BODY_MARKER not in raw
    assert f"/docs/{FINANCE_DOC}" not in raw
    assert FINANCE_DOC not in ids(r.json())
    # Response shape exposes no candidate count that would hint at hidden docs.
    assert set(r.json()) == {"query", "cache", "permitted_count", "results"}


def test_cross_user_cache_shares_ids_not_results(client):
    # Maya (Finance) warms the cache; Sam (no Finance) reuses it.
    assert search(client, MAYA)["cache"] == "miss"
    sam = client.get("/search", params={"q": QUERY}, headers=SAM)
    assert sam.json()["cache"] == "hit"
    assert ids(sam.json()) == [WORKSPACE_DOC]
    assert FINANCE_TITLE not in sam.text


def test_cross_user_cache_does_not_narrow_results(client):
    # Sam warms the cache; Maya still sees everything she is allowed to see.
    assert search(client, SAM)["cache"] == "miss"
    maya = search(client, MAYA)
    assert maya["cache"] == "hit"
    assert set(ids(maya)) == {WORKSPACE_DOC, FINANCE_DOC}


def test_regrant_restores_access_with_same_cache(client):
    search(client, MAYA)
    revoke(client)
    assert ids(search(client, MAYA)) == [WORKSPACE_DOC]
    r = client.post(
        "/admin/memberships/grant", json={"user": "maya", "group": "finance"},
        headers=ADMIN,
    )
    assert r.status_code == 200
    again = search(client, MAYA)
    assert again["cache"] == "hit"
    assert set(ids(again)) == {WORKSPACE_DOC, FINANCE_DOC}


def test_snippets_built_only_for_permitted_docs(app, client, monkeypatch):
    search(client, MAYA)  # warm
    revoke(client)
    seen_bodies = []
    real = s.build_snippet

    def spy(body, terms, *a, **kw):
        seen_bodies.append(body)
        return real(body, terms, *a, **kw)

    monkeypatch.setattr(s, "build_snippet", spy)
    search(client, MAYA)
    assert len(seen_bodies) == 1
    assert all(FINANCE_BODY_MARKER not in b for b in seen_bodies)


def test_restricted_text_never_loaded_after_revocation(tmp_path):
    path = tmp_path / "trace.db"
    db.init_db(path)
    cache = s.CandidateCache()
    conn = db.connect(path)
    s.search(conn, cache, "maya", QUERY)  # warm
    s.revoke_membership(conn, "maya", "finance")

    statements = []
    conn.set_trace_callback(statements.append)
    s.search(conn, cache, "maya", QUERY)
    loads = [q for q in statements if "FROM documents WHERE id IN" in q]
    assert len(loads) == 1
    assert f"IN ({WORKSPACE_DOC})" in loads[0]
    assert str(FINANCE_DOC) not in loads[0].split("IN", 1)[1]


def test_transaction_boundary(tmp_path):
    """A request whose snapshot predates the revocation commit stays
    consistent; every request started after the commit enforces it."""
    path = tmp_path / "tx.db"
    db.init_db(path)
    cache = s.CandidateCache()
    reader = db.connect(path)
    writer = db.connect(path)
    s.search(reader, cache, "maya", QUERY)  # warm

    def revoke_mid_request():
        assert s.revoke_membership(writer, "maya", "finance") is True

    in_flight = s.search(reader, cache, "maya", QUERY, after_acl_filter=revoke_mid_request)
    # Started before the commit: one snapshot, so ACL and text agree.
    assert set(ids(in_flight)) == {WORKSPACE_DOC, FINANCE_DOC}

    nxt = s.search(reader, cache, "maya", QUERY)
    assert nxt["cache"] == "hit"
    assert ids(nxt) == [WORKSPACE_DOC]


def test_unknown_user_rejected(client):
    assert client.get("/search", params={"q": QUERY}).status_code == 401
    assert client.get("/search", params={"q": QUERY}, headers={"X-User": "eve"}).status_code == 401


def test_only_admin_can_revoke(client):
    r = client.post(
        "/admin/memberships/revoke", json={"user": "maya", "group": "finance"},
        headers=SAM,
    )
    assert r.status_code == 403
    assert set(ids(search(client, MAYA))) == {WORKSPACE_DOC, FINANCE_DOC}


def test_fts_operators_in_query_are_literal(client):
    for q in ['renewal OR budget', '"renewal', 'budget*', 'NEAR(renewal', '']:
        r = client.get("/search", params={"q": q}, headers=SAM)
        assert r.status_code == 200, (q, r.text)
        assert FINANCE_TITLE not in r.text
    assert json.loads(client.get("/search", params={"q": "zzz"}, headers=SAM).text)["results"] == []


def test_query_normalization_shares_cache_entry(client):
    search(client, MAYA, "renewal budget")
    assert search(client, MAYA, "  Renewal   BUDGET ")["cache"] == "hit"


def test_live_page_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Access Revoked" in r.text


def test_me_reports_current_groups(client):
    assert client.get("/me", headers=MAYA).json()["groups"] == ["finance", "workspace"]
    revoke(client)
    assert client.get("/me", headers=MAYA).json()["groups"] == ["workspace"]


def test_reset_restores_access_and_empties_cache(client):
    search(client, MAYA)
    revoke(client)
    assert client.post("/admin/reset", headers=SAM).status_code == 403
    assert client.post("/admin/reset", headers=ADMIN).status_code == 200
    first = search(client, MAYA)
    assert first["cache"] == "miss"
    assert set(ids(first)) == {WORKSPACE_DOC, FINANCE_DOC}
