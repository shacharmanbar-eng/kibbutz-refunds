# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

"החזר קל" (Hechzer Kal): a Hebrew, RTL, mobile-first Flask app where invited kibbutz members upload receipts, build a refund request, review the exact email, and approve sending it to the accounting office. `Hechzer-Kal-Spec.md` is the authoritative spec (Hebrew). It separates approved decisions (מאושר) from proposals (מוצע) and open items (פתוח). Keep the approved decisions, treat the proposals as provisional, and never invent business rules or eligibility. The user writes in Hebrew, so answer in Hebrew.

Current state: build stages 1–3 of spec §13 are done. **Sending is not implemented.** The default sender raises `SendingDisabled`, so approved requests end in status `held`. Never present a stub or a simulated send as a working capability.

## Workflow (required by the user)

1. **Plan before executing.** For any change beyond a trivial fix, write a plan first (plan mode) and get the user's go-ahead before editing code.
2. **Models:** planning uses Opus 5.5 (`claude-opus-5-5`), and execution always uses Sonnet 5.5 (`claude-sonnet-5-5`). If the session is on the wrong model for the current phase, tell the user to switch with `/model` rather than continuing. When delegating execution to a subagent, pass `model: "sonnet"`.
3. **After every execution, run end-to-end tests:**
   - `pytest -q` (the full suite must pass), then
   - start the dev server and walk through the real flow in a browser: log in → upload a synthetic receipt → fill in amount/category → create a request → check the email preview → approve → confirm the resulting status (`held` while sending is disabled) and the history page. Also cover whatever flow the change touched.
   - Report what was checked and what failed. Don't claim e2e passed if the browser step was skipped.
4. **Regression after every code change:** always run the *full* `pytest` suite, not only the tests near the change. Every bug fix gets a test that would have caught it, so the suite keeps growing as the regression net.
5. **Security review after every code change:** run `/security-review` on the pending changes (it needs a git repo with the change uncommitted or on a branch). Also check the diff against this project's own risks:
   - every new route that touches receipts or requests goes through `own_receipt` / `own_request` (no unscoped queries, including file paths);
   - every new POST form carries the CSRF token;
   - SQL stays parameterized; templates don't use `|safe` on user or receipt data;
   - the mail recipient still comes only from settings, and nothing can send without a fresh approval hash;
   - uploads are checked by extension and size, and stored under the owner's folder with a generated name;
   - logs and the Obsidian log contain no receipt images, receipt text, amounts, tokens or secrets.
   Report findings (or "none found") together with the test results.

## Commands (Windows, run from repo root)

```
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pytest -q                                   # all tests
.venv\Scripts\python -m pytest -q tests/test_app.py::test_invite_flow   # single test
.venv\Scripts\flask --app app run                                   # dev server, http://127.0.0.1:5000
.venv\Scripts\flask --app app create-admin you@example.com          # prints one-time /invite/<token> path
```

There is no linter or build step. Runtime data (SQLite DB and uploaded files) lives in `instance/` and is gitignored. `SECRET_KEY` comes from the environment or `.env` (loaded by python-dotenv through the Flask CLI).

## Architecture

- `app/__init__.py` holds `create_app(test_config)`. It initializes the schema on startup (no migrations; `CREATE TABLE IF NOT EXISTS`), enforces a session CSRF token on **every** POST (each form needs `<input name="csrf" value="{{ csrf_token }}">`), and registers the `create-admin` CLI.
- `app/db.py` uses raw `sqlite3` in autocommit mode (`isolation_level=None`), with no ORM. Admin-editable config (`accounting_email`, `test_email`, `target_mode` = test|live) lives in the `settings` table.
- `app/views.py` holds all user-facing routes. **Data isolation:** every receipt or request lookup goes through `own_receipt` / `own_request`, which scope by `g.user["id"]` and return 404 otherwise, including the file download route. The admin page shows users and status counts only, never receipt content.
- `app/mail.py` composes and sends.

### Send pipeline invariants (spec §10, §14, §15, all covered by tests)

1. `compose()` builds the message from a fixed template. The recipient always comes from settings (the test address unless `target_mode == live`), never from receipt text. `content_hash()` hashes to/cc/subject/body plus each attachment's sha256.
2. The request page embeds that hash, and `approve` rejects the POST if the current hash differs ("you approve exactly what you saw"). On approval, the snapshot (to/cc/subject/body/hash) is stored on the request.
3. `send_request()` claims the request atomically (`UPDATE … SET status='sending' WHERE status='approved'`), so double clicks send at most once. It re-checks the hash before sending and falls back to `draft` if the content changed.
4. The sender is pluggable: `app.config["SENDER"]` takes a message dict and returns a message id, or raises `SendingDisabled` → `held`, `DefiniteFailure` → `failed` (re-approvable), or `UnknownOutcome`/any other exception → `unknown`. An `unknown` request is **never** retried automatically. Tests inject fake senders this way, and the Gmail sender (stage 4) should plug in here.
5. Editing a receipt, or removing one from a request in `draft/held/failed`, voids the approval (`void_approval`). Receipts in `sending/sent/unknown` requests are locked.

Request statuses: `draft, approved, sending, sent, failed, unknown, held`. Receipt statuses: `needs_input, ready, submitted`. Hebrew labels live in `STATUS_LABELS`. "נשלח" means only that the provider accepted the message, never that the refund was approved.

Duplicates: `UNIQUE(owner_id, sha256)` blocks re-uploading the same file, and a receipt that has already been submitted can't join a new request. Equal amounts alone must never block. Detecting a photo and a PDF of the same receipt needs OCR, which isn't built yet.

## Constraints from the spec

- No automatic sending without explicit approval. Don't send to the accounting address until the user explicitly switches to live. Test sends go to the user's test address.
- The app does **not** read the user's mailbox (decision in spec v1.1). Gmail is for sending only, and receipts from email are uploaded manually.
- Never put real receipts, tokens or `.env` in git. Test data must be synthetic.
- Keep costs near zero. OCR, hosting and the Google OAuth setup are still open (spec stage 0: feasibility check).

## Tooling

`.claude/settings.json` has a PostToolUse hook (`.claude/hooks/obsidian_log.py`) that appends every Write/Edit/Bash/PowerShell action to `Projects/Hechzer-Kal/יומן פעולות.md` in the user's Obsidian vault. The Bash `description` is what gets logged, so write it meaningfully. The project overview note `Projects/Hechzer-Kal/החזר קל.md` in the vault should be updated when a build stage or key decision changes.
