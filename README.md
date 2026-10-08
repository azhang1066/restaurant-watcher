# Restaurant Watcher

Tracks a personal list of restaurants and notifies you when one closes, or
when recent news suggests one is about to.

## Why it's not a live Google Maps sync

Google doesn't expose a public API to read a personal "saved places" list
from Maps -- that data lives behind your Google account, not the Places API.
So this tool flips the model: you seed the list once (from Google Takeout
export or just typing names in), and the agent's job becomes checking each
place's status on a schedule rather than syncing the list itself.

## Setup

```bash
pip install -c requirements-lock.txt -r requirements.txt   # runtime
# pip install -c requirements-lock.txt -r requirements-dev.txt   # + pytest, ruff, mypy
cp .env.example .env   # fill in your keys
```

Python 3.13 (`.python-version`). Dependencies are pinned exactly, and
`requirements-lock.txt` pins the transitive ones too. `.github/dependabot.yml`
opens a weekly PR for any bump, and CI (`.github/workflows/tests.yml`) runs
`ruff check .`, `mypy` and `pytest` on it -- run the same three locally before
merging anything. The Claude model for the news check
is a default in `closure_checker.py` that `CLOSURE_CHECK_MODEL` overrides, so
when a model is retired you can switch it from `.env` without a code change.

