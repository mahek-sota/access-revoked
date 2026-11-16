"""Browser check of the built static page (report/index.html).

Serves report/ with a plain static server (no FastAPI), then checks tabs,
direct links, replay controls, the request trace, footer links, that the page
makes no outside requests, and that nothing overflows at 375px.

    .venv/bin/python verify/check_site.py [--shots DIR]

Set PLAYWRIGHT_CHROMIUM to a Chromium binary to skip `playwright install`.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import re
import socket
import threading
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "report"
FOOTER = {
    "Impact doc": "https://impact-doc.vercel.app/",
    "LinkedIn": "https://www.linkedin.com/in/mahek-sota/",
    "GitHub": "https://github.com/mahek-sota",
}
TABS = ["demo", "report", "why", "about"]


def serve() -> tuple[str, http.server.ThreadingHTTPServer]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    handler = functools.partial(Quiet, directory=str(REPORT))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}/", srv


def embedded(page) -> dict:
    return json.loads(page.locator("#replay-data").text_content())


def visible_panels(page) -> list[str]:
    return [t for t in TABS if page.locator(f"#{t}").is_visible()]


def check(base: str, shots: Path | None) -> list[str]:
    passed: list[str] = []
    ok = passed.append
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM") or None)

        for width, label in ((1280, "desktop"), (375, "mobile")):
            ctx = browser.new_context(viewport={"width": width, "height": 900})
            page = ctx.new_page()
            errors, outside = [], []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("request", lambda r: outside.append(r.url) if not r.url.startswith(base) and not r.url.startswith("data:") else None)

            # Direct links: every hash opens its own tab on first load.
            for tab in TABS:
                page.goto(base + "#" + tab)
                assert visible_panels(page) == [tab], (tab, visible_panels(page))
                expect(page.locator(f"#tab-{tab}")).to_have_attribute("aria-selected", "true")
                overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                assert overflow <= 0, f"{label} #{tab} overflows by {overflow}px"
                if shots:
                    page.screenshot(path=shots / f"{label}-{tab}.png", full_page=True)
            ok(f"{label}: direct links #demo #report #why #about, no horizontal overflow")

            page.goto(base)
            assert visible_panels(page) == ["demo"]
            page.goto(base + "#nope")
            expect(page.locator("#demo")).to_be_visible()
            assert visible_panels(page) == ["demo"]
            for tab in ["report", "why", "about", "demo"]:
                page.click(f"#tab-{tab}")
                expect(page.locator(f"#{tab}")).to_be_visible()
                assert page.evaluate("location.hash") == "#" + tab
                assert visible_panels(page) == [tab]
            ok(f"{label}: default tab is Demo, unknown hash falls back, tab clicks update hash")

            # Replay against embedded recorded data.
            page.goto(base + "#demo")
            data = {s["label"]: s for s in embedded(page)["steps"]}
            warm, after, gone = data["search_warm"], data["search_after_revoke"], data["open_link_after"]
            card = page.locator("#results .result:not(.gone)")
            trace_rows = page.locator("#trace-body tr:not([hidden])")

            def state0():
                expect(page.locator("#cache-text")).to_have_text("Cache hit")
                expect(page.locator("#q")).to_have_text("renewal budget")
                expect(card).to_have_count(warm["permitted_count"])
                assert warm["permitted_count"] == 2
                expect(page.locator("#count-n")).to_have_text("2")
                expect(page.locator("#link-panel")).to_be_hidden()
                expect(page.locator("#btn-revoke")).to_be_enabled()
                expect(page.locator("#btn-link")).to_be_disabled()
                expect(trace_rows).to_have_count(3)

            state0()
            ok(f"{label}: replay opens on Maya's second search, Cache hit, 2 results")

            page.click("#btn-revoke")
            expect(page.locator("#cache-text")).to_have_text("Cache hit")
            expect(card).to_have_count(after["permitted_count"])  # waits for the animation
            expect(page.locator("#results .ghost")).to_have_count(1)
            expect(page.locator("#count-n")).to_have_text(str(after["permitted_count"]))
            shown = page.locator("#results .result:not(.gone) h4").all_text_contents()
            assert shown == [r["title"] for r in after["results"]], shown
            assert not any("Finance only" in t for t in shown)
            expect(trace_rows).to_have_count(5)
            ok(f"{label}: Show revocation keeps Cache hit, Finance result leaves, recorded post-revocation result stays")

            page.click("#btn-link")
            expect(page.locator("#link-panel")).to_be_visible()
            expect(page.locator("#link-status")).to_have_text(str(gone["status"]))
            assert gone["status"] == 404
            assert json.loads(page.locator("#link-json").text_content()) == gone["response"]
            expect(trace_rows).to_have_count(6)
            ok(f"{label}: Try the old link shows the recorded 404")

            page.click("summary:has-text('Request trace')")
            rows = page.locator("#trace-body tr").all()
            labels = ["search_cold", "search_warm", "open_link_before", "revoke", "search_after_revoke", "open_link_after"]
            for row, lab in zip(rows, labels):
                cells = row.locator("td").all_text_contents()
                s = data[lab]
                assert cells[2] == (s.get("query") or "—"), (lab, cells)
                assert cells[3] == (f"Cache {s['cache']}" if s["cache"] else "—"), (lab, cells)
                assert cells[4] == (str(s["permitted_count"]) if "permitted_count" in s else "—"), (lab, cells)
                assert cells[5] == str(s["status"]), (lab, cells)
            if shots:
                page.screenshot(path=shots / f"{label}-demo-final.png", full_page=True)
            ok(f"{label}: trace shows query, cache, permitted count, HTTP status from captured data")

            page.click("#btn-reset")
            state0()
            page.click("#btn-revoke")
            expect(card).to_have_count(1)
            ok(f"{label}: Reset replays from the start, and the replay runs again")

            hrefs = {a.text_content().strip(): a.get_attribute("href") for a in page.locator("footer a").all()}
            for name, url in FOOTER.items():
                assert hrefs.get(name) == url, (name, hrefs)
            ok(f"{label}: footer links point to the right URLs")

            assert not errors, errors
            assert not outside, outside
            ok(f"{label}: no console errors, no requests outside the static page")
            ctx.close()
        browser.close()
    return passed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", type=Path, help="write screenshots here")
    args = ap.parse_args()
    if args.shots:
        args.shots.mkdir(parents=True, exist_ok=True)
    html = (REPORT / "index.html").read_text()
    assert not re.search(r"\{\{[A-Z_]+\}\}", html), "unfilled placeholders"
    base, srv = serve()
    try:
        for line in check(base, args.shots):
            print("PASS", line)
    finally:
        srv.shutdown()


if __name__ == "__main__":
    main()
