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

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "restaurant-watcher-changeme")
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
EMAIL_FROM = os.environ.get("EMAIL_FROM", SMTP_USER)
EMAIL_TO = os.environ.get("EMAIL_TO")
EMAIL_ENABLED = bool(SMTP_HOST and EMAIL_FROM and EMAIL_TO)

_session = requests.Session()
_retry = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=("POST",),
)
_session.mount("https://", HTTPAdapter(max_retries=_retry))


def notify(title, message, priority="default", url=None):
    headers = {"Title": title, "Priority": priority}
    if url:
        headers["Click"] = url
    _session.post(NTFY_URL, data=message.encode("utf-8"), headers=headers, timeout=10)
    _send_email(title, message, url)


def _send_email(subject, message, url=None):
    if not EMAIL_ENABLED:
        return
    body = f"{message}\n\n{url}" if url else message
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            if SMTP_USER and SMTP_PASSWORD:
                server.login(SMTP_USER, SMTP_PASSWORD)
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
