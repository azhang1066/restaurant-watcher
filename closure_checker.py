"""Uses the Claude API with the web_search tool to check for recent news
signals that a restaurant is closing soon -- a lease ending, an owner
announcement, local press coverage, etc. This is a judgment call, not a
hard API field, so it's run less often than the Places status check
(e.g. monthly, not weekly) to keep cost and noise down.
"""
import os
import json
import anthropic

MODEL = "claude-sonnet-4-6"

PROMPT_TEMPLATE = """Search for recent news (last 90 days) about whether the restaurant
"{name}" at "{address}" is closing, has announced a closing date, or is otherwise
winding down (final week, lease not renewed, "closing after X years", etc.).

Respond with ONLY a JSON object, no other text, in this exact shape:
{{"closing_soon": true/false, "confidence": "low"/"medium"/"high", "summary": "one sentence, or empty string if closing_soon is false"}}
"""


def check_closing_soon(name, address):
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    resp = client.messages.create(
        model=MODEL,
        max_tokens=500,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": PROMPT_TEMPLATE.format(name=name, address=address or "")}],
    )

    text_blocks = [b.text for b in resp.content if b.type == "text"]
    raw = "\n".join(text_blocks).strip()

    # Model sometimes wraps JSON in prose despite instructions; grab the {...}
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        return {"closing_soon": False, "confidence": "low", "summary": ""}

    try:
        parsed = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {"closing_soon": False, "confidence": "low", "summary": ""}

    return {
        "closing_soon": bool(parsed.get("closing_soon", False)),
        "confidence": parsed.get("confidence", "low"),
        "summary": parsed.get("summary", ""),
    }
