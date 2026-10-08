"""Dashboard over the watcher's state.

    /                            every restaurant (paged, filterable), current
                                 status, last checked
    /restaurant/<id>             one restaurant, plus its per-month history
    /restaurant/<id>/unarchive   POST: put an archived restaurant back (see
                                 db.unarchive_restaurant for the caveat)
    /restaurant/<id>/verify      POST: confirm this is the right place
    /restaurant/<id>/reject      POST: it wasn't -- delete it and search again
    /verify-selected             POST: verify every ticked row on the index
    /restaurant/<id>/check       POST: re-poll Places for this one, now
    /restaurant/<id>/delete      POST: stop watching it -- delete row + history
    /add                         POST: search Google Places for a name
    /add/confirm                 GET: show the match, POST: track it

Every read goes through `db.py` rather than issuing its own SQL, so the
numbers here can't drift from the values the scheduler decides notifications on.

Adding spends money -- a Places Text Search per search -- and can be silently
*wrong*: Text Search always answers with its single best guess, so tracking the
wrong "Lilia" looks exactly like tracking the right one. Hence two steps: the
search stashes its candidate (in the database, keyed by a token in the session)
and redirects, and a separate POST writes the row. The redirect also means
reloading the confirm page re-reads the stash rather than re-running (and
re-paying for) the search.

Verifying is the same question asked late: seed.py resolves a whole file of
names with nobody looking, and the scheduler refuses to check a restaurant
until someone has said yes. Rows added through the confirm page are stored
already verified, so this only asks about places nobody has looked at. Saying
no deletes the row rather than archiving it, because its history describes a
different restaurant, and re-opens the search with the name filled in.

Checking now is scoped to the cheap half: it re-polls Google Places for one
restaurant and leaves the Claude news check on the weekly schedule, where its
120-second timeout is nobody's problem. See check_now().

Deleting is the only destructive button that touches a restaurant whose history
is real, so it's the only one that asks the browser to confirm first. Archiving
is the softer option, and what a closure triggers on its own: the row and its
history stay on the page, greyed out.

Every POST is CSRF-checked in one `before_request` hook -- see `_require_csrf`
-- against a token tied to the session. That needs a signing key, so set
`DASHBOARD_SECRET_KEY` in `.env` to keep sessions across restarts; without one
a per-process key is generated and an open tab's token stops matching when the
server restarts. Pages load no inline script or style, and a Content-Security-
Policy header says so.

There's deliberately no module-level `app`: Flask's loader finds the
`create_app()` factory by name, and building the app at import time would run
`init_db()` against the real database just for importing this module.

Run it:
    flask --app app run     # http://127.0.0.1:5000
    python app.py           # same, explicit host/port
"""
import hmac
import logging
import math
import secrets
from collections.abc import Callable
from datetime import UTC, datetime

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.wrappers import Response

from checker import AlertNotSent, check_one, is_checkable
from config import DASHBOARD_HOST, DASHBOARD_PORT, STALE_AFTER_DAYS, configure_logging, env_str
from db import (
    add_restaurant,
    check_history,
    delete_pending_add,
    delete_restaurant,
    get_pending_add,
    get_restaurant,
    get_restaurant_by_place_id,
    init_db,
    list_restaurants,
    stash_pending_add,
    unarchive_restaurant,
    verify_restaurant,
    verify_restaurants,
)
from places_client import PlaceNotFound, find_place_id, place_summary
from statuses import CLOSED_PERMANENTLY, CLOSED_STATUSES, CLOSED_TEMPORARILY, LABELS, OPERATIONAL

logger = logging.getLogger(__name__)

# STALE_AFTER_DAYS (config.py): a watcher that has quietly stopped watching
# looks exactly like one with no bad news, so the page surfaces it.

_ATTENTION_STATUSES = CLOSED_STATUSES

# Cap what gets sent to Text Search. Long queries don't match better.
MAX_QUERY_CHARS = 120

