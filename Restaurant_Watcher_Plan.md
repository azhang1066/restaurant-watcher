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
| `db.py` | Schema, versioned migrations, all SQL. Also the small `meta`, `pending_adds` and `api_calls` (spend budget) tables. |
| `places_client.py` | Google Places API (New): text search, status poll, typed errors. |
| `closure_checker.py` | Claude + web search "closing soon" judgment. Raises when it can't read a verdict. |
| `notifier.py` | ntfy + optional SMTP, healthcheck ping. |
| `app.py`, `templates/`, `static/` | Flask dashboard. `static/app.js` holds the delete confirm and the lock-on-submit for POST forms. |
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
- **The spending buttons are bounded in layers.** Check now has a per-row cooldown (checked within
  `CHECK_NOW_COOLDOWN_HOURS`, by anyone, is refused); Search and Check now each draw on a rolling
  24-hour budget (`db.spend_budget`, counted *before* the call so a billed failure still counts);
  forms lock their buttons on submit as a convenience. The budgets count dashboard presses only —
  refusing a scheduled check would silently stop the watching, and the dashboard never calls Claude,
  so there is no Claude budget. Provider quotas are the backstop (section 4).
- **Destruction is graded:** archive (a closure; reversible), reject (an unverified wrong match;
  history deleted), delete (anything; confirm dialog, logged with the place_id).
- **Watching the watcher.** A dead scheduler can't report itself: `HEALTHCHECK_URL` is pinged after
  each run for an external monitor, the scheduler pushes on restart after a long gap, and the
  dashboard flags stale rows.

## 3. External services

- **Google Places (New).** `places:searchText` (seed + add form; sends `includedType: restaurant` plus an optional
  `SEARCH_BIAS_LAT/LNG/RADIUS_M` location bias) and `GET /places/{id}` (every run).
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
- **Provider-side spend caps are manual and unconfirmed.** The in-app limits (section 2) don't stop
  a bug in the app or the scheduler itself. Set the Google Cloud Places quota + billing budget alert
  and the Anthropic monthly limit (README, "Spending limits"), then delete this line. There is
  still no per-IP rate limit, which only matters once the dashboard is exposed.
- **Delete's confirm dialog is client-side**, so a browser with JavaScript off submits unasked.
  The route logs what it removed, place_id included.
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
a reverse proxy with its own auth, or app-level login), then add a per-IP rate limit on Search / Check now and replace the dev server with a WSGI server.

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
