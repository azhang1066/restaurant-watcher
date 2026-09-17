"""Push notifications via ntfy.sh (same service the reservation notifier
uses, so this can share a topic/setup if you want one phone feed for both)
and, optionally, email via SMTP.
"""
import logging
import os
import smtplib
from email.message import EmailMessage

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

DEFAULT_NTFY_TOPIC = "restaurant-watcher-changeme"

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


def notify(title, message, priority="default", url=None):
    headers = {"Title": title, "Priority": priority}
    if url:
        headers["Click"] = url
    _session.post(_ntfy_url(), data=message.encode("utf-8"), headers=headers, timeout=10)
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