# Rows per page of the main table, and how many unverified rows the "waiting to
# be verified" card spells out. Each row carries its own forms, so an
# unbounded page with a few hundred restaurants is a few hundred forms.
PAGE_SIZE = 100
PENDING_SHOWN = 50

# Ids beyond SQLite's INTEGER range can't match a row and raise in the driver.
_MAX_ID = 2**63 - 1

# Badge colour per status; anything else (e.g. an unspecified status) is "bad".
_STATUS_BADGES = {OPERATIONAL: "ok", CLOSED_TEMPORARILY: "warn"}

# Sort the worst news to the top of the list.
_STATUS_RANK = {CLOSED_PERMANENTLY: 0, CLOSED_TEMPORARILY: 1, OPERATIONAL: 2}

_CSP = ("default-src 'none'; style-src 'self'; script-src 'self'; "
        "img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")


def _secret_key() -> str:
    """Signing key for the session cookie the CSRF token lives in.

    A generated key is fine for a personal localhost tool -- it only means
    tokens don't outlive a restart -- but it's logged, because silently
    invalidating sessions on every reload is confusing if you didn't expect it.
    """
    key = env_str("DASHBOARD_SECRET_KEY")
    if key:
        return key
    logger.info("No DASHBOARD_SECRET_KEY set -- generating a per-process key. "
                "Open pages will need a reload after a restart.")
    return secrets.token_hex(32)


def _csrf_token() -> str:
    """The session's CSRF token, minted on first use. Exposed to templates."""
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _require_csrf() -> None:
    """Reject a POST whose token doesn't match the session's.

    Registered as a `before_request` hook, so every POST route is covered
    without remembering to ask: a route added later is protected by default.
    Flask's SameSite=Lax cookie already blocks the cross-site form post, but
    this is the check that doesn't depend on the browser getting that right.
    """
    if request.method != "POST":
        return
    expected = session.get("csrf_token")
    provided = request.form.get("csrf_token", "")
    if not expected or not hmac.compare_digest(str(expected), provided):
        abort(400, "CSRF token missing or stale -- reload the page and retry.")


def _security_headers(response: Response) -> Response:
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


