from __future__ import annotations

from collections.abc import Callable
from typing import Any

from aicoding_agent.persistence.base import EventSink


class SafeProgressEventSink:
    """Persist complete events and render progress without rendering event payloads."""

    def __init__(self, delegate: EventSink, *, writer: Callable[[str], None]) -> None:
        self.delegate = delegate
        self.writer = writer

    async def emit(self, run_id: str, event_type: str, data: dict[str, Any]) -> None:
        await self.delegate.emit(run_id, event_type, data)
        if event_type == "workflow.stage_changed":
            self.writer(f"  [workflow] {data.get('stage', 'unknown')}")
        elif event_type == "agent.turn_started":
            self.writer(f"  [agent] reasoning round {data.get('turn', '?')}")
        elif event_type == "tool.started":
            self.writer(f"  [tool] {data.get('tool_name', 'unknown')}")
        elif event_type == "tool.finished":
            status = "ok" if data.get("ok") else "failed"
            self.writer(f"  [tool:{status}] {data.get('tool_name', 'unknown')}")
        elif event_type == "agent.completed":
            self.writer("  [agent] completed")
        elif event_type == "agent.submitted_unverified":
            self.writer("  [agent] submitted without verification")
        elif event_type in {"agent.failed", "agent.budget_exhausted"}:
            self.writer("  [agent] failed")
