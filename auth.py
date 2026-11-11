"""Accounts, login, sign-up and per-IP rate limits for the dashboard.

Threat model: a small set of trusted people reaching the dashboard from any
device (a phone, typically over HTTPS behind a reverse proxy or tunnel). Every
page needs a login, and accounts live on the server, so one account works on
every device. Each account has its own restaurant list; nobody sees another's.
Anyone who can create an account can press the buttons that spend money (the
daily budgets are shared by everyone), so sign-up takes a code
(`DASHBOARD_SIGNUP_CODE`) or is switched off (`DASHBOARD_SIGNUP=off`) before the
server will listen beyond loopback (see `require_safe_binding`).

The limiters are in-process memory: they reset on restart and aren't shared
between processes. That's right for a one-process dashboard, and a restart is
not a way around them that matters -- the spend budget in the database
(`db.spend_budget`) is the durable cap; these limits only slow guessing and
button-mashing down.
"""
import hmac
import ipaddress
import re
import threading
import time
from collections import deque
from collections.abc import Callable

from werkzeug.security import check_password_hash, generate_password_hash

import db
from config import env_str

# Failed logins tolerated per client IP inside the window.
LOGIN_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def _hash_ok(stored: str, password: str) -> bool:
    try:
        return check_password_hash(stored, password)
    except ValueError:  # a malformed hash must fail closed, not 500
        return False


# A real hash to check against when the username doesn't exist, so an unknown
# username costs the same time as a wrong password.
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def authenticate(username: str, password: str) -> dict | None:
    """The account these credentials belong to, or None."""
    user = db.get_user_by_name(username.strip()) if username.strip() else None
    ok = _hash_ok(user["password_hash"] if user else _DUMMY_HASH, password)
    return user if (user and ok) else None


def has_login() -> bool:
    """Whether anyone could sign in right now."""
    return db.count_users() > 0


# --- sign-up ----------------------------------------------------------------

USERNAME_RE = re.compile(r"[A-Za-z0-9_.-]{3,32}")
MIN_PASSWORD_CHARS = 8
SIGNUP_ATTEMPTS = 10
SIGNUP_WINDOW_SECONDS = 60 * 60


def signup_mode() -> str:
    """'closed' (`DASHBOARD_SIGNUP=off`), 'code' (`DASHBOARD_SIGNUP_CODE` set:
    sign-up needs it) or 'open'."""
    if (env_str("DASHBOARD_SIGNUP", "") or "").lower() in ("off", "0", "false", "no"):
        return "closed"
    return "code" if env_str("DASHBOARD_SIGNUP_CODE") else "open"


def signup_code_ok(candidate: str) -> bool:
    code = env_str("DASHBOARD_SIGNUP_CODE")
    return code is None or hmac.compare_digest(code.encode(), candidate.encode())


def create_account(username: str, password: str, confirm: str) -> tuple[int | None, str | None]:
    """Make an account. Returns (user id, None) on success, or (None, a message
    for the person)."""
    username = username.strip()
    if not USERNAME_RE.fullmatch(username):
        return None, "Usernames are 3-32 letters, numbers, dots, dashes or underscores."
    if len(password) < MIN_PASSWORD_CHARS:
        return None, f"Use a password of at least {MIN_PASSWORD_CHARS} characters."
    if password != confirm:
        return None, "Those passwords didn't match."
    user_id = db.create_user(username, hash_password(password))
    if user_id is None:
        return None, "That username is taken."
    return user_id, None


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_safe_binding(host: str) -> None:
    """Refuse to listen beyond loopback while anyone could walk in.

    That's open sign-up (anyone who reaches the port makes an account), or
    sign-up closed with no account to sign in with. Raised rather than warned:
    the whole point of this check is the case where nobody is reading the log.
    """
    if is_loopback(host):
        return
    mode = signup_mode()
    if mode == "open":
        raise SystemExit(
            f"Refusing to listen on {host} with open sign-up. Set DASHBOARD_SIGNUP_CODE "
            "in .env (or DASHBOARD_SIGNUP=off once you have an account), or bind to 127.0.0.1.")
    if mode == "closed" and not has_login():
        raise SystemExit(
            f"Refusing to listen on {host}: sign-up is off and there is no account. Create one "
            "first (`python app.py create-user`, or run once on 127.0.0.1).")


class RateLimiter:
    """Sliding-window counter per key. `hit()` records an attempt and says
    whether it's within the limit; a refused attempt isn't recorded, so a
    client that keeps hammering recovers as soon as its oldest hit ages out."""

    def __init__(self, limit: int, window_seconds: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _trim(self, key: str, now: float) -> deque[float]:
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        return hits

    def hit(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        with self._lock:
            now = self._clock()
            hits = self._trim(key, now)
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            if len(self._hits) > 1024:  # bound memory against many distinct IPs
                self._sweep(now)
            return True

    def blocked(self, key: str) -> bool:
        """Whether `key` is at its limit, without recording anything."""
        if self.limit <= 0:
            return False
        with self._lock:
            return len(self._trim(key, self._clock())) >= self.limit

    def _sweep(self, now: float) -> None:
        for key in [k for k in self._hits if not self._trim(k, now)]:
            del self._hits[key]