def _parse_ts(value) -> datetime | None:
    """Parse a stored timestamp into an aware UTC datetime, or None.

    SQLite's CURRENT_TIMESTAMP writes naive 'YYYY-MM-DD HH:MM:SS' in **UTC**,
    so the tzinfo has to be attached here -- treating it as local time would
    shift every age on the page by the viewer's offset.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _age(then: datetime | None, now: datetime) -> str | None:
    """Coarse '3 days ago' for a timestamp, or None if it wouldn't parse."""
    if then is None:
        return None
    seconds = (now - then).total_seconds()
    if seconds < 0:
        return "just now"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        count = int(seconds // size)
        if count:
            return f"{count} {unit}{'s' if count != 1 else ''} ago"
    return "just now"


def _query_label(name: str, hint: str) -> str:
    """What the user typed, for echoing back on the confirm page and in
    'no match' messages -- the same string that was sent to Text Search."""
    return f"{name} {hint}".strip() if hint else name


def _view(restaurant: dict, now: datetime) -> dict:
    """Presentation-ready copy of a restaurant row.

    Built as a separate dict so the templates never reach into raw column
    values, and so the age/staleness arithmetic stays testable on its own.
    """
    status = restaurant["business_status"] or OPERATIONAL
    checked_at = _parse_ts(restaurant.get("last_checked_at"))
    closing_soon = bool(restaurant["closing_soon_flag"])
    archived = bool(restaurant["archived"])
    # Exactly the rows a scheduled run skips, so what the page calls "needs
    # verifying" and what actually goes unchecked can't drift apart: that
    # loop only ever looks at unarchived rows.
    needs_verification = not restaurant.get("verified_at") and not archived
    return {
        "id": restaurant["id"],
        "name": restaurant["name"],
        "address": restaurant.get("address") or "",
        "maps_url": restaurant.get("maps_url") or "",
        "status": status,
        "status_label": LABELS.get(status, status),
        "status_badge": _STATUS_BADGES.get(status, "bad"),
        "closing_soon": closing_soon,
        # A closing-soon flag is news about a *future* closure, so it stops
        # being news once the place is shut for good. Decided here rather than
        # in the template so the badge and the summary count can't disagree
        # about what the page is showing. checker.check_one() clears the
        # stored flag on a permanent closure too; this keeps the page honest
        # about a row written by anything else.
        "closing_soon_current": closing_soon and status != CLOSED_PERMANENTLY,
        "closing_soon_summary": restaurant.get("closing_soon_summary") or "",
        "archived": archived,
        "verified": bool(restaurant.get("verified_at")),
        "needs_verification": needs_verification,
        # The schema defaults business_status to OPERATIONAL, so an unchecked
        # row would otherwise render as a confident "Open" for a place nobody
        # has ever looked up -- indefinitely, since an unverified one is
        # never checked at all.
        "status_known": checked_at is not None,
        # Whether to offer the Check-now button. Decided here, not in the
        # template, so it and the route's guard read the same rule.
        "checkable": is_checkable(restaurant),
        "checked_at": checked_at,
        "checked_age": _age(checked_at, now),
        # Never checked is its own state, not a stale one -- a freshly seeded
        # restaurant hasn't missed anything yet.
        "stale": (checked_at is not None
                  and (now - checked_at).days >= STALE_AFTER_DAYS),
        "needs_attention": (closing_soon or status in _ATTENTION_STATUSES
                            or needs_verification),
    }


def _sort_key(view: dict) -> tuple:
    return (not view["needs_attention"],
            _STATUS_RANK.get(view["status"], 3),
            view["name"].lower())


def _int_arg(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default))
    except ValueError:
        return default


# --- routes ---------------------------------------------------------------
#
# Registered by create_app() from _URLS at the bottom of this module.


def index():
    now = datetime.now(UTC)
    views = sorted((_view(r, now) for r in list_restaurants(active_only=False)),
                   key=_sort_key)
    active = [v for v in views if not v["archived"]]
    pending = [v for v in views if v["needs_verification"]]
    summary = {
        "active": len(active),
        "closed": sum(1 for v in active if v["status"] in _ATTENTION_STATUSES),
        "closing_soon": sum(1 for v in active if v["closing_soon_current"]),
        "archived": sum(1 for v in views if v["archived"]),
        "stale": sum(1 for v in active if v["stale"]),
        "unverified": len(pending),
    }

    # The table is filtered, then paged; the summary above is of everything.
    find = (request.args.get("find") or "").strip()[:MAX_QUERY_CHARS]
    matching = views
    if find:
        needle = find.casefold()
        matching = [v for v in views if needle in v["name"].casefold()
                    or needle in v["address"].casefold()]
    pages = max(1, math.ceil(len(matching) / PAGE_SIZE))
    page = min(max(_int_arg("page", 1), 1), pages)
    first = (page - 1) * PAGE_SIZE

    return render_template("index.html", restaurants=matching[first:first + PAGE_SIZE],
                           summary=summary, pending=pending[:PENDING_SHOWN],
                           find=find, page=page, pages=pages,
                           matching=len(matching),
                           stale_after_days=STALE_AFTER_DAYS,
                           max_query_chars=MAX_QUERY_CHARS,
                           # Set when a rejected match bounced back here,
                           # so the box is ready for a narrower query.
                           prefill=(request.args.get("q") or "")[:MAX_QUERY_CHARS])


def _back_to(restaurant_id: int) -> Response:
    """Send a POST back to the page it came from.

    `return_to` selects an endpoint *name*, never a URL or path, so a
    crafted value can only ever land on one of these two pages -- there's
    no open redirect to get wrong. Anything unrecognised falls through to
    the detail page.
    """
    if request.form.get("return_to") == "index":
        return redirect(url_for("index"))
    return redirect(url_for("detail", restaurant_id=restaurant_id))


