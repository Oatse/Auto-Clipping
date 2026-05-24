"""
processors/clip_finder/gemini_client.py — Gemini HTTP client with key + model rotation.

Pulls all networking concerns out of the detector so the detector can stay
focused on prompt orchestration. The client:

  - rotates through ``api_keys`` on 429 / 403 / timeout / 5xx / connection-error
  - falls back across ``model`` and ``fallback_models`` on HTTP 404 (model
    decommissioned / regional unavailability)
  - sends the API key via the ``x-goog-api-key`` HEADER (not query string),
    keeping it out of proxy / CDN access logs
  - applies a hard request timeout so a hung Gemini call never freezes the job
  - returns the first text candidate, or raises ClipFinderError when every
    (model × key) combination is exhausted
"""

from __future__ import annotations

from typing import Callable, Sequence

import httpx

from .clip_selection import ClipFinderError

LogFn = Callable[[str], None]

_DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_DEFAULT_TIMEOUT_SECONDS = 180.0


class GeminiClient:
    """Thin wrapper around generativelanguage.googleapis.com generateContent."""

    def __init__(
        self,
        api_keys: list[str],
        model: str = "gemini-3.5-flash",
        base_url: str = _DEFAULT_BASE,
        fallback_models: Sequence[str] | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ):
        if not api_keys:
            raise ValueError("GeminiClient requires at least one API key")
        self._api_keys = api_keys
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds

        # Build a deduplicated, ordered model list: primary first, fallbacks
        # tried only when the primary returns 404 (model decommissioned).
        seen: set[str] = set()
        ordered: list[str] = []
        for m in [model, *(fallback_models or [])]:
            if m and m not in seen:
                ordered.append(m)
                seen.add(m)
        self._models = ordered

    @property
    def num_keys(self) -> int:
        return len(self._api_keys)

    @property
    def models(self) -> list[str]:
        return list(self._models)

    async def generate(
        self,
        prompt: str,
        *,
        temperature: float = 0.3,
        max_output_tokens: int = 65536,
        log_fn: LogFn | None = None,
        log_label: str = "Gemini",
    ) -> str:
        """POST a single-turn prompt and return the text part of the first
        candidate.

        Rotates through (model × key) combinations, retrying on transient
        errors (429 / 403 / 5xx / timeout / connection error) and falling
        back to the next model on 404 (model unavailable).
        """
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_output_tokens,
                "responseMimeType": "application/json",
            },
        }

        last_error: str | None = None
        for model in self._models:
            url = f"{self._base_url}/{model}:generateContent"
            for idx, key in enumerate(self._api_keys):
                label = f"{log_label} model={model} Key #{idx + 1}"
                try:
                    async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                        response = await client.post(
                            url,
                            json=payload,
                            headers={"x-goog-api-key": key},
                        )

                    if response.status_code in (429, 403):
                        if log_fn:
                            log_fn(
                                f"{label} rate-limited (HTTP {response.status_code}), "
                                "trying next key..."
                            )
                        last_error = f"HTTP {response.status_code}"
                        continue

                    if response.status_code == 404:
                        # Model gone or unsupported — abandon this model and
                        # fall through to the next one in the chain.
                        if log_fn:
                            log_fn(
                                f"Gemini model '{model}' not available "
                                "(HTTP 404); falling back to next model"
                            )
                        last_error = f"Model '{model}' not found"
                        break  # break key loop, advance to next model

                    if response.status_code >= 500:
                        if log_fn:
                            log_fn(
                                f"{label} server error (HTTP {response.status_code}), "
                                "trying next key..."
                            )
                        last_error = f"HTTP {response.status_code}"
                        continue

                    if response.status_code != 200:
                        raise ClipFinderError(
                            f"Gemini API error (HTTP {response.status_code}): "
                            f"{response.text[:500]}"
                        )

                    result = response.json()
                    candidates = result.get("candidates", [])
                    if not candidates:
                        raise ClipFinderError("Gemini returned no candidates")

                    content = candidates[0].get("content", {})
                    parts = content.get("parts", [])
                    text = parts[0].get("text", "") if parts else ""
                    return text

                except (httpx.TimeoutException, httpx.ConnectError) as exc:
                    err_type = (
                        "timeout" if isinstance(exc, httpx.TimeoutException)
                        else "connection error"
                    )
                    if log_fn:
                        log_fn(f"{label} {err_type}, trying next key...")
                    last_error = f"{err_type}: {exc}"
                    continue

        raise ClipFinderError(
            f"All Gemini (model x key) combinations failed for {log_label}. "
            f"Tried {len(self._models)} model(s) x {len(self._api_keys)} key(s). "
            f"Last error: {last_error}"
        )


__all__ = ["GeminiClient"]