You'll need:
- **Google Places API key** (Places API "New" enabled in Google Cloud Console)
- **Anthropic API key** (for the closing-soon news check)
- **ntfy topic** (`NTFY_TOPIC`) -- pick a long, unguessable string, then subscribe
  to it in the [ntfy app](https://ntfy.sh/) on your phone. Topics are public: anyone
  who knows the name can read the feed. So **no push is sent** while it's unset or
  still the placeholder from `.env.example` (an error is logged instead, and email
  still goes out if you've configured it).
- **SMTP credentials** (optional) -- set `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`,
  `EMAIL_FROM`, and `EMAIL_TO` in `.env` to also get alerts by email. Leave these
  unset and only ntfy push notifications will fire. For Gmail, use an
  [app password](https://myaccount.google.com/apppasswords) rather than your
  normal password.

## Seed your list

Export via Google Takeout (Maps > "Your places"), or just make a text file:

```
Lilia, Brooklyn NY
Don Angie
Katz's Delicatessen, Manhattan
```

```bash
python seed.py restaurants.txt
```

Seeding resolves each line through Google Places and takes the first hit, with
nobody looking at any of them -- so seeded restaurants land **unverified** and
are not checked until you confirm each one on the dashboard. See
[Verifying a restaurant](#verifying-a-restaurant).

## Run

```bash
python main.py --once     # one check, right now
python main.py            # runs forever, checks weekly
```

For a real deployment, run `main.py` as a background service (systemd,
Replit scheduled deployment, or a cron entry calling `--once`) rather than
leaving a terminal open.

Only one run happens at a time: a second `--once` while the scheduler is
mid-run is skipped with a warning, not run alongside it. Restarting the
scheduler doesn't trigger a check; its first run is a week after the last
completed one (or immediately, if that was longer ago or there never was one).
The run counter and the time of the last completed run live in the database.

Logs go to stderr and to `data/restaurant-watcher.log` (rotating; `LOG_FILE`
moves it, `LOG_FILE=off` disables it).

### Noticing a dead scheduler

A scheduler that has stopped can't tell you so itself. Two things cover it:

- Set `HEALTHCHECK_URL` to a push monitor ([healthchecks.io](https://healthchecks.io),
  Uptime Kuma, ...). Each completed run pings it (`/fail` if every check failed), and
  the monitor alerts you when the pings stop. This is the one that catches the
  scheduler while it is down.
- When the scheduler starts after 14+ days without a completed run, it sends a
  high-priority push saying so. That only fires once it's back, so it covers
  "I restarted it and want to know what I missed", not "it died".

The dashboard also flags anything not checked in 14+ days as **Stale**.

## Dashboard

A local page over whatever the checks have recorded:

```bash
python app.py           # http://127.0.0.1:5000
```

The index lists every tracked restaurant -- closures and closing-soon
signals sorted to the top -- with its status, last-checked age, and the
summary behind any closing-soon flag. It's paged (100 a page) and has a filter
box matching name or address; the tiles at the top always count everything.
The "waiting to be verified" card spells out the first 50. Clicking one opens its per-month
check history (`db.check_history()`, which merges live `check_log` rows with
the rollups older checks are folded into, so the totals survive pruning).
Archived restaurants stay listed, greyed out, rather than disappearing.

Anything not checked in 14+ days is flagged **Stale** -- a scheduler that
has quietly died otherwise looks just like a week with no bad news.

### Checking a restaurant now

Next to Delete, every row a scheduled run would check carries a **Check now**
button: it re-polls Google for that one restaurant immediately, writes the
result, logs it, and sends the same alerts and archives on the same rule the
weekly run does.

It runs the *cheap* half of a check only. A full check also asks Claude
whether there is closure news about the place, and that call is allowed 120
seconds before it gives up -- too long to hold a page open, and it spends
real credits per press. So **Check now** never moves the closing-soon flag;
whatever the last news check concluded is carried forward untouched, and the
next news check happens on the weekly schedule as usual. Pressing the button
does not shift that schedule either: the run counter that decides when the
next news check falls due is left alone, so checking one restaurant cannot
delay the news check for the rest.

The button is only offered where a scheduled run would act: not on an
unverified restaurant (that gate exists so nothing is spent on a row nobody
has confirmed) and not on an archived one (re-activate it first).

### Removing a restaurant

Each row in the table ends with a **Delete** button: the restaurant stops
being checked, and its row and check history are removed. The browser asks
you to confirm by name first, because nothing here can put it back -- only
the log line records what went, place_id included.

Deleting is for somewhere you no longer care about. **Archiving** is the
softer option and the one a permanent closure triggers by itself: the row
stays in the table, greyed out, skipped by the weekly check, with its history
intact and a **Re-activate** button if the place turns out to have reopened.
Archived rows keep their Delete button too -- a closure you've read about is
often exactly the thing you want off the page.

### Verifying a restaurant

A name search returns exactly one guess with no confidence beside it, so the
wrong "Lilia" gets watched just as happily as the right one -- and looks
identical once it's in the table. A restaurant is therefore **not checked at
all** until you've confirmed it points at the place you meant.

Unverified restaurants collect at the top of the index with their name,
address and a Maps link, and two buttons:

- **Yes, that's the place** -- starts checking it from the next run onward.
  Nothing else about the row changes.
- **Wrong place -- remove it** -- deletes the row and its history and reopens
  the search with the name filled in (but not the address, which belonged to
  the wrong place). Deleting rather than archiving is deliberate: its check
  history describes a different restaurant. This only works on an unverified
  row; a restaurant you confirmed long ago can't be deleted from a stale tab.

Until then nothing is spent on them -- no Places call, no Claude call -- and
no alert can fire about them, since an alert about the wrong restaurant is
exactly what this is here to prevent. Each run sends **one** push saying how
many are waiting (not one per restaurant, so seeding a long file doesn't empty
your battery), linking to `DASHBOARD_URL`.

Restaurants added through the dashboard's search box skip all this: the
confirm page **is** the verification, so they're stored already verified.

Upgrading an existing database is handled automatically -- anything that had
already been checked is treated as verified, since you've been reading its
alerts for months. Only never-checked rows get asked about.

### Adding a restaurant

The search box at the top of the index takes a name and, optionally, a city
or street. It resolves the name through Google Places Text Search and shows
you the single match it found -- name, address and a Maps link -- and nothing
is tracked until you press **Track this restaurant** on that page.

The confirm step is not ceremony. Text Search always answers with its one
best guess and no confidence alongside it, so a mistyped or ambiguous name
comes back as a real restaurant somewhere else, and a wrong one being watched
looks exactly like a right one. The address is what tells them apart. It's the
same question [verification](#verifying-a-restaurant) asks of seeded rows --
asked before the row is written rather than after.

This is also the only button here that spends money -- one Text Search call
per search, the same call `seed.py` makes per line -- so the search is a POST
that a link or a prefetch can't trigger, and confirming (or reloading the
confirm page) costs nothing further. The Flask process needs
`GOOGLE_PLACES_API_KEY` in its environment for this; without it the page says
so instead of failing silently.

Searching for something already tracked takes you to its row rather than
adding a duplicate -- including when it's archived, which is how you'd
rediscover that a place reopened.

`seed.py` is still the way to add a list in bulk.

### Re-activating an archived restaurant

Permanent closures archive themselves, which is right when a place is gone
and wrong when Google was mistaken or the place came back. Archived rows stay
listed with a **Re-activate** button that puts them on the active list again.

Only the archived flag changes -- the stored status and closing-soon flag are
left as they were, so re-activating can't re-fire a closure alert that already
went out. That does mean a place Google still reports as permanently closed
gets archived again by the next check; the page says so when you click it.

Every POST is CSRF-checked by one hook (a route added later is covered by
default), against a token tied to the session cookie, so set
`DASHBOARD_SECRET_KEY` in `.env` to choose the key. Without it a key is
generated once and kept in `data/.dashboard_secret`, so an open page survives
a restart (delete the file to rotate it). Pages load no inline script or style, and
send a Content-Security-Policy saying so.

It binds to localhost and has no auth or login, so don't expose it to a
network -- more so now that buttons on it add rows and spend API calls.
Checks themselves still run from `main.py`.

## How checking works

1. **Business status** (every run, cheap): Places API `businessStatus`
   field -- flags `CLOSED_TEMPORARILY` / `CLOSED_PERMANENTLY` directly.
2. **Closing soon** (every Nth run, costs more): Claude API with web
   search looks for recent news of an announced closure. Runs less often
   (default: every 4th check) since it's a heavier, fuzzier signal.

Both steps are skipped entirely for a restaurant you haven't verified yet;
see [Verifying a restaurant](#verifying-a-restaurant).

The dashboard's **Check now** button runs step 1 on a single restaurant,
never step 2 -- see [Checking a restaurant now](#checking-a-restaurant-now).
Both paths go through the same `checker.check_one()`, so a manual check writes,
alerts and archives exactly as a scheduled one does.

Permanent closures auto-archive the restaurant after notifying; temporary
closures and closing-soon flags stay active so you keep getting the
context on future runs.

A check's result and the alert it owes are recorded together; the alert is
then sent and marked delivered. If ntfy and email are both down, the new status
is still saved and the alert goes out first thing on the next run (archived
rows included), once -- it is neither lost nor repeated.

A news-check reply that is cut off, not JSON, or otherwise unreadable counts as
a *failed* news check, not as "no closure news": the stored flag is kept and
the log says why.

## History retention

Every check appends a row to `check_log`, so after each run rows older than
`CHECK_LOG_RETAIN_DAYS` (default 90) are folded into per-month counts in
`check_log_monthly` and deleted. Checks that *changed* something -- a
`businessStatus` transition or the closing-soon flag flipping -- are kept as
detail rows regardless of age, so the interesting history never gets summarized
away. `db.check_history(restaurant_id)` reads the two tables back as one
per-month view. Set `CHECK_LOG_RETAIN_DAYS=0` to keep everything.

## Backups

The database holds your hand-verified list and `data/` is gitignored, so each
run ends by copying it to `data/backups/restaurants-<timestamp>.db` and keeping
the newest `BACKUPS_KEPT` (default 8). Set `BACKUPS_KEPT=0` to turn that off.
To restore, stop the scheduler and dashboard and copy a backup over
`data/restaurants.db`.

## When checks fail

A run that couldn't check some restaurants sends one push saying how many,
and names any that Google answers 404 for (a retired place id). Those keep
failing every run until you delete the row and add the place again from the
dashboard.

A run stops early, counting the rest as failed, when Google refuses the API key
(401/403) or 10 checks in a row fail for any other reason -- there's no point
grinding through the list one retry at a time when the key or the network is
down. A `businessStatus` value Google adds in future is stored as
`BUSINESS_STATUS_UNSPECIFIED` and logged, rather than failing that row forever.

## Cost notes

- Places `businessStatus` stays in the cheap Essentials/Pro tier (~$5/1K
  calls) as long as you don't add fields like `rating` or `currentOpeningHours`
  to the field mask -- those bump the whole call to the pricier Enterprise tier.
- The Claude + web search step is the main cost driver; the `CLOSING_SOON_CHECK_EVERY`
  setting in `.env` controls how often it runs. Nothing on the dashboard triggers
  it -- **Check now** is deliberately the Places half only, so no button on the
  page can run up a Claude bill.
- Two dashboard buttons do spend, both in the cheap tier and both once per
  press, with no rate limit in front of them: **Search** (one Text Search) and
  **Check now** (one `businessStatus` call). Fine for one person clicking on
  localhost; it's the first thing to revisit if the dashboard is ever exposed.