def detail(restaurant_id: int):
    restaurant = get_restaurant(restaurant_id)
    if restaurant is None:
        abort(404)
    return render_template(
        "detail.html",
        restaurant=_view(restaurant, datetime.now(UTC)),
        history=list(reversed(check_history(restaurant_id))),
    )


def unarchive(restaurant_id: int):
    restaurant = get_restaurant(restaurant_id)
    if restaurant is None:
        abort(404)

    if not restaurant["archived"]:
        # Already active: a double-submitted form or a stale page. Nothing
        # to undo, so say so rather than reporting a change that didn't
        # happen.
        flash(f"{restaurant['name']} was already active.", "info")
        return _back_to(restaurant_id)

    unarchive_restaurant(restaurant_id)
    logger.info("Un-archived %s (id=%s)", restaurant["name"], restaurant_id)

    message = f"{restaurant['name']} is back on the active list."
    if restaurant["business_status"] == CLOSED_PERMANENTLY:
        # Worth saying out loud: run_check archives on the *status*, so
        # this row goes straight back to archived on the next run unless
        # Places has changed its mind about the place.
        message += (" Heads up: Google still reports it permanently closed,"
                    " so the next check will archive it again unless that"
                    " has changed.")
        flash(message, "warn")
    else:
        flash(message, "info")
    return _back_to(restaurant_id)

# --- checking a restaurant now ---------------------------------------


def check_now(restaurant_id: int):
    """Re-poll Google Places for this one restaurant, right now.

    The cheap half of a check only. A full check would also ask Claude
    whether there's closure news about the place, and that call is allowed
    120 seconds before it gives up -- a page cannot sit on that, and it
    spends real credits per press. So this fetches businessStatus, writes
    the result, fires the same alerts and archives on the same rule as a
    scheduled run, and leaves the closing-soon flag exactly where it was.
    Only a news check moves that flag, and news checks stay on the weekly
    schedule; the button's title says so on the page.

    Scoped to the rows a scheduled run would check (`is_checkable`), so
    an unverified or archived restaurant is a 400 rather than a check the
    weekly run would never have made. Neither is reachable from the page
    -- this is for a stale tab or a crafted post.
    """
    restaurant = get_restaurant(restaurant_id)
    if restaurant is None:
        abort(404)
    if not is_checkable(restaurant):
        abort(400, "That restaurant isn't being checked -- verify it, or "
                   "re-activate it, first.")

    previous = restaurant["business_status"]
    try:
        result = check_one(restaurant)
    except PlaceNotFound:
        logger.error("Manual check: Google no longer recognises %s (id=%s, "
                     "place_id=%s)", restaurant["name"], restaurant_id,
                     restaurant["place_id"])
        flash(f"Google no longer recognises {restaurant['name']}'s place id. "
              "Delete it and add it again with the search box.", "warn")
        return _back_to(restaurant_id)
    except AlertNotSent:
        # The new status is recorded; only the notification failed, and the
        # next run (scheduled or manual) delivers it.
        logger.exception("Manual check of %s (id=%s): alert not sent",
                         restaurant["name"], restaurant_id)
        flash(f"Checked {restaurant['name']}, but the alert couldn't be sent. "
              "It will go out on the next check.", "warn")
        return _back_to(restaurant_id)
    except Exception:
        # Places was unreachable (or the key is wrong). check_one writes
        # nothing until it has the status, so there's no half-applied check
        # to explain -- say so and leave the row as it was.
        logger.exception("Manual check failed for %s (id=%s)",
                         restaurant["name"], restaurant_id)
        flash(f"Couldn't reach Google Places to check {restaurant['name']} "
              "— nothing was changed.", "warn")
        return _back_to(restaurant_id)

    status = result["status"]
    label = LABELS.get(status, status)
    logger.info("Checked %s (id=%s) from the dashboard: %s",
                restaurant["name"], restaurant_id, status)

    if status == previous:
        flash(f"Checked {restaurant['name']} — still {label}.", "info")
    elif status == CLOSED_PERMANENTLY:
        # check_one archived it and sent the alert; the row is about to
        # drop out of the active list, so say where it went.
        flash(f"{restaurant['name']} is now {label}. It's been archived.",
              "warn")
    elif status in _ATTENTION_STATUSES:
        flash(f"{restaurant['name']} is now {label}.", "warn")
    else:
        flash(f"{restaurant['name']} is {label} again.", "info")
    return _back_to(restaurant_id)

