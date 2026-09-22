import json
from pathlib import Path
from typing import Any

import pytest

from aicoding_agent.context.archive import ContextEvidenceArchive
from aicoding_agent.context.budget import ContextBudgetConfig, ContextBudgetManager
from aicoding_agent.context.compaction import AgentContextCompactor, CompactionStore
from aicoding_agent.context.session_memory import (
    ConversationCompactionConfig,
    ConversationCompactor,
    SessionMemoryStore,
    SessionMemoryStrategy,
)


class SummaryStub:
    async def summarize(
        self,
        messages: list[dict[str, Any]],
        *,
        previous_summary: str | None,
        focus: str,
        strategy: SessionMemoryStrategy,
    ) -> str:
        return "Kept the task intent, changed files, test evidence, and pending work."


@pytest.mark.asyncio
async def test_agent_context_compaction_reinjects_control_state_and_boundary(
    tmp_path: Path,
) -> None:
    budget = ContextBudgetManager(
        ContextBudgetConfig(
            context_window_tokens=8_192,
            max_output_tokens=512,
            summary_output_tokens=256,
            compact_buffer_tokens=512,
            warning_buffer_tokens=1_024,
        )
    )
    conversation = ConversationCompactor(
        summarizer=SummaryStub(),
        budget=budget,
        store=SessionMemoryStore(tmp_path / "memory"),
        config=ConversationCompactionConfig(
            min_recent_tokens=0,
            max_recent_tokens=10_000,
            min_recent_messages=1,
            max_recent_messages=2,
        ),
    )
    compactor = AgentContextCompactor(
        conversation_compactor=conversation,
        budget=budget,
        archive=ContextEvidenceArchive(tmp_path / "evidence", large_output_chars=2_000),
        store=CompactionStore(tmp_path / "records"),
    )
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old discussion"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "old-read", "type": "function"}],
        },
        {"role": "tool", "tool_call_id": "old-read", "content": "x" * 4_000},
        {"role": "user", "content": "<active_plan>fix login</active_plan>"},
        {"role": "user", "content": "# Task contract\ncurrent"},
    ]

    prepared = await compactor.prepare(messages, run_id="run-1", force=True)
    serialized = json.dumps(prepared.messages, ensure_ascii=False)

    assert "<new_context" in serialized
    assert "<notes>" in serialized
    assert 'history_window=\\"W000001\\"' in serialized
    assert "fix login" in serialized
    assert "# Task contract" in serialized
    assert prepared.record.compacted is True
    assert prepared.record.summary_version == 1
    assert prepared.record.history_window_id == "W000001"
    assert (tmp_path / "records" / "run-1" / "compactions.jsonl").is_file()
    assert (tmp_path / "evidence" / "run-1" / "transcript.json").is_file()
    history_store = conversation.history_store
    assert history_store is not None
    item, _, _ = await history_store.read_item(
        "run-run-1",
        window_id="W000001",
        item_id="item-000004",
        limit_chars=50,
    )
    assert item.message["content"] == "x" * 4_000


@pytest.mark.asyncio
async def test_archive_reduction_is_distinct_from_semantic_compaction(tmp_path: Path) -> None:
    budget = ContextBudgetManager(
        ContextBudgetConfig(
            context_window_tokens=131_072,
            max_output_tokens=8_192,
            summary_output_tokens=8_192,
            compact_buffer_tokens=13_000,
            warning_buffer_tokens=20_000,
        )
    )
    conversation = ConversationCompactor(
        summarizer=SummaryStub(),
        budget=budget,
    )
    compactor = AgentContextCompactor(
        conversation_compactor=conversation,
        budget=budget,
        archive=ContextEvidenceArchive(tmp_path / "evidence", large_output_chars=1_000),
    )
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "large-read", "type": "function"}],
        },
        {"role": "tool", "tool_call_id": "large-read", "content": "x" * 10_000},
        {"role": "user", "content": "continue"},
    ]

    prepared = await compactor.prepare(messages, run_id="archive-only")

    assert prepared.record.archive_compacted is True
    assert prepared.record.semantic_compacted is False
    assert prepared.record.raw_tokens > prepared.record.archived_tokens
    assert prepared.record.archived_tokens == prepared.record.model_tokens
