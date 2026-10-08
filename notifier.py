"""Push notifications via ntfy.sh (same service the reservation notifier
uses, so this can share a topic/setup if you want one phone feed for both)
and, optionally, email via SMTP.
"""
import logging
import smtplib
import ssl
from email.header import Header
from email.message import EmailMessage

from config import DEFAULT_DASHBOARD_URL, env_int, env_str
from http_session import retrying_session
from statuses import PHRASES

logger = logging.getLogger(__name__)

DEFAULT_NTFY_TOPIC = "restaurant-watcher-changeme"
# ntfy topics are public: anyone who knows (or guesses) the name can read the
# feed, and the name is the only secret. So the placeholder values shipped in
# the code and in .env.example are treated as "not configured", never used.
_PLACEHOLDER_TOPICS = {DEFAULT_NTFY_TOPIC, "pick-a-unique-topic-name"}
SMTP_SSL_PORT = 465

# How many names to spell out before the rest become a count. A push
# notification gets a couple of lines on a lock screen, and a seeded file
# can leave a hundred restaurants waiting at once.
NAMES_IN_PUSH = 5

_session = retrying_session(("POST",))


def _ntfy_url() -> str | None:
    """The ntfy topic URL, or None when no private topic is configured.

    Read at call time, not import time, so `.env` lands however this module
    was imported -- see `places_client._api_key()` for the same pattern.
    """
    topic = env_str("NTFY_TOPIC", DEFAULT_NTFY_TOPIC)
    if topic in _PLACEHOLDER_TOPICS:
        return None
    return f"https://ntfy.sh/{topic}"


def _dashboard_url() -> str:
    """Where to send someone who taps the verify notification."""
    return env_str("DASHBOARD_URL", DEFAULT_DASHBOARD_URL)


def _email_settings() -> dict | None:
    """SMTP config, or None when email isn't configured (host/from/to are the
    required trio)."""
    user = env_str("SMTP_USER")
    host = env_str("SMTP_HOST")
    sender = env_str("EMAIL_FROM") or user
    to = env_str("EMAIL_TO")
    if not (host and sender and to):
        return None
    return {
        "host": host,
        "port": env_int("SMTP_PORT", 587),
        "user": user,
        "password": env_str("SMTP_PASSWORD"),
        "sender": sender,
        "to": to,
    }


def _header_value(value: str) -> str:
    """An HTTP header value latin-1 can carry, RFC 2047-encoded if it can't.

    ntfy takes the title and click URL as headers, and http.client encodes
    header values as latin-1, so an emoji title raises UnicodeEncodeError
    before the request is sent. ntfy decodes RFC 2047 encoded-words back to
    the original text. Only the body is exempt: it's sent as UTF-8.
    """
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return Header(value, "utf-8").encode()
    return value


def _names_summary(names: list[str]) -> str:
    """'A, B, C, D, E, and 3 more' -- the first NAMES_IN_PUSH, then a count."""
    shown = ", ".join(names[:NAMES_IN_PUSH])
    if len(names) > NAMES_IN_PUSH:
        shown += f", and {len(names) - NAMES_IN_PUSH} more"
    return shown


def notify(title: str, message: str, priority: str = "default",
           url: str | None = None) -> None:
    """Send on every configured channel. Raises only if the alert reached
    none of them: a dead ntfy with a working email (or the reverse) is logged,
    because the message got through."""
    ntfy_error = None
    ntfy_url = _ntfy_url()
    if ntfy_url is None:
        # Posting to the placeholder topic would publish restaurant names and
        # addresses to a feed anyone can subscribe to.
        logger.error("NTFY_TOPIC isn't set to a private topic name -- not sending "
                     "the push %r. Set NTFY_TOPIC in .env.", title)
    else:
        headers = {"Title": _header_value(title), "Priority": priority}
        if url:
            headers["Click"] = _header_value(url)
        try:
            _session.post(ntfy_url, data=message.encode("utf-8"), headers=headers,
                          timeout=10)
        except Exception as e:
            ntfy_error = e
            logger.exception("ntfy push failed")
    # The email subject is set through EmailMessage, which does its own
    # encoding -- so it gets the original title, not the wire-safe one.
    emailed = _send_email(title, message, url)
    if ntfy_error is not None and not emailed:
        raise ntfy_error


