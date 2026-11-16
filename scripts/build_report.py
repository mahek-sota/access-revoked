"""Generate report/index.html from a real test run and a real walkthrough.

    .venv/bin/python scripts/build_report.py [--repo-url https://github.com/<you>/access-revoked]

1. Runs pytest. Any failure, error, or zero collected tests stops generation.
2. Runs scripts/walkthrough.py against a live uvicorn server.
3. Checks the observed behavior against the claims the report makes.
   If they disagree, generation stops.
4. Sanitizes the captured responses and renders a single self-contained
   HTML file with the data embedded.
"""
from __future__ import annotations

import argparse
import ast
import html
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import walkthrough  # noqa: E402
from app.db import DOCUMENTS  # noqa: E402

TEMPLATE = ROOT / "scripts" / "report_template.html"
OUT = ROOT / "report" / "index.html"
ARTIFACTS = ROOT / "artifacts"
TEST_FILE = "tests/test_access.py"


class GenerationError(SystemExit):
    def __init__(self, msg: str) -> None:
        super().__init__(f"report generation failed: {msg}")


# --------------------------------------------------------------------- tests

def run_tests() -> dict:
    ARTIFACTS.mkdir(exist_ok=True)
    xml_path = ARTIFACTS / "pytest.xml"
    xml_path.unlink(missing_ok=True)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         f"--junitxml={xml_path}"],
        cwd=ROOT, capture_output=True, text=True,
    )
    print(proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "(no pytest output)")
    if proc.returncode != 0 or not xml_path.exists():
        print(proc.stdout, proc.stderr, sep="\n")
        raise GenerationError(f"pytest exited with {proc.returncode}")

    root = ET.parse(xml_path).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    counts = {k: int(suite.get(k, 0)) for k in ("tests", "failures", "errors", "skipped")}
    if counts["tests"] == 0 or counts["failures"] or counts["errors"]:
        raise GenerationError(f"test results not clean: {counts}")
    names = [tc.get("name") for tc in suite.iter("testcase")]
    counts["passed"] = counts["tests"] - counts["skipped"]
    counts["names"] = names
    return counts


# --------------------------------------------------------------- walkthrough

def body(step: dict):
    try:
        return json.loads(step["raw_body"])
    except json.JSONDecodeError:
        return None


def access_label(doc_id: int) -> str:
    for d in DOCUMENTS:
        if d[0] == doc_id:
            return "Finance only" if d[3] == ["finance"] else "Workspace-wide"
    return "Unknown"


def analyze(capture: dict) -> dict:
    steps = {s["label"]: s for s in capture["steps"]}
    warm, after = body(steps["search_warm"]), body(steps["search_after_revoke"])
    other = body(steps["search_other_user"])
    after_ids = {r["id"] for r in after["results"]}
    removed = [r for r in warm["results"] if r["id"] not in after_ids]
    kept = [r for r in warm["results"] if r["id"] in after_ids]

    # Restricted fields: everything Maya could see about the removed document
    # before revocation (title, snippet, URL, and the full text from the old
    # link). None of it may appear in the post-revocation responses.
    before_doc = body(steps["open_link_before"]) or {}
    restricted = []
    for r in removed:
        restricted += [("title", r["title"]), ("snippet", r["snippet"]), ("url", r["url"])]
    if before_doc.get("body"):
        restricted.append(("body", before_doc["body"]))
    post_raw = steps["search_after_revoke"]["raw_body"] + steps["open_link_after"]["raw_body"]
    leaked = [kind for kind, value in restricted if value and value in post_raw]
    leaked += ["id"] if any(r["id"] in after_ids for r in removed) else []

    checks = {
        "cold search was a cache miss": steps["search_cold"]["x_cache"] == "miss",
        "second search was a cache hit": warm["cache"] == "hit",
        "old link opened before revocation": steps["open_link_before"]["status"] == 200,
        "revocation committed": steps["revoke"]["status"] == 200 and body(steps["revoke"])["revoked"],
        "post-revocation search was a cache hit": after["cache"] == "hit",
        "a result disappeared after revocation": len(removed) >= 1,
        "a workspace result remained": len(kept) >= 1,
        "old link returns 404 after revocation": steps["open_link_after"]["status"] == 404,
        "no restricted fields in post-revocation responses": not leaked,
        "other user reused the cache": other["cache"] == "hit",
        "other user saw no Finance result": all(
            access_label(r["id"]) != "Finance only" for r in other["results"]
        ),
    }
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        raise GenerationError("observed behavior contradicts the report: " + "; ".join(failed))

    return {
        "steps": steps, "warm": warm, "after": after, "other": other,
        "removed": removed, "kept": kept, "restricted_checked": [k for k, _ in restricted] + ["id"],
        "leaked": leaked,
    }


