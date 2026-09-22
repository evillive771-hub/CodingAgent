import asyncio
from pathlib import Path
from typing import Any

import pytest
from conftest import ScriptedModel

from aicoding_agent.agent.control import ControlIntent, SteeringInbox
from aicoding_agent.agent.goal import GoalEvaluation, GoalEvaluationError, GoalEvaluationResult
from aicoding_agent.agent.hooks import AsyncHookBus, HookContext, HookEvent
from aicoding_agent.agent.loop import AgentLoop
from aicoding_agent.agent.permission import PermissionBroker, PermissionDecision
from aicoding_agent.agent.plan import PlanItem, PlanManager, PlanStatus
from aicoding_agent.agent.recovery import RecoveryController, RecoveryPolicy
from aicoding_agent.agent.subagent import ReadOnlySubagentManager
from aicoding_agent.agent.task_graph import TaskGraphManager
from aicoding_agent.context.budget import ContextBudgetConfig, ContextBudgetManager
from aicoding_agent.context.compaction import AgentContextCompactor
from aicoding_agent.context.session_memory import (
    ConversationCompactionConfig,
    ConversationCompactor,
    SessionMemoryStrategy,
)
from aicoding_agent.domain.models import (
    DiagnosticCommand,
    DiagnosticParser,
    ModelResponse,
    TaskContract,
    ToolCall,
    ToolRisk,
    VerificationCommand,
)
from aicoding_agent.model.base import ModelTransientError
from aicoding_agent.tools.base import BaseTool, ToolContext, ToolExecution, ToolInput
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor


@pytest.mark.asyncio
async def test_agent_recovers_model_call_without_replaying_tools(tmp_path: Path) -> None:
    class TransientThenSuccessModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, tools):
            del messages, tools
            self.calls += 1
            if self.calls == 1:
                raise ModelTransientError("temporary model outage")
            if self.calls == 2:
                return ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="submit-after-recovery",
                            name="submit_result",
                            arguments={"summary": "Recovered safely", "changed_files": []},
                        )
                    ]
                )
            return ModelResponse(content="Recovered.")

        async def aclose(self) -> None:
            return None

    delays: list[float] = []

    async def record_delay(delay: float) -> None:
        delays.append(delay)

    model = TransientThenSuccessModel()
    recovery = RecoveryController(
        RecoveryPolicy(base_delay_seconds=0.25),
        sleeper=record_delay,
    )
    contract = TaskContract(
        task_id="model-recovery-agent",
        objective="Finish after a transient model failure",
        acceptance_criteria=["No tool is replayed by the host"],
        allowed_tools={"submit_result"},
    )

    result = await AgentLoop(
        model=model,
        executor=ToolExecutor(build_builtin_registry()),
        recovery_controller=recovery,
    ).run(contract, tmp_path, run_id="model-recovery-run")

    assert result.status == "completed"
    assert model.calls == 2
    assert delays == [0.25]


@pytest.mark.asyncio
async def test_agent_stops_repeated_identical_tool_failure_loop(tmp_path: Path) -> None:
    calls = [
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id=f"missing-{index}",
                    name="read_file",
                    arguments={"path": "missing.txt"},
                )
            ]
        )
        for index in range(3)
    ]
    model = ScriptedModel(calls)
    contract = TaskContract(
        task_id="tool-recovery-agent",
        objective="Do not loop forever on an impossible tool call",
        acceptance_criteria=["Repeated identical failures open the circuit"],
        allowed_tools={"read_file"},
    )

    result = await AgentLoop(
        model=model,
        executor=ToolExecutor(build_builtin_registry()),
    ).run(contract, tmp_path, run_id="tool-recovery-run")

    assert result.status == "failed"
    assert result.reason is not None
    assert "failed identically 3 times" in result.reason
    assert len(model.requests) == 3


