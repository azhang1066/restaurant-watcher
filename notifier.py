"""Push notifications via ntfy.sh -- same service the reservation notifier
uses, so this can share a topic/setup if you want one phone feed for both.
"""
import os
import requests

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "restaurant-watcher-changeme")
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"


def notify(title, message, priority="default", url=None):
    headers = {"Title": title, "Priority": priority}
    if url:
        headers["Click"] = url
    requests.post(NTFY_URL, data=message.encode("utf-8"), headers=headers, timeout=10)


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