# --- removing a restaurant -------------------------------------------


def delete(restaurant_id: int):
    """Stop watching a restaurant: drop the row and everything logged
    about it.

    Not the same destruction as `reject`, which throws a row away because
    its history describes a *different* restaurant. This one deletes a
    place that really was the right match, history and all, so it's the
    button a confirmation dialog guards -- see delete_form in
    _status.html. That dialog is client-side and so not a security
    control; it's there because the row is unrecoverable, which is also
    why the removal is logged with the place_id needed to re-add it.

    Archiving remains the gentler option for somewhere that closed: the
    row stays listed, greyed out, and its months of history stay
    queryable. This is for a restaurant you don't want watched at all.

    Always lands on the index -- the detail page it may have been pressed
    from no longer exists -- so there's no `return_to` to honour.
    """
    restaurant = get_restaurant(restaurant_id)
    if restaurant is None:
        # Already gone: a double submit, or a tab left open past the row
        # being deleted elsewhere. Unlike the other routes this doesn't
        # 404, because the state the request asked for is the state we're
        # in -- and the page that would show the 404 is itself gone.
        flash("That restaurant had already been removed.", "info")
        return redirect(url_for("index"))

    delete_restaurant(restaurant_id)
    logger.info("Deleted %s (id=%s, place_id=%s) from the dashboard",
                restaurant["name"], restaurant_id, restaurant["place_id"])
    flash(f"Removed {restaurant['name']} — it won't be checked again.",
          "info")
    return redirect(url_for("index"))

# --- verifying a restaurant ------------------------------------------


def verify(restaurant_id: int):
    """Yes, that's the place. From here on the scheduled run will check it."""
    restaurant = get_restaurant(restaurant_id)
    if restaurant is None:
        abort(404)

    if not verify_restaurant(restaurant_id):
        # Already verified: a double submit, or a page left open past
        # someone else confirming it. Nothing changed, so don't claim it.
        flash(f"{restaurant['name']} was already verified.", "info")
        return _back_to(restaurant_id)

    logger.info("Verified %s (id=%s, place_id=%s)",
                restaurant["name"], restaurant_id, restaurant["place_id"])
    flash(f"Verified {restaurant['name']} — the next check will "
          "include it.", "info")
    return _back_to(restaurant_id)


def verify_selected():
    """Yes, those are all the right places -- for every ticked row at once.

    Only ever writes `verified_at`, through the same once-only UPDATE the
    single-row route uses, so ids that are unknown, already verified or
    just malformed are skipped rather than failing the batch. The count in
    the flash is the number that actually changed, not the number posted.
    """
    ids = []
    for raw in request.form.getlist("ids"):
        try:
            value = int(raw)
        except ValueError:
            continue
        if 0 < value <= _MAX_ID:
            ids.append(value)
    if not ids:
        flash("Tick at least one restaurant to verify.", "warn")
        return redirect(url_for("index"))

    changed = verify_restaurants(ids)
    logger.info("Bulk-verified %d of %d selected restaurants (ids=%s)",
                changed, len(ids), sorted(set(ids)))
    if changed:
        flash(f"Verified {changed} restaurant{'s' if changed != 1 else ''}"
              " — the next check will include "
              f"{'them' if changed != 1 else 'it'}.", "info")
    else:
        flash("Those were already verified.", "info")
    return redirect(url_for("index"))


