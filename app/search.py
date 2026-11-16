"""Search and document access.

Request path for a search:
  1. The candidate cache (or FTS5 on a miss) supplies ranked candidate IDs.
     Candidates depend only on the query text, never on who is asking.
  2. The current ACL filters those IDs, inside one read transaction.
  3. Text is loaded, and snippets are built, only for the permitted IDs,
     inside that same transaction.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from collections import OrderedDict
from typing import Callable, Optional

TOKEN = re.compile(r"\w+", re.UNICODE)


def normalize(query: str) -> str:
    return " ".join(TOKEN.findall(query.lower()))


class CandidateCache:
    """Shared LRU of query -> ranked candidate doc IDs.

    It stores IDs only: no titles, no text, no permission decisions. That is
    what makes it safe to share across users and across permission changes.
    """

    def __init__(self, max_entries: int = 256) -> None:
        self._data: "OrderedDict[str, tuple[int, ...]]" = OrderedDict()
        self._lock = threading.Lock()
        self._max = max_entries

    def get(self, key: str) -> Optional[tuple[int, ...]]:
        with self._lock:
            ids = self._data.get(key)
            if ids is not None:
                self._data.move_to_end(key)
            return ids

    def put(self, key: str, ids: tuple[int, ...]) -> None:
        with self._lock:
            self._data[key] = ids
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


def fts_candidates(conn: sqlite3.Connection, key: str, limit: int = 50) -> tuple[int, ...]:
    terms = key.split()
    if not terms:
        return ()
    # Quote every term so user input can't use FTS5 operators.
    match = " ".join('"' + t.replace('"', '""') + '"' for t in terms)
    rows = conn.execute(
        "SELECT rowid FROM documents_fts WHERE documents_fts MATCH ? "
        "ORDER BY bm25(documents_fts) LIMIT ?",
        (match, limit),
    ).fetchall()
    return tuple(r[0] for r in rows)


def permitted_ids(conn: sqlite3.Connection, user_id: str, ids: tuple[int, ...]) -> list[int]:
    """Filter candidate IDs through the ACL as it stands in this transaction."""
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT DISTINCT g.doc_id
              FROM doc_grants g
              JOIN memberships m ON m.group_id = g.group_id
             WHERE m.user_id = ? AND g.doc_id IN ({placeholders})""",
        (user_id, *ids),
    ).fetchall()
    allowed = {r[0] for r in rows}
    return [i for i in ids if i in allowed]  # keep the ranking order


def load_documents(conn: sqlite3.Connection, ids: list[int]) -> dict[int, sqlite3.Row]:
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, title, body FROM documents WHERE id IN ({placeholders})", ids
    ).fetchall()
    return {r["id"]: r for r in rows}


def build_snippet(body: str, terms: list[str], width: int = 120) -> str:
    lower = body.lower()
    hits = [i for i in (lower.find(t) for t in terms) if i >= 0]
    start = max(0, min(hits) - 30) if hits else 0
    text = body[start : start + width].strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if start + width < len(body) else ""
    return prefix + text + suffix


def search(
    conn: sqlite3.Connection,
    cache: CandidateCache,
    user_id: str,
    query: str,
    after_acl_filter: Optional[Callable[[], None]] = None,
) -> dict:
    key = normalize(query)
    cached = cache.get(key)
    cache_status = "hit" if cached is not None else "miss"

    # One read transaction covers the ACL check and the text load. In WAL
    # mode the snapshot is fixed at the first read, so both steps see the
    # same permissions: a revocation either committed before this request's
    # snapshot (and is enforced) or after it (and applies to the next request).
    conn.execute("BEGIN")
    try:
        if cached is None:
            cached = fts_candidates(conn, key)
            cache.put(key, cached)
        allowed = permitted_ids(conn, user_id, cached)
        if after_acl_filter is not None:  # test hook
            after_acl_filter()
        docs = load_documents(conn, allowed)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    terms = key.split()
    results = [
        {
            "id": doc_id,
            "title": docs[doc_id]["title"],
            "snippet": build_snippet(docs[doc_id]["body"], terms),
            "url": f"/docs/{doc_id}",
        }
        for doc_id in allowed
        if doc_id in docs  # skip candidates deleted since they were cached
    ]
    return {
        "query": query,
        "cache": cache_status,
        "permitted_count": len(results),
        "results": results,
    }


def get_document(conn: sqlite3.Connection, user_id: str, doc_id: int) -> Optional[dict]:
    """Return the document only if the user can read it right now.

    Missing and forbidden look identical to the caller (both None -> 404),
    so a revoked link doesn't confirm the document still exists.
    """
    row = conn.execute(
        """SELECT d.id, d.title, d.body
             FROM documents d
            WHERE d.id = ?
              AND EXISTS (SELECT 1 FROM doc_grants g
                            JOIN memberships m ON m.group_id = g.group_id
                           WHERE g.doc_id = d.id AND m.user_id = ?)""",
        (doc_id, user_id),
    ).fetchone()
    return dict(row) if row else None


def revoke_membership(conn: sqlite3.Connection, user_id: str, group_id: str) -> bool:
    """Remove a membership. The candidate cache is deliberately left alone."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "DELETE FROM memberships WHERE user_id = ? AND group_id = ?",
            (user_id, group_id),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return cur.rowcount > 0


def grant_membership(conn: sqlite3.Connection, user_id: str, group_id: str) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO memberships VALUES (?, ?)", (user_id, group_id)
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
