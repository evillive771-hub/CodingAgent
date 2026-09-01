from __future__ import annotations

from collections.abc import Callable
from typing import Any

from aicoding_agent.persistence.base import EventSink


class ChatEventSink:
    """Persist full events while rendering bounded progress lines for an interactive user."""

    def __init__(self, delegate: EventSink, *, writer: Callable[[str], None]) -> None:
        self.delegate = delegate
        self.writer = writer

    async def emit(self, run_id: str, event_type: str, data: dict[str, Any]) -> None:
        await self.delegate.emit(run_id, event_type, data)
        if event_type == "agent.turn_started":
            self.writer(f"  [agent] reasoning round {data.get('turn', '?')}")
        elif event_type == "tool.started":
            self.writer(f"  [tool] {data.get('tool_name', 'unknown')}")
        elif event_type == "tool.finished":
            status = "ok" if data.get("ok") else "failed"
            output = str(data.get("output", "")).strip().splitlines()
            preview = output[0][:160] if output else ""
            suffix = f" - {preview}" if preview else ""
            self.writer(f"  [tool:{status}] {data.get('tool_name', 'unknown')}{suffix}")
