from __future__ import annotations

from collections.abc import Callable
from typing import Any

from aicoding_agent.agent.observability import TelemetryRegistry
from aicoding_agent.persistence.base import EventSink


class ChatEventSink:
    """Persist full events while rendering bounded progress lines for an interactive user."""

    def __init__(
        self,
        delegate: EventSink,
        *,
        writer: Callable[[str], None],
        telemetry: TelemetryRegistry | None = None,
    ) -> None:
        self.delegate = delegate
        self.writer = writer
        self.telemetry = telemetry or TelemetryRegistry()
        self._context_warning_runs: set[str] = set()
        self._notes_versions: dict[str, int] = {}

    async def emit(self, run_id: str, event_type: str, data: dict[str, Any]) -> None:
        self.telemetry.observe(run_id, event_type, data)
        await self.delegate.emit(run_id, event_type, data)
        if event_type == "agent.turn_started":
            self.writer(f"  [agent] reasoning round {data.get('turn', '?')}")
        elif event_type == "context.prepared":
            notes_version = int(data.get("notes_version") or data.get("summary_version") or 0)
            previous_version = self._notes_versions.get(run_id, 0)
            if notes_version > previous_version:
                self._notes_versions[run_id] = notes_version
                self.writer(
                    f"  [context] rolled over with "
                    f"{data.get('compaction_strategy', 'unknown')} Notes v{notes_version}"
                )
            elif data.get("context_warning") and run_id not in self._context_warning_runs:
                self._context_warning_runs.add(run_id)
                self.writer(
                    f"  [context] near automatic rollover threshold "
                    f"({data.get('raw_tokens', '?')} estimated tokens)"
                )
        elif event_type == "tool.started":
            self.writer(f"  [tool] {data.get('tool_name', 'unknown')}")
        elif event_type == "tool.finished":
            status = "ok" if data.get("ok") else "failed"
            output = str(data.get("output", "")).strip().splitlines()
            preview = output[0][:160] if output else ""
            suffix = f" - {preview}" if preview else ""
            self.writer(f"  [tool:{status}] {data.get('tool_name', 'unknown')}{suffix}")
        elif event_type == "permission.requested":
            request_id = str(data.get("request_id", ""))[:8]
            self.writer(
                f"  [permission] {data.get('tool_name', 'unknown')} requires approval; "
                f"use /allow {request_id}, /allow {request_id} session, or /deny {request_id}"
            )
        elif event_type == "permission.resolved":
            self.writer(
                f"  [permission:{data.get('decision', 'unknown')}] "
                f"{data.get('tool_name', 'unknown')}"
            )
