"""Dashboard over the watcher's state (Phase 2).

Until now the only way to see what the watcher knew was to open the SQLite
file by hand. This serves:

    /                            every restaurant, current status, last checked
    /restaurant/<id>             one restaurant, plus its per-month history
    /restaurant/<id>/unarchive   POST: put an archived restaurant back (see
                                 db.unarchive_restaurant for the caveat)
    /add                         POST: search Google Places for a name
    /add/confirm                 GET: show the match, POST: track it

Every read goes through `db.py` rather than issuing its own SQL, so the
numbers here can't drift from the values `main.py` decides notifications on.
Of the manual actions, adding and un-archiving are wired up; "force a check
now" still needs `main.py`'s loop split up before a request can drive it.

Adding is the one thing here that spends money -- a Places Text Search per
search -- and the one thing that can be silently *wrong*: Text Search always
answers with its single best guess, so tracking the wrong "Lilia" looks
exactly like tracking the right one. Hence the two steps: the search stashes
its candidate and redirects, and a second, separate POST is what writes the
row. The redirect in between also means a reload of the confirm page re-reads
the stash rather than re-running (and re-paying for) the search.

Because there is now a state-changing route, every form carries a CSRF token
tied to the session -- see `_csrf_token()`. That needs a signing key, so set
`DASHBOARD_SECRET_KEY` in `.env` to keep sessions across restarts; without
one a per-process key is generated and the only cost is that an open tab's
token stops matching when the server restarts.

There's deliberately no module-level `app`: Flask's loader finds the
`create_app()` factory by name, and building the app at import time would
run `init_db()` against the real database just for importing this module
(the tests import it).

Run it:
    flask --app app run     # http://127.0.0.1:5000
    python app.py           # same, explicit host/port
"""
import hmac
import logging
import os
import secrets
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import (Flask, abort, flash, redirect, render_template, request,
                   session, url_for)

from db import (add_restaurant, check_history, get_restaurant,
                get_restaurant_by_place_id, init_db, list_restaurants,
                unarchive_restaurant)
from places_client import find_place_id, place_summary

load_dotenv()

logger = logging.getLogger(__name__)

# Checks run weekly by default, so a fortnight of silence means the scheduler
# died rather than that nothing happened. Worth surfacing: a watcher that has
# quietly stopped watching looks exactly like one with no bad news.
STALE_AFTER_DAYS = 14

_ATTENTION_STATUSES = ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY")

# Cap what gets sent to Text Search. Long queries don't match better, and the
# candidate rides back to the confirm page in the session cookie, which has
# 4KB to work with.
MAX_QUERY_CHARS = 120

_STATUS_LABELS = {
    "OPERATIONAL": "Open",
    "CLOSED_TEMPORARILY": "Closed temporarily",
    "CLOSED_PERMANENTLY": "Closed permanently",
}

# Sort the worst news to the top of the list.
_STATUS_RANK = {"CLOSED_PERMANENTLY": 0, "CLOSED_TEMPORARILY": 1, "OPERATIONAL": 2}


def _secret_key():
    """Signing key for the session cookie the CSRF token lives in.

    A generated key is fine for a personal localhost tool -- it only means
    tokens don't outlive a restart -- but it's logged, because silently
    invalidating sessions on every reload is confusing if you didn't expect it.
    """
    key = os.environ.get("DASHBOARD_SECRET_KEY")
    if key:
        return key
    logger.info("No DASHBOARD_SECRET_KEY set -- generating a per-process key. "
                "Open pages will need a reload after a restart.")
    return secrets.token_hex(32)


def _csrf_token():
    """The session's CSRF token, minted on first use. Exposed to templates."""
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _require_csrf():
    """Reject a POST whose token doesn't match the session's.

    Flask's SameSite=Lax cookie already blocks the cross-site form post, but
    this is the check that doesn't depend on the browser getting that right.
    """
    expected = session.get("csrf_token")
    provided = request.form.get("csrf_token", "")
    if not expected or not hmac.compare_digest(str(expected), provided):
        abort(400, "CSRF token missing or stale -- reload the page and retry.")


def _parse_ts(value):
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
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _age(then, now):
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


def _query_label(name, hint):
    """What the user typed, for echoing back on the confirm page and in
    'no match' messages -- the same string that was sent to Text Search."""
    return f"{name} {hint}".strip() if hint else name


def _view(restaurant, now):
    """Presentation-ready copy of a restaurant row.

    Built as a separate dict so the templates never reach into raw column
    values, and so the age/staleness arithmetic stays testable on its own.
    """
    status = restaurant["business_status"] or "OPERATIONAL"
    checked_at = _parse_ts(restaurant.get("last_checked_at"))
    closing_soon = bool(restaurant["closing_soon_flag"])
    return {
        "id": restaurant["id"],
        "name": restaurant["name"],
        "address": restaurant.get("address") or "",
        "maps_url": restaurant.get("maps_url") or "",
        "status": status,
        "status_label": _STATUS_LABELS.get(status, status),
        "closing_soon": closing_soon,
        # A closing-soon flag is news about a *future* closure, so it stops
        # being news once the place is shut for good. Decided here rather than
        # in the template so the badge and the summary count can't disagree
        # about what the page is showing. main.run_check() now settles the
        # stored flag on a permanent closure too, so this is belt-and-braces
        # -- it keeps the page honest about a row written by anything else.
        "closing_soon_current": closing_soon and status != "CLOSED_PERMANENTLY",
        "closing_soon_summary": restaurant.get("closing_soon_summary") or "",
        "archived": bool(restaurant["archived"]),
        "checked_at": checked_at,
        "checked_age": _age(checked_at, now),
        # Never checked is its own state, not a stale one -- a freshly seeded
        # restaurant hasn't missed anything yet.
        "stale": (checked_at is not None
                  and (now - checked_at).days >= STALE_AFTER_DAYS),
        "needs_attention": closing_soon or status in _ATTENTION_STATUSES,
    }


