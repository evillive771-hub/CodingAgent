from typing import Any

import pytest

from aicoding_agent.workflow.progress import SafeProgressEventSink


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    async def emit(self, run_id: str, event_type: str, data: dict[str, Any]) -> None:
        self.events.append((run_id, event_type, data))


@pytest.mark.asyncio
async def test_progress_is_visible_but_event_payload_is_not_rendered() -> None:
    delegate = RecordingSink()
    lines: list[str] = []
    sink = SafeProgressEventSink(delegate, writer=lines.append)

    await sink.emit("run-1", "workflow.stage_changed", {"stage": "building"})
    await sink.emit("run-1", "tool.started", {"tool_name": "run_process"})
    await sink.emit(
        "run-1",
        "tool.finished",
        {"tool_name": "run_process", "ok": False, "output": "API_KEY=secret-value"},
    )

    assert lines == [
        "  [workflow] building",
        "  [tool] run_process",
        "  [tool:failed] run_process",
    ]
    assert "secret-value" not in "\n".join(lines)
    assert delegate.events[-1][2]["output"] == "API_KEY=secret-value"
