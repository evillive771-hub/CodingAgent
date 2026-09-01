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


def test_compaction_preserves_runtime_control_state_and_latest_verification() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old history" + ("x" * 4000)},
        {"role": "user", "content": "# Task contract\ncurrent task"},
        {"role": "user", "content": "<active_plan>plan v2</active_plan>"},
        {"role": "user", "content": "<user_steering>fix login first</user_steering>"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "verify", "type": "function"}],
        },
        {
            "role": "tool",
            "tool_call_id": "verify",
            "content": '{"data":{"verification_id":"tests","exit_code": 0}}',
        },
        {"role": "assistant", "content": "recent reasoning" + ("y" * 4000)},
    ]

    compacted = compact_messages(messages, max_chars=1500, recent_messages=2)
    serialized = str(compacted)

    assert "current task" in serialized
    assert "plan v2" in serialized
    assert "fix login first" in serialized
    assert '"verification_id":"tests"' in serialized
    assert "old history" not in serialized
    assert "verify" in {
        call["id"] for message in compacted for call in message.get("tool_calls", [])
    }
