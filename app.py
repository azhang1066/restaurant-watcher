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
    /restaurant/<id>/delete      GET: ask first, POST: stop watching it --
                                 delete row + history
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

Login: every page but /login needs a signed-in session, and failed logins are
throttled per client IP. Accounts are made on /signup (gated by DASHBOARD_SIGNUP_CODE,
or closed with DASHBOARD_SIGNUP=off, or `python app.py create-user`). Every
restaurant belongs to one account and every query here is scoped to the signed-in
one (`_uid()`), so nobody sees or changes another's list. Alerts go to that
account's own ntfy topic (/settings). Search and Check now also carry a per-IP rate limit
(`DASHBOARD_ACTIONS_PER_MINUTE`) in front of the daily budgets. See auth.py.
/settings also changes the password (other devices' sessions end: the session
carries a stamp of the password hash), sets the account's email and deletes the
account with its list. Every account needs a confirmed email: sign-up mails a link
(/verify/<token>, confirmed by a POST so mail scanners can't spend it) and the
account is held at /verify-email until it's used -- but only while SMTP is
configured, since otherwise nobody could ever confirm. A forgotten password is
reset by emailed link (/forgot, /reset/<token>) to that address, or from the terminal.
HTTPS: set DASHBOARD_TLS_CERT/KEY to have `python app.py` serve it (cheroot).

Every POST is CSRF-checked in one `before_request` hook -- see `_require_csrf`
-- against a token tied to the session. That needs a signing key, so set
`DASHBOARD_SECRET_KEY` in `.env` to choose the key; without one a generated
key is kept in data/.dashboard_secret so an open tab's token survives a restart. Pages load no inline script or style, and a Content-Security-
Policy header says so.

There's deliberately no module-level `app`: Flask's loader finds the
`create_app()` factory by name, and building the app at import time would run
`init_db()` against the real database just for importing this module.

Run it:
    python app.py                  # waitress on DASHBOARD_HOST:DASHBOARD_PORT
    python app.py create-user       # make an account from the terminal
    python app.py reset-password    # set a new password for a forgotten one
    flask --app app run            # dev server, localhost only
"""
import hmac
import logging
import math
import secrets
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from flask import (
    Flask,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.wrappers import Response

import auth
import db
from checker import AlertNotSent, check_one, is_checkable
from config import (
    DASHBOARD_HOST,
    DASHBOARD_PORT,
    STALE_AFTER_DAYS,
    configure_logging,
    env_int,
    env_str,
)
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
from notifier import email_configured, send_email_verification, send_password_reset, valid_topic
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

# Limits on the two buttons that spend money, each read at request time from
# `.env` (0 turns it off). The budgets are rolling 24-hour counts of dashboard
# presses -- the scheduler's own calls neither draw on them nor are blocked by
# them, since refusing a scheduled check would silently stop the watching.
DEFAULT_SEARCHES_PER_DAY = 25
DEFAULT_CHECKS_PER_DAY = 50
# Check now refuses a restaurant checked (by anyone) more recently than this.
DEFAULT_CHECK_COOLDOWN_HOURS = 24


def _check_cooldown() -> timedelta:
    return timedelta(hours=max(0, env_int("CHECK_NOW_COOLDOWN_HOURS",
                                          DEFAULT_CHECK_COOLDOWN_HOURS)))


# Ids beyond SQLite's INTEGER range can't match a row and raise in the driver.
_MAX_ID = 2**63 - 1

# Badge colour per status; anything else (e.g. an unspecified status) is "bad".
_STATUS_BADGES = {OPERATIONAL: "ok", CLOSED_TEMPORARILY: "warn"}

# Sort the worst news to the top of the list.
_STATUS_RANK = {CLOSED_PERMANENTLY: 0, CLOSED_TEMPORARILY: 1, OPERATIONAL: 2}

# Where a generated signing key is kept, beside the database.
SECRET_KEY_FILE = ".dashboard_secret"

# Endpoints that spend money, rate-limited per client IP.
_SPENDING_ENDPOINTS = frozenset({"add_search", "check_now"})
DEFAULT_ACTIONS_PER_MINUTE = 10

# Tests turn the login off wholesale (conftest); nothing in production does.
LOGIN_DISABLED = False

# A signed-in session lasts this long.
SESSION_DAYS = 30

_CSP = ("default-src 'none'; style-src 'self'; script-src 'self'; "
        "img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")


def _secret_key() -> str:
    """Signing key for the session cookie the CSRF token lives in.

    `DASHBOARD_SECRET_KEY` wins. Without one, a generated key is kept in a file
    beside the database so open pages survive a restart; only if that file
    can't be read or written does it fall back to a per-process key (logged,
    because silently invalidating sessions on every reload is confusing).
    """
    key = env_str("DASHBOARD_SECRET_KEY")
    if key:
        return key
    path = db.DB_PATH.parent / SECRET_KEY_FILE
    try:
        stored = path.read_text(encoding="utf-8").strip()
        if stored:
            return stored
    except FileNotFoundError:
        pass
    except OSError:
        logger.exception("Couldn't read %s", path)
    key = secrets.token_hex(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(key, encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        logger.warning("No DASHBOARD_SECRET_KEY set and %s isn't writable -- using a "
                       "per-process key. Open pages will need a reload after a restart.", path)
    return key


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


def _client_ip() -> str:
    return request.remote_addr or "unknown"


# What a signed-in account with an unconfirmed email can still reach.
_UNCONFIRMED_OK = frozenset({"verify_email", "verify_link", "logout", "static"})


def _require_login():
    """Send anyone without a signed-in session to the login page.

    Registered first, so nothing else (not even the CSRF check) runs for a
    stranger, and load the signed-in user into `g.user`. A session whose
    account no longer exists counts as signed out.
    """
    if LOGIN_DISABLED:
        return None
    user = _session_user()
    if user is not None:
        g.user = user
        # An account must confirm its email before it can be used. Skipped when
        # the server can't send mail: nobody could ever confirm, so gating would
        # just lock everyone out.
        if (not user["email_verified_at"] and request.endpoint not in _UNCONFIRMED_OK
                and email_configured()):
            return redirect(url_for("verify_email"))
        return None
    if request.endpoint in ("login", "signup", "forgot_password", "reset_with_link",
                            "verify_link", "static"):
        return None
    session.pop("user_id", None)
    return redirect(url_for("login"))


def _session_user() -> dict | None:
    """The signed-in account, or None. The session must also carry the stamp of
    the account's current password, so changing or resetting it ends every other
    device's session."""
    user_id = session.get("user_id")
    user = db.get_user(user_id) if isinstance(user_id, int) else None
    if user is None:
        return None
    stamp = auth.session_stamp(user.pop("password_hash"))
    if not hmac.compare_digest(str(session.get("pw", "")), stamp):
        return None
    return user