@pytest.mark.asyncio
async def test_agent_loop_uses_session_memory_compaction_before_model_call(
    tmp_path: Path,
) -> None:
    class SummaryStub:
        async def summarize(
            self,
            messages: list[dict[str, Any]],
            *,
            previous_summary: str | None,
            focus: str,
            strategy: SessionMemoryStrategy,
        ) -> str:
            del messages, previous_summary, focus, strategy
            return "Earlier investigation and decisions were retained."

    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit-compacted",
                        name="submit_result",
                        arguments={"summary": "Compacted safely", "changed_files": []},
                    )
                ]
            ),
            ModelResponse(content="Completed with compacted context."),
        ]
    )
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
        config=ConversationCompactionConfig(
            min_recent_tokens=0,
            max_recent_tokens=10_000,
            min_recent_messages=1,
            max_recent_messages=2,
        ),
    )
    agent = AgentLoop(
        model=model,
        executor=ToolExecutor(build_builtin_registry()),
        context_compactor=AgentContextCompactor(
            conversation_compactor=conversation,
            budget=budget,
        ),
    )
    contract = TaskContract(
        task_id="compacted-agent",
        objective="Finish with a compacted history",
        acceptance_criteria=["Context is safely compacted"],
        allowed_tools={"submit_result"},
    )
    history = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": "旧" * 2_000}
        for index in range(10)
    ]

    result = await agent.run(
        contract,
        tmp_path,
        run_id="compacted-agent-run",
        conversation_history=history,
    )

    assert result.status == "completed"
    first_request = str(model.requests[0][0])
    assert "<new_context" in first_request
    assert "<notes>" in first_request
    assert "History index only" in first_request
    assert "# Task contract" in first_request


@pytest.mark.asyncio
async def test_agent_receives_structured_frontend_diagnostics(tmp_path: Path) -> None:
    (tmp_path / "diagnose.py").write_text(
        'print("src/app.ts(5,4): error TS2322: incompatible type")\nraise SystemExit(1)\n',
        encoding="utf-8",
    )
    contract = TaskContract(
        task_id="diagnostics-agent",
        objective="Inspect the configured frontend diagnostics",
        acceptance_criteria=["Use structured TypeScript findings"],
        allowed_tools={"get_diagnostics", "submit_result"},
        allowed_commands={"python"},
        diagnostic_commands=[
            DiagnosticCommand(
                id="types",
                program="python",
                args=["diagnose.py"],
                parser=DiagnosticParser.TYPESCRIPT,
            )
        ],
    )
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="diagnostics",
                        name="get_diagnostics",
                        arguments={"diagnostic_id": "types"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit-diagnostics",
                        name="submit_result",
                        arguments={
                            "summary": "Found the TypeScript assignment error.",
                            "changed_files": [],
                        },
                    )
                ]
            ),
            ModelResponse(content="The diagnostic points to src/app.ts:5:4."),
        ]
    )

    result = await AgentLoop(
        model=model,
        executor=ToolExecutor(build_builtin_registry()),
    ).run(contract, tmp_path, run_id="diagnostics-agent-run")

    assert result.status == "completed"
    diagnostic_result = str(model.requests[1][0])
    assert '"path": "src/app.ts"' in diagnostic_result
    assert '"code": "TS2322"' in diagnostic_result


@pytest.mark.asyncio
async def test_stop_gate_requires_persistent_task_graph_completion(tmp_path: Path) -> None:
    graph = TaskGraphManager()
    contract = TaskContract(
        task_id="task-graph-agent",
        objective="Track dependent work before completing",
        acceptance_criteria=["All task nodes have evidence"],
        allowed_tools={"update_task_graph", "submit_result"},
    )
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="graph-pending",
                        name="update_task_graph",
                        arguments={
                            "tasks": [
                                {
                                    "task_id": "inspect",
                                    "title": "Inspect the code",
                                    "status": "in_progress",
                                    "attempts": 1,
                                }
                            ]
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit-too-early",
                        name="submit_result",
                        arguments={"summary": "Too early", "changed_files": []},
                    )
                ]
            ),
            ModelResponse(content="Done too early."),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="graph-complete",
                        name="update_task_graph",
                        arguments={
                            "tasks": [
                                {
                                    "task_id": "inspect",
                                    "title": "Inspect the code",
                                    "status": "completed",
                                    "attempts": 1,
                                    "evidence": ["tool://read-code"],
                                }
                            ]
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit-complete",
                        name="submit_result",
                        arguments={"summary": "Graph complete", "changed_files": []},
                    )
                ]
            ),
            ModelResponse(content="Done with task evidence."),
        ]
    )

    result = await AgentLoop(
        model=model,
        executor=ToolExecutor(build_builtin_registry()),
    ).run(
        contract,
        tmp_path,
        run_id="task-graph-agent-run",
        task_graph_manager=graph,
    )

    assert result.status == "completed"
    assert graph.current is not None
    assert graph.current.tasks[0].status == "completed"
    gate_feedback = str(model.requests[3][0][-1]["content"])
    assert "task graph still contains unfinished tasks" in gate_feedback


