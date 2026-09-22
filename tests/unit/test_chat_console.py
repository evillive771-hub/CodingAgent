from typing import Any

import pytest

from aicoding_agent.chat.console import ChatEventSink


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    async def emit(self, run_id: str, event_type: str, data: dict[str, Any]) -> None:
        self.events.append((run_id, event_type, data))


@pytest.mark.asyncio
async def test_chat_console_reports_warning_once_and_new_summary_versions() -> None:
    lines: list[str] = []
    sink = ChatEventSink(RecordingSink(), writer=lines.append)

    await sink.emit(
        "run-1",
        "context.prepared",
        {"context_warning": True, "raw_tokens": 100, "summary_version": 0},
    )
    await sink.emit(
        "run-1",
        "context.prepared",
        {"context_warning": True, "raw_tokens": 110, "summary_version": 0},
    )
    await sink.emit(
        "run-1",
        "context.prepared",
        {
            "context_warning": True,
            "raw_tokens": 120,
            "summary_version": 1,
            "compaction_strategy": "full",
        },
    )

    assert sum("near automatic" in line for line in lines) == 1
    assert any("Notes v1" in line for line in lines)
