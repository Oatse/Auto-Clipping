"""
processors/clip_finder/nine_router_client.py — 9router (Kiro Pro) HTTP client.

Drop-in alternative to :class:`.gemini_client.GeminiClient` used when the
user picks a Kiro-routed model on the Clip Finder workspace. Exposes
exactly one public method — ``generate(prompt, *, temperature,
max_output_tokens, log_fn, log_label) -> str`` — matching the Gemini
client signature byte-for-byte so ``ClipDetector``, ``HunterRunner``,
and ``ClipScorer`` can swap backends transparently.

Backend choice is the LLM transport only. Detection prompts, scoring
rubric, boundary refinement, and rendering stay unchanged — same
Moments, same Clips, just a different model deciding which segments
matter.

The endpoint is the OpenAI-compatible ``/v1/chat/completions`` route on
9router (Kiro Pro proxy default). System instruction is pinned to the
Gemini detector's expectation: "respond with strict JSON only, no
prose, no code fences" — which lets the existing ``parse_candidates_json``
salvage path work without changes.
"""

from __future__ import annotations

import re
from typing import Callable

import httpx

import config

from .clip_selection import ClipFinderError

LogFn = Callable[[str], None]

_DEFAULT_TIMEOUT_SECONDS = 300.0  # Detection prompts are large; +Thinking is slow.

_SYSTEM_INSTRUCTION = (
    "You are a video clip detection assistant. Respond ONLY with the "
    "JSON value the user asked for — a JSON array or a JSON object — "
    "and nothing else. Do not wrap the JSON in markdown code fences. "
    "Do not add any prose, explanations, or trailing commentary. The "
    "first character of your reply must be '[' or '{'."
)


def _extract_upstream_error(response: httpx.Response) -> str:
    """Pull the human-readable message out of a 9router error body.

    9router wraps the *upstream provider's* error (Kiro/Codex session
    expired, rate-limited, etc.) in ``{"error": {"message": "..."}}``.
    That nested message is what actually explains a 401/403 — the
    bearer token in ``Authorization`` can be perfectly valid for 9router
    itself while the provider session behind it has expired.
    """
    try:
        body = response.json()
        message = body.get("error", {}).get("message")
        if message:
            return str(message).strip()
    except Exception:
        pass
    return response.text[:300] or f"HTTP {response.status_code}"


def _strip_code_fence(raw_text: str) -> str:
    """Trim ```json ... ``` fences and trailing commas.

    Some routed models — especially +Thinking variants — wrap JSON in
    fences even when explicitly asked not to. The clip-finder parser
    can already tolerate prose, but stripping fences here keeps logs
    cleaner and avoids a salvage round-trip on the happy path.
    """
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json|JSON)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    text = re.sub(r",(\s*[\]}])", r"\1", text)
    return text


class NineRouterClient:
    """Thin wrapper around 9router's OpenAI-compatible chat/completions.

    Matches :class:`.gemini_client.GeminiClient` so detector / hunters /
    scorer can hold either via a structural-typing handle (`_client`).

    Unlike Gemini this client does NOT rotate keys — 9router is a single
    local proxy with one bearer token. We do still fall back across
    ``model`` and ``fallback_models`` on HTTP 404, mirroring the
    Gemini client's contract for "model decommissioned / not exposed".
    """

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        fallback_models: list[str] | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ):
        self._api_key = (api_key or config.NINEROUTER_API_KEY or "").strip()
        self._base_url = (base_url or config.NINEROUTER_BASE_URL).rstrip("/")
        self._timeout_seconds = timeout_seconds

        seen: set[str] = set()
        ordered: list[str] = []
        for m in [model, *(fallback_models or [])]:
            if m and m not in seen:
                ordered.append(m)
                seen.add(m)
        if not ordered:
            raise ValueError("NineRouterClient requires at least one model id")
        self._models = ordered

    # API-parity surface — detector probes ``num_keys`` for log lines and
    # ``models`` for diagnostics. We expose 1 / [primary] respectively.
    @property
    def num_keys(self) -> int:
        return 1

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
        log_label: str = "Kiro",
    ) -> str:
        """POST a single-turn prompt and return the assistant message text.

        Raises :class:`ClipFinderError` when every model in the chain
        fails so callers can surface a single terminal-state error
        identical to the Gemini path.
        """
        if not self._api_key:
            raise ClipFinderError(
                "Kiro / 9router model selected but NINEROUTER_API_KEY is "
                "not set. Add it to .env or switch back to Gemini.",
            )

        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        last_error: str | None = None
        for model in self._models:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_INSTRUCTION},
                    {"role": "user", "content": prompt},
                ],
                "temperature": temperature,
                "max_tokens": max_output_tokens,
                # 9router defaults to SSE streaming; force a single JSON
                # body so r.json() works and the salvage path receives a
                # complete response.
                "stream": False,
            }
            label = f"{log_label} model={model}"

            try:
                async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                    response = await client.post(url, json=payload, headers=headers)

                if response.status_code == 404:
                    if log_fn:
                        log_fn(
                            f"Kiro model '{model}' not available "
                            "(HTTP 404); falling back to next model"
                        )
                    last_error = f"Model '{model}' not found"
                    continue

                if response.status_code in (401, 403):
                    upstream_message = _extract_upstream_error(response)
                    if log_fn:
                        log_fn(
                            f"{label} auth/session error "
                            f"(HTTP {response.status_code}): {upstream_message}; "
                            "trying next model..."
                        )
                    last_error = (
                        f"HTTP {response.status_code} for model '{model}': "
                        f"{upstream_message}"
                    )
                    continue

                if response.status_code in (429,) or response.status_code >= 500:
                    if log_fn:
                        log_fn(
                            f"{label} transient error "
                            f"(HTTP {response.status_code}), trying next model..."
                        )
                    last_error = f"HTTP {response.status_code}"
                    continue

                if response.status_code != 200:
                    raise ClipFinderError(
                        f"9router API error (HTTP {response.status_code}): "
                        f"{response.text[:500]}"
                    )

                result = response.json()

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                err_type = (
                    "timeout" if isinstance(exc, httpx.TimeoutException)
                    else "connection error"
                )
                if log_fn:
                    log_fn(f"{label} {err_type}, trying next model...")
                last_error = f"{err_type}: {exc}"
                continue

            choices = result.get("choices") or []
            if not choices:
                last_error = "no choices in response"
                if log_fn:
                    log_fn(f"{label} returned no choices, trying next model...")
                continue

            content = (
                choices[0].get("message", {}).get("content", "") or ""
            )
            if not content.strip():
                last_error = "empty assistant content"
                if log_fn:
                    log_fn(f"{label} returned empty content, trying next model...")
                continue

            return _strip_code_fence(content)

        raise ClipFinderError(
            f"All Kiro / 9router models failed for {log_label}. "
            f"Tried {len(self._models)} model(s). Last error: {last_error}"
        )


__all__ = ["NineRouterClient"]