def _uid() -> int | None:
    """Whose list this request is about. Every dashboard query passes it, so
    one user's rows can't be read or changed through another's session. None
    only when the login is switched off (tests), meaning every list."""
    return None if LOGIN_DISABLED else g.user["id"]


def _sign_in(user_id: int) -> None:
    # A fresh session on privilege change: nothing minted before login (the
    # CSRF token included) carries over.
    user = db.get_user(user_id)
    session.clear()
    if user is None:  # deleted in the instant since it was made; stay signed out
        return
    session["user_id"] = user_id
    session["pw"] = auth.session_stamp(user["password_hash"])
    session.permanent = True


def _rate_limit_spending():
    if request.method != "POST" or request.endpoint not in _SPENDING_ENDPOINTS:
        return None
    if not current_app.extensions["action_limiter"].hit(_client_ip()):
        logger.warning("Rate limit hit on %s from %s", request.endpoint, _client_ip())
        abort(429, "Too many requests -- wait a minute and try again.")
    return None


def _security_headers(response: Response) -> Response:
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.is_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
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
        # Set while the button is cooling down: the wait, spelled out for its
        # tooltip. Same rule the route enforces (`_check_wait`).
        "check_wait": (_duration(wait) if (wait := _check_wait(checked_at, now)) else None),
        "checked_at": checked_at,
        "checked_age": _age(checked_at, now),
        # Never checked is its own state, not a stale one -- a freshly seeded
        # restaurant hasn't missed anything yet.
        "stale": (checked_at is not None
                  and (now - checked_at).days >= STALE_AFTER_DAYS),
        "needs_attention": (closing_soon or status in _ATTENTION_STATUSES
                            or needs_verification),
    }


