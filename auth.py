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
import hashlib
import hmac
import ipaddress
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

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
VERIFY_REQUESTS = 10
RESET_REQUESTS = 5
RESET_WINDOW_SECONDS = 60 * 60


def signup_mode() -> str:
    """'closed' (`DASHBOARD_SIGNUP=off`), 'code' (`DASHBOARD_SIGNUP_CODE` set:
    sign-up needs it) or 'open'."""
    if (env_str("DASHBOARD_SIGNUP", "") or "").lower() in ("off", "0", "false", "no"):
        return "closed"
    return "code" if env_str("DASHBOARD_SIGNUP_CODE") else "open"


def signup_code_ok(candidate: str) -> bool:
    code = env_str("DASHBOARD_SIGNUP_CODE")
    return code is None or hmac.compare_digest(code.encode(), candidate.encode())


EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
EMAIL_TAKEN = "That email is already on another account."


def clean_email(raw: str) -> tuple[str | None, str | None]:
    """(address or None when blank, a message for the person if it's malformed)."""
    email = raw.strip().lower()
    if not email:
        return None, None
    if len(email) > 254 or not EMAIL_RE.fullmatch(email):
        return None, "That doesn't look like an email address."
    return email, None


def create_account(username: str, password: str, confirm: str, email: str = "",
                   verified: bool = False) -> tuple[int | None, str | None]:
    """Make an account. An email is required. With `verified` (the terminal,
    where the person at the keyboard is trusted) it's stored as confirmed;
    otherwise the account starts with none, and the caller sends a confirmation
    link (`start_verification`) -- the address only lands on the account when
    that link is used. Returns (user id, None), or (None, a message)."""
    username = username.strip()
    if not USERNAME_RE.fullmatch(username):
        return None, "Usernames are 3-32 letters, numbers, dots, dashes or underscores."
    if error := password_problem(password, confirm):
        return None, error
    address, error = clean_email(email)
    if error:
        return None, error
    if address is None:
        return None, "An email address is required."
    if db.get_user_by_email(address):
        return None, EMAIL_TAKEN
    user_id = db.create_user(username, hash_password(password),
                             address if verified else None, verified)
    if user_id is None:
        return None, "That username is taken."
    return user_id, None


def password_problem(password: str, confirm: str) -> str | None:
    """Why this isn't an acceptable new password, or None."""
    if len(password) < MIN_PASSWORD_CHARS:
        return f"Use a password of at least {MIN_PASSWORD_CHARS} characters."
    if password != confirm:
        return "Those passwords didn't match."
    return None


# --- changing a password, resetting one, removing an account -------------------

WRONG_PASSWORD = "Your current password isn't right."
NO_SUCH_USER = "There's no account with that username."


def session_stamp(password_hash: str) -> str:
    """What a session carries to prove it was opened under the *current* password.
    Derived from the stored hash (salt included), so changing or resetting the
    password signs out every other device without a session table."""
    return hashlib.sha256(password_hash.encode()).hexdigest()[:32]


def change_password(user_id: int, current: str, new: str, confirm: str) -> str | None:
    """Set a new password after checking the current one. Returns a message for
    the person on failure, None on success."""
    user = db.get_user(user_id)
    if user is None or not _hash_ok(user["password_hash"], current):
        return WRONG_PASSWORD
    if error := password_problem(new, confirm):
        return error
    db.set_password_hash(user_id, hash_password(new))
    return None


def reset_password(username: str, new: str, confirm: str) -> str | None:
    """Set a password without knowing the old one -- the reset for someone who
    has forgotten theirs. Only reachable from the server's terminal
    (`python app.py reset-password`), since there is no per-account email to
    send a link to. Returns an error message, or None on success."""
    user = db.get_user_by_name(username.strip())
    if user is None:
        return NO_SUCH_USER
    if error := password_problem(new, confirm):
        return error
    db.set_password_hash(user["id"], hash_password(new))
    return None


# --- reset by email ---------------------------------------------------------------


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_reset(identifier: str) -> tuple[dict, str] | None:
    """Start a reset for the account with this username or email, if it has an
    email to send to. Returns (account, the link's secret token) for the caller
    to mail, or None. The caller must answer the same either way, so nobody can
    use the form to learn which usernames or addresses exist."""
    identifier = identifier.strip()
    if not identifier:
        return None
    user = db.get_user_by_email(identifier.lower()) if "@" in identifier \
        else db.get_user_by_name(identifier)
    # Only a confirmed address: one nobody has proven is theirs could be anyone's.
    if user is None or not user.get("email") or not user.get("email_verified_at"):
        return None
    token = secrets.token_urlsafe(32)
    db.create_reset(_token_hash(token), user["id"])
    return user, token


def reset_link_valid(token: str) -> bool:
    return db.peek_reset(_token_hash(token)) is not None


def finish_reset(token: str, new: str, confirm: str) -> str | None:
    """Set a new password using a reset link, which is then used up. A bad
    password is refused *before* the link is spent, so a typo doesn't cost a new
    email. Returns a message for the person, or None on success."""
    if error := password_problem(new, confirm):
        return error
    user_id = db.consume_reset(_token_hash(token))
    if user_id is None:
        return RESET_EXPIRED
    db.set_password_hash(user_id, hash_password(new))
    return None


RESET_EXPIRED = "That reset link has expired or was already used. Ask for a new one."


def start_verification(user_id: int, raw: str) -> tuple[str | None, str | None, str | None]:
    """Begin confirming `raw` as this account's email. Returns (address, token,
    None) for the caller to mail, or (None, None, a message for the person). The
    account keeps its current email until the link is used."""
    address, error = clean_email(raw)
    if error:
        return None, None, error
    if address is None:
        return None, None, "An email address is required."
    owner = db.get_user_by_email(address)
    if owner and owner["id"] != user_id:
        return None, None, EMAIL_TAKEN
    token = secrets.token_urlsafe(32)
    db.create_verification(_token_hash(token), user_id, address)
    return address, token, None


def verification_pending(token: str) -> dict | None:
    return db.peek_verification(_token_hash(token))


def confirm_email(token: str) -> str:
    """'ok', 'invalid' or 'taken' (see db.confirm_email)."""
    return db.confirm_email(_token_hash(token))


def change_email(user_id: int, current: str, raw: str) -> tuple[str | None, str | None, str | None]:
    """Start moving the account to a new email, after the current password.
    Same return as `start_verification`."""
    user = db.get_user(user_id)
    if user is None or not _hash_ok(user["password_hash"], current):
        return None, None, WRONG_PASSWORD
    return start_verification(user_id, raw)


# --- TLS -------------------------------------------------------------------------


def tls_files() -> tuple[Path, Path] | None:
    """The certificate and key to serve HTTPS with (`DASHBOARD_TLS_CERT` and
    `DASHBOARD_TLS_KEY`), or None when TLS is left to a proxy. Setting one
    without the other, or naming a file that isn't there, is an error: a typo
    must not quietly fall back to serving passwords in the clear."""
    cert, key = env_str("DASHBOARD_TLS_CERT"), env_str("DASHBOARD_TLS_KEY")
    if not cert and not key:
        return None
    if not (cert and key):
        raise SystemExit("Set both DASHBOARD_TLS_CERT and DASHBOARD_TLS_KEY, or neither.")
    paths = (Path(cert), Path(key))
    for path in paths:
        if not path.is_file():
            raise SystemExit(f"TLS file not found: {path}")
    return paths


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
