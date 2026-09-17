"""Push notifications via ntfy.sh (same service the reservation notifier
uses, so this can share a topic/setup if you want one phone feed for both)
and, optionally, email via SMTP.
"""
import logging
import os
import smtplib
from email.header import Header
from email.message import EmailMessage

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

DEFAULT_NTFY_TOPIC = "restaurant-watcher-changeme"
DEFAULT_DASHBOARD_URL = "http://127.0.0.1:5000"

# How many names to spell out before the rest become a count. A push
# notification gets a couple of lines on a lock screen, and a seeded file
# can leave a hundred restaurants waiting at once.
NAMES_IN_VERIFY_PUSH = 5

_session = requests.Session()
_retry = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=("POST",),
)
_session.mount("https://", HTTPAdapter(max_retries=_retry))


def _ntfy_url():
    """Read at call time, not import time, so `.env` lands however this module
    was imported -- see `places_client._api_key()` for the same pattern."""
    return f"https://ntfy.sh/{os.environ.get('NTFY_TOPIC', DEFAULT_NTFY_TOPIC)}"


def _dashboard_url():
    """Where to send someone who taps the verify notification. Read at call
    time for the same reason as `_ntfy_url()`."""
    return os.environ.get("DASHBOARD_URL") or DEFAULT_DASHBOARD_URL


def _email_settings():
    """SMTP config, or None when email isn't configured (host/from/to are the
    required trio). Read at call time for the same reason as `_ntfy_url()`."""
    user = os.environ.get("SMTP_USER")
    host = os.environ.get("SMTP_HOST")
    sender = os.environ.get("EMAIL_FROM") or user
    to = os.environ.get("EMAIL_TO")
    if not (host and sender and to):
        return None
    return {
        "host": host,
        "port": int(os.environ.get("SMTP_PORT") or 587),
        "user": user,
        "password": os.environ.get("SMTP_PASSWORD"),
        "sender": sender,
        "to": to,
    }


def _header_value(value):
    """An HTTP header value latin-1 can carry, RFC 2047-encoded if it can't.

    ntfy takes the title and click URL as headers, and http.client encodes
    header values as latin-1 -- so the emoji in every title below raised
    UnicodeEncodeError before the request was ever sent, and the alert was
    lost. ntfy decodes RFC 2047 encoded-words back to the original text, so
    the phone still shows the emoji. Only the body is exempt: it's the
    request payload, sent as UTF-8.
    """
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return Header(value, "utf-8").encode()
    return value


def notify(title, message, priority="default", url=None):
    headers = {"Title": _header_value(title), "Priority": priority}
    if url:
        headers["Click"] = _header_value(url)
    _session.post(_ntfy_url(), data=message.encode("utf-8"), headers=headers, timeout=10)
    # The email subject is set through EmailMessage, which does its own
    # encoding -- so it gets the original title, not the wire-safe one.
    _send_email(title, message, url)


def _send_email(subject, message, url=None):
    # Everything below is best-effort: a misconfigured or unreachable mail
    # server must not take down the ntfy alert that already went out.
    try:
        cfg = _email_settings()
        if cfg is None:
            return
        body = f"{message}\n\n{url}" if url else message
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = cfg["sender"]
        msg["To"] = cfg["to"]
        msg.set_content(body)
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=10) as server:
            server.starttls()
            if cfg["user"] and cfg["password"]:
                server.login(cfg["user"], cfg["password"])
            server.send_message(msg)
    except Exception:
        logger.exception("Email notification failed")


def notify_closed(restaurant, status):
    label = "permanently closed" if status == "CLOSED_PERMANENTLY" else "temporarily closed"
    notify(
        title=f"🚫 {restaurant['name']} is {label}",
        message=f"{restaurant.get('address', '')}".strip() or "No address on file.",
        priority="high",
        url=restaurant.get("maps_url"),
    )


def notify_closing_soon(restaurant, summary):
    notify(
        title=f"⚠️ {restaurant['name']} may be closing soon",
        message=summary or "Recent coverage suggests a closure is coming.",
        priority="default",
        url=restaurant.get("maps_url"),
    )


def notify_needs_verification(restaurants):
    """One push per run covering everything waiting to be verified.

    Deliberately batched rather than one push per restaurant: seeding a file
    of two hundred names would otherwise empty the phone's battery to say a
    single thing. The trade is that this re-fires every run while anything is
    unverified -- which is the point, since those restaurants aren't being
    checked at all until they're confirmed.
    """
    count = len(restaurants)
    names = [r["name"] for r in restaurants]
    shown = ", ".join(names[:NAMES_IN_VERIFY_PUSH])
    if count > NAMES_IN_VERIFY_PUSH:
        shown += f", and {count - NAMES_IN_VERIFY_PUSH} more"
    notify(
        title=f"📍 {count} restaurant{'s' if count != 1 else ''} to verify",
        message=f"{shown}\n\nNot being checked until you confirm each one is the "
                f"right place on the dashboard.",
        priority="default",
        url=_dashboard_url(),
    )
