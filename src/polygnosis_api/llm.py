"""OpenAI-compatible chat completions client (AI Gateway / LexGateway / OpenAI)."""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from typing import Any

import httpx

from polygnosis_api.config import Settings

logger = logging.getLogger("polygnosis_api.llm")

# HTTP status codes worth retrying: rate limiting and transient server errors.
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503})

# Default ceiling on concurrent in-flight LLM HTTP calls across the process.
DEFAULT_MAX_LLM_CONCURRENCY = 8

# Process-global semaphore bounding concurrent LLM calls. Lazily created on
# first use so the concurrency value can come from Settings (Owner C adds
# ``max_llm_concurrency`` in parallel; we read it defensively via getattr).
_semaphore: threading.Semaphore | None = None
_semaphore_lock = threading.Lock()


def _get_semaphore(max_concurrency: int) -> threading.Semaphore:
    """Return the process-global LLM concurrency semaphore, creating it once."""
    global _semaphore
    if _semaphore is None:
        with _semaphore_lock:
            if _semaphore is None:
                _semaphore = threading.Semaphore(max(1, max_concurrency))
    return _semaphore


def extract_json(text: str) -> str:
    """Salvage the best JSON string candidate from text with fences or prose.

    Strategy, in order:
      1. Strip a surrounding markdown code fence if present.
      2. If the (fence-stripped) text parses as JSON whole, return it.
      3. Try ``json.JSONDecoder().raw_decode`` from the first ``{`` and return
         exactly the substring it accepts (handles trailing prose / multiple
         top-level objects).
      4. Fall back to the outermost brace slice; the caller still runs
         ``json.loads`` on the result.
    """
    text = text.strip()
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    # Whole thing is already valid JSON — nothing to salvage.
    try:
        json.loads(text)
        return text
    except (ValueError, TypeError):
        pass

    start = text.find("{")
    if start != -1:
        decoder = json.JSONDecoder()
        try:
            _obj, end = decoder.raw_decode(text[start:])
            return text[start : start + end]
        except ValueError:
            pass

        # Last resort: outermost brace slice.
        last = text.rfind("}")
        if last != -1 and last > start:
            return text[start : last + 1]

    return text


class LLMClient:
    """Thin sync chat client against an OpenAI-compatible /chat/completions API."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        if not self.settings.api_key:
            logger.warning("POLYGNOSIS_API_KEY is empty — LLM calls will fail until set")
        # Pooled client reused across complete() calls; timeout is applied
        # per-request so a single pool can serve calls with different budgets.
        self._client = httpx.Client()

    def close(self) -> None:
        """Close the pooled HTTP client. Safe to call more than once."""
        self._client.close()

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

    def _max_concurrency(self) -> int:
        # Settings may not carry max_llm_concurrency yet (Owner C adds it in
        # parallel); fall back to the frozen default of 8.
        return int(
            getattr(self.settings, "max_llm_concurrency", DEFAULT_MAX_LLM_CONCURRENCY)
        )

    def complete(
        self,
        prompt: str,
        model: str | None = None,
        *,
        temperature: float = 0.3,
        max_tokens: int | None = None,
        timeout: float = 300.0,
        label: str = "llm",
    ) -> str:
        """Synchronous chat completion with retries. Returns text or "".

        Retries up to ``settings.llm_max_retries`` times on timeout, connect
        error, and HTTP 429/500/502/503 with exponential backoff (1s, 2s, 4s)
        plus small jitter. Non-retryable HTTP errors (e.g. 4xx other than 429)
        return "" immediately. Returns "" once retries are exhausted.

        ``max_tokens`` is forwarded in the request payload only when set.
        A process-global semaphore bounds concurrent in-flight HTTP attempts.
        """
        model_id = model or self.settings.default_model
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        max_retries = self._max_retries()
        # Total attempts = 1 initial try + max_retries retries.
        total_attempts = max_retries + 1
        semaphore = _get_semaphore(self._max_concurrency())

        for attempt in range(1, total_attempts + 1):
            try:
                logger.info(
                    "[%s] calling %s (attempt %d/%d)",
                    label,
                    model_id,
                    attempt,
                    total_attempts,
                )
                # Bound concurrency around the actual HTTP attempt only, so the
                # slot is released during backoff sleeps between retries.
                with semaphore:
                    resp = self._client.post(
                        self._url(),
                        headers=self._headers(),
                        json=payload,
                        timeout=timeout,
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