@pytest.mark.asyncio
async def test_semantic_goal_evaluator_returns_missing_work_to_main_agent(
    tmp_path: Path,
) -> None:
    class SequencedEvaluator:
        def __init__(self) -> None:
            self.calls = []

        async def evaluate(self, goal):
            self.calls.append(goal)
            if len(self.calls) == 1:
                return GoalEvaluationResult(
                    evaluation=GoalEvaluation(
                        status="incomplete",
                        reason="The edge case is not addressed.",
                        missing=["Explain the empty-state behavior"],
                        evidence_refs=["outcome"],
                    ),
                    attempts=1,
                )
            return GoalEvaluationResult(
                evaluation=GoalEvaluation(
                    status="complete",
                    reason="The revised answer covers the edge case.",
                    evidence_refs=["outcome"],
                ),
                attempts=1,
            )

    evaluator = SequencedEvaluator()
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit-semantic",
                        name="submit_result",
                        arguments={"summary": "Explained behavior", "changed_files": []},
                    )
                ]
            ),
            ModelResponse(content="Revised explanation including the empty state."),
        ]
    )
    contract = TaskContract(
        task_id="semantic-goal",
        objective="Explain all search states",
        acceptance_criteria=["Include empty-state behavior"],
        allowed_tools={"submit_result"},
    )

    result = await AgentLoop(
        model=model,
        executor=ToolExecutor(build_builtin_registry()),
        goal_evaluator=evaluator,
    ).run(contract, tmp_path, run_id="semantic-goal-run")

    assert result.status == "completed"
    assert len(evaluator.calls) == 2
    assert "<goal_evaluation_blocked>" in str(model.requests[1][0][-1]["content"])


@pytest.mark.asyncio
async def test_deterministic_gate_runs_before_semantic_goal_evaluator(tmp_path: Path) -> None:
    class RecordingEvaluator:
        def __init__(self) -> None:
            self.calls = []

        async def evaluate(self, goal):
            self.calls.append(goal)
            return GoalEvaluationResult(
                evaluation=GoalEvaluation(
                    status="complete",
                    reason="The verified outcome covers the criterion.",
                    evidence_refs=["outcome", "verification:tests"],
                ),
                attempts=1,
            )

    (tmp_path / "value.txt").write_text("bad\n", encoding="utf-8")
    (tmp_path / "verify.py").write_text(
        "from pathlib import Path\n"
        "raise SystemExit(0 if Path('value.txt').read_text() == 'good\\n' else 1)\n",
        encoding="utf-8",
    )
    evaluator = RecordingEvaluator()
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="edit-goal",
                        name="apply_text_patch",
                        arguments={
                            "path": "value.txt",
                            "old_text": "bad\n",
                            "new_text": "good\n",
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit-before-tests",
                        name="submit_result",
                        arguments={"summary": "Changed value", "changed_files": ["value.txt"]},
                    )
                ]
            ),
            ModelResponse(content="Done before tests."),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="verify-goal",
                        name="run_process",
                        arguments={"program": "python", "args": ["verify.py"]},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit-after-tests",
                        name="submit_result",
                        arguments={"summary": "Changed value", "changed_files": ["value.txt"]},
                    )
                ]
            ),
            ModelResponse(content="Done after tests."),
        ]
    )
    contract = TaskContract(
        task_id="goal-after-gate",
        objective="Change and verify the value",
        acceptance_criteria=["Value is good"],
        verification_commands=[
            VerificationCommand(id="tests", program="python", args=["verify.py"])
        ],
    )

    result = await AgentLoop(
        model=model,
        executor=ToolExecutor(build_builtin_registry()),
        goal_evaluator=evaluator,
    ).run(contract, tmp_path, run_id="goal-after-gate-run")

    assert result.status == "completed"
    assert len(evaluator.calls) == 1
    assert "verification:tests" in evaluator.calls[0].available_evidence


