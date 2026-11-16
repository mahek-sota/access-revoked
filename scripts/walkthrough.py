"""Run the revocation walkthrough against a real local server and record it.

Starts `uvicorn app.main:create_app --factory` on a free port with a fresh
database, sends real HTTP requests, and writes every request/response to
artifacts/walkthrough.json.

    .venv/bin/python scripts/walkthrough.py
"""
from __future__ import annotations

import json
import os
import platform
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUERY = "renewal budget"

# (label, user, method, path, json body)
STEPS = [
    ("search_cold", "maya", "GET", "/search?" + urllib.parse.urlencode({"q": QUERY}), None),
    ("search_warm", "maya", "GET", "/search?" + urllib.parse.urlencode({"q": QUERY}), None),
    # Cross-user: Sam is not in Finance and reuses the cache Maya warmed.
    ("search_other_user", "sam", "GET", "/search?" + urllib.parse.urlencode({"q": QUERY}), None),
    ("open_link_before", "maya", "GET", "/docs/{finance_doc}", None),
    ("revoke", "admin", "POST", "/admin/memberships/revoke", {"user": "maya", "group": "finance"}),
    ("search_after_revoke", "maya", "GET", "/search?" + urllib.parse.urlencode({"q": QUERY}), None),
    ("open_link_after", "maya", "GET", "/docs/{finance_doc}", None),
]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(base: str, user: str, method: str, path: str, body) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("X-User", user)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status, headers, raw = resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as err:
        status, headers, raw = err.code, dict(err.headers), err.read().decode()
    return {
        "user": user,
        "method": method,
        "path": path,
        "request_body": body,
        "status": status,
        "x_cache": headers.get("x-cache") or headers.get("X-Cache"),
        "raw_body": raw,
    }


def wait_until_up(base: str, proc: subprocess.Popen, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("uvicorn exited early:\n" + proc.stdout.read())
        try:
            urllib.request.urlopen(base + "/openapi.json", timeout=1).read()
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("server did not start in time")


def run() -> dict:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "ACCESS_REVOKED_DB": str(Path(tmp) / "walkthrough.db")}
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory",
             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            wait_until_up(base, proc)
            steps = []
            finance_doc = None
            for label, user, method, path, body in STEPS:
                if "{finance_doc}" in path:
                    path = path.format(finance_doc=finance_doc)
                step = {"label": label, **request(base, user, method, path, body)}
                steps.append(step)
                if label == "search_warm":
                    # The old link is whichever result Maya could open before
                    # revocation and is granted only to Finance (from the seed).
                    finance_doc = finance_doc_id(json.loads(step["raw_body"]))
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    return {
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "query": QUERY,
        "steps": steps,
    }


def finance_doc_id(search_result: dict) -> int:
    sys.path.insert(0, str(ROOT))
    from app.db import DOCUMENTS

    finance_only = {d[0] for d in DOCUMENTS if d[3] == ["finance"]}
    hits = [r["id"] for r in search_result["results"] if r["id"] in finance_only]
    if len(hits) != 1:
        raise RuntimeError(f"expected one Finance-only result, got {hits}")
    return hits[0]


def main() -> None:
    out = ROOT / "artifacts" / "walkthrough.json"
    out.parent.mkdir(exist_ok=True)
    capture = run()
    out.write_text(json.dumps(capture, indent=2))
    for s in capture["steps"]:
        print(f'{s["label"]:<22} {s["method"]:<5} {s["path"]:<40} {s["status"]}  cache={s["x_cache"]}')
    print(f"wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
