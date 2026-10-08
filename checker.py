"""Checking one restaurant: the unit both the weekly run and the dashboard use.

Lives apart from main.py so the web app can import it without inheriting the
scheduler's import-time side effects (load_dotenv, logging.basicConfig).
"""
import logging

from closure_checker import check_closing_soon
from db import archive_restaurant, update_check_result
from notifier import notify_closed, notify_closing_soon
from places_client import get_business_status
from statuses import CLOSED_PERMANENTLY, CLOSED_STATUSES, OPERATIONAL

logger = logging.getLogger(__name__)


def check_one(r, include_news_check=False):
    """Check one restaurant, write the result, and alert or archive on it.

    Shared by run_check's loop and the dashboard's "Check now" button, so
    there is one implementation of both. The parts worth not
    duplicating are the subtle ones: when an alert fires (a move *into* a
    closed status, not merely a closed status), when the closing-soon flag is
    carried forward rather than cleared, and when a row gets archived. A
    second copy would drift on exactly those, and the drift would show up as
    a missed alert rather than as an error.

    Deliberately does not touch the run counter. That counter decides when the
    next news check falls due for the whole list, so pressing a button on one
    restaurant must not move the schedule for the others.

    `include_news_check` is off by default because the expensive half is
    opt-in: run_check turns it on every Nth run, and the dashboard never does
    -- a request thread can't sit on a 120-second Anthropic timeout. With it
    off the stored flag is carried forward untouched, exactly as on a plain
    run.

    Returns the new status, the closing-soon flag as written, and whether a
    news check was attempted and failed. Whatever the Places call raises
    propagates: the caller knows whether that's one row of many or the whole
    of what it was asked to do.
    """
    status = get_business_status(r["place_id"])

    # Carry the stored flag forward: only a news check can change it,
    # so defaulting to False here would clear it on every plain run and
    # let the next news check re-alert about a closure already sent.
    closing_soon = bool(r["closing_soon_flag"])
    summary = r.get("closing_soon_summary") or ""
    news_failed = False
    if include_news_check and status == OPERATIONAL:
        try:
            result = check_closing_soon(r["name"], r.get("address"))
        except Exception:
            # The news check is the optional half of a check and the
            # SDK has already retried by the time we get here. Don't
            # throw away the Places status we just paid for: carry the
            # stored flag forward exactly as a plain run does (clearing
            # it would re-arm an alert already sent) and try again on
            # the next news cycle.
            news_failed = True
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
    if status == CLOSED_PERMANENTLY:
        closing_soon = False

    update_check_result(r["id"], status, closing_soon, summary)

    # Notify on any move *into* a closed status, not just from
    # OPERATIONAL -- temporarily-closed places close for good too.
    status_changed = status != r["business_status"]
    if status in CLOSED_STATUSES and status_changed:
        notify_closed(r, status)
    elif closing_soon and not r["closing_soon_flag"]:
        notify_closing_soon(r, summary)

    # Archive on the status itself rather than on the transition, so a
    # place that reached CLOSED_PERMANENTLY by some path that skipped
    # the notify above still leaves the active list. Idempotent.
    if status == CLOSED_PERMANENTLY:
        archive_restaurant(r["id"])

    return {"status": status, "closing_soon": closing_soon, "news_failed": news_failed}