@pytest.mark.asyncio
async def test_fresh_deterministic_verification_overrides_semantic_incomplete(
    tmp_path: Path,
) -> None:
    class IncompleteEvaluator:
        async def evaluate(self, goal):
            return GoalEvaluationResult(
                evaluation=GoalEvaluation(
                    status="incomplete",
                    reason="Semantic review is uncertain.",
                    missing=["More explanation"],
                    evidence_refs=["verification:tests"],
                ),
                attempts=1,
            )

    events: list[tuple[str, dict[str, Any]]] = []

    class RecordingSink:
        async def emit(self, run_id, event_type, data):
            events.append((event_type, data))

    (tmp_path / "value.txt").write_text("bad\n", encoding="utf-8")
    (tmp_path / "verify.py").write_text(
        "from pathlib import Path\n"
        "raise SystemExit(0 if Path('value.txt').read_text() == 'good\\n' else 1)\n",
        encoding="utf-8",
    )
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="edit",
                        name="apply_text_patch",
                        arguments={"path": "value.txt", "old_text": "bad\n", "new_text": "good\n"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="verify",
                        name="run_process",
                        arguments={"program": "python", "args": ["verify.py"]},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit",
                        name="submit_result",
                        arguments={"summary": "Changed value", "changed_files": ["value.txt"]},
                    )
                ]
            ),
        ]
    )
    contract = TaskContract(
        task_id="deterministic-authority",
        objective="Change and verify the value",
        acceptance_criteria=["Value is good"],
        verification_commands=[
            VerificationCommand(id="tests", program="python", args=["verify.py"])
        ],
    )

    result = await AgentLoop(
        model=model,
        executor=ToolExecutor(build_builtin_registry()),
        goal_evaluator=IncompleteEvaluator(),
        event_sink=RecordingSink(),
    ).run(contract, tmp_path, run_id="deterministic-authority")

    assert result.status == "completed"
    goal_event = next(data for event, data in events if event == "goal.evaluated")
    assert goal_event["authoritative"] is False
    assert goal_event["overridden_by"] == "deterministic_verification"


@pytest.mark.asyncio
async def test_goal_protocol_error_cannot_override_fresh_deterministic_verification(
    tmp_path: Path,
) -> None:
    class BrokenEvaluator:
        async def evaluate(self, goal):
            raise GoalEvaluationError("invalid evaluator response")

    events: list[str] = []

    class RecordingSink:
        async def emit(self, run_id, event_type, data):
            events.append(event_type)

    (tmp_path / "value.txt").write_text("good\n", encoding="utf-8")
    (tmp_path / "verify.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="edit",
                        name="apply_text_patch",
                        arguments={
                            "path": "value.txt",
                            "old_text": "good\n",
                            "new_text": "better\n",
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="verify",
                        name="run_process",
                        arguments={"program": "python", "args": ["verify.py"]},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit",
                        name="submit_result",
                        arguments={"summary": "Changed value", "changed_files": ["value.txt"]},
                    )
                ]
            ),
        ]
    )
    contract = TaskContract(
        task_id="goal-protocol-advisory",
        objective="Change and verify the value",
        acceptance_criteria=["Verification passes"],
        verification_commands=[
            VerificationCommand(id="tests", program="python", args=["verify.py"])
        ],
    )

    result = await AgentLoop(
        model=model,
        executor=ToolExecutor(build_builtin_registry()),
        goal_evaluator=BrokenEvaluator(),
        event_sink=RecordingSink(),
    ).run(contract, tmp_path, run_id="goal-protocol-advisory")

    assert result.status == "completed"
    assert "goal.evaluation_failed" in events
    assert "agent.failed" not in events