def reject(restaurant_id: int):
    """No, wrong place. Delete the row and re-open the search.

    The only route here that destroys anything, so it's deliberately
    narrow: an already-verified restaurant can't be deleted through it.
    That stops a stale tab throwing away a year of history for a place
    confirmed long ago -- archiving is the tool for one that closed.
    """
    restaurant = get_restaurant(restaurant_id)
    if restaurant is None:
        abort(404)
    if restaurant["verified_at"]:
        abort(400, "That restaurant is already verified -- archive it instead.")

    delete_restaurant(restaurant_id)
    logger.info("Rejected %s (id=%s, place_id=%s) as the wrong place -- deleted",
                restaurant["name"], restaurant_id, restaurant["place_id"])
    flash(f"Removed {restaurant['name']}. Search again with a street or city "
          "— that's what narrows it down.", "info")
    # The stored address belongs to the wrong place, so only the name is
    # carried over; re-using the address would just find it again.
    return redirect(url_for("index", q=restaurant["name"][:MAX_QUERY_CHARS]))

# --- adding a restaurant ---------------------------------------------


def _pending_candidate() -> dict | None:
    """The search result stashed for this session, or None if there is none
    or it has expired."""
    return get_pending_add(session.get("pending_add"))


def _clear_pending() -> None:
    delete_pending_add(session.pop("pending_add", None))


def add_search():
    """Step 1: ask Places what this name resolves to.

    POST-only and CSRF-checked because this is the one route that costs
    money: a link, a prefetch or a crawler must not be able to spend a
    Text Search call. Nothing is written to the restaurant list here -- the
    candidate is stashed and the browser is redirected to the confirm page,
    so reloading that page doesn't buy the same search twice.
    """
    name = (request.form.get("name") or "").strip()[:MAX_QUERY_CHARS]
    hint = (request.form.get("address_hint") or "").strip()[:MAX_QUERY_CHARS]
    if not name:
        # Checked before the call, not after: an empty box is a slip, and
        # it shouldn't cost a search to find that out.
        flash("Enter a restaurant name to search for.", "warn")
        return redirect(url_for("index"))

    query = _query_label(name, hint)
    try:
        place = find_place_id(name, hint)
    except Exception:
        # Missing API key, a 4xx, a timeout after the adapter's retries.
        # The traceback goes to the log; the page says what didn't happen.
        logger.exception("Places text search failed for %r", query)
        flash("Couldn't reach Google Places just now, so nothing was added. "
              "Try again in a minute.", "warn")
        return redirect(url_for("index"))

    if not place:
        flash(f"No match on Google Places for “{query}”. "
              "Adding a city or street usually fixes it.", "warn")
        return redirect(url_for("index"))

    candidate = place_summary(place, fallback_name=name)
    existing = get_restaurant_by_place_id(candidate["place_id"])
    if existing is not None:
        # Already known. add_restaurant() would quietly ignore the insert,
        # which reads as "added" on a page that then shows one row -- so
        # go to the row instead and say which one it is.
        if existing["archived"]:
            flash(f"Already tracking {existing['name']}, but it's archived -- "
                  "re-activate it here.", "warn")
        else:
            flash(f"Already tracking {existing['name']}.", "info")
        return redirect(url_for("detail", restaurant_id=existing["id"]))

    _clear_pending()
    token = secrets.token_urlsafe(24)
    stash_pending_add(token, dict(candidate, query=query))
    session["pending_add"] = token
    return redirect(url_for("add_confirm"))


def add_confirm():
    """Step 2: show the single best guess and make the user agree to it."""
    candidate = _pending_candidate()
    if not candidate:
        # No stash: a bookmarked URL, an expired search, or a restart with a
        # generated key that invalidated the session cookie.
        flash("That search has expired -- run it again.", "warn")
        return redirect(url_for("index"))
    return render_template("confirm_add.html", candidate=candidate)


