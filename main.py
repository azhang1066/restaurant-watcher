"""Orchestrates the periodic checks.

- Business-status check (cheap, Places API): every run
- Closing-soon news check (Claude + web search, costs more per call): only
  every Nth run, controlled by CLOSING_SOON_CHECK_EVERY, to keep this from
  burning tokens checking the same stable restaurants every day.
- Housekeeping: after each run, aged-out check_log detail rows are folded
  into monthly rollups (CHECK_LOG_RETAIN_DAYS, 0 to disable).

Run once manually:  python main.py --once
Run on a schedule:   python main.py           (blocks, checks weekly)
"""
import argparse
import logging
import os
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

from db import (init_db, list_restaurants, update_check_result, archive_restaurant,
                prune_check_log)
from places_client import get_business_status
from closure_checker import check_closing_soon
from notifier import notify_closed, notify_closing_soon

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_CLOSING_SOON_CHECK_EVERY = 4
DEFAULT_CHECK_LOG_RETAIN_DAYS = 90

_run_count_file = os.path.join(os.path.dirname(__file__), "data", ".run_count")


def _closing_soon_check_every():
    """Read at call time, not import time, so `.env` lands however this module
    was imported -- see `places_client._api_key()` for the same pattern."""
    return int(os.environ.get("CLOSING_SOON_CHECK_EVERY") or DEFAULT_CLOSING_SOON_CHECK_EVERY)


def _check_log_retain_days():
    """Days of per-check detail to keep before folding into monthly rollups;
    0 disables pruning. Read at call time for the same reason as above."""
    return int(os.environ.get("CHECK_LOG_RETAIN_DAYS") or DEFAULT_CHECK_LOG_RETAIN_DAYS)


def _next_run_count():
    count = 0
    if os.path.exists(_run_count_file):
        count = int(open(_run_count_file).read().strip() or 0)
    count += 1
    os.makedirs(os.path.dirname(_run_count_file), exist_ok=True)
    with open(_run_count_file, "w") as f:
        f.write(str(count))
    return count


def run_check(include_news_check=None):
    init_db()
    run_count = _next_run_count()
    if include_news_check is None:
        include_news_check = (run_count % _closing_soon_check_every() == 0)

    restaurants = list_restaurants(active_only=True)
    logger.info("Checking %d restaurants (news check: %s)",
                len(restaurants), "on" if include_news_check else "off")

    failures = news_failures = 0
    for r in restaurants:
        try:
            status = get_business_status(r["place_id"])

            # Carry the stored flag forward: only a news check can change it,
            # so defaulting to False here would clear it on every plain run and
            # let the next news check re-alert about a closure already sent.
            closing_soon = bool(r["closing_soon_flag"])
            summary = r.get("closing_soon_summary") or ""
            if include_news_check and status == "OPERATIONAL":
                try:
                    result = check_closing_soon(r["name"], r.get("address"))
                except Exception:
                    # The news check is the optional half of a check and the
                    # SDK has already retried by the time we get here. Don't
                    # throw away the Places status we just paid for: carry the
                    # stored flag forward exactly as a plain run does (clearing
                    # it would re-arm an alert already sent) and try again on
                    # the next news cycle.
                    news_failures += 1
                    logger.exception("News check failed for %s (id=%s) -- keeping the stored "
                                     "closing-soon flag", r["name"], r["id"])
                else:
                    closing_soon = result["closing_soon"] and result["confidence"] in ("medium", "high")
                    summary = result["summary"]

            # A closing-soon flag is a prediction about a *future* closure,
            # so the closure landing settles it rather than confirming it.
            # Clear it here or nothing ever will: permanent closures archive
            # on this same run, and the news check that owns the flag only
            # runs on OPERATIONAL places. The summary is kept as the record
            # of what was predicted. Temporary closures keep their flag --
            # there, "and it's not coming back" is still an open question.
            if status == "CLOSED_PERMANENTLY":
                closing_soon = False

            update_check_result(r["id"], status, closing_soon, summary)

            # Notify on any move *into* a closed status, not just from
            # OPERATIONAL -- temporarily-closed places close for good too.
            status_changed = status != r["business_status"]
            if status in ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY") and status_changed:
                notify_closed(r, status)
            elif closing_soon and not r["closing_soon_flag"]:
                notify_closing_soon(r, summary)

            # Archive on the status itself rather than on the transition, so a
            # place that reached CLOSED_PERMANENTLY by some path that skipped
            # the notify above still leaves the active list. Idempotent.
            if status == "CLOSED_PERMANENTLY":
                archive_restaurant(r["id"])
        except Exception:
            failures += 1
            logger.exception("Check failed for %s (id=%s) -- skipping", r["name"], r["id"])

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

    logger.info("Done. %d/%d restaurants failed.", failures, len(restaurants))
    if news_failures:
        logger.warning("%d news check(s) failed; those restaurants kept their stored "
                       "closing-soon flag.", news_failures)


if __name__ == "__main__":
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
