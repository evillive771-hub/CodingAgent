from pathlib import Path
from typing import Any

import pytest

from aicoding_agent.context.budget import ContextBudgetConfig, ContextBudgetManager
from aicoding_agent.context.session_memory import (
    ConversationCompactionConfig,
    ConversationCompactor,
    SessionMemoryError,
    SessionMemoryManager,
    SessionMemoryStore,
    SessionMemoryStrategy,
)


class RecordingSummarizer:
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[dict[str, Any]] = []

    async def summarize(
        self,
        messages: list[dict[str, Any]],
        *,
        previous_summary: str | None,
        focus: str,
        strategy: SessionMemoryStrategy,
    ) -> str:
        self.calls.append(
            {
                "messages": messages,
                "previous_summary": previous_summary,
                "focus": focus,
                "strategy": strategy,
            }
        )
        if self.failures:
            self.failures -= 1
            raise ValueError("prompt too long")
        return f"summary-{len(self.calls)}-{strategy}"


def messages(count: int) -> list[dict[str, str]]:
    return [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"message-{index}"}
        for index in range(count)
    ]


@pytest.mark.asyncio
async def test_session_memory_rotates_windows_and_persists_notes(tmp_path: Path) -> None:
    summarizer = RecordingSummarizer()
    store = SessionMemoryStore(tmp_path)
    manager = SessionMemoryManager(scope_id="session-default", summarizer=summarizer, store=store)

    first = await manager.compact_prefix(messages(6), covered_message_count=4)
    second = await manager.compact_prefix(messages(8), covered_message_count=6)

    assert first.strategy == SessionMemoryStrategy.FULL
    assert second.strategy == SessionMemoryStrategy.ROLLOVER
    assert summarizer.calls[1]["previous_summary"] == first.summary
    assert summarizer.calls[1]["messages"] == messages(8)[4:6]
    assert first.history_window_id == "W000001"
    assert second.history_window_id == "W000002"
    assert second.history_item_refs == ["W000002:item-000001", "W000002:item-000002"]

    restored = SessionMemoryManager(scope_id="session-default", summarizer=summarizer, store=store)
    await restored.initialize()
    assert restored.current == second


@pytest.mark.asyncio
async def test_checkpoint_writer_receives_exact_history_refs(tmp_path: Path) -> None:
    class RecordingCheckpointer(RecordingSummarizer):
        async def checkpoint(
            self,
            source: list[dict[str, Any]],
            *,
            previous_notes: str | None,
            history_refs: list[str],
            focus: str,
        ) -> str:
            self.calls.append(
                {
                    "messages": source,
                    "previous_notes": previous_notes,
                    "history_refs": history_refs,
                    "focus": focus,
                }
            )
            return "# Goal\nKeep exact evidence\n# Important History\n" + ", ".join(
                history_refs
            )

    checkpointer = RecordingCheckpointer()
    manager = SessionMemoryManager(
        scope_id="checkpoint-refs",
        summarizer=checkpointer,
        store=SessionMemoryStore(tmp_path),
    )

    snapshot = await manager.compact_prefix(messages(5), covered_message_count=3)

    assert checkpointer.calls[0]["previous_notes"] is None
    assert checkpointer.calls[0]["history_refs"] == [
        "W000001:item-000001",
        "W000001:item-000002",
        "W000001:item-000003",
    ]
    assert snapshot.history_item_refs == checkpointer.calls[0]["history_refs"]


@pytest.mark.asyncio
async def test_changed_covered_prefix_forces_full_summary() -> None:
    summarizer = RecordingSummarizer()
    manager = SessionMemoryManager(scope_id="changed-prefix", summarizer=summarizer)
    source = messages(6)
    await manager.compact_prefix(source, covered_message_count=4)
    changed = messages(8)
    changed[0]["content"] = "changed"

    snapshot = await manager.compact_prefix(changed, covered_message_count=6)

    assert snapshot.strategy == SessionMemoryStrategy.FULL
    assert summarizer.calls[-1]["previous_summary"] is None


