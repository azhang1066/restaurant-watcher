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
pip install -r requirements.txt
cp .env.example .env   # fill in your keys
```

You'll need:
- **Google Places API key** (Places API "New" enabled in Google Cloud Console)
- **Anthropic API key** (for the closing-soon news check)
- **ntfy topic** -- pick any unique string, then subscribe to it in the
  [ntfy app](https://ntfy.sh/) on your phone

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

## Run

```bash
python main.py --once     # one check, right now
python main.py            # runs forever, checks weekly
```

For a real deployment, run `main.py` as a background service (systemd,
Replit scheduled deployment, or a cron entry calling `--once`) rather than
leaving a terminal open.

## How checking works

1. **Business status** (every run, cheap): Places API `businessStatus`
   field -- flags `CLOSED_TEMPORARILY` / `CLOSED_PERMANENTLY` directly.
2. **Closing soon** (every Nth run, costs more): Claude API with web
   search looks for recent news of an announced closure. Runs less often
   (default: every 4th check) since it's a heavier, fuzzier signal.

Permanent closures auto-archive the restaurant after notifying; temporary
closures and closing-soon flags stay active so you keep getting the
context on future runs.

## Cost notes

- Places `businessStatus` stays in the cheap Essentials/Pro tier (~$5/1K
  calls) as long as you don't add fields like `rating` or `currentOpeningHours`
  to the field mask -- those bump the whole call to the pricier Enterprise tier.
- The Claude + web search step is the main cost driver; the `CLOSING_SOON_CHECK_EVERY`
  setting in `.env` controls how often it runs.