def _check_wait(checked_at: datetime | None, now: datetime) -> timedelta | None:
    """How much longer Check now is off for a row, or None when it's allowed."""
    if checked_at is None:
        return None
    remaining = checked_at + _check_cooldown() - now
    return remaining if remaining > timedelta(0) else None


def _duration(delta: timedelta) -> str:
    """'3 hours' / '20 minutes', rounded up so it never promises too early."""
    minutes = max(1, math.ceil(delta.total_seconds() / 60))
    if minutes >= 60:
        hours = math.ceil(minutes / 60)
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


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


def login():
    if LOGIN_DISABLED or "user" in g:
        return redirect(url_for("index"))
    if request.method == "POST":
        limiter = current_app.extensions["login_limiter"]
        ip = _client_ip()
        if limiter.blocked(ip):
            logger.warning("Login throttled for %s", ip)
            abort(429, "Too many failed sign-ins -- try again in a few minutes.")
        user = auth.authenticate(request.form.get("username", ""), request.form.get("password", ""))
        if user is not None:
            _sign_in(user["id"])
            return redirect(url_for("index"))
        limiter.hit(ip)
        logger.warning("Failed login from %s", ip)
        flash("Wrong username or password.", "warn")
        return redirect(url_for("login"))
    return render_template("login.html", has_login=auth.has_login(),
                           signup_open=auth.signup_mode() != "closed",
                           can_reset=email_configured())


def signup():
    """Create an account, then sign it in. Closed entirely by
    DASHBOARD_SIGNUP=off; with DASHBOARD_SIGNUP_CODE set it also asks for the
    code. Throttled per IP because a guessable code is the only gate."""
    mode = auth.signup_mode()
    if LOGIN_DISABLED or "user" in g or mode == "closed":
        return redirect(url_for("index"))
    if request.method == "POST":
        ip = _client_ip()
        if not email_configured():
            # An account needs a confirmed email, and confirming needs mail out.
            flash("Sign-up needs this server to be able to send email, and it can't yet. "
                  "Ask whoever runs it to create your account with "
                  "`python app.py create-user`.", "warn")
            return redirect(url_for("login"))
        if not current_app.extensions["signup_limiter"].hit(ip):
            logger.warning("Sign-up throttled for %s", ip)
            abort(429, "Too many sign-up attempts -- try again later.")
        if not auth.signup_code_ok(request.form.get("code", "")):
            logger.warning("Wrong sign-up code from %s", ip)
            flash("That sign-up code is wrong.", "warn")
            return redirect(url_for("signup"))
        user_id, error = auth.create_account(request.form.get("username", ""),
                                             request.form.get("password", ""),
                                             request.form.get("confirm", ""),
                                             request.form.get("email", ""))
        if user_id is None:
            flash(error or "Couldn't create the account.", "warn")
            return redirect(url_for("signup"))
        logger.info("New account %r from %s", request.form.get("username", "").strip(), ip)
        _send_verification(user_id, request.form.get("email", ""))
        _sign_in(user_id)
        return redirect(url_for("verify_email"))
    return render_template("signup.html", needs_code=mode == "code",
                           min_password=auth.MIN_PASSWORD_CHARS,
                           can_reset=email_configured())


def _send_verification(user_id: int, raw_email: str) -> str | None:
    """Start confirming an address for this account and mail the link in the
    background. Returns a message for the person if it couldn't be started."""
    address, token, error = auth.start_verification(user_id, raw_email)
    if error is None:
        _mail_verification(user_id, address, token)
    return error