@pytest.mark.asyncio
async def test_full_summary_retries_by_dropping_old_complete_groups() -> None:
    summarizer = RecordingSummarizer(failures=1)
    manager = SessionMemoryManager(scope_id="ptl", summarizer=summarizer)

    snapshot = await manager.compact_prefix(messages(10), covered_message_count=8)

    assert snapshot.lossy_fallback is True
    assert snapshot.source_start_index > 0
    assert len(summarizer.calls[1]["messages"]) < len(summarizer.calls[0]["messages"])


@pytest.mark.asyncio
async def test_conversation_compactor_keeps_recent_messages_and_uses_summary() -> None:
    summarizer = RecordingSummarizer()
    budget = ContextBudgetManager(
        ContextBudgetConfig(
            context_window_tokens=8_192,
            max_output_tokens=512,
            summary_output_tokens=256,
            compact_buffer_tokens=512,
            warning_buffer_tokens=1_024,
        )
    )
    compactor = ConversationCompactor(
        summarizer=summarizer,
        budget=budget,
        config=ConversationCompactionConfig(
            min_recent_tokens=0,
            max_recent_tokens=10_000,
            min_recent_messages=1,
            max_recent_messages=2,
        ),
    )

    first = await compactor.prepare(messages(6), scope_id="chat-default", force=True)
    second = await compactor.prepare(messages(8), scope_id="chat-default", force=True)

    assert first.compacted is True
    assert len(first.messages) == 2
    assert second.strategy == SessionMemoryStrategy.ROLLOVER
    assert len(second.messages) == 2
    assert second.summary is not None


@pytest.mark.asyncio
async def test_session_memory_redacts_secrets_before_persistence(tmp_path: Path) -> None:
    class SecretSummarizer(RecordingSummarizer):
        async def summarize(self, *args, **kwargs) -> str:
            del args, kwargs
            return "Use api_key=never-store-this-secret-value for the request"

    manager = SessionMemoryManager(
        scope_id="redacted",
        summarizer=SecretSummarizer(),
        store=SessionMemoryStore(tmp_path),
    )

    snapshot = await manager.compact_prefix(messages(4), covered_message_count=2)

    assert "never-store" not in snapshot.summary
    assert "[REDACTED]" in snapshot.summary
    assert "never-store" not in (tmp_path / "redacted" / "session-memory.jsonl").read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_rollover_failure_falls_back_to_a_full_checkpoint() -> None:
    class RolloverFailureSummarizer(RecordingSummarizer):
        async def summarize(
            self,
            messages: list[dict[str, Any]],
            *,
            previous_summary: str | None,
            focus: str,
            strategy: SessionMemoryStrategy,
        ) -> str:
            self.calls.append(
                {
                    "messages": messages,
                    "previous_summary": previous_summary,
                    "focus": focus,
                    "strategy": strategy,
                }
            )
            if strategy == SessionMemoryStrategy.ROLLOVER:
                raise ValueError("rollover checkpoint rejected")
            return "rebuilt full checkpoint"

    summarizer = RolloverFailureSummarizer()
    manager = SessionMemoryManager(scope_id="fallback", summarizer=summarizer)
    await manager.compact_prefix(messages(6), covered_message_count=4)

    snapshot = await manager.compact_prefix(messages(8), covered_message_count=6)

    assert snapshot.strategy == SessionMemoryStrategy.FULL
    assert snapshot.summary == "rebuilt full checkpoint"
    assert manager.consecutive_failures == 0


@pytest.mark.asyncio
async def test_session_memory_opens_circuit_after_three_failed_compactions() -> None:
    summarizer = RecordingSummarizer(failures=100)
    manager = SessionMemoryManager(scope_id="breaker", summarizer=summarizer)

    for covered in (2, 4, 6):
        with pytest.raises(SessionMemoryError, match="rollover failed"):
            await manager.compact_prefix(messages(8), covered_message_count=covered)

    with pytest.raises(SessionMemoryError, match="circuit breaker is open"):
        await manager.compact_prefix(messages(8), covered_message_count=7)
