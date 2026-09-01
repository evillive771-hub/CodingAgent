import json

import httpx
import pytest

from aicoding_agent.config import ModelSettings
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
