# Restaurant Watcher — Working Plan

This file tracks **open work and the decisions behind the design**. It is deliberately not a
second copy of the code's documentation: what each module does and why is in its docstring
(start with `main.py`, `checker.py`, `app.py`, `db.py`), and how to run and operate the tool is
in the [README](README.md). Finished work is dropped once done — `git log` has it. Numbers that
change daily (how many rows are verified, test counts) are not recorded here; ask the database
and `pytest`.

## 1. Overview

**Stack:** Python 3.13, SQLite (stdlib), `requests`, the `anthropic` SDK, `apscheduler`,
`python-dotenv`, `flask`; `pytest` / `ruff` / `mypy` for checks (CI runs all three).

Flat layout, one module per role:

| File | Role |
|---|---|
| `main.py` | The weekly run: lock, retry pending alerts, check each verified restaurant, housekeeping, monitor ping. Also the scheduler. |
| `checker.py` | `check_one()` — one restaurant's whole check; used by the scheduler and the dashboard's Check-now. |
| `db.py` | Schema, versioned migrations, all SQL. Also the small `meta` and `pending_adds` tables. |
| `places_client.py` | Google Places API (New): text search, status poll, typed errors. |
| `closure_checker.py` | Claude + web search "closing soon" judgment. Raises when it can't read a verdict. |
| `notifier.py` | ntfy + optional SMTP, healthcheck ping. |
| `app.py`, `templates/`, `static/` | Flask dashboard. |
| `seed.py` | Bulk-add from a text file (rows land unverified). |
| `config.py`, `statuses.py`, `http_session.py` | Env helpers + logging, status constants/labels, the shared retry policy. |

## 2. Design decisions worth keeping

- **Verification gates checking.** Text Search returns exactly one guess with no confidence, so a
  wrong "Lilia" watches as happily as the right one. Unverified rows are skipped entirely (no API
  calls, no alerts); the dashboard's confirm step is the same question asked before the row exists.
- **Result and alert are recorded together.** `check_one()` writes the new status and a
  `pending_alert` marker in one transaction, then sends and clears it. A dead ntfy loses nothing and
  repeats nothing; each run delivers leftovers first, archived rows included.
- **Alerts fire on transitions, not states** — a move *into* a closed status, or the closing-soon
  flag going 0→1. Only a news check moves that flag; plain runs and Check-now carry it forward.
  Archiving keys off the status itself so a stranded row still leaves the active list.
- **The news check is optional and fallible.** A failed or unreadable one keeps the stored flag
  (clearing it would re-arm an alert already sent); it never costs the Places status.
- **Check-now is the cheap half only** (no Claude call, run counter untouched), and is offered only
  for rows a scheduled run would check — one rule, `checker.is_checkable()`.
- **Destruction is graded:** archive (a closure; reversible), reject (an unverified wrong match;
  history deleted), delete (anything; confirm dialog, logged with the place_id).
- **Watching the watcher.** A dead scheduler can't report itself: `HEALTHCHECK_URL` is pinged after
  each run for an external monitor, the scheduler pushes on restart after a long gap, and the
  dashboard flags stale rows.

## 3. External services

- **Google Places (New).** `places:searchText` (seed + add form) and `GET /places/{id}` (every run).
  The status field mask stays at `id,businessStatus,displayName` on purpose — adding rating, hours
  and the like moves the whole call to the Enterprise SKU. 404 → `PlaceNotFound`; 401/403 →
  `PlacesAuthError` (aborts the run); an unknown status value is stored as `UNSPECIFIED`.
- **Anthropic.** Model and tool version are constants overridable from `.env`; the SDK's retries are
  set explicitly (`MAX_RETRIES`, `TIMEOUT_SECONDS`). Truncated, `pause_turn` and non-JSON replies
  are handled in `closure_checker`.
- **ntfy.** Topics are public; the name is the secret, so placeholders are refused. No delivery
  confirmation.

## 4. Known gaps and risks

- **No auth.** The dashboard binds to localhost and is served by Flask's dev server. That is fine
  until anyone wants it on a phone — at which point the list stops being local-only data, and the
  buttons that spend money (Search, Check now) and destroy history (Delete) stop being harmless.
- **No rate limit** on the two spending buttons; bounded by how fast one person clicks.
- **Delete's confirm dialog is client-side**, so a browser with JavaScript off submits unasked.
  The route logs what it removed, place_id included.
- **CSRF tokens live in a signed session cookie.** Without `DASHBOARD_SECRET_KEY` a per-process key
  is generated and open pages need a reload after a restart.
- **Serial checks.** One restaurant at a time, with retry backoff. Fine at the current size;
  the first thing to revisit is `concurrent.futures`, or per-restaurant cadence.
- **The needs-verifying push repeats every run** while anything is unverified. By design (those
  rows are unwatched), but a seeded file that is never triaged turns it into a standing weekly nag.
- **A temporarily closed place can keep its closing-soon flag indefinitely** — the news check only
  runs on `OPERATIONAL` rows. Deliberate ("closed temporarily" plus "reported closing for good" is
  what the flag is for), but the flag then reflects the last news check, not the present.
- **The restaurants-to-verify queue is manual.** Batch verify exists; there is no one-click "all",
  on purpose — ticking is the act of having looked at the address.
- **Text Search bias is soft.** It sends `includedType: restaurant` and, when `SEARCH_BIAS_LAT/LNG`
  are set, a location bias circle. Both are preferences, not filters, so wrong matches are fewer
  but the confirm step still matters.

## 5. Work plan

### Phase 1 — Auth (the blocker for serving beyond localhost)
Needed before the dashboard is reachable from a phone. Settle the threat model first (single user,
a reverse proxy with its own auth, or app-level login), then put rate limits on Search / Check now
and replace the dev server with a WSGI server.

### Phase 2 — Scale polish (only once the list outgrows a serial loop)
- Concurrency in the per-restaurant loop, or batched status checks if Places ever supports them.
- Per-restaurant cadence (e.g. news-check flagged restaurants more often).
- Move the lock and run state wholly into the database if a second host ever runs the scheduler.

## 6. Next steps

1. Run the scheduler against the real list and watch the first genuine status transition end to end
   — the serial loop's wall-clock time and cost at real length are still estimates.
2. Set `HEALTHCHECK_URL` against a real monitor and confirm that stopping the scheduler alerts.
3. Work down the verify queue in batches, looking at each address before ticking it.
4. Set `SEARCH_BIAS_LAT/LNG` in `.env` and watch whether wrong matches actually drop.
