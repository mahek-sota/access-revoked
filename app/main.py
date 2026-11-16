"""FastAPI app. Run with: uvicorn app.main:create_app --factory --reload

Identity is a demo-only `X-User` header (maya, sam, admin). It stands in for
a real session so the permission logic can be exercised locally.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db, search as s

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "access_revoked.db"
LIVE_PAGE = Path(__file__).resolve().parent / "static" / "live.html"


class MembershipChange(BaseModel):
    user: str
    group: str


def create_app(db_path: str | Path | None = None, reset: bool = True) -> FastAPI:
    db_path = Path(db_path or os.environ.get("ACCESS_REVOKED_DB", DEFAULT_DB))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if reset or not db_path.exists():
        db.init_db(db_path)

    app = FastAPI(title="Access Revoked")
    app.state.cache = s.CandidateCache()
    app.state.db_path = db_path

    def get_conn() -> Iterator:
        conn = db.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def current_user(x_user: str = Header(default=""), conn=Depends(get_conn)):
        row = conn.execute(
            "SELECT id, is_admin FROM users WHERE id = ?", (x_user,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=401, detail="Unknown user")
        return dict(row)

    def require_admin(user=Depends(current_user)):
        if not user["is_admin"]:
            raise HTTPException(status_code=403, detail="Admin only")
        return user

    @app.get("/", include_in_schema=False)
    def live_page():
        # Local-only UI. Every action on it is a real request to this app.
        return FileResponse(LIVE_PAGE)

    @app.get("/me")
    def me(user=Depends(current_user), conn=Depends(get_conn)):
        groups = [r[0] for r in conn.execute(
            "SELECT group_id FROM memberships WHERE user_id = ? ORDER BY group_id",
            (user["id"],),
        )]
        return {"user": user["id"], "groups": groups}

    @app.get("/search")
    def search_endpoint(
        q: str, response: Response, request: Request,
        user=Depends(current_user), conn=Depends(get_conn),
    ):
        result = s.search(conn, request.app.state.cache, user["id"], q)
        response.headers["X-Cache"] = result["cache"]
        return result

    @app.get("/docs/{doc_id}")
    def read_document(doc_id: int, user=Depends(current_user), conn=Depends(get_conn)):
        doc = s.get_document(conn, user["id"], doc_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="Not found")
        return doc

    @app.post("/admin/memberships/revoke")
    def revoke(change: MembershipChange, _=Depends(require_admin), conn=Depends(get_conn)):
        removed = s.revoke_membership(conn, change.user, change.group)
        return {"revoked": removed, "user": change.user, "group": change.group}

    @app.post("/admin/memberships/grant")
    def grant(change: MembershipChange, _=Depends(require_admin), conn=Depends(get_conn)):
        s.grant_membership(conn, change.user, change.group)
        return {"granted": True, "user": change.user, "group": change.group}

    @app.post("/admin/reset")
    def reset_demo(request: Request, _=Depends(require_admin), conn=Depends(get_conn)):
        """Restore the seed data and empty the cache, to rerun the scenario."""
        db.seed(conn)
        request.app.state.cache.clear()
        return {"reset": True}

    return app