def _mail_verification(user_id: int, address: str | None, token: str | None) -> None:
    user = db.get_user(user_id)
    if user and address and token:
        _dispatch(send_email_verification, address, user["username"], token)


def verify_email():
    """Where an account with an unconfirmed email lands: says where the link went,
    and lets the person resend it or give a different address. No password is
    asked for -- the account can't do anything yet, and a new address only counts
    once its link is used."""
    if LOGIN_DISABLED:
        return redirect(url_for("index"))
    if g.user["email_verified_at"] or not email_configured():
        return redirect(url_for("index"))
    pending = db.pending_verification_email(g.user["id"]) or g.user["email"] or ""
    if request.method == "POST":
        if not current_app.extensions["verify_limiter"].hit(_client_ip()):
            abort(429, "Too many confirmation emails -- try again later.")
        error = _send_verification(g.user["id"], request.form.get("email", pending))
        flash(error or "Sent. Open the link in it to finish.", "warn" if error else "info")
        return redirect(url_for("verify_email"))
    return render_template("verify_email.html", email=pending)


def verify_link(token: str):
    """The page a confirmation email links to. Confirming is a POST behind a
    button, not the GET itself: mail scanners and link previews open every link
    in a message, and would otherwise spend it."""
    if LOGIN_DISABLED:
        return redirect(url_for("index"))
    if request.method == "POST":
        result = auth.confirm_email(token)
        if result == "ok":
            flash("Email confirmed.", "info")
        elif result == "taken":
            flash("Another account confirmed that address first.", "warn")
        else:
            flash("That confirmation link has expired or was already used.", "warn")
        return redirect(url_for("index" if "user" in g else "login"))
    pending = auth.verification_pending(token)
    if pending is None:
        flash("That confirmation link has expired or was already used.", "warn")
        return redirect(url_for("index" if "user" in g else "login"))
    return render_template("verify_link.html", email=pending["email"])


def _dispatch(fn: Callable[..., object], *args: object) -> None:
    """Run `fn` off the request thread. Used for the reset email so the page
    answers in the same time whether or not an account matched (a slow SMTP
    server would otherwise reveal which usernames exist). Tests replace this
    with a direct call."""
    threading.Thread(target=fn, args=args, daemon=True).start()


def forgot_password():
    """Ask for a reset link by username or email. The answer is the same whether
    or not an account matched, and per-IP throttled."""
    if LOGIN_DISABLED or "user" in g:
        return redirect(url_for("index"))
    if not email_configured():
        flash("Reset by email isn't set up on this server. Ask whoever runs it to "
              "run `python app.py reset-password`.", "warn")
        return redirect(url_for("login"))
    if request.method == "POST":
        ip = _client_ip()
        if not current_app.extensions["reset_limiter"].hit(ip):
            logger.warning("Password-reset requests throttled for %s", ip)
            abort(429, "Too many reset requests -- try again later.")
        issued = auth.issue_reset(request.form.get("identifier", ""))
        if issued is not None:
            user, token = issued
            logger.info("Password reset link issued for %r from %s", user["username"], ip)
            _dispatch(send_password_reset, user["email"], user["username"], token)
        flash("If that matches an account with an email address, a reset link is on "
              "its way. It works once and expires in an hour.", "info")
        return redirect(url_for("login"))
    return render_template("forgot.html")


def reset_with_link(token: str):
    """The page a reset email links to: pick a new password. Shows the form only
    for a live link, and uses the link up when the password is set."""
    if LOGIN_DISABLED or "user" in g:
        return redirect(url_for("index"))
    if request.method == "POST":
        error = auth.finish_reset(token, request.form.get("password", ""),
                                  request.form.get("confirm", ""))
        if error is None:
            flash("Password changed. Sign in with the new one.", "info")
            return redirect(url_for("login"))
        flash(error, "warn")
        if error == auth.RESET_EXPIRED:
            return redirect(url_for("forgot_password"))
        return redirect(url_for("reset_with_link", token=token))
    if not auth.reset_link_valid(token):
        flash(auth.RESET_EXPIRED, "warn")
        return redirect(url_for("forgot_password"))
    return render_template("reset.html", min_password=auth.MIN_PASSWORD_CHARS)