def sanitize(capture: dict) -> dict:
    """Keep only what the page displays. Raw bodies, headers, and the
    server's host and port are dropped."""
    out = []
    for s in capture["steps"]:
        b = body(s) or {}
        step = {"label": s["label"], "user": s["user"], "method": s["method"],
                "path": s["path"], "status": s["status"], "cache": s["x_cache"]}
        if s["path"].startswith("/search"):
            step.update(query=b["query"], permitted_count=b["permitted_count"], results=[
                {"id": r["id"], "title": r["title"], "snippet": r["snippet"],
                 "url": r["url"], "access": access_label(r["id"])}
                for r in b["results"]
            ])
        elif s["path"].startswith("/docs/"):
            step["response"] = {"detail": b["detail"]} if "detail" in b else {"id": b.get("id"), "title": b.get("title")}
        else:
            step["request_body"] = s["request_body"]
            step["response"] = b
        out.append(step)
    return {"captured_at": capture["captured_at"], "query": capture["query"], "steps": out}


# ------------------------------------------------------------------ rendering

def esc(x) -> str:
    return html.escape(str(x), quote=True)


def line_of(path: str, pattern: str) -> int:
    for n, line in enumerate((ROOT / path).read_text().splitlines(), 1):
        if re.search(pattern, line):
            return n
    raise GenerationError(f"could not find {pattern!r} in {path}")


def code_ref(repo: str | None, path: str, symbol: str | None, label: str) -> str:
    line = line_of(path, rf"^\s*(def|class) {re.escape(symbol)}\b") if symbol else None
    loc = f"{path}:{line}" if line else path
    if repo:
        href = f"{repo.rstrip('/')}/blob/main/{path}" + (f"#L{line}" if line else "")
        return f'<a class="code-ref" href="{esc(href)}" target="_blank" rel="noopener"><code>{esc(label)}</code><span>{esc(loc)}</span></a>'
    return f'<span class="code-ref pending" title="Repository link added when the repo is public"><code>{esc(label)}</code><span>{esc(loc)}</span></span>'


def test_source(name: str) -> str:
    src = (ROOT / TEST_FILE).read_text()
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise GenerationError(f"test {name} not found")


def result_titles(results: list[dict]) -> str:
    if not results:
        return "<em>none</em>"
    return "<br>".join(f'{esc(r["title"])} <span class="tag {"tag-fin" if r["id"] in FIN_IDS else "tag-ws"}">{esc(access_label(r["id"]))}</span>' for r in results)


FIN_IDS = {d[0] for d in DOCUMENTS if d[3] == ["finance"]}


