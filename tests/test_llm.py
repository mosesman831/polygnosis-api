"""Unit tests for the retrying LLMClient and extract_json helper."""

from __future__ import annotations

import json
import threading

import httpx
import pytest

from polygnosis_api.config import Settings
from polygnosis_api.llm import LLMClient, extract_json


class _FakeResponse:
    """Minimal stand-in for httpx.Response used by LLMClient.complete."""

    def __init__(self, status_code: int, content: str = ""):
        self.status_code = status_code
        self._content = content
        # raise_for_status inspects .request/.response, so build a real one.
        self.request = httpx.Request("POST", "https://example.test/chat/completions")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=self.request,
                response=httpx.Response(self.status_code, request=self.request),
            )

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self._content}}]}


class _FakeClient:
    """Pooled client stand-in that pops queued responses/exceptions per post()."""

    def __init__(self, outcomes: list):
        self._outcomes = outcomes
        self.post_calls: list[dict] = []
        self.closed = False

    def post(self, *args, **kwargs):
        self.post_calls.append(kwargs)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self) -> None:
        self.closed = True


def _install_fake_client(monkeypatch, outcomes: list) -> list:
    """Patch httpx.Client so every construction serves the shared outcome queue.

    Returns a list that records each constructed ``_FakeClient`` so tests can
    assert on pool reuse (one construction) and captured post kwargs.
    """
    constructed: list[_FakeClient] = []

    def _factory(*args, **kwargs):
        client = _FakeClient(outcomes)
        constructed.append(client)
        return client

    monkeypatch.setattr(httpx, "Client", _factory)
    # Never actually sleep during backoff.
    monkeypatch.setattr("polygnosis_api.llm.time.sleep", lambda *_: None)
    return constructed


def _client() -> LLMClient:
    return LLMClient(Settings(api_key="test-key"))


def test_extract_json_unchanged_fenced():
    assert extract_json("```json\n{\"a\": 1}\n```") == '{"a": 1}'


def test_extract_json_unchanged_braces():
    assert extract_json('prose {"a": 1} more') == '{"a": 1}'


def test_extract_json_prose_then_json():
    text = 'Here is the result you asked for: {"answer": 42, "ok": true}. Thanks!'
    candidate = extract_json(text)
    assert json.loads(candidate) == {"answer": 42, "ok": True}


def test_extract_json_multiple_top_level_objects_takes_first():
    # Two adjacent objects: the outermost brace slice would be invalid JSON,
    # so raw_decode must return only the first complete object.
    candidate = extract_json('{"a": 1} {"b": 2}')
    assert json.loads(candidate) == {"a": 1}


def test_extract_json_nested_with_prose():
    text = 'noise before {"a": {"b": [1, 2]}, "c": 3} noise after }'
    candidate = extract_json(text)
    assert json.loads(candidate) == {"a": {"b": [1, 2]}, "c": 3}


def test_extract_json_fenced_with_trailing_prose():
    text = '```json\n{"x": 1} trailing junk\n```'
    candidate = extract_json(text)
    assert json.loads(candidate) == {"x": 1}


def test_retry_500_then_200_returns_content(monkeypatch):
    outcomes = [
        _FakeResponse(500),
        _FakeResponse(200, content="hello world"),
    ]
    _install_fake_client(monkeypatch, outcomes)

    result = _client().complete("hi")

    assert result == "hello world"
    assert outcomes == []  # both outcomes consumed


def test_exhausted_retries_returns_empty(monkeypatch):
    # 1 initial + 3 retries = 4 attempts, all 503.
    outcomes = [_FakeResponse(503) for _ in range(4)]
    _install_fake_client(monkeypatch, outcomes)

    result = _client().complete("hi")

    assert result == ""
    assert outcomes == []


def test_non_retryable_4xx_returns_empty_immediately(monkeypatch):
    outcomes = [_FakeResponse(400), _FakeResponse(200, content="never")]
    _install_fake_client(monkeypatch, outcomes)

    result = _client().complete("hi")

    assert result == ""
    # Only the first (400) outcome should be consumed; no retry.
    assert len(outcomes) == 1


