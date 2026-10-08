"""Environment configuration, loaded once.

Importing this module loads `.env`, so every other module gets the same
environment however it was imported. Values are still read at call time (the
helpers below), not cached here, so a test or a long-running process sees the
current environment.

A malformed number falls back to its default with a warning rather than
raising: these are read mid-run, and a typo in `.env` shouldn't abort a
weekly check halfway through the list.
"""
import logging
import logging.handlers
import os
from pathlib import Path
from typing import overload

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Checks run weekly by default, so this long without one means the scheduler
# died rather than that nothing happened. Shared by the dashboard (Stale badge)
# and the scheduler (its "I was down" push on restart).
STALE_AFTER_DAYS = 14

# Where the dashboard listens (localhost only: there's no auth, and the DB
# holds a personal list), and so where a notification should point.
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 5000
DEFAULT_DASHBOARD_URL = f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}"

DEFAULT_LOG_FILE = Path(__file__).parent / "data" / "restaurant-watcher.log"
LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
LOG_FILE_BYTES = 1_000_000
LOG_FILES_KEPT = 5


@overload
def env_str(name: str) -> str | None: ...
@overload
def env_str(name: str, default: str) -> str: ...
def env_str(name: str, default: str | None = None) -> str | None:
    """The variable's value, or `default` when it's unset or empty."""
    return os.environ.get(name) or default


def env_int(name: str, default: int) -> int:
    """The variable as an int, or `default` when it's unset, empty or not a
    number (the last is logged)."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r isn't a whole number -- using the default, %s.",
                       name, raw, default)
        return default


def configure_logging() -> None:
    """Give the root logger handlers for the entry points: stderr, plus a
    rotating file (LOG_FILE, `off` to disable) so a scheduler run under cron or
    Task Scheduler leaves a trail. Left alone when handlers already exist
    (tests, an embedding server, `flask run`'s own)."""
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    target = env_str("LOG_FILE", str(DEFAULT_LOG_FILE))
    if target.lower() == "off":
        return
    try:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            target, maxBytes=LOG_FILE_BYTES, backupCount=LOG_FILES_KEPT, encoding="utf-8")
    except OSError:
        logger.exception("Couldn't open the log file %s -- logging to stderr only", target)
        return
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(handler)
