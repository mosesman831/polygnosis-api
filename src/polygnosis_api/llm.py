"""OpenAI-compatible chat completions client (AI Gateway / LexGateway / OpenAI)."""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any

import httpx

from polygnosis_api.config import Settings

logger = logging.getLogger("polygnosis_api.llm")

# HTTP status codes worth retrying: rate limiting and transient server errors.
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503})


def extract_json(text: str) -> str:
    """Salvage JSON from text that may contain markdown fences or prose."""
    text = text.strip()
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


class LLMClient:
    """Thin sync chat client against an OpenAI-compatible /chat/completions API."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        if not self.settings.api_key:
            logger.warning("POLYGNOSIS_API_KEY is empty — LLM calls will fail until set")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        }

    def _url(self) -> str:
        base = self.settings.api_base_url.rstrip("/")
        return f"{base}/chat/completions"

    def _max_retries(self) -> int:
        # Settings may not carry llm_max_retries yet (Owner A adds it in parallel);
        # fall back to the frozen default of 3.
        return int(getattr(self.settings, "llm_max_retries", 3))

    def complete(
        self,
        prompt: str,
        model: str | None = None,
        *,
        temperature: float = 0.3,
        timeout: float = 300.0,
        label: str = "llm",
    ) -> str:
        """Synchronous chat completion with retries. Returns text or "".

        Retries up to ``settings.llm_max_retries`` times on timeout, connect
        error, and HTTP 429/500/502/503 with exponential backoff (1s, 2s, 4s)
        plus small jitter. Non-retryable HTTP errors (e.g. 4xx other than 429)
        return "" immediately. Returns "" once retries are exhausted.
        """
        model_id = model or self.settings.default_model
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }
        max_retries = self._max_retries()
        # Total attempts = 1 initial try + max_retries retries.
        total_attempts = max_retries + 1

        for attempt in range(1, total_attempts + 1):
            try:
                logger.info(
                    "[%s] calling %s (attempt %d/%d)",
                    label,
                    model_id,
                    attempt,
                    total_attempts,
                )
                with httpx.Client(timeout=timeout) as client:
                    resp = client.post(
                        self._url(), headers=self._headers(), json=payload
                    )
                    resp.raise_for_status()
                    data = resp.json()
                content = data["choices"][0]["message"]["content"]
                return (content or "").strip()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status not in RETRYABLE_STATUS_CODES:
                    # Non-retryable HTTP errors (e.g. 4xx except 429): give up now.
                    logger.error(
                        "[%s] non-retryable HTTP %d: %s", label, status, exc
                    )
                    return ""
                logger.warning(
                    "[%s] retryable HTTP %d on attempt %d/%d",
                    label,
                    status,
                    attempt,
                    total_attempts,
                )
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                logger.warning(
                    "[%s] %s on attempt %d/%d: %s",
                    label,
                    type(exc).__name__,
                    attempt,
                    total_attempts,
                    exc,
                )
            except Exception as exc:  # noqa: BLE001 — pipeline must degrade gracefully
                # Unexpected/non-retryable error: log and give up.
                logger.error("[%s] LLM error: %s", label, exc)
                return ""

            if attempt < total_attempts:
                # Exponential backoff: 1s, 2s, 4s, ... plus a little jitter.
                backoff = 2.0 ** (attempt - 1)
                sleep_for = backoff + random.uniform(0, 0.25)
                logger.info(
                    "[%s] sleeping %.2fs before retry %d/%d",
                    label,
                    sleep_for,
                    attempt + 1,
                    total_attempts,
                )
                time.sleep(sleep_for)

        logger.error("[%s] giving up after %d attempts", label, total_attempts)
        return ""
