from aicoding_agent.context.builder import compact_messages


def test_compaction_keeps_tool_call_and_result_together() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "contract"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-old",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-old", "content": "x" * 5000},
        {"role": "user", "content": "continue"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-new",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-new", "content": "recent-result"},
    ]

    compacted = compact_messages(messages, max_chars=1000, recent_messages=3)
    assistant_ids = {
        call["id"]
        for message in compacted
        for call in message.get("tool_calls", [])
        if message.get("role") == "assistant"
    }
    tool_ids = {message["tool_call_id"] for message in compacted if message.get("role") == "tool"}

    assert assistant_ids == tool_ids
