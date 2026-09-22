from pathlib import Path
from typing import Any

import pytest
from conftest import ScriptedModel

from aicoding_agent.agent.loop import AgentLoop
from aicoding_agent.agent.observability import TelemetryRegistry
from aicoding_agent.domain.models import ModelResponse, TaskContract, ToolCall
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor


def test_telemetry_registry_projects_bounded_numeric_events() -> None:
    telemetry = TelemetryRegistry()
    telemetry.observe("run-1", "agent.started", {"task_id": "task-1"})
    telemetry.observe("run-1", "agent.turn_started", {"turn": 2})
    telemetry.observe(
        "run-1",
        "context.prepared",
        {
            "raw_chars": 2000,
            "model_chars": 1200,
            "raw_tokens": 600,
            "archived_tokens": 500,
            "model_tokens": 350,
            "compacted": True,
            "archive_compacted": True,
            "semantic_compacted": True,
            "compaction_strategy": "rollover",
            "notes_version": 2,
        },
    )
    telemetry.observe("run-1", "model.retry_scheduled", {"retry": 1})
    telemetry.observe(
        "run-1",
        "model.response",
        {
            "duration_ms": 12.5,
            "usage": {"prompt_tokens": 30, "completion_tokens": 7, "total_tokens": 37},
        },
    )
    telemetry.observe(
        "run-1",
        "tool.finished",
        {"ok": False, "duration_ms": 3.25, "output": "must not be rendered"},
    )
    telemetry.observe("run-1", "permission.requested", {})
    telemetry.observe(
        "run-1",
        "goal.evaluated",
        {"duration_ms": 4.5, "usage": {"total_tokens": 11}},
    )
    telemetry.observe("run-1", "subagent.started", {})
    telemetry.observe(
        "run-1",
        "subagent.finished",
        {"status": "blocked", "finding_count": 2},
    )
    telemetry.observe("run-1", "agent.failed", {})

    run = telemetry.get("run-1")

    assert run is not None
    assert run.status == "failed"
    assert run.turns == 2
    assert run.compactions == 1
    assert run.context_tokens_estimated == 350
    assert run.archived_context_tokens_estimated == 500
    assert run.archive_reductions == 1
    assert run.compaction_strategy == "rollover"
    assert run.session_memory_version == 2
    assert run.model_retries == 1
    assert run.total_tokens == 37
    assert run.tool_failures == 1
    assert run.permission_requests == 1
    assert run.goal_evaluations == 1
    assert run.goal_tokens == 11
    assert run.subagent_runs == 1
    assert run.subagent_blocked == 1
    assert run.subagent_findings == 2
    rendered = telemetry.render_latest()
    assert "tokens=37" in rendered
    assert "notes_v=2" in rendered
    assert "must not be rendered" not in rendered


def test_telemetry_projects_unverified_terminal_status() -> None:
    telemetry = TelemetryRegistry()
    telemetry.observe("run-unverified", "agent.started", {"task_id": "task-1"})
    telemetry.observe("run-unverified", "agent.submitted_unverified", {})

    run = telemetry.get("run-unverified")

    assert run is not None
    assert run.status == "submitted_unverified"


@pytest.mark.asyncio
async def test_agent_events_feed_live_telemetry_without_prompt_contents(tmp_path: Path) -> None:
    telemetry = TelemetryRegistry()
    captured: list[tuple[str, dict[str, Any]]] = []

    class TelemetrySink:
        async def emit(self, run_id: str, event_type: str, data: dict[str, Any]) -> None:
            telemetry.observe(run_id, event_type, data)
            captured.append((event_type, data))

    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit",
                        name="submit_result",
                        arguments={"summary": "Observed", "changed_files": []},
                    )
                ],
                usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            ),
        ]
    )
    contract = TaskContract(
        task_id="telemetry-agent",
        objective="Complete a read-only observed turn",
        acceptance_criteria=["Metrics are projected from events"],
        allowed_tools={"submit_result"},
    )

    result = await AgentLoop(
        model=model,
        executor=ToolExecutor(build_builtin_registry()),
        event_sink=TelemetrySink(),
    ).run(contract, tmp_path, run_id="telemetry-run")

    assert result.status == "completed"
    run = telemetry.get("telemetry-run")
    assert run is not None
    assert run.status == "completed"
    assert run.model_calls == 1
    assert run.total_tokens == 12
    assert run.tool_calls == 1
    context_events = [data for kind, data in captured if kind == "context.prepared"]
    assert len(context_events) == 1
    assert all(event["raw_chars"] >= event["model_chars"] for event in context_events)
    model_events = [data for kind, data in captured if kind == "model.response"]
    assert all(isinstance(event["duration_ms"], float) for event in model_events)
    tool_events = [data for kind, data in captured if kind == "tool.finished"]
    assert isinstance(tool_events[0]["duration_ms"], float)