def logout():
    session.clear()
    return redirect(url_for("login"))


def settings():
    """The signed-in user's own alert topic. ntfy topics are public, so the
    name is the secret: it must be long and unguessable, and is never shown to
    anyone else. Empty means "use the install's NTFY_TOPIC"."""
    if LOGIN_DISABLED:
        return redirect(url_for("index"))
    if request.method == "POST":
        topic = request.form.get("ntfy_topic", "").strip()
        if not topic:
            db.set_ntfy_topic(g.user["id"], None)
            flash("Alert topic cleared -- alerts use the install's default topic, if any.",
                  "info")
        elif valid_topic(topic):
            db.set_ntfy_topic(g.user["id"], topic)
            flash("Alert topic saved. Subscribe to it in the ntfy app.", "info")
        else:
            flash("A topic is 8-64 letters, numbers, dashes or underscores -- long and "
                  "unguessable, since anyone who knows it can read your alerts.", "warn")
        return redirect(url_for("settings"))
    return render_template("settings.html", topic=g.user["ntfy_topic"] or "",
                           email=g.user["email"] or "",
                           email_confirmed=bool(g.user["email_verified_at"]),
                           pending_email=db.pending_verification_email(g.user["id"]) or "",
                           can_reset=email_configured(),
                           min_password=auth.MIN_PASSWORD_CHARS)


def change_email():
    """Move the account to a new email. Needs the current password (otherwise a
    borrowed session could redirect the reset link), and the new address only
    replaces the old one once its confirmation link is used."""
    if LOGIN_DISABLED:
        return redirect(url_for("index"))
    _check_password_attempts_allowed()
    if not email_configured():
        flash("This server can't send email, so an address can't be confirmed yet.", "warn")
        return redirect(url_for("settings"))
    if not current_app.extensions["verify_limiter"].hit(_client_ip()):
        abort(429, "Too many confirmation emails -- try again later.")
    address, token, error = auth.change_email(g.user["id"], request.form.get("current", ""),
                                              request.form.get("email", ""))
    if error is None:
        _mail_verification(g.user["id"], address, token)
        flash(f"We sent a confirmation link to {address}. Your email changes when you "
              "open it.", "info")
    else:
        if error == auth.WRONG_PASSWORD:
            _note_wrong_password()
        flash(error, "warn")
    return redirect(url_for("settings"))


def _check_password_attempts_allowed() -> None:
    """Changing a password or deleting an account asks for the current one, so a
    stolen session could use those forms to guess it. Wrong guesses share the
    login's per-IP failure budget."""
    if current_app.extensions["login_limiter"].blocked(_client_ip()):
        logger.warning("Password prompt throttled for %s", _client_ip())
        abort(429, "Too many failed attempts -- try again in a few minutes.")


def _note_wrong_password() -> None:
    current_app.extensions["login_limiter"].hit(_client_ip())
    logger.warning("Wrong current password for %r from %s", g.user["username"], _client_ip())


def change_password():
    """Set a new password. Asks for the current one; the other devices' sessions
    end, this one carries on under the new password."""
    if LOGIN_DISABLED:
        return redirect(url_for("index"))
    _check_password_attempts_allowed()
    error = auth.change_password(g.user["id"], request.form.get("current", ""),
                                 request.form.get("password", ""),
                                 request.form.get("confirm", ""))
    if error is not None:
        if error == auth.WRONG_PASSWORD:
            _note_wrong_password()
        flash(error, "warn")
        return redirect(url_for("settings"))
    logger.info("Password changed for %r", g.user["username"])
    _sign_in(g.user["id"])
    flash("Password changed. Other devices have been signed out.", "info")
    return redirect(url_for("settings"))


