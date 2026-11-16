# Access Revoked

Same search. Same cache. Different permissions.

A small FastAPI + SQLite FTS5 app that keeps search results and old document
links in sync with permission changes, even when the search is served from a
warm cache. All people and documents in it are fictional.

## Layout
- `app/`: the FastAPI app (`main.py`), search and ACL logic (`search.py`), schema and seed data (`db.py`)
- `tests/`: pytest suite
- `scripts/walkthrough.py`: starts the app with uvicorn and records a real revocation walkthrough over HTTP
- `scripts/build_report.py`: runs the tests and the walkthrough, then generates the report
- `scripts/report_template.html`: the page template
- `report/index.html`: the generated, self-contained static report (the Vercel root)
- `verify/check_site.py`: Playwright check of the built page on desktop and at 375px

## Setup
```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m playwright install chromium   # only needed for verify/
```
Tested with Python 3.11.4 and SQLite 3.41.2.

## Run the app and try it live
```sh
.venv/bin/uvicorn app.main:create_app --factory
```
Open http://127.0.0.1:8000/ for the live page: search as Maya or Sam, revoke or restore Maya's Finance access,
re-open old links, and watch the request log. Every click is a real request, and "Reset demo data" restores
the seed and empties the cache. The same flow with curl:
```sh
curl -H 'X-User: maya' 'localhost:8000/search?q=renewal+budget'
curl -X POST -H 'X-User: admin' -H 'Content-Type: application/json' \
     -d '{"user":"maya","group":"finance"}' localhost:8000/admin/memberships/revoke
curl -H 'X-User: maya' 'localhost:8000/search?q=renewal+budget'   # cache hit, 1 result
curl -i -H 'X-User: maya' localhost:8000/docs/2                    # 404
```
The database is reseeded on every start. `X-User` (maya, sam, admin) is a demo stand-in for a real session.

## Tests
```sh
.venv/bin/python -m pytest
```

## Regenerate the report
```sh
.venv/bin/python scripts/build_report.py
# once the repo is public, turn code references into links:
.venv/bin/python scripts/build_report.py --repo-url https://github.com/mahek-sota/access-revoked
```
The script stops without writing anything if a test fails or errors, if no tests run,
or if the walkthrough's observed behavior contradicts a claim the report makes.
Raw captures go to `artifacts/` (git-ignored). The page embeds only sanitized fields.

Then check the built page:
```sh
.venv/bin/python verify/check_site.py
```

## Deploy to Vercel
`report/` is a single static file with no build step. Deploy that folder as the project root.

**CLI**
```sh
npm i -g vercel
cd report
vercel login
vercel --prod
```
When prompted, set up a new project and accept the default settings. Vercel detects no framework and serves `index.html` as is.

**Git integration** (after pushing the repo to GitHub)
1. In Vercel, choose Add New → Project and import the repository.
2. Set **Root Directory** to `report`.
3. Set **Framework Preset** to `Other`. Leave the Build Command empty and the Output Directory as the default.
4. Deploy.

Direct links work as `/#demo`, `/#report`, `/#why`, and `/#about`. Remember to run `build_report.py` and commit the regenerated `report/index.html` before each deploy. Vercel serves the file as committed.

## How it works
- **Candidates.** The query is normalized, then the shared `CandidateCache` returns ranked doc IDs. On a miss, FTS5 returns them and they are cached. Cache entries hold IDs only, never text or permission decisions.
- **ACL.** `permitted_ids()` joins `doc_grants` and `memberships` and keeps only the IDs the user can read now.
- **Text.** `load_documents()` and `build_snippet()` run only for permitted IDs.
- **Transaction.** The ACL filter and the text load share one read transaction. In WAL mode, that gives one snapshot. A revocation that committed before the snapshot is enforced. One that commits after it applies from the next request.
- **Direct links.** `/docs/{id}` uses the same ACL join and returns an identical 404 for forbidden and missing documents.

Scope: requests started after the revocation commits are covered. Copies already delivered, and offline devices, are not.
