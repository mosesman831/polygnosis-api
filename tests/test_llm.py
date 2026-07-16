"""Unit tests for the retrying LLMClient and extract_json helper."""

from __future__ import annotations

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
    """Context-manager client that pops queued responses/exceptions per post()."""

    def __init__(self, outcomes: list):
        self._outcomes = outcomes

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, *args, **kwargs):
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _install_fake_client(monkeypatch, outcomes: list) -> None:
    """Patch httpx.Client so each construction serves the shared outcome queue."""

    def _factory(*args, **kwargs):
        return _FakeClient(outcomes)

    monkeypatch.setattr(httpx, "Client", _factory)
    # Never actually sleep during backoff.
    monkeypatch.setattr("polygnosis_api.llm.time.sleep", lambda *_: None)


def _client() -> LLMClient:
    return LLMClient(Settings(api_key="test-key"))


def test_extract_json_unchanged_fenced():
    assert extract_json("```json\n{\"a\": 1}\n```") == '{"a": 1}'


def test_extract_json_unchanged_braces():
    assert extract_json('prose {"a": 1} more') == '{"a": 1}'


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


class _FakeSettings:
    """Lightweight settings carrying the llm_max_retries knob (Owner A adds it)."""

    api_key = "test-key"
    api_base_url = "https://example.test/v1"
    default_model = "test-model"
    llm_max_retries = 1


def test_respects_custom_max_retries(monkeypatch):
    client = LLMClient(_FakeSettings())

    # 1 initial + 1 retry = 2 attempts; both fail → "".
    outcomes = [_FakeResponse(500) for _ in range(2)]
    _install_fake_client(monkeypatch, outcomes)

    assert client.complete("hi") == ""
    assert outcomes == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
