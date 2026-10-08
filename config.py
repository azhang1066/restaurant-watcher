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
import os

from dotenv import load_dotenv

load_dotenv()

# Checks run weekly by default, so this long without one means the scheduler
# died rather than that nothing happened. Shared by the dashboard (Stale badge)
# and the scheduler (its "I was down" push on restart).
STALE_AFTER_DAYS = 14

logger = logging.getLogger(__name__)


def env_str(name, default=None):
    """The variable's value, or `default` when it's unset or empty."""
    return os.environ.get(name) or default


def env_int(name, default):
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


def configure_logging():
    """Give the root logger a handler for the entry points. Left alone when
    one already exists (tests, an embedding server, `flask run`'s own)."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