def delete_account():
    """Remove the signed-in account and its whole list, after the password is
    given again. Unrecoverable, so the form says so and takes the username too."""
    if LOGIN_DISABLED:
        return redirect(url_for("index"))
    _check_password_attempts_allowed()
    user = g.user
    if request.form.get("username", "").strip().lower() != user["username"].lower():
        flash("Type your username to confirm deleting the account.", "warn")
        return redirect(url_for("settings"))
    if auth.authenticate(user["username"], request.form.get("current", "")) is None:
        _note_wrong_password()
        flash(auth.WRONG_PASSWORD, "warn")
        return redirect(url_for("settings"))
    db.delete_user(user["id"])
    logger.info("Deleted account %r (id=%s) and its list", user["username"], user["id"])
    session.clear()
    flash("Your account and its restaurant list were deleted.", "info")
    return redirect(url_for("login"))


def index():
    now = datetime.now(UTC)
    views = sorted((_view(r, now) for r in list_restaurants(active_only=False, user_id=_uid())),
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
    restaurant = get_restaurant(restaurant_id, _uid())
    if restaurant is None:
        abort(404)
    return render_template(
        "detail.html",
        restaurant=_view(restaurant, datetime.now(UTC)),
        history=list(reversed(check_history(restaurant_id))),
    )


def unarchive(restaurant_id: int):
    restaurant = get_restaurant(restaurant_id, _uid())
    if restaurant is None:
        abort(404)

    if not restaurant["archived"]:
        # Already active: a double-submitted form or a stale page. Nothing
        # to undo, so say so rather than reporting a change that didn't
        # happen.
        flash(f"{restaurant['name']} was already active.", "info")
        return _back_to(restaurant_id)

    unarchive_restaurant(restaurant_id, _uid())
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
    restaurant = get_restaurant(restaurant_id, _uid())
    if restaurant is None:
        abort(404)
    if not is_checkable(restaurant):
        abort(400, "That restaurant isn't being checked -- verify it, or "
                   "re-activate it, first.")

    # Cooldown before budget: a refused press must not use up a slot. The page
    # disables the button in this window, so this is a stale tab or a crafted
    # post -- a message rather than a 4xx, as for the other already-done cases.
    wait = _check_wait(_parse_ts(restaurant.get("last_checked_at")), datetime.now(UTC))
    if wait:
        flash(f"{restaurant['name']} was checked recently. Check now is "
              f"available again in {_duration(wait)}.", "info")
        return _back_to(restaurant_id)
    if not db.spend_budget("check", env_int("DASHBOARD_CHECKS_PER_DAY", DEFAULT_CHECKS_PER_DAY)):
        logger.warning("Daily Check now limit reached -- refused %s (id=%s)",
                       restaurant["name"], restaurant_id)
        flash("Daily limit for Check now reached. It frees up as presses age past "
              "24 hours; the weekly run isn't affected.", "warn")
        return _back_to(restaurant_id)

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


def delete_confirm(restaurant_id: int):
    """Ask before deleting: a page naming the restaurant, whose button
    POSTs to `delete`. Server-rendered so the guard holds with JavaScript
    off. Read-only -- a GET here never removes anything."""
    restaurant = get_restaurant(restaurant_id, _uid())
    if restaurant is None:
        flash("That restaurant had already been removed.", "info")
        return redirect(url_for("index"))
    return render_template("confirm_delete.html", restaurant=restaurant)


def delete(restaurant_id: int):
    """Stop watching a restaurant: drop the row and everything logged
    about it.

    Not the same destruction as `reject`, which throws a row away because
    its history describes a *different* restaurant. This one deletes a
    place that really was the right match, history and all, so it sits
    behind a confirmation page (`delete_confirm`) rather than a dialog.
    The row is unrecoverable, which is also why the removal is logged
    with the place_id needed to re-add it.

    Archiving remains the gentler option for somewhere that closed: the
    row stays listed, greyed out, and its months of history stay
    queryable. This is for a restaurant you don't want watched at all.

    Always lands on the index -- the detail page it may have been pressed
    from no longer exists -- so there's no `return_to` to honour.
    """
    restaurant = get_restaurant(restaurant_id, _uid())
    if restaurant is None:
        # Already gone: a double submit, or a tab left open past the row
        # being deleted elsewhere. Unlike the other routes this doesn't
        # 404, because the state the request asked for is the state we're
        # in -- and the page that would show the 404 is itself gone.
        flash("That restaurant had already been removed.", "info")
        return redirect(url_for("index"))

    delete_restaurant(restaurant_id, _uid())
    logger.info("Deleted %s (id=%s, place_id=%s) from the dashboard",
                restaurant["name"], restaurant_id, restaurant["place_id"])
    flash(f"Removed {restaurant['name']} — it won't be checked again.",
          "info")
    return redirect(url_for("index"))

# --- verifying a restaurant ------------------------------------------


def verify(restaurant_id: int):
    """Yes, that's the place. From here on the scheduled run will check it."""
    restaurant = get_restaurant(restaurant_id, _uid())
    if restaurant is None:
        abort(404)

    if not verify_restaurant(restaurant_id, _uid()):
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

    changed = verify_restaurants(ids, _uid())
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
    restaurant = get_restaurant(restaurant_id, _uid())
    if restaurant is None:
        abort(404)
    if restaurant["verified_at"]:
        abort(400, "That restaurant is already verified -- archive it instead.")

    delete_restaurant(restaurant_id, _uid())
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
    if not db.spend_budget("search", env_int("DASHBOARD_SEARCHES_PER_DAY",
                                             DEFAULT_SEARCHES_PER_DAY)):
        logger.warning("Daily search limit reached -- refused %r", query)
        flash("Daily limit for searches reached. It frees up as searches age past "
              "24 hours.", "warn")
        return redirect(url_for("index"))
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
    existing = get_restaurant_by_place_id(candidate["place_id"], _uid())
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
    existing = get_restaurant_by_place_id(posted_place_id, _uid())
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
                   verified=True, user_id=_uid())
    _clear_pending()

    added = get_restaurant_by_place_id(candidate["place_id"], _uid())
    if added is None:  # pragma: no cover -- the insert just succeeded
        logger.error("Added %r but couldn't read it back", candidate["place_id"])
        flash("Something went wrong adding that -- check the log.", "warn")
        return redirect(url_for("index"))

    logger.info("Added %s (place_id=%s) from the dashboard",
                added["name"], added["place_id"])
    flash(f"Now tracking {added['name']}. The next check will pick it up.", "info")
    return redirect(url_for("detail", restaurant_id=added["id"]))


