from pathlib import Path

import pytest

from aicoding_agent.context.history import HistoryStore
from aicoding_agent.domain.models import Budget, TaskContract, ToolCall, ToolRisk
from aicoding_agent.tools.base import ToolContext
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor


@pytest.mark.asyncio
async def test_history_archives_immutable_windows_and_recovers_after_restart(
    tmp_path: Path,
) -> None:
    store = HistoryStore(tmp_path)
    first = await store.archive_window(
        "chat-default",
        [
            {"role": "user", "content": "investigate optimistic locking"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "shell.exec", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "compare_and_swap failed with timeout",
            },
        ],
        source_start_index=0,
    )
    duplicate = await store.archive_window(
        "chat-default",
        [
            {"role": "user", "content": "investigate optimistic locking"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "shell.exec", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "compare_and_swap failed with timeout",
            },
        ],
        source_start_index=0,
    )

    assert first == duplicate
    assert first.window_id == "W000001"
    restarted = HistoryStore(tmp_path)
    matches = await restarted.search_contents(
        "chat-default",
        "compare_and_swap",
        role="tool",
        tool_namespace="shell",
        tool_name="exec",
    )
    assert [(match.window_id, match.item_id) for match in matches] == [
        ("W000001", "item-000003")
    ]
    item, content, has_more = await restarted.read_item(
        "chat-default",
        window_id="W000001",
        item_id="item-000003",
        offset_chars=0,
        limit_chars=60,
    )
    assert item.message["content"] == "compare_and_swap failed with timeout"
    assert content.startswith('{"role":"tool"')
    assert has_more is True


@pytest.mark.asyncio
async def test_history_tools_are_read_only_filtered_and_bounded(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "history")
    await store.archive_window(
        "chat-tools",
        [{"role": "assistant", "content": "exact evidence " + ("x" * 4_000)}],
        source_start_index=0,
    )
    contract = TaskContract(
        task_id="history-tools",
        objective="Recover exact history",
        acceptance_criteria=["History is read without mutation"],
        allowed_tools={"history_search", "history_read_item", "history_list_items"},
        budget=Budget(max_tool_output_chars=1_000),
    )
    context = ToolContext(
        workspace=tmp_path,
        contract=contract,
        history_store=store,
        history_scope_id="chat-tools",
    )
    registry = build_builtin_registry()
    executor = ToolExecutor(registry)

    assert registry.get("history_search").risk == ToolRisk.READ
    search = await executor.execute(
        ToolCall(
            id="search",
            name="history_search",
            arguments={"query": "exact evidence", "role": "assistant"},
        ),
        context,
    )
    read = await executor.execute(
        ToolCall(
            id="read",
            name="history_read_item",
            arguments={
                "window_id": "W000001",
                "item_id": "item-000001",
                "limit_chars": 50_000,
            },
        ),
        context,
    )

    assert search.ok and search.data["count"] == 1
    assert read.ok and read.data["has_more"] is True
    assert len(read.output) <= contract.budget.max_tool_output_chars


@pytest.mark.asyncio
async def test_history_tool_schema_and_contract_fail_closed(tmp_path: Path) -> None:
    contract = TaskContract(
        task_id="history-deny",
        objective="Reject invalid or disallowed history reads",
        acceptance_criteria=["Policy remains authoritative"],
        allowed_tools={"submit_result"},
    )
    context = ToolContext(workspace=tmp_path, contract=contract)
    executor = ToolExecutor(build_builtin_registry())

    invalid = await executor.execute(
        ToolCall(
            id="invalid",
            name="history_read_item",
            arguments={"window_id": "bad", "item_id": "also-bad"},
        ),
        context,
    )
    denied = await executor.execute(
        ToolCall(
            id="denied",
            name="history_search",
            arguments={"query": "evidence"},
        ),
        context,
    )

    assert not invalid.ok and "Invalid tool arguments" in invalid.output
    assert not denied.ok and denied.policy_denied