def test_timeout_then_success(monkeypatch):
    req = httpx.Request("POST", "https://example.test/chat/completions")
    outcomes = [
        httpx.TimeoutException("boom", request=req),
        _FakeResponse(200, content="recovered"),
    ]
    _install_fake_client(monkeypatch, outcomes)

    result = _client().complete("hi")

    assert result == "recovered"


def test_pool_is_reused_across_calls(monkeypatch):
    outcomes = [
        _FakeResponse(200, content="one"),
        _FakeResponse(200, content="two"),
    ]
    constructed = _install_fake_client(monkeypatch, outcomes)

    client = _client()
    assert client.complete("a") == "one"
    assert client.complete("b") == "two"

    # A single pooled client should have been constructed and reused.
    assert len(constructed) == 1
    assert len(constructed[0].post_calls) == 2


def test_per_request_timeout_is_passed(monkeypatch):
    outcomes = [_FakeResponse(200, content="ok")]
    constructed = _install_fake_client(monkeypatch, outcomes)

    _client().complete("hi", timeout=12.5)

    assert constructed[0].post_calls[0]["timeout"] == 12.5


def test_close_closes_pooled_client(monkeypatch):
    outcomes = [_FakeResponse(200, content="ok")]
    constructed = _install_fake_client(monkeypatch, outcomes)

    client = _client()
    client.complete("hi")
    client.close()

    assert constructed[0].closed is True


def test_max_tokens_included_in_payload_when_set(monkeypatch):
    outcomes = [_FakeResponse(200, content="ok")]
    constructed = _install_fake_client(monkeypatch, outcomes)

    _client().complete("hi", max_tokens=256, temperature=0.7)

    payload = constructed[0].post_calls[0]["json"]
    assert payload["max_tokens"] == 256
    assert payload["temperature"] == 0.7


def test_max_tokens_omitted_when_none(monkeypatch):
    outcomes = [_FakeResponse(200, content="ok")]
    constructed = _install_fake_client(monkeypatch, outcomes)

    _client().complete("hi")

    payload = constructed[0].post_calls[0]["json"]
    assert "max_tokens" not in payload


def test_semaphore_does_not_break_retries(monkeypatch):
    # A size-1 semaphore must be released between attempts so retries proceed.
    monkeypatch.setattr("polygnosis_api.llm._semaphore", threading.Semaphore(1))
    outcomes = [
        _FakeResponse(503),
        _FakeResponse(500),
        _FakeResponse(200, content="through the gate"),
    ]
    _install_fake_client(monkeypatch, outcomes)

    result = _client().complete("hi")

    assert result == "through the gate"
    assert outcomes == []


class _ConcurrencySettings:
    """Settings variant carrying max_llm_concurrency (Owner C adds it)."""

    api_key = "test-key"
    api_base_url = "https://example.test/v1"
    default_model = "test-model"
    max_llm_concurrency = 3


def test_semaphore_reads_settings_concurrency(monkeypatch):
    # Global semaphore is created lazily from settings.max_llm_concurrency.
    monkeypatch.setattr("polygnosis_api.llm._semaphore", None)
    outcomes = [_FakeResponse(200, content="ok")]
    _install_fake_client(monkeypatch, outcomes)

    client = LLMClient(_ConcurrencySettings())
    assert client.complete("hi") == "ok"

    import polygnosis_api.llm as llm_mod

    # Value restored to 3 after acquire/release → initial value came from settings.
    assert llm_mod._semaphore._value == 3


class _FakeSettings:
    """Lightweight settings carrying the llm_max_retries knob (Owner A adds it)."""

    api_key = "test-key"
    api_base_url = "https://example.test/v1"
    default_model = "test-model"
    llm_max_retries = 1


def test_respects_custom_max_retries(monkeypatch):
    # 1 initial + 1 retry = 2 attempts; both fail → "".
    outcomes = [_FakeResponse(500) for _ in range(2)]
    _install_fake_client(monkeypatch, outcomes)

    client = LLMClient(_FakeSettings())

    assert client.complete("hi") == ""
    assert outcomes == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