def add_commit():
    """Step 3: write the row. No API call here -- this only ever stores
    the candidate the search already paid for."""
    posted_place_id = request.form.get("place_id", "")

    # Checked first because this is also the double-submit path: the first
    # submit pops the stash, so by the second there's nothing left to tell
    # "already added" from "expired". INSERT OR IGNORE makes the repeat
    # harmless either way; reporting it as a fresh add would not be. The
    # posted id is only ever read from here, never written, so a crafted
    # one can do no more than name a row that's already on the page.
    existing = get_restaurant_by_place_id(posted_place_id)
    if existing is not None:
        _clear_pending()
        flash(f"{existing['name']} was already being tracked.", "info")
        return redirect(url_for("detail", restaurant_id=existing["id"]))

    candidate = _pending_candidate()
    if not candidate:
        flash("That search has expired -- run it again.", "warn")
        return redirect(url_for("index"))

    # The form posts back the place_id alone, and it has to match the
    # candidate the server actually looked up. The row is then built from
    # the stashed copy, so a doctored form can't show one restaurant on
    # the page and file a different one in the database.
    if posted_place_id != candidate["place_id"]:
        abort(400, "That confirmation doesn't match the search it came from.")

    # Verified on the way in: this POST *is* the user agreeing to the
    # match the confirm page showed, so asking again at first check would
    # be the same question about the same two facts.
    add_restaurant(name=candidate["name"], place_id=candidate["place_id"],
                   address=candidate["address"], maps_url=candidate["maps_url"],
                   verified=True)
    _clear_pending()

    added = get_restaurant_by_place_id(candidate["place_id"])
    if added is None:  # pragma: no cover -- the insert just succeeded
        logger.error("Added %r but couldn't read it back", candidate["place_id"])
        flash("Something went wrong adding that -- check the log.", "warn")
        return redirect(url_for("index"))

    logger.info("Added %s (place_id=%s) from the dashboard",
                added["name"], added["place_id"])
    flash(f"Now tracking {added['name']}. The next check will pick it up.", "info")
    return redirect(url_for("detail", restaurant_id=added["id"]))


_URLS: list[tuple[str, Callable, tuple[str, ...]]] = [
    ("/", index, ("GET",)),
    ("/restaurant/<int:restaurant_id>", detail, ("GET",)),
    ("/restaurant/<int:restaurant_id>/unarchive", unarchive, ("POST",)),
    ("/restaurant/<int:restaurant_id>/check", check_now, ("POST",)),
    ("/restaurant/<int:restaurant_id>/delete", delete, ("POST",)),
    ("/restaurant/<int:restaurant_id>/verify", verify, ("POST",)),
    ("/verify-selected", verify_selected, ("POST",)),
    ("/restaurant/<int:restaurant_id>/reject", reject, ("POST",)),
    ("/add", add_search, ("POST",)),
    ("/add/confirm", add_confirm, ("GET",)),
    ("/add/confirm", add_commit, ("POST",)),
]


def create_app(config: dict | None = None) -> Flask:
    """Build the app. `config` (e.g. {"SECRET_KEY": ...}) is applied last, so
    a caller -- a test, an embedding server -- can override anything."""
    configure_logging()
    app = Flask(__name__)
    app.secret_key = _secret_key()
    app.jinja_env.globals["csrf_token"] = _csrf_token
    app.before_request(_require_csrf)
    app.after_request(_security_headers)
    if config:
        app.config.update(config)

    # Idempotent (every statement is CREATE ... IF NOT EXISTS). Without it,
    # viewing the dashboard before the first seed/check raises "no such
    # table" instead of showing an honest empty list.
    init_db()

    # The endpoint name is the function's name, which is what the templates'
    # url_for() calls use.
    for rule, view, methods in _URLS:
        app.add_url_rule(rule, view_func=view, methods=methods)
    return app


if __name__ == "__main__":
    # Localhost only: there's no auth here, and the DB holds a personal list.
    create_app().run(host=DASHBOARD_HOST, port=DASHBOARD_PORT)
