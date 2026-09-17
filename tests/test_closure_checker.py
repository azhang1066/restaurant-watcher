"""Tests for closure_checker's JSON-extraction fallback -- the model is asked
for strict JSON but sometimes wraps it in prose, so this defensively pulls
out the {...} substring and must degrade to a safe default rather than
raising when that still fails.

Also pins the client's retry/timeout configuration. That config used to be
whatever the anthropic SDK defaulted to, which was quietly out of step with
the urllib3 Retry adapters in places_client/notifier -- the point of the
tests below is that changing it has to be deliberate.
"""
import pytest
from types import SimpleNamespace

import anthropic

import closure_checker


class _FakeMessages:
    def __init__(self, response_text, raises=None):
        self._response_text = response_text
        self._raises = raises

    def create(self, **kwargs):
        if self._raises is not None:
            raise self._raises
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._response_text)]
        )


class _FakeAnthropic:
    def __init__(self, response_text, raises=None):
        self.messages = _FakeMessages(response_text, raises)


def _patch_client(monkeypatch, response_text, raises=None):
    monkeypatch.setattr(
        closure_checker, "_client", lambda: _FakeAnthropic(response_text, raises)
    )


def test_parses_clean_json(monkeypatch):
    _patch_client(
        monkeypatch,
        '{"closing_soon": true, "confidence": "high", "summary": "Closing March 1st."}',
    )
    result = closure_checker.check_closing_soon("Lilia", "Brooklyn NY")
    assert result == {
        "closing_soon": True,
        "confidence": "high",
        "summary": "Closing March 1st.",
    }


def test_extracts_json_wrapped_in_prose(monkeypatch):
    _patch_client(
        monkeypatch,
        'Here is my answer:\n{"closing_soon": false, "confidence": "low", "summary": ""}\nHope that helps!',
    )
    result = closure_checker.check_closing_soon("Lilia", "Brooklyn NY")
    assert result == {"closing_soon": False, "confidence": "low", "summary": ""}


def test_no_braces_falls_back_to_default(monkeypatch):
    _patch_client(monkeypatch, "I could not find any relevant news.")
    result = closure_checker.check_closing_soon("Lilia", "Brooklyn NY")
    assert result == {"closing_soon": False, "confidence": "low", "summary": ""}


def test_malformed_json_falls_back_to_default(monkeypatch):
    _patch_client(monkeypatch, '{"closing_soon": true, "confidence": }')
    result = closure_checker.check_closing_soon("Lilia", "Brooklyn NY")
    assert result == {"closing_soon": False, "confidence": "low", "summary": ""}


def test_missing_fields_default_within_parsed_json(monkeypatch):
    _patch_client(monkeypatch, '{"closing_soon": true}')
    result = closure_checker.check_closing_soon("Lilia", "Brooklyn NY")
    assert result == {"closing_soon": True, "confidence": "low", "summary": ""}


# --- client configuration ----------------------------------------------

def test_client_sets_retries_and_timeout_explicitly(monkeypatch):
    """The SDK's own defaults (2 retries, a 600s read timeout) are not what
    this caller wants: one hung request would stall the serial loop for ten
    minutes, and the rest of the repo retries 3 times."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    client = closure_checker._client()

    assert client.max_retries == closure_checker.MAX_RETRIES == 3
    assert client.timeout == closure_checker.TIMEOUT_SECONDS
    assert client.timeout < anthropic._constants.DEFAULT_TIMEOUT.read


def test_client_reads_api_key_at_call_time(monkeypatch):
    """Same reason as notifier/places_client: the key must be picked up
    however this module was imported relative to load_dotenv()."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "set-after-import")

    assert closure_checker._client().api_key == "set-after-import"


def test_api_failure_propagates_once_retries_are_spent(monkeypatch):
    """The SDK retries internally; what escapes it is a real failure, and it
    has to reach main.run_check rather than be papered over with a
    'no closure signal' result that would clear a live flag."""
    _patch_client(monkeypatch, "", raises=RuntimeError("API is down"))

    with pytest.raises(RuntimeError):
        closure_checker.check_closing_soon("Lilia", "Brooklyn NY")
