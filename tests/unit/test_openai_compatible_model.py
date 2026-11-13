import json

import httpx
import pytest

from aicoding_agent.config import ModelSettings
from aicoding_agent.model import OpenAICompatibleChatModel, create_chat_model


@pytest.mark.asyncio
async def test_deepseek_provider_uses_chat_completions_and_parses_tool_call() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": "",
                            "reasoning_content": "Inspect the file.",
                            "tool_calls": [
                                {
                                    "id": "call-deepseek-1",
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
                "usage": {"prompt_tokens": 12, "completion_tokens": 5},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = ModelSettings(
        provider="deepseek",
        model="deepseek-flash",
        api_key="test-deepseek-key",
        base_url="https://api.deepseek.com",
    )
    model = create_chat_model(settings, client=client)

    assert isinstance(model, OpenAICompatibleChatModel)
    result = await model.complete(
        [{"role": "user", "content": "Read README"}],
        [
            {
                "type": "function",
                "function": {"name": "read_file", "parameters": {"type": "object"}},
            }
        ],
    )

    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["authorization"] == "Bearer test-deepseek-key"
    assert captured["payload"] == {
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": "Read README"}],
        "max_tokens": 8192,
        "tools": [
            {
                "type": "function",
                "function": {"name": "read_file", "parameters": {"type": "object"}},
            }
        ],
        "thinking": {"type": "enabled"},
    }
    assert result.reasoning_content == "Inspect the file."
    assert result.tool_calls[0].name == "read_file"
    assert result.tool_calls[0].arguments == {"path": "README.md"}
    await client.aclose()