_URLS: list[tuple[str, Callable, tuple[str, ...]]] = [
    ("/login", login, ("GET", "POST")),
    ("/signup", signup, ("GET", "POST")),
    ("/logout", logout, ("POST",)),
    ("/settings", settings, ("GET", "POST")),
    ("/forgot", forgot_password, ("GET", "POST")),
    ("/reset/<token>", reset_with_link, ("GET", "POST")),
    ("/verify-email", verify_email, ("GET", "POST")),
    ("/verify/<token>", verify_link, ("GET", "POST")),
    ("/settings/email", change_email, ("POST",)),
    ("/settings/password", change_password, ("POST",)),
    ("/settings/delete-account", delete_account, ("POST",)),
    ("/", index, ("GET",)),
    ("/restaurant/<int:restaurant_id>", detail, ("GET",)),
    ("/restaurant/<int:restaurant_id>/unarchive", unarchive, ("POST",)),
    ("/restaurant/<int:restaurant_id>/check", check_now, ("POST",)),
    ("/restaurant/<int:restaurant_id>/delete", delete_confirm, ("GET",)),
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
    app.config.update(
        SESSION_COOKIE_SAMESITE="Lax",
        # Set when it's served over HTTPS (a proxy or tunnel), so the cookie
        # never travels in the clear.
        # Always on when the server does TLS itself.
        SESSION_COOKIE_SECURE=(bool(env_int("DASHBOARD_COOKIE_SECURE", 0))
                               or auth.tls_files() is not None),
        PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_DAYS),
    )
    hops = max(0, env_int("DASHBOARD_PROXY_HOPS", 0))
    if hops:
        # Behind that many trusted proxies, the client IP (what the rate limits
        # key on) and scheme come from X-Forwarded-*. Never set this when the
        # port is directly reachable: the header would be spoofable.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops)  # type: ignore[method-assign]
    app.extensions["login_limiter"] = auth.RateLimiter(auth.LOGIN_FAILURES,
                                                       auth.LOGIN_WINDOW_SECONDS)
    app.extensions["verify_limiter"] = auth.RateLimiter(auth.VERIFY_REQUESTS,
                                                        auth.RESET_WINDOW_SECONDS)
    app.extensions["reset_limiter"] =auth.RateLimiter(auth.RESET_REQUESTS,
                                                       auth.RESET_WINDOW_SECONDS)
    app.extensions["signup_limiter"] = auth.RateLimiter(auth.SIGNUP_ATTEMPTS,
                                                        auth.SIGNUP_WINDOW_SECONDS)
    app.extensions["action_limiter"] = auth.RateLimiter(
        env_int("DASHBOARD_ACTIONS_PER_MINUTE", DEFAULT_ACTIONS_PER_MINUTE), 60)
    app.jinja_env.globals["csrf_token"] = _csrf_token
    app.context_processor(lambda: {"auth_enabled": not LOGIN_DISABLED,
                                    "current_user": g.get("user")})
    app.before_request(_require_login)
    app.before_request(_require_csrf)
    app.before_request(_rate_limit_spending)
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


