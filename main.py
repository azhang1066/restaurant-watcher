"""Orchestrates the periodic checks.

- Business-status check (cheap, Places API): every run
- Closing-soon news check (Claude + web search, costs more per call): only
  every Nth run, controlled by CLOSING_SOON_CHECK_EVERY, to keep this from
  burning tokens checking the same stable restaurants every day.
- Housekeeping: after each run, aged-out check_log detail rows are folded
  into monthly rollups (CHECK_LOG_RETAIN_DAYS, 0 to disable).

A restaurant is not checked at all until the user has confirmed it points at
the right place. Text Search resolves a name to exactly one place_id with no
confidence beside it, and seed.py resolves a whole file of them with nobody
watching -- so the wrong "Lilia" is indistinguishable from the right one
until somebody looks. Unverified rows are skipped here (no Places call, no
Claude call, no alerts) and a single push per run says how many are waiting.
The dashboard is where they get confirmed or thrown out.

Run once manually:  python main.py --once
Run on a schedule:   python main.py           (blocks, checks weekly)
"""
import argparse
import logging
import os
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler

from checker import check_one, is_checkable
from config import configure_logging, env_int
from db import (DEFAULT_BACKUPS_KEPT, DEFAULT_RETAIN_DAYS, backup_db, init_db,
                list_restaurants, prune_check_log)
from notifier import notify_needs_verification, notify_run_problems
from places_client import PlaceNotFound

logger = logging.getLogger(__name__)

DEFAULT_CLOSING_SOON_CHECK_EVERY = 4

_run_count_file = os.path.join(os.path.dirname(__file__), "data", ".run_count")


def _closing_soon_check_every():
    """Read at call time, not import time, so `.env` lands however this module
    was imported -- see `places_client._api_key()` for the same pattern."""
    return env_int("CLOSING_SOON_CHECK_EVERY", DEFAULT_CLOSING_SOON_CHECK_EVERY)


def _check_log_retain_days():
    """Days of per-check detail to keep before folding into monthly rollups;
    0 disables pruning. Read at call time for the same reason as above."""
    return env_int("CHECK_LOG_RETAIN_DAYS", DEFAULT_RETAIN_DAYS)


def _backups_kept():
    """How many database backups to keep; 0 disables them."""
    return env_int("BACKUPS_KEPT", DEFAULT_BACKUPS_KEPT)


def _read_run_count():
    """Completed runs so far. A missing, empty or corrupt file reads as 0:
    losing the count only shifts when the next news check falls due."""
    try:
        with open(_run_count_file) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _write_run_count(count):
    """Atomic: write a sibling file, then replace, so a crash mid-write can't
    leave a truncated counter behind."""
    os.makedirs(os.path.dirname(_run_count_file), exist_ok=True)
    tmp = _run_count_file + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(count))
    os.replace(tmp, _run_count_file)


def run_check(include_news_check=None):
    init_db()
    # Only recorded once the run has finished (see the end of this function),
    # so a run that dies partway doesn't advance the news-check cadence.
    run_count = _read_run_count() + 1
    if include_news_check is None:
        include_news_check = (run_count % _closing_soon_check_every() == 0)

    # Split rather than filtering in SQL: both halves are wanted, and one
    # read keeps the count that gets notified about consistent with the list
    # that got skipped.
    active = list_restaurants(active_only=True)
    unverified = [r for r in active if not r["verified_at"]]
    restaurants = [r for r in active if is_checkable(r)]
    logger.info("Checking %d restaurants (news check: %s%s)",
                len(restaurants), "on" if include_news_check else "off",
                f", {len(unverified)} unverified and skipped" if unverified else "")

    failures = news_failures = 0
    gone = []
    for r in restaurants:
        try:
            news_failures += check_one(r, include_news_check=include_news_check)["news_failed"]
        except PlaceNotFound:
            failures += 1
            gone.append(r)
            logger.error("Google no longer recognises %s (id=%s, place_id=%s) -- "
                         "skipping", r["name"], r["id"], r["place_id"])
        except Exception:
            failures += 1
            logger.exception("Check failed for %s (id=%s) -- skipping", r["name"], r["id"])

    if unverified:
        # One push for the whole batch, after the checks so a run with real
        # news leads with it. Isolated like the housekeeping below: a dead
        # ntfy must not lose the check results already written.
        try:
            notify_needs_verification(unverified)
        except Exception:
            logger.exception("Couldn't send the needs-verifying notification -- continuing")

    if failures:
        try:
            notify_run_problems(failures, len(restaurants), gone)
        except Exception:
            logger.exception("Couldn't send the check-failures notification -- continuing")

    retain_days = _check_log_retain_days()
    if retain_days > 0:
        try:
            pruned = prune_check_log(retain_days)
            if pruned:
                logger.info("Folded %d check_log rows older than %d days into monthly rollups.",
                            pruned, retain_days)
        except Exception:
            # Housekeeping only -- never fail a run whose checks already landed.
            logger.exception("check_log pruning failed -- continuing")

    keep = _backups_kept()
    if keep > 0:
        try:
            backup_db(keep)
        except Exception:
            logger.exception("Database backup failed -- continuing")

    try:
        _write_run_count(run_count)
    except OSError:
        logger.exception("Couldn't record the run count -- the news-check "
                         "cadence won't advance this run")

    logger.info("Done. %d/%d restaurants failed.", failures, len(restaurants))
    if unverified:
        logger.warning("%d restaurant(s) skipped pending verification: %s. Confirm them "
                       "on the dashboard to start checking them.",
                       len(unverified), ", ".join(r["name"] for r in unverified))
    if news_failures:
        logger.warning("%d news check(s) failed; those restaurants kept their stored "
                       "closing-soon flag.", news_failures)


if __name__ == "__main__":
    configure_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run a single check and exit")
    args = parser.parse_args()

    if args.once:
        run_check()
    else:
        scheduler = BlockingScheduler()
        scheduler.add_job(run_check, "interval", weeks=1, next_run_time=datetime.now())
        logger.info("Scheduler started -- checking weekly. Ctrl+C to stop.")
        scheduler.start()