def _sort_key(view):
    return (not view["needs_attention"],
            _STATUS_RANK.get(view["status"], 3),
            view["name"].lower())


def create_app():
    app = Flask(__name__)
    app.secret_key = _secret_key()
    app.jinja_env.globals["csrf_token"] = _csrf_token

    # Idempotent (every statement is CREATE ... IF NOT EXISTS). Without it,
    # viewing the dashboard before the first seed/check raises "no such
    # table" instead of showing an honest empty list.
    init_db()

    @app.route("/")
    def index():
        now = datetime.now(timezone.utc)
        views = sorted((_view(r, now) for r in list_restaurants(active_only=False)),
                       key=_sort_key)
        active = [v for v in views if not v["archived"]]
        summary = {
            "active": len(active),
            "closed": sum(1 for v in active if v["status"] in _ATTENTION_STATUSES),
            "closing_soon": sum(1 for v in active if v["closing_soon_current"]),
            "archived": sum(1 for v in views if v["archived"]),
            "stale": sum(1 for v in active if v["stale"]),
        }
        return render_template("index.html", restaurants=views, summary=summary,
                               stale_after_days=STALE_AFTER_DAYS,
                               max_query_chars=MAX_QUERY_CHARS)

    def _back_to(restaurant_id):
        """Send a POST back to the page it came from.

        `return_to` selects an endpoint *name*, never a URL or path, so a
        crafted value can only ever land on one of these two pages -- there's
        no open redirect to get wrong. Anything unrecognised falls through to
        the detail page.
        """
        if request.form.get("return_to") == "index":
            return redirect(url_for("index"))
        return redirect(url_for("detail", restaurant_id=restaurant_id))

    @app.route("/restaurant/<int:restaurant_id>")
    def detail(restaurant_id):
        restaurant = get_restaurant(restaurant_id)
        if restaurant is None:
            abort(404)
        return render_template(
            "detail.html",
            restaurant=_view(restaurant, datetime.now(timezone.utc)),
            history=list(reversed(check_history(restaurant_id))),
        )

    @app.post("/restaurant/<int:restaurant_id>/unarchive")
    def unarchive(restaurant_id):
        _require_csrf()
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
        if restaurant["business_status"] == "CLOSED_PERMANENTLY":
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

    # --- adding a restaurant ---------------------------------------------

    @app.post("/add")
    def add_search():
        """Step 1: ask Places what this name resolves to.

        POST-only and CSRF-checked because this is the one route that costs
        money: a link, a prefetch or a crawler must not be able to spend a
        Text Search call. Nothing is written here -- the candidate goes in
        the session and the browser is redirected to the confirm page, so
        reloading that page doesn't buy the same search twice.
        """
        _require_csrf()
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

        session["pending_add"] = dict(candidate, query=query)
        return redirect(url_for("add_confirm"))

    @app.get("/add/confirm")
    def add_confirm():
        """Step 2: show the single best guess and make the user agree to it."""
        candidate = session.get("pending_add")
        if not candidate:
            # No stash: a bookmarked URL, or a restart with a generated key
            # that invalidated the session cookie.
            flash("That search has expired -- run it again.", "warn")
            return redirect(url_for("index"))
        return render_template("confirm_add.html", candidate=candidate)

    @app.post("/add/confirm")
    def add_commit():
        """Step 3: write the row. No API call here -- this only ever stores
        the candidate the search already paid for."""
        _require_csrf()
        posted_place_id = request.form.get("place_id", "")

        # Checked first because this is also the double-submit path: the first
        # submit pops the stash, so by the second there's nothing left to tell
        # "already added" from "expired". INSERT OR IGNORE makes the repeat
        # harmless either way; reporting it as a fresh add would not be. The
        # posted id is only ever read from here, never written, so a crafted
        # one can do no more than name a row that's already on the page.
        existing = get_restaurant_by_place_id(posted_place_id)
        if existing is not None:
            session.pop("pending_add", None)
            flash(f"{existing['name']} was already being tracked.", "info")
            return redirect(url_for("detail", restaurant_id=existing["id"]))

        candidate = session.get("pending_add")
        if not candidate:
            flash("That search has expired -- run it again.", "warn")
            return redirect(url_for("index"))

        # The form posts back the place_id alone, and it has to match the
        # candidate the server actually looked up. The row is then built from
        # the session copy, so a doctored form can't show one restaurant on
        # the page and file a different one in the database.
        if posted_place_id != candidate["place_id"]:
            abort(400, "That confirmation doesn't match the search it came from.")

        add_restaurant(name=candidate["name"], place_id=candidate["place_id"],
                       address=candidate["address"], maps_url=candidate["maps_url"])
        session.pop("pending_add", None)

        added = get_restaurant_by_place_id(candidate["place_id"])
        if added is None:  # pragma: no cover -- the insert just succeeded
            logger.error("Added %r but couldn't read it back", candidate["place_id"])
            flash("Something went wrong adding that -- check the log.", "warn")
            return redirect(url_for("index"))

        logger.info("Added %s (place_id=%s) from the dashboard",
                    added["name"], added["place_id"])
        flash(f"Now tracking {added['name']}. The next check will pick it up.", "info")
        return redirect(url_for("detail", restaurant_id=added["id"]))

    return app


if __name__ == "__main__":
    # Localhost only: there's no auth here, and the DB holds a personal list.
    create_app().run(host="127.0.0.1", port=5000)