@pytest.mark.asyncio
async def test_parent_agent_receives_only_grounded_readonly_subagent_report(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("def normalize(value):\n    return value.strip()\n")
    child_model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="child-read",
                        name="read_file",
                        arguments={"path": "app.py"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="child-report",
                        name="submit_subagent_report",
                        arguments={
                            "summary": "normalize strips surrounding whitespace.",
                            "findings": [
                                {
                                    "path": "app.py",
                                    "line": 2,
                                    "observation": "The return expression strips the value.",
                                }
                            ],
                        },
                    )
                ]
            ),
        ]
    )
    parent_model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="delegate",
                        name="delegate_readonly",
                        arguments={
                            "role": "explore",
                            "task": "Find how normalize handles whitespace",
                            "context_paths": ["app.py"],
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit-parent",
                        name="submit_result",
                        arguments={
                            "summary": "normalize strips surrounding whitespace.",
                            "changed_files": [],
                        },
                    )
                ]
            ),
            ModelResponse(content="normalize uses value.strip()."),
        ]
    )
    executor = ToolExecutor(build_builtin_registry())
    subagents = ReadOnlySubagentManager(model=child_model, executor=executor)
    contract = TaskContract(
        task_id="parent-subagent",
        objective="Explain normalize behavior using a child exploration",
        acceptance_criteria=["Use grounded repository evidence"],
        allowed_tools={"delegate_readonly", "submit_result"},
    )

    result = await AgentLoop(
        model=parent_model,
        executor=executor,
        subagent_manager=subagents,
    ).run(contract, tmp_path, run_id="parent-subagent-run")

    assert result.status == "completed"
    parent_report_message = str(parent_model.requests[1][0])
    assert "return value.strip()" in parent_report_message
    assert "app.py" in parent_report_message
    child_tool_names = {item["function"]["name"] for item in child_model.requests[0][1]}
    assert "delegate_readonly" not in child_tool_names
    assert "apply_text_patch" not in child_tool_names


class ExternalReadInput(ToolInput):
    resource: str


class ExternalReadTool(BaseTool[ExternalReadInput]):
    name = "external_read"
    description = "Test-only external read"
    input_model = ExternalReadInput
    risk = ToolRisk.EXTERNAL

    async def run(
        self,
        arguments: ExternalReadInput,
        context: ToolContext,
    ) -> ToolExecution:
        return ToolExecution(output=f"external value for {arguments.resource}")


@pytest.mark.asyncio
async def test_agent_waits_for_interactive_permission_before_tool_execution(
    tmp_path: Path,
) -> None:
    contract = TaskContract(
        task_id="permission-agent",
        objective="Read the approved external status",
        acceptance_criteria=["The user approves the exact request"],
        allowed_tools={"external_read", "submit_result"},
    )
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="external-1",
                        name="external_read",
                        arguments={"resource": "status"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit-1",
                        name="submit_result",
                        arguments={"summary": "Read approved status", "changed_files": []},
                    )
                ]
            ),
            ModelResponse(content="Approved status read."),
        ]
    )
    registry = build_builtin_registry()
    registry.register(ExternalReadTool())
    broker = PermissionBroker(timeout_seconds=1)
    run = asyncio.create_task(
        AgentLoop(
            model=model,
            executor=ToolExecutor(registry, permission_authorizer=broker),
        ).run(contract, tmp_path, run_id="permission-agent-run")
    )
    for _ in range(100):
        if broker.pending:
            break
        await asyncio.sleep(0.005)
    assert broker.pending
    assert not run.done()

    await broker.resolve(broker.pending[0].request_id, PermissionDecision.APPROVE_ONCE)
    result = await run

    assert result.status == "completed"
    assert "external value for status" in str(model.requests[1][0])


@pytest.mark.asyncio
async def test_agent_discovers_and_loads_relevant_skill_on_demand(tmp_path: Path) -> None:
    skill = tmp_path / "skills" / "frontend-debugging" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\n"
        "name: frontend-debugging\n"
        "description: Diagnose browser-facing failures\n"
        "---\n\n"
        "# Frontend debugging\n\nInspect console failures before editing.\n",
        encoding="utf-8",
    )
    contract = TaskContract(
        task_id="skill-agent",
        objective="Explain the frontend debugging approach",
        acceptance_criteria=["Use relevant project guidance"],
        allowed_tools={"load_skill", "submit_result"},
    )
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="skill-1",
                        name="load_skill",
                        arguments={"name": "frontend-debugging"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit-1",
                        name="submit_result",
                        arguments={
                            "summary": "Followed the frontend debugging guidance.",
                            "changed_files": [],
                        },
                    )
                ]
            ),
            ModelResponse(content="Inspect console failures before editing."),
        ]
    )

    result = await AgentLoop(
        model=model,
        executor=ToolExecutor(build_builtin_registry()),
    ).run(contract, tmp_path, run_id="skill-agent-run")

    assert result.status == "completed"
    first_messages, first_tools = model.requests[0]
    assert "Diagnose browser-facing failures" in str(first_messages)
    assert "Inspect console failures before editing." not in str(first_messages)
    assert any(tool["function"]["name"] == "load_skill" for tool in first_tools)
    assert "Inspect console failures before editing." in str(model.requests[1][0])


