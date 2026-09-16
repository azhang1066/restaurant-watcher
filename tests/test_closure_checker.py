"""Tests for closure_checker's JSON-extraction fallback -- the model is asked
for strict JSON but sometimes wraps it in prose, so this defensively pulls
out the {...} substring and must degrade to a safe default rather than
raising when that still fails."""
from types import SimpleNamespace

import closure_checker


class _FakeMessages:
    def __init__(self, response_text):
        self._response_text = response_text

    def create(self, **kwargs):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._response_text)]
        )


class _FakeAnthropic:
    def __init__(self, response_text):
        self.messages = _FakeMessages(response_text)


def _patch_client(monkeypatch, response_text):
    monkeypatch.setattr(
        closure_checker.anthropic, "Anthropic", lambda: _FakeAnthropic(response_text)
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