def _send_email(subject: str, message: str, url: str | None = None) -> bool:
    """Best effort. True if the email went out, False if it didn't or isn't
    configured."""
    try:
        cfg = _email_settings()
        if cfg is None:
            return False
        body = f"{message}\n\n{url}" if url else message
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = cfg["sender"]
        msg["To"] = cfg["to"]
        msg.set_content(body)
        # An explicit context: smtplib's default for starttls() doesn't verify
        # the server's certificate, which would hand the password to anyone
        # who can sit in the middle.
        context = ssl.create_default_context()
        if cfg["port"] == SMTP_SSL_PORT:
            server_cm: smtplib.SMTP = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=10,
                                         context=context)
        else:
            server_cm = smtplib.SMTP(cfg["host"], cfg["port"], timeout=10)
        with server_cm as server:
            if cfg["port"] != SMTP_SSL_PORT:
                server.starttls(context=context)
            if cfg["user"] and cfg["password"]:
                server.login(cfg["user"], cfg["password"])
            server.send_message(msg)
        return True
    except Exception:
        logger.exception("Email notification failed")
        return False


def notify_closed(restaurant: dict, status: str) -> None:
    notify(
        title=f"🚫 {restaurant['name']} is {PHRASES.get(status, 'closed')}",
        # `or ""`: the column is NULL for a place with no address, and .get()'s
        # default only covers a missing key, so None would print as "None".
        message=(restaurant.get("address") or "").strip() or "No address on file.",
        priority="high",
        url=restaurant.get("maps_url"),
    )


def notify_closing_soon(restaurant: dict, summary: str) -> None:
    notify(
        title=f"⚠️ {restaurant['name']} may be closing soon",
        message=summary or "Recent coverage suggests a closure is coming.",
        priority="default",
        url=restaurant.get("maps_url"),
    )


def notify_needs_verification(restaurants: list[dict]) -> None:
    """One push per run covering everything waiting to be verified.

    Deliberately batched rather than one push per restaurant: seeding a file
    of two hundred names would otherwise empty the phone's battery to say a
    single thing. The trade is that this re-fires every run while anything is
    unverified -- which is the point, since those restaurants aren't being
    checked at all until they're confirmed.
    """
    count = len(restaurants)
    notify(
        title=f"📍 {count} restaurant{'s' if count != 1 else ''} to verify",
        message=f"{_names_summary([r['name'] for r in restaurants])}\n\nNot being "
                f"checked until you confirm each one is the right place on the "
                f"dashboard.",
        priority="default",
        url=_dashboard_url(),
    )


def notify_run_problems(failed: int, total: int, gone: list[dict]) -> None:
    """One push when a run couldn't check some restaurants.

    A failure that is only logged looks, from the phone, exactly like a quiet
    week -- and a watcher that has stopped watching is the one thing this
    tool can't afford. `gone` is the subset Google answered 404 for: those
    will fail again every run until they're found again by hand.
    """
    parts = [f"{failed} of {total} restaurant checks failed -- see the log."]
    if gone:
        parts.append(f"Google no longer recognises: "
                     f"{_names_summary([r['name'] for r in gone])}. Delete and "
                     "re-add them on the dashboard.")
    notify(
        title=f"⚠️ {failed} check{'s' if failed != 1 else ''} failed",
        message="\n\n".join(parts),
        # Every check failing points at the key or the network, not a place.
        priority="high" if failed == total else "default",
        url=_dashboard_url(),
    )


def notify_scheduler_gap(days: int) -> None:
    """The scheduler has just started after going `days` without a completed
    run. Nothing else says so: a watcher that stopped looks like a quiet week.
    This can only fire once it's back, so an always-on external monitor
    (HEALTHCHECK_URL, see `ping_healthcheck`) is what catches it while down."""
    notify(
        title="⏰ Restaurant checks were not running",
        message=f"No check had completed for {days} days before this start. "
                "Closures in that time were not noticed until now.",
        priority="high",
        url=_dashboard_url(),
    )


def ping_healthcheck(ok: bool = True) -> None:
    """Tell an external dead-man's-switch (healthchecks.io, Uptime Kuma push
    monitor, ...) that a run finished. If the pings stop, *it* alerts -- the
    one failure this process can't report on itself is being dead. No-op unless
    HEALTHCHECK_URL is set; `ok=False` hits the `/fail` endpoint. Best effort,
    like email: a monitor being down must not fail a run."""
    url = env_str("HEALTHCHECK_URL")
    if not url:
        return
    try:
        _session.post(url.rstrip("/") + ("" if ok else "/fail"), timeout=10)
    except Exception:
        logger.exception("Healthcheck ping failed")