@pytest.mark.asyncio
async def test_agent_edits_verifies_and_submits_result(tmp_path: Path) -> None:
    (tmp_path / "value.txt").write_text("bad\n", encoding="utf-8")
    (tmp_path / "verify_value.py").write_text(
        "from pathlib import Path\n"
        "value = Path('value.txt').read_text(encoding='utf-8')\n"
        "raise SystemExit(0 if value == 'good\\n' else 1)\n",
        encoding="utf-8",
    )
    contract = TaskContract(
        task_id="agent-integration",
        objective="Change value.txt from bad to good",
        acceptance_criteria=["The value test passes"],
        verification_commands=[
            VerificationCommand(id="tests", program="python", args=["verify_value.py"])
        ],
    )
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="edit-1",
                        name="apply_text_patch",
                        arguments={
                            "path": "value.txt",
                            "old_text": "bad\n",
                            "new_text": "good\n",
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="verify-1",
                        name="run_process",
                        arguments={"program": "python", "args": ["verify_value.py"]},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit-1",
                        name="submit_result",
                        arguments={
                            "summary": "Corrected the value.",
                            "changed_files": ["value.txt"],
                            "risk_notes": [],
                        },
                    )
                ]
            ),
            ModelResponse(content="Done."),
        ]
    )
    agent = AgentLoop(model=model, executor=ToolExecutor(build_builtin_registry()))

    result = await agent.run(contract, tmp_path, run_id="agent-run")

    assert result.status == "completed"
    assert result.outcome is not None
    assert result.outcome.changed_files == ["value.txt"]
    assert (tmp_path / "value.txt").read_text(encoding="utf-8") == "good\n"


@pytest.mark.asyncio
async def test_stop_gate_requires_verification_after_last_mutation(tmp_path: Path) -> None:
    (tmp_path / "value.txt").write_text("bad\n", encoding="utf-8")
    (tmp_path / "verify_value.py").write_text(
        "from pathlib import Path\n"
        "raise SystemExit(0 if Path('value.txt').read_text() in {'bad\\n', 'good\\n'} else 1)\n",
        encoding="utf-8",
    )
    verify_call = ToolCall(
        id="verify",
        name="run_process",
        arguments={"program": "python", "args": ["verify_value.py"]},
    )
    submit_call = ToolCall(
        id="submit",
        name="submit_result",
        arguments={"summary": "Changed value", "changed_files": ["value.txt"]},
    )
    contract = TaskContract(
        task_id="fresh-evidence",
        objective="Change value",
        acceptance_criteria=["Verification is fresh"],
        verification_commands=[
            VerificationCommand(id="verify", program="python", args=["verify_value.py"])
        ],
    )
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=[verify_call]),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="edit",
                        name="apply_text_patch",
                        arguments={
                            "path": "value.txt",
                            "old_text": "bad\n",
                            "new_text": "good\n",
                        },
                    )
                ]
            ),
            ModelResponse(tool_calls=[submit_call]),
            ModelResponse(content="Done too early"),
            ModelResponse(tool_calls=[verify_call.model_copy(update={"id": "verify-2"})]),
            ModelResponse(tool_calls=[submit_call.model_copy(update={"id": "submit-2"})]),
            ModelResponse(content="Done with fresh evidence"),
        ]
    )
    agent = AgentLoop(model=model, executor=ToolExecutor(build_builtin_registry()))

    result = await agent.run(contract, tmp_path, run_id="fresh-run")

    assert result.status == "completed"
    assert len(model.requests) == 6
    gate_feedback = model.requests[4][0][-1]["content"]
    assert "must be rerun after the last mutation" in gate_feedback


