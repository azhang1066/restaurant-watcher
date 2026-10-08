"""Uses the Claude API with the web_search tool to check for recent news
signals that a restaurant is closing soon -- a lease ending, an owner
announcement, local press coverage, etc. This is a judgment call, not a
hard API field, so it's run less often than the Places status check
(every CLOSING_SOON_CHECK_EVERY runs) to keep cost and noise down.
"""
import json
from typing import Any

import anthropic

from config import env_str

# Default model and server-side tool version. Both are API identifiers that
# get retired, so the model can be overridden from `.env` (CLOSURE_CHECK_MODEL)
# without a code change, and the tool type has one definition to update.
MODEL = "claude-sonnet-5-5"
WEB_SEARCH_TOOL = "web_search_20250305"

# The SDK runs its own retry loop (exponential backoff, honors `retry-after`,
# covers connection/timeout errors plus 408/409/429/5xx), so there is no
# urllib3 Retry adapter to mount here the way places_client.py and notifier.py
# do -- but its defaults are wrong for this caller. Two retries is one fewer
# than everywhere else in the repo, and the 600s read timeout would stall the
# serial per-restaurant loop for ten minutes on a single hung request. Both
# are set explicitly below so the behaviour is visible rather than inherited.
MAX_RETRIES = 3
# Generous because a web_search turn does several searches server-side before
# answering, but bounded: a news check is the optional half of a run.
TIMEOUT_SECONDS = 120.0

# Headroom for the answer after the search turns. At 500 a long reply could be
# cut off mid-JSON, which has to be told apart from "no news".
MAX_TOKENS = 1500
# Server-side tools can hand the turn back (`pause_turn`) before the model has
# answered; the request is re-sent to let it continue, this many times at most.
MAX_CONTINUATIONS = 3

CONFIDENCES = ("low", "medium", "high")
# What check_one acts on; a "low" verdict is recorded but never alerts.
TRUSTED_CONFIDENCES = ("medium", "high")


class NewsCheckError(Exception):
    """The news check ran but produced no usable verdict (truncated, not JSON,
    or the wrong shape). Distinct from "no closure news": the caller must keep
    the stored closing-soon flag rather than clear it."""

PROMPT_TEMPLATE = """Search for recent news (last 90 days) about whether the restaurant
"{name}" at "{address}" is closing, has announced a closing date, or is otherwise
winding down (final week, lease not renewed, "closing after X years", etc.).

Respond with ONLY a JSON object, no other text, in this exact shape:
{{"closing_soon": true/false, "confidence": "low"/"medium"/"high", "summary": "one sentence, or empty string if closing_soon is false"}}
"""


def _model() -> str:
    """Read at call time for the same reason as `_client()`."""
    return env_str("CLOSURE_CHECK_MODEL", MODEL)


def _client() -> anthropic.Anthropic:
    """Built at call time, not import time, so `.env` lands however this module
    was imported -- see `places_client._api_key()` for the same pattern. Reads
    ANTHROPIC_API_KEY from the environment."""
    return anthropic.Anthropic(max_retries=MAX_RETRIES, timeout=TIMEOUT_SECONDS)


def check_closing_soon(name: str, address: str | None) -> dict:
    """Raises if the API is still failing once the SDK's retries are spent, or
    NewsCheckError if the reply can't be read as a verdict --
    `checker.check_one` treats both as a failed news check and keeps the
    stored flag, rather than reading silence as "not closing"."""
    client = _client()
    messages: list[Any] = [{"role": "user",
                 "content": PROMPT_TEMPLATE.format(name=name, address=address or "")}]
    tools: Any = [{"type": WEB_SEARCH_TOOL, "name": "web_search"}]
    for _ in range(MAX_CONTINUATIONS + 1):
        resp = client.messages.create(
            model=_model(),
            max_tokens=MAX_TOKENS,
            tools=tools,
            messages=messages,
        )
        if resp.stop_reason != "pause_turn":
            break
        messages = messages + [{"role": "assistant", "content": resp.content}]
    else:
        raise NewsCheckError("the model kept pausing without answering")

    if resp.stop_reason == "max_tokens":
        raise NewsCheckError("the reply was cut off at max_tokens")

    text_blocks = [b.text for b in resp.content if b.type == "text"]
    raw = "\n".join(text_blocks).strip()

    # Model sometimes wraps JSON in prose despite instructions; grab the {...}
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise NewsCheckError(f"no JSON object in the reply: {raw[:200]!r}")

    try:
        parsed = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        raise NewsCheckError(f"unparseable JSON in the reply: {e}") from e

    confidence = parsed.get("confidence", "low")
    summary = parsed.get("summary", "")
    return {
        "closing_soon": parsed.get("closing_soon", False) is True,
        "confidence": confidence if confidence in CONFIDENCES else "low",
        "summary": summary if isinstance(summary, str) else "",
    }
