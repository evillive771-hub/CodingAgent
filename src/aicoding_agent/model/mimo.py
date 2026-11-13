from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from aicoding_agent.config import ModelSettings
from aicoding_agent.domain.models import ModelResponse, ToolCall
from aicoding_agent.model.base import (
    ModelAuthenticationError,
    ModelPromptTooLongError,
    ModelProtocolError,
    ModelRateLimitError,
    ModelRequestError,
    ModelTransientError,
)


class OpenAICompatibleChatModel:
    """OpenAI-compatible Chat Completions adapter for configured providers."""

    def __init__(
        self,
        settings: ModelSettings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._provider = "DeepSeek" if settings.provider == "deepseek" else "MiMo"
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_seconds)
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self._settings.model,
            "messages": messages,
            "max_tokens": self._settings.max_output_tokens,
        }
        if tools:
            payload["tools"] = tools
        if self._settings.thinking_enabled:
            payload["thinking"] = {"type": "enabled"}

        response: httpx.Response | None = None
        for attempt in range(self._settings.max_retries + 1):
            try:
                response = await self._client.post(
                    f"{self._settings.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._settings.api_key.get_secret_value()}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            except httpx.HTTPError as exc:
                if attempt >= self._settings.max_retries:
                    raise ModelTransientError(
                        f"{self._provider} request failed: {type(exc).__name__}"
                    ) from exc
                await asyncio.sleep(0.5 * (2**attempt))
                continue

            if response.status_code < 400:
                break
            if response.status_code in {401, 403}:
                raise ModelAuthenticationError(
                    f"{self._provider} rejected model credentials: HTTP {response.status_code}"
                )
            if response.status_code not in {408, 429, 500, 502, 503, 504}:
                if response.status_code in {400, 413} and _is_prompt_too_long(response):
                    raise ModelPromptTooLongError(
                        f"{self._provider} rejected an oversized prompt: "
                        f"HTTP {response.status_code}"
                    )
                raise ModelRequestError(f"{self._provider} returned HTTP {response.status_code}")
            if attempt >= self._settings.max_retries:
                if response.status_code == 429:
                    raise ModelRateLimitError(
                        f"{self._provider} remained rate limited: HTTP 429",
                        retry_after_seconds=_retry_after_seconds(response),
                    )
                raise ModelTransientError(
                    f"{self._provider} remained unavailable: HTTP {response.status_code}"
                )
            await asyncio.sleep(0.5 * (2**attempt))

        if response is None:
            raise ModelTransientError(f"{self._provider} request did not produce a response")

        try:
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelProtocolError(
                f"{self._provider} response is missing choices[0].message"
            ) from exc

        tool_calls: list[ToolCall] = []
        for raw_call in message.get("tool_calls") or []:
            try:
                function = raw_call["function"]
                raw_arguments = function.get("arguments", "{}")
                arguments = (
                    json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                )
                if not isinstance(arguments, dict):
                    raise TypeError("tool arguments must decode to an object")
                tool_calls.append(
                    ToolCall(
                        id=raw_call["id"],
                        name=function["name"],
                        arguments=arguments,
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ModelProtocolError(f"{self._provider} returned an invalid tool call") from exc

        usage = body.get("usage") or {}
        numeric_usage = {
            str(key): value for key, value in usage.items() if isinstance(value, (int, float))
        }
        return ModelResponse(
            content=message.get("content") or "",
            reasoning_content=message.get("reasoning_content"),
            tool_calls=tool_calls,
            usage=numeric_usage,
            finish_reason=choice.get("finish_reason"),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class MimoChatModel(OpenAICompatibleChatModel):
    """Backward-compatible name for the original MiMo adapter."""


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if 0 <= value <= 300 else None


def _is_prompt_too_long(response: httpx.Response) -> bool:
    try:
        payload = response.json()
    except ValueError:
        return False
    text = json.dumps(payload, ensure_ascii=False).casefold()
    return any(
        marker in text
        for marker in (
            "context_length_exceeded",
            "maximum context length",
            "prompt is too long",
            "prompt too long",
            "context window",
        )
    )