def _create_user_from_terminal() -> None:
    """For when sign-up is off (or you'd rather not use the browser)."""
    import getpass
    init_db()
    while True:
        # Typed at the server's own terminal, so the address counts as confirmed.
        _, error = auth.create_account(input("Username: "), getpass.getpass("Password: "),
                                       getpass.getpass("Again: "), input("Email: "),
                                       verified=True)
        if error is None:
            print("Account created.")
            return
        print(error)


def _reset_password_from_terminal() -> None:
    """The reset for a forgotten password: whoever can run this on the server can
    set any account's password. It also ends that account's signed-in sessions."""
    import getpass
    init_db()
    username = input("Username: ")
    while True:
        error = auth.reset_password(username, getpass.getpass("New password: "),
                                    getpass.getpass("Again: "))
        if error is None:
            print("Password changed. Every device signed in to that account is signed out.")
            return
        print(error)
        if error == auth.NO_SUCH_USER:
            return


def serve() -> None:
    """Run the dashboard: under waitress, a production WSGI server (Flask's dev
    server isn't meant to face a network), or under cheroot with HTTPS when
    DASHBOARD_TLS_CERT / DASHBOARD_TLS_KEY are set (waitress can't do TLS).
    Refuses a non-loopback host while sign-up is open."""
    host = env_str("DASHBOARD_HOST", DASHBOARD_HOST)
    port = env_int("DASHBOARD_PORT", DASHBOARD_PORT)
    init_db()  # the binding check counts accounts, so the table must exist
    auth.require_safe_binding(host)
    tls = auth.tls_files()
    app = create_app()
    if tls is None:
        if not auth.is_loopback(host) and not env_int("DASHBOARD_COOKIE_SECURE", 0):
            logger.warning("Listening on %s without TLS: passwords and sessions cross the "
                           "network in the clear. Set DASHBOARD_TLS_CERT/KEY, or put an HTTPS "
                           "proxy in front and set DASHBOARD_COOKIE_SECURE=1.", host)
        from waitress import serve as waitress_serve
        logger.info("Dashboard on http://%s:%s", host, port)
        waitress_serve(app, host=host, port=port, threads=4)
        return
    from cheroot.ssl.builtin import BuiltinSSLAdapter
    from cheroot.wsgi import Server
    server = Server((host, port), app, numthreads=4)
    server.ssl_adapter = BuiltinSSLAdapter(str(tls[0]), str(tls[1]))
    logger.info("Dashboard on https://%s:%s", host, port)
    try:
        server.start()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    if sys.argv[1:] == ["create-user"]:
        _create_user_from_terminal()
    elif sys.argv[1:] == ["reset-password"]:
        _reset_password_from_terminal()
    else:
        serve()
