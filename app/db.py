"""SQLite storage: documents, an FTS5 index, and the ACL tables.

All seed data is fictional.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS groups (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
    user_id  TEXT NOT NULL REFERENCES users(id),
    group_id TEXT NOT NULL REFERENCES groups(id),
    PRIMARY KEY (user_id, group_id)
);
CREATE TABLE IF NOT EXISTS documents (
    id    INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    body  TEXT NOT NULL
);
-- A document is readable by a user who belongs to any group granted on it.
CREATE TABLE IF NOT EXISTS doc_grants (
    doc_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    group_id TEXT    NOT NULL REFERENCES groups(id),
    PRIMARY KEY (doc_id, group_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    title, body, content='documents', content_rowid='id'
);
"""

# Fictional workspace. "workspace" contains everyone; "finance" contains Maya.
USERS = [
    ("maya", "Maya (fictional)", 0),
    ("sam", "Sam (fictional)", 0),
    ("admin", "Workspace admin", 1),
]
GROUPS = [("workspace", "Everyone"), ("finance", "Finance")]
MEMBERSHIPS = [
    ("maya", "workspace"),
    ("maya", "finance"),
    ("sam", "workspace"),
    ("admin", "workspace"),
]
DOCUMENTS = [
    (
        1,
        "Renewal budget planning: kickoff notes",
        "Kickoff notes for the renewal budget cycle. Teams submit renewal "
        "requests by the end of the month. Owners and timelines are listed "
        "below. Shared with the whole workspace.",
        ["workspace"],
    ),
    (
        2,
        "Renewal budget: vendor line items (Finance only)",
        "Draft renewal budget with per-vendor line items for the fictional "
        "Example Co. Includes negotiated discount targets and the walk-away "
        "number for each renewal. Restricted to Finance.",
        ["finance"],
    ),
    (
        3,
        "Office move checklist",
        "Checklist for the office move: badges, desks, and parking. "
        "Shared with the whole workspace.",
        ["workspace"],
    ),
]


def connect(path: str | Path) -> sqlite3.Connection:
    # isolation_level=None: we issue BEGIN/COMMIT ourselves so the
    # transaction boundaries are explicit in the code.
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(path: str | Path) -> None:
    """Create a fresh database with the fictional seed data."""
    path = Path(path)
    if path.exists():
        path.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    conn = connect(path)
    # WAL gives each read transaction a stable snapshot while a writer commits.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    seed(conn)
    conn.close()


def seed(conn: sqlite3.Connection) -> None:
    """Replace all rows with the fictional seed data in one transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("INSERT INTO documents_fts(documents_fts) VALUES ('delete-all')")
        for table in ("doc_grants", "memberships", "documents", "groups", "users"):
            conn.execute(f"DELETE FROM {table}")
        conn.executemany("INSERT INTO users VALUES (?, ?, ?)", USERS)
        conn.executemany("INSERT INTO groups VALUES (?, ?)", GROUPS)
        conn.executemany("INSERT INTO memberships VALUES (?, ?)", MEMBERSHIPS)
        for doc_id, title, body, grants in DOCUMENTS:
            conn.execute("INSERT INTO documents VALUES (?, ?, ?)", (doc_id, title, body))
            conn.execute(
                "INSERT INTO documents_fts(rowid, title, body) VALUES (?, ?, ?)",
                (doc_id, title, body),
            )
            conn.executemany(
                "INSERT INTO doc_grants VALUES (?, ?)", [(doc_id, g) for g in grants]
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
