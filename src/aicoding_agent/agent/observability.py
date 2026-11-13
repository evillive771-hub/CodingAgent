from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import Field

from aicoding_agent.domain.models import StrictModel

_TERMINAL_EVENTS = {
    "agent.completed": "completed",
    "agent.submitted_unverified": "submitted_unverified",
    "agent.failed": "failed",
    "agent.blocked": "blocked",
    "agent.waiting_for_user": "waiting_for_user",
    "agent.interrupted": "interrupted",
    "agent.budget_exhausted": "budget_exhausted",
}


class RunTelemetry(StrictModel):
    run_id: str
    task_id: str = ""
    status: str = "running"
    turns: int = 0
    context_chars: int = 0
    raw_context_chars: int = 0
    context_tokens_estimated: int = 0
    raw_context_tokens_estimated: int = 0
    archived_context_tokens_estimated: int = 0
    archive_reductions: int = 0
    compactions: int = 0
    compaction_strategy: str = "none"
    session_memory_version: int = 0
    compaction_failures: int = 0
    model_calls: int = 0
    model_retries: int = 0
    model_duration_ms: float = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    tool_duration_ms: float = 0
    permission_requests: int = 0
    goal_evaluations: int = 0
    goal_duration_ms: float = 0
    goal_tokens: int = 0
    subagent_runs: int = 0
    subagent_blocked: int = 0
    subagent_findings: int = 0
    browser_runs: int = 0
    browser_failures: int = 0
    browser_infra_failures: int = 0
    # A task permits at most 1000 tool calls, with at most one environment retry per call.
    browser_duration_ms: list[float] = Field(default_factory=list, max_length=2000)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TelemetryRegistry:
    """In-memory projection of append-only run events; event logs remain authoritative."""

    def __init__(self) -> None:
        self._runs: dict[str, RunTelemetry] = {}
        self._order: list[str] = []

    def observe(self, run_id: str, event_type: str, data: dict[str, Any]) -> None:
        run = self._runs.get(run_id)
        if run is None:
            run = RunTelemetry(run_id=run_id)
            self._runs[run_id] = run
            self._order.append(run_id)
        run.updated_at = datetime.now(UTC)
        if event_type == "agent.started":
            run.task_id = str(data.get("task_id", ""))[:128]
            run.status = "running"
        elif event_type == "agent.turn_started":
            run.turns = max(run.turns, _safe_int(data.get("turn")))
        elif event_type == "context.prepared":
            run.raw_context_chars = _safe_int(data.get("raw_chars"))
            run.context_chars = _safe_int(data.get("model_chars"))
            run.raw_context_tokens_estimated = _safe_int(data.get("raw_tokens"))
            run.archived_context_tokens_estimated = _safe_int(data.get("archived_tokens"))
            run.context_tokens_estimated = _safe_int(data.get("model_tokens"))
            strategy = data.get("compaction_strategy")
            if strategy:
                run.compaction_strategy = str(strategy)
            run.session_memory_version = max(
                run.session_memory_version,
                _safe_int(data.get("notes_version") or data.get("summary_version")),
            )
            if data.get("compaction_error"):
                run.compaction_failures += 1
            if bool(data.get("archive_compacted")):
                run.archive_reductions += 1
            if bool(data.get("semantic_compacted")) or (
                "semantic_compacted" not in data and bool(data.get("compacted"))
            ):
                run.compactions += 1
        elif event_type == "model.response":
            run.model_calls += 1
            run.model_duration_ms += _safe_float(data.get("duration_ms"))
            usage = data.get("usage")
            if isinstance(usage, dict):
                run.prompt_tokens += _usage_value(usage, "prompt_tokens", "input_tokens")
                run.completion_tokens += _usage_value(
                    usage,
                    "completion_tokens",
                    "output_tokens",
                )
                run.total_tokens += _usage_value(usage, "total_tokens")
        elif event_type == "model.retry_scheduled":
            run.model_retries += 1
        elif event_type == "tool.finished":
            run.tool_calls += 1
            if not bool(data.get("ok")):
                run.tool_failures += 1
            run.tool_duration_ms += _safe_float(data.get("duration_ms"))
        elif event_type in {"tool.policy_denied", "tool.hook_blocked"}:
            run.tool_calls += 1
            run.tool_failures += 1
        elif event_type == "permission.requested":
            run.permission_requests += 1
        elif event_type == "goal.evaluated":
            run.goal_evaluations += 1
            run.goal_duration_ms += _safe_float(data.get("duration_ms"))
            usage = data.get("usage")
            if isinstance(usage, dict):
                run.goal_tokens += _usage_value(usage, "total_tokens")
        elif event_type == "subagent.started":
            run.subagent_runs += 1
        elif event_type == "subagent.finished":
            if str(data.get("status")) == "blocked":
                run.subagent_blocked += 1
            run.subagent_findings += _safe_int(data.get("finding_count"))
        elif event_type == "browser.verification_finished":
            run.browser_runs += 1
            status = data.get("status")
            if status == "failed":
                run.browser_failures += 1
            elif status in {"infra_error", "timeout"}:
                run.browser_infra_failures += 1
            duration = (
                _safe_float(data["duration_ms"])
                if "duration_ms" in data
                else _safe_float(data.get("duration_seconds")) * 1000
            )
            run.browser_duration_ms.append(duration)
            del run.browser_duration_ms[:-2000]
        if event_type in _TERMINAL_EVENTS:
            run.status = _TERMINAL_EVENTS[event_type]

    def get(self, run_id: str) -> RunTelemetry | None:
        run = self._runs.get(run_id)
        return run.model_copy(deep=True) if run is not None else None

    @property
    def latest(self) -> RunTelemetry | None:
        if not self._order:
            return None
        return self.get(self._order[-1])

    def render_latest(self) -> str:
        run = self.latest
        if run is None:
            return "<no Agent metrics>"
        token_text = (
            str(run.total_tokens)
            if run.total_tokens
            else f"{run.prompt_tokens}+{run.completion_tokens}"
        )
        return (
            f"run={run.run_id}; status={run.status}; turns={run.turns}; "
            f"context={run.context_chars}/{run.raw_context_chars} chars; "
            f"context_tokens~={run.context_tokens_estimated}/"
            f"{run.raw_context_tokens_estimated}; archived_tokens~="
            f"{run.archived_context_tokens_estimated}; archive_reductions="
            f"{run.archive_reductions}; rollovers={run.compactions}; "
            f"strategy={run.compaction_strategy}; notes_v={run.session_memory_version}; "
            f"rollover_failures={run.compaction_failures}; model_calls={run.model_calls}; "
            f"model_retries={run.model_retries}; model_ms={run.model_duration_ms:.1f}; "
            f"tokens={token_text}; tools={run.tool_calls}; "
            f"tool_failures={run.tool_failures}; tool_ms={run.tool_duration_ms:.1f}; "
            f"permission_requests={run.permission_requests}; "
            f"goal_evaluations={run.goal_evaluations}; goal_ms={run.goal_duration_ms:.1f}; "
            f"goal_tokens={run.goal_tokens}; subagents={run.subagent_runs}; "
            f"subagent_blocked={run.subagent_blocked}; "
            f"subagent_findings={run.subagent_findings}; browser_runs={run.browser_runs}; "
            f"browser_failures={run.browser_failures}; browser_infra={run.browser_infra_failures}; "
            f"browser_latency={duration_percentiles(run.browser_duration_ms)}"
        )


def duration_percentiles(samples: list[float]) -> dict[str, float | int]:
    """Report sample count; do not label a single measurement as a distribution."""
    ordered = sorted(samples)
    result: dict[str, float | int] = {"n": len(ordered)}
    if len(ordered) == 1:
        result["duration_ms"] = ordered[0]
    elif len(ordered) > 1:
        for name, quantile in (("p50_ms", 0.5), ("p90_ms", 0.9), ("p95_ms", 0.95)):
            position = (len(ordered) - 1) * quantile
            lower = int(position)
            upper = min(lower + 1, len(ordered) - 1)
            result[name] = round(
                ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 3
            )
    return result


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    return int(value) if isinstance(value, (int, float)) else 0


def _safe_float(value: Any) -> float:
    if isinstance(value, bool):
        return 0
    return float(value) if isinstance(value, (int, float)) else 0


def _usage_value(usage: dict[str, Any], *names: str) -> int:
    for name in names:
        value = usage.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    return 0
