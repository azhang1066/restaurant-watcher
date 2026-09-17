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

CLOSING_SOON_CHECK_EVERY = int(os.environ.get("CLOSING_SOON_CHECK_EVERY", 4))  # every 4th run
CHECK_LOG_RETAIN_DAYS = int(os.environ.get("CHECK_LOG_RETAIN_DAYS", 90))  # 0 disables pruning
_run_count_file = os.path.join(os.path.dirname(__file__), "data", ".run_count")


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
        include_news_check = (run_count % CLOSING_SOON_CHECK_EVERY == 0)

    restaurants = list_restaurants(active_only=True)
    logger.info("Checking %d restaurants (news check: %s)",
                len(restaurants), "on" if include_news_check else "off")

    failures = 0
    for r in restaurants:
        try:
            status = get_business_status(r["place_id"])

            closing_soon, summary = False, r.get("closing_soon_summary") or ""
            if include_news_check and status == "OPERATIONAL":
                result = check_closing_soon(r["name"], r.get("address"))
                closing_soon = result["closing_soon"] and result["confidence"] in ("medium", "high")
                summary = result["summary"]

            update_check_result(r["id"], status, closing_soon, summary)

            was_operational = r["business_status"] == "OPERATIONAL"
            if status in ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY") and was_operational:
                notify_closed(r, status)
                if status == "CLOSED_PERMANENTLY":
                    archive_restaurant(r["id"])
            elif closing_soon and not r["closing_soon_flag"]:
                notify_closing_soon(r, summary)
        except Exception:
            failures += 1
            logger.exception("Check failed for %s (id=%s) -- skipping", r["name"], r["id"])

    if CHECK_LOG_RETAIN_DAYS > 0:
        try:
            pruned = prune_check_log(CHECK_LOG_RETAIN_DAYS)
            if pruned:
                logger.info("Folded %d check_log rows older than %d days into monthly rollups.",
                            pruned, CHECK_LOG_RETAIN_DAYS)
        except Exception:
            # Housekeeping only -- never fail a run whose checks already landed.
            logger.exception("check_log pruning failed -- continuing")

    logger.info("Done. %d/%d restaurants failed.", failures, len(restaurants))


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