@pytest.mark.asyncio
async def test_agent_can_pause_for_user_clarification(tmp_path: Path) -> None:
    contract = TaskContract(
        task_id="clarification",
        objective="Implement the requested behavior",
        acceptance_criteria=["The ambiguous choice is clarified"],
        allowed_tools={"ask_user"},
    )
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="ask-1",
                        name="ask_user",
                        arguments={"question": "Should this support mobile layouts too?"},
                    )
                ]
            )
        ]
    )
    agent = AgentLoop(model=model, executor=ToolExecutor(build_builtin_registry()))

    result = await agent.run(contract, tmp_path, run_id="clarification-run")

    assert result.status == "waiting_for_user"
    assert result.final_text == "Should this support mobile layouts too?"


@pytest.mark.asyncio
async def test_read_only_conversation_turn_keeps_history_without_running_tests(
    tmp_path: Path,
) -> None:
    contract = TaskContract(
        task_id="read-only-chat",
        objective="Explain the existing code",
        acceptance_criteria=["Answer the user's question"],
        verification_commands=[
            VerificationCommand(id="tests", program="python", args=["missing-test.py"])
        ],
    )
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit-read-only",
                        name="submit_result",
                        arguments={
                            "summary": "The module validates user input.",
                            "changed_files": [],
                        },
                    )
                ]
            ),
            ModelResponse(content="It validates user input before saving."),
        ]
    )
    agent = AgentLoop(model=model, executor=ToolExecutor(build_builtin_registry()))

    result = await agent.run(
        contract,
        tmp_path,
        run_id="read-only-run",
        conversation_history=[
            {"role": "user", "content": "Earlier question"},
            {"role": "assistant", "content": "Earlier answer"},
        ],
    )

    assert result.status == "completed"
    assert model.requests[0][0][1:3] == [
        {"role": "user", "content": "Earlier question"},
        {"role": "assistant", "content": "Earlier answer"},
    ]


@pytest.mark.asyncio
async def test_user_steering_supersedes_plan_and_forces_replanning(tmp_path: Path) -> None:
    plan = PlanManager(scope_id="steering")
    await plan.replace([PlanItem(content="Fix search", status=PlanStatus.IN_PROGRESS)])
    control = SteeringInbox(scope_id="steering")
    await control.submit(ControlIntent.STEER, "Fix login first")
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="plan-1",
                        name="update_plan",
                        arguments={
                            "direction": "Fix login first",
                            "items": [{"content": "Fix login", "status": "completed"}],
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit-1",
                        name="submit_result",
                        arguments={"summary": "Login inspected", "changed_files": []},
                    )
                ]
            ),
            ModelResponse(content="Done."),
        ]
    )
    contract = TaskContract(
        task_id="steering-test",
        objective="Inspect the requested code",
        acceptance_criteria=["Address the latest direction"],
        allowed_tools={"update_plan", "submit_result"},
    )

    result = await AgentLoop(model=model, executor=ToolExecutor(build_builtin_registry())).run(
        contract, tmp_path, plan_manager=plan, control=control
    )

    assert result.status == "completed"
    assert plan.current is not None
    assert plan.current.version == 3
    assert plan.current.direction == "Fix login first"
    assert "<user_steering>Fix login first" in model.requests[0][0][-1]["content"]


@pytest.mark.asyncio
async def test_user_can_interrupt_at_safe_boundary(tmp_path: Path) -> None:
    control = SteeringInbox(scope_id="interrupt")
    await control.submit(ControlIntent.INTERRUPT)
    model = ScriptedModel([])
    contract = TaskContract(
        task_id="interrupt-test",
        objective="Stop safely",
        acceptance_criteria=["The run is interrupted"],
    )

    result = await AgentLoop(model=model, executor=ToolExecutor(build_builtin_registry())).run(
        contract, tmp_path, control=control
    )

    assert result.status == "interrupted"
    assert result.turns == 0


