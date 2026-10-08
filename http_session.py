"""One place to build the retrying `requests` session that places_client and
notifier both need, so the retry policy can't drift between them."""
from collections.abc import Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def retrying_session(methods: Iterable[str]) -> requests.Session:
    """A session that retries `methods` up to 3 times on 429/5xx and on
    connection errors, with exponential backoff."""
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=tuple(methods),
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session
