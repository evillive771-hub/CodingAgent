import json

import httpx
import pytest

from aicoding_agent.config import ModelSettings
from aicoding_agent.model.base import (
    ModelAuthenticationError,
    ModelPromptTooLongError,
    ModelProtocolError,
    ModelRateLimitError,
)
from aicoding_agent.model.mimo import MimoChatModel


@pytest.mark.asyncio
async def test_mimo_adapter_builds_and_parses_tool_calls() -> None:
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": "",
                            "reasoning_content": "Need to inspect.",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"README.md"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = ModelSettings(
        model="mimo-v2.5-pro",
        api_key="test-key",
        base_url="https://example.invalid/v1",
    )
    model = MimoChatModel(settings, client=client)
    result = await model.complete(
        [{"role": "user", "content": "Read README"}],
        [
            {
                "type": "function",
                "function": {"name": "read_file", "parameters": {"type": "object"}},
            }
        ],
    )

    assert captured["model"] == "mimo-v2.5-pro"
    assert captured["thinking"] == {"type": "enabled"}
    assert result.reasoning_content == "Need to inspect."
    assert result.tool_calls[0].arguments == {"path": "README.md"}
    await client.aclose()


@pytest.mark.asyncio
async def test_mimo_classifies_authentication_as_non_retryable() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = MimoChatModel(
        ModelSettings(
            model="mimo-v2.5-pro",
            api_key="bad-key",
            base_url="https://example.invalid/v1",
            max_retries=5,
        ),
        client=client,
    )

    with pytest.raises(ModelAuthenticationError, match="credentials"):
        await model.complete([], [])

    assert calls == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_mimo_preserves_bounded_retry_after_for_rate_limit() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "3"}, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = MimoChatModel(
        ModelSettings(
            model="mimo-v2.5-pro",
            api_key="test-key",
            base_url="https://example.invalid/v1",
            max_retries=0,
        ),
        client=client,
    )

    with pytest.raises(ModelRateLimitError) as caught:
        await model.complete([], [])

    assert caught.value.retry_after_seconds == 3
    await client.aclose()


@pytest.mark.asyncio
async def test_mimo_marks_invalid_tool_json_as_retryable_protocol_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "bad-call",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": "{not-json}",
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = MimoChatModel(
        ModelSettings(
            model="mimo-v2.5-pro",
            api_key="test-key",
            base_url="https://example.invalid/v1",
        ),
        client=client,
    )

    with pytest.raises(ModelProtocolError) as caught:
        await model.complete([], [])

    assert caught.value.retryable
    await client.aclose()


@pytest.mark.asyncio
async def test_mimo_classifies_prompt_too_long_without_exposing_response_body() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            request=request,
            json={"error": {"code": "context_length_exceeded", "message": "secret body"}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = MimoChatModel(
        ModelSettings(
            model="mimo-v2.5-pro",
            api_key="test-key",
            base_url="https://example.invalid/v1",
        ),
        client=client,
    )

    with pytest.raises(ModelPromptTooLongError) as caught:
        await model.complete([], [])

    assert "secret body" not in str(caught.value)
    await client.aclose()