@pytest.mark.asyncio
async def test_steering_during_model_call_cancels_stale_tool_execution(
    tmp_path: Path,
) -> None:
    class DelayedFirstModel:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.requests: list[list[dict[str, Any]]] = []
            self.call_number = 0

        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
        ) -> ModelResponse:
            del tools
            self.requests.append(messages)
            self.call_number += 1
            if self.call_number == 1:
                self.started.set()
                await self.release.wait()
                return ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="stale-edit",
                            name="apply_text_patch",
                            arguments={
                                "path": "value.txt",
                                "old_text": "original\n",
                                "new_text": "stale direction\n",
                            },
                        )
                    ]
                )
            if self.call_number == 2:
                return ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="replan",
                            name="update_plan",
                            arguments={
                                "direction": "Inspect only",
                                "items": [
                                    {"content": "Inspect without editing", "status": "completed"}
                                ],
                            },
                        )
                    ]
                )
            if self.call_number == 3:
                return ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="submit",
                            name="submit_result",
                            arguments={"summary": "Inspected only", "changed_files": []},
                        )
                    ]
                )
            return ModelResponse(content="Done without changing the file.")

        async def aclose(self) -> None:
            return None

    (tmp_path / "value.txt").write_text("original\n", encoding="utf-8")
    plan = PlanManager(scope_id="mid-call-steering")
    await plan.replace([PlanItem(content="Edit the file", status=PlanStatus.IN_PROGRESS)])
    control = SteeringInbox(scope_id="mid-call-steering")
    model = DelayedFirstModel()
    contract = TaskContract(
        task_id="mid-call-steering",
        objective="Follow the current user direction",
        acceptance_criteria=["The newest direction wins"],
        allowed_tools={"apply_text_patch", "update_plan", "submit_result"},
    )

    run_task = asyncio.create_task(
        AgentLoop(model=model, executor=ToolExecutor(build_builtin_registry())).run(
            contract,
            tmp_path,
            plan_manager=plan,
            control=control,
        )
    )
    await model.started.wait()
    await control.submit(ControlIntent.STEER, "Inspect only")
    model.release.set()
    result = await run_task

    assert result.status == "completed"
    assert (tmp_path / "value.txt").read_text(encoding="utf-8") == "original\n"
    assert plan.current is not None
    assert plan.current.direction == "Inspect only"
    assert plan.current.version == 3
    assert any(
        "Cancelled before execution by a user control message" in str(message.get("content"))
        for message in model.requests[1]
    )
    assert any(
        "<user_steering>Inspect only</user_steering>" in str(message.get("content"))
        for message in model.requests[1]
    )


@pytest.mark.asyncio
async def test_steering_between_mutations_cancels_remaining_stale_tools(
    tmp_path: Path,
) -> None:
    (tmp_path / "first.txt").write_text("old\n", encoding="utf-8")
    (tmp_path / "second.txt").write_text("old\n", encoding="utf-8")
    plan = PlanManager(scope_id="between-tools")
    await plan.replace([PlanItem(content="Edit both files", status=PlanStatus.IN_PROGRESS)])
    control = SteeringInbox(scope_id="between-tools")
    hook_bus = AsyncHookBus()

    async def steer_after_first_tool(context: HookContext) -> None:
        if context.tool_call is not None and context.tool_call.id == "first-edit":
            await control.submit(ControlIntent.STEER, "Keep the second file unchanged")

    hook_bus.register(HookEvent.POST_TOOL_USE, steer_after_first_tool)
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="first-edit",
                        name="apply_text_patch",
                        arguments={
                            "path": "first.txt",
                            "old_text": "old\n",
                            "new_text": "new\n",
                        },
                    ),
                    ToolCall(
                        id="second-edit",
                        name="apply_text_patch",
                        arguments={
                            "path": "second.txt",
                            "old_text": "old\n",
                            "new_text": "stale\n",
                        },
                    ),
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="replan",
                        name="update_plan",
                        arguments={
                            "direction": "Keep the second file unchanged",
                            "items": [{"content": "Keep second.txt", "status": "completed"}],
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="submit",
                        name="submit_result",
                        arguments={
                            "summary": "Updated only first.txt",
                            "changed_files": ["first.txt"],
                        },
                    )
                ]
            ),
            ModelResponse(content="Done."),
        ]
    )
    contract = TaskContract(
        task_id="between-tools",
        objective="Follow steering at every safe boundary",
        acceptance_criteria=["Do not run stale mutations"],
        allowed_tools={"apply_text_patch", "update_plan", "submit_result"},
    )
    executor = ToolExecutor(build_builtin_registry(), hook_bus=hook_bus)

    result = await AgentLoop(
        model=model,
        executor=executor,
        hook_bus=hook_bus,
    ).run(contract, tmp_path, plan_manager=plan, control=control)

    assert result.status == "submitted_unverified"
    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "new\n"
    assert (tmp_path / "second.txt").read_text(encoding="utf-8") == "old\n"
    assert "Cancelled before execution" in str(model.requests[1][0][-2:])
