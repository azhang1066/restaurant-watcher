"""Checking one restaurant: the unit both the weekly run and the dashboard use.

Lives apart from main.py so the web app can import it without pulling in the
scheduler.
"""
import logging

from closure_checker import TRUSTED_CONFIDENCES, check_closing_soon
from db import clear_pending_alert, update_check_result
from notifier import notify_closed, notify_closing_soon
from places_client import get_business_status
from statuses import CLOSED_PERMANENTLY, CLOSED_STATUSES, OPERATIONAL

logger = logging.getLogger(__name__)


class AlertNotSent(Exception):
    """The check's result was recorded, but the alert about it couldn't be
    delivered. It stays pending and goes out on the next run."""


def is_checkable(restaurant: dict) -> bool:
    """Whether a scheduled run would check this row: verified and not
    archived. The one definition -- run_check, check_one and the dashboard's
    Check-now button all read it, so they can't disagree."""
    return bool(restaurant.get("verified_at")) and not restaurant["archived"]


def deliver_pending_alert(r: dict) -> None:
    """Send the alert `update_check_result` recorded for this row, then clear
    it. Raises AlertNotSent, leaving it recorded, if the send fails."""
    kind = r.get("pending_alert")
    if not kind:
        return
    try:
        if kind == "closed":
            notify_closed(r, r["business_status"])
        else:
            notify_closing_soon(r, r.get("closing_soon_summary") or "")
    except Exception as e:
        raise AlertNotSent(f"{r['name']} (id={r['id']}): {kind} alert") from e
    clear_pending_alert(r["id"])


def check_one(r: dict, include_news_check: bool = False) -> dict:
    """Check one restaurant, write the result, and alert or archive on it.

    Shared by run_check's loop and the dashboard's "Check now" button, so
    there is one implementation of both. The parts worth not duplicating are
    the subtle ones: when an alert fires (a move *into* a closed status, not
    merely a closed status), when the closing-soon flag is carried forward
    rather than cleared, and when a row gets archived. A second copy would
    drift on exactly those, and the drift would show up as a missed alert
    rather than as an error.

    Deliberately does not touch the run counter: that decides when the next
    news check falls due for the whole list, so pressing a button on one
    restaurant must not move the schedule for the others.

    `include_news_check` is off by default because the expensive half is
    opt-in: run_check turns it on every Nth run, and the dashboard never does
    -- a request thread can't sit on a 120-second Anthropic timeout. With it
    off the stored flag is carried forward untouched.

    Refuses a row a scheduled run would skip (unverified or archived) with a
    ValueError -- see `is_checkable`.

    The result and the alert it owes are written in one transaction
    (`pending_alert`), and only then is the alert sent and the marker cleared.
    So a dead ntfy can neither lose the alert (the next run delivers it
    before anything else) nor repeat it (the transition is already recorded,
    so it isn't detected again). A failed send raises AlertNotSent.

    Returns the new status, the closing-soon flag as written, and whether a
    news check was attempted and failed. Whatever the Places call raises
    propagates: the caller knows whether that's one row of many or the whole
    of what it was asked to do.
    """
    if not is_checkable(r):
        raise ValueError(f"{r['name']} (id={r['id']}) is unverified or archived "
                         "and isn't checked")
    # An earlier check's alert that never went out comes first.
    deliver_pending_alert(r)

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
            # The optional half of a check. Don't throw away the Places status
            # we just paid for: keep the stored flag (clearing it would re-arm
            # an alert already sent) and try again on the next news cycle.
            news_failed = True
            logger.exception("News check failed for %s (id=%s) -- keeping the stored "
                             "closing-soon flag", r["name"], r["id"])
        else:
            closing_soon = (result["closing_soon"]
                            and result["confidence"] in TRUSTED_CONFIDENCES)
            summary = result["summary"]

    # A closing-soon flag predicts a *future* closure, so the closure landing
    # settles it. Clear it here or nothing ever will: permanent closures
    # archive on this same run, and the news check only runs on OPERATIONAL
    # places. The summary stays as the record of what was predicted.
    # Temporary closures keep their flag -- "and it's not coming back" is
    # still an open question there.
    if status == CLOSED_PERMANENTLY:
        closing_soon = False

    # Alert on any move *into* a closed status, not just from OPERATIONAL --
    # temporarily-closed places close for good too.
    if status in CLOSED_STATUSES and status != r["business_status"]:
        alert = "closed"
    elif closing_soon and not r["closing_soon_flag"]:
        alert = "closing_soon"
    else:
        alert = None

    # Archive on the status itself rather than on the transition, so a place
    # that reached CLOSED_PERMANENTLY by some path that skipped the alert
    # above still leaves the active list.
    written = update_check_result(r["id"], status, closing_soon, summary,
                                  archive=status == CLOSED_PERMANENTLY,
                                  pending_alert=alert)
    if not written:
        logger.warning("%s (id=%s) was removed during its check -- nothing recorded",
                       r["name"], r["id"])
    elif alert:
        deliver_pending_alert(dict(r, business_status=status, closing_soon_summary=summary,
                                   pending_alert=alert))

    return {"status": status, "closing_soon": closing_soon, "news_failed": news_failed}