def render(tests: dict, a: dict, clean: dict, meta: dict, repo: str | None) -> str:
    st = a["steps"]
    warm, after = a["warm"], a["after"]
    link_path = st["open_link_after"]["path"]
    n_before, n_after = warm["permitted_count"], after["permitted_count"]

    rows = [
        ("Query", f'<code>{esc(warm["query"])}</code>', f'<code>{esc(after["query"])}</code>'),
        ("Cache status", f'<span class="cache-pill sm">Cache {esc(warm["cache"])}</span>',
         f'<span class="cache-pill sm">Cache {esc(after["cache"])}</span>'),
        ("Visible results", f'<strong class="num">{n_before}</strong>{result_titles(warm["results"])}',
         f'<strong class="num">{n_after}</strong>{result_titles(after["results"])}'),
        (f"Direct link <code>{esc(link_path)}</code>",
         f'<span class="status ok">{st["open_link_before"]["status"]}</span>',
         f'<span class="status bad">{st["open_link_after"]["status"]}</span>'),
    ]
    before_after = "\n".join(
        f'<tr><th scope="row">{k}</th><td>{b}</td><td class="after">{c}</td></tr>' for k, b, c in rows
    )

    tests_list = "\n".join(f"<li><code>{esc(n)}</code></li>" for n in tests["names"])
    other = a["other"]
    removed_title = a["removed"][0]["title"]
    fields = ", ".join(a["restricted_checked"])

    refs = {
        "REF_CACHE": code_ref(repo, "app/search.py", "CandidateCache", "CandidateCache"),
        "REF_FTS": code_ref(repo, "app/search.py", "fts_candidates", "fts_candidates()"),
        "REF_ACL": code_ref(repo, "app/search.py", "permitted_ids", "permitted_ids()"),
        "REF_LOAD": code_ref(repo, "app/search.py", "load_documents", "load_documents()"),
        "REF_SNIPPET": code_ref(repo, "app/search.py", "build_snippet", "build_snippet()"),
        "REF_SEARCH": code_ref(repo, "app/search.py", "search", "search()"),
        "REF_GETDOC": code_ref(repo, "app/search.py", "get_document", "get_document()"),
        "REF_REVOKE": code_ref(repo, "app/search.py", "revoke_membership", "revoke_membership()"),
        "REF_T_WARM": code_ref(repo, TEST_FILE, "test_revocation_with_warm_cache_hides_finance_doc", "test_revocation_with_warm_cache_hides_finance_doc"),
        "REF_T_LINK": code_ref(repo, TEST_FILE, "test_old_link_returns_404_after_revocation", "test_old_link_returns_404_after_revocation"),
        "REF_T_FIELDS": code_ref(repo, TEST_FILE, "test_post_revocation_response_has_no_restricted_fields", "test_post_revocation_response_has_no_restricted_fields"),
        "REF_T_CROSS": code_ref(repo, TEST_FILE, "test_cross_user_cache_shares_ids_not_results", "test_cross_user_cache_shares_ids_not_results"),
        "REF_T_CROSS2": code_ref(repo, TEST_FILE, "test_cross_user_cache_does_not_narrow_results", "test_cross_user_cache_does_not_narrow_results"),
        "REF_T_TX": code_ref(repo, TEST_FILE, "test_transaction_boundary", "test_transaction_boundary"),
        "REF_T_LOAD": code_ref(repo, TEST_FILE, "test_restricted_text_never_loaded_after_revocation", "test_restricted_text_never_loaded_after_revocation"),
        "REF_WALK": code_ref(repo, "scripts/walkthrough.py", None, "scripts/walkthrough.py"),
        "REF_BUILD": code_ref(repo, "scripts/build_report.py", None, "scripts/build_report.py"),
    }
    repo_footer = (
        f'<a href="{esc(repo)}" target="_blank" rel="noopener">Project repository</a>' if repo
        else '<span class="pending" title="Added when the repository is public">Project repository (link coming soon)</span>'
    )
    repo_note = "" if repo else '<p class="note">Code references show file and line. Links to the repository will be added when it is public.</p>'

    data_json = json.dumps(clean, ensure_ascii=False).replace("<", "\\u003c")
    values = {
        **refs,
        "TESTS_PASSED": str(tests["passed"]),
        "TESTS_TOTAL": str(tests["tests"]),
        "TESTS_SKIPPED": str(tests["skipped"]),
        "TEST_LIST": tests_list,
        "N_BEFORE": str(n_before),
        "N_AFTER": str(n_after),
        "CACHE_AFTER": esc(after["cache"]),
        "LINK_BEFORE": str(st["open_link_before"]["status"]),
        "LINK_AFTER": str(st["open_link_after"]["status"]),
        "LINK_PATH": esc(link_path),
        "LEAK_COUNT": str(len(a["leaked"])),
        "FIELDS_CHECKED": esc(fields),
        "REMOVED_TITLE": esc(removed_title),
        "BEFORE_AFTER_ROWS": before_after,
        "OTHER_CACHE": esc(other["cache"]),
        "OTHER_COUNT": f'{other["permitted_count"]} result{"" if other["permitted_count"] == 1 else "s"}',
        "OTHER_TITLES": result_titles(other["results"]),
        "CROSS_TEST_SRC": esc(test_source("test_cross_user_cache_shares_ids_not_results")),
        "CAPTURED_AT": esc(clean["captured_at"]),
        "PYTHON": esc(meta["python"]),
        "SQLITE": esc(meta["sqlite"]),
        "REPO_FOOTER": repo_footer,
        "REPO_NOTE": repo_note,
        "DATA_JSON": data_json,
    }
    page = TEMPLATE.read_text()
    for k, v in values.items():
        page = page.replace("{{" + k + "}}", v)
    leftover = re.findall(r"\{\{[A-Z0-9_]+\}\}", page)
    if leftover:
        raise GenerationError(f"unfilled placeholders: {sorted(set(leftover))}")
    return page


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-url", default=os.environ.get("ACCESS_REVOKED_REPO_URL"),
                    help="public repo URL; code references become links when set")
    args = ap.parse_args()

    tests = run_tests()
    capture = walkthrough.run()
    (ARTIFACTS / "walkthrough.json").write_text(json.dumps(capture, indent=2))

    analysis = analyze(capture)
    clean = sanitize(capture)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(render(tests, analysis, clean, capture, args.repo_url))
    print(f"tests: {tests['passed']}/{tests['tests']} passed")
    print(f"walkthrough: {analysis['warm']['permitted_count']} -> {analysis['after']['permitted_count']} results, "
          f"cache {analysis['after']['cache']}, old link {analysis['steps']['open_link_after']['status']}, "
          f"restricted fields leaked: {len(analysis['leaked'])}")
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
