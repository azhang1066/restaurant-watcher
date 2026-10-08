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

Only one run happens at a time: `run_check` takes a lock file, so a manual
`--once` beside a running scheduler (or two schedulers) skips rather than
racing on the run counter and double-alerting.

Run once manually:  python main.py --once
Run on a schedule:   python main.py           (blocks, checks weekly)
"""
import argparse
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler

from checker import check_one, is_checkable
from config import STALE_AFTER_DAYS, configure_logging, env_int
from db import (DEFAULT_BACKUPS_KEPT, DEFAULT_RETAIN_DAYS, backup_db,
                get_restaurant, init_db, list_restaurants, prune_check_log)
from notifier import (notify_needs_verification, notify_run_problems,
                      notify_scheduler_gap, ping_healthcheck)
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


class RunInProgress(Exception):
    """Another process holds the run lock."""


@contextmanager
def _run_lock():
    """Exclusive, non-blocking OS lock on a file beside the run counter.

    An OS lock rather than a marker file, so a crashed run can't leave a stale
    lock behind: the lock dies with its process.
    """
    path = _run_count_file + ".lock"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, "a+b")
    try:
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise RunInProgress(path) from e
        yield
    finally:
        f.close()  # closing releases the lock


def _last_run_at():
    """When the last run finished, or None if none has. The run counter is
    only rewritten at the end of a completed run, so its mtime is that."""
    try:
        return datetime.fromtimestamp(os.path.getmtime(_run_count_file))
    except OSError:
        return None


def run_check(include_news_check=None):
    """One full pass, unless another is already running -- see `_run_lock`."""
    try:
        with _run_lock():
            _run_check(include_news_check)
    except RunInProgress:
        logger.warning("Another check run is already in progress -- skipping this one.")


def _run_check(include_news_check):
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
    for listed in restaurants:
        # The list was read before the loop started and a run can take a
        # while: re-read the row, so one deleted, archived or un-verified from
        # the dashboard meanwhile isn't checked (or alerted on) from a stale
        # copy, and the flags carried forward are current.
        r = get_restaurant(listed["id"])
        if r is None or not is_checkable(r):
            logger.info("Skipping %s (id=%s): changed during the run",
                        listed["name"], listed["id"])
            continue
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

    # Tell the external monitor this run finished. Every check failing points
    # at the key or the network, so that run reports as a failure.
    ping_healthcheck(ok=not (failures and failures == len(restaurants)))

    logger.info("Done. %d/%d restaurants failed.", failures, len(restaurants))
    if unverified:
        logger.warning("%d restaurant(s) skipped pending verification: %s. Confirm them "
                       "on the dashboard to start checking them.",
                       len(unverified), ", ".join(r["name"] for r in unverified))
    if news_failures:
        logger.warning("%d news check(s) failed; those restaurants kept their stored "
                       "closing-soon flag.", news_failures)


def _next_run_time(now):
    """When the scheduler should first fire: now if a week has passed since the
    last completed run (or there never was one), otherwise when that week is
    up. Firing on every start would run a check -- and advance the news-check
    cadence -- each time the process is restarted."""
    last = _last_run_at()
    if last is None or last + timedelta(weeks=1) <= now:
        return now
    return last + timedelta(weeks=1)


def _warn_if_scheduler_was_down(now):
    last = _last_run_at()
    if last is None or (now - last).days < STALE_AFTER_DAYS:
        return
    days = (now - last).days
    logger.warning("No check completed for %d days before this start.", days)
    try:
        notify_scheduler_gap(days)
    except Exception:
        logger.exception("Couldn't send the scheduler-gap notification -- continuing")


def start_scheduler():
    now = datetime.now()
    _warn_if_scheduler_was_down(now)
    scheduler = BlockingScheduler()
    # max_instances/coalesce: never overlap runs, and collapse missed ones into
    # one. misfire_grace_time=None: a laptop asleep at the due time still runs
    # the check on waking, instead of APScheduler's default of skipping it
    # after one second.
    scheduler.add_job(run_check, "interval", weeks=1, next_run_time=_next_run_time(now),
                      max_instances=1, coalesce=True, misfire_grace_time=None)
    logger.info("Scheduler started -- checking weekly. Ctrl+C to stop.")
    scheduler.start()


if __name__ == "__main__":
    configure_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run a single check and exit")
    args = parser.parse_args()

    if args.once:
        run_check()
    else:
        start_scheduler()
