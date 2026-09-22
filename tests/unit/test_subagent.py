from pathlib import Path

import pytest
from conftest import ScriptedModel

from aicoding_agent.agent.subagent import ReadOnlySubagentManager
from aicoding_agent.domain.models import (
    ModelResponse,
    SubagentFinding,
    SubagentReport,
    SubagentRole,
    TaskContract,
    ToolCall,
)
from aicoding_agent.tools.base import ToolContext
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor
from aicoding_agent.tools.subagent import _bounded_report


def parent_context(tmp_path: Path) -> ToolContext:
    return ToolContext(
        workspace=tmp_path,
        contract=TaskContract(
            task_id="parent",
            objective="Parent task content must not become child transcript history",
            acceptance_criteria=["Child report is read-only and grounded"],
        ),
        run_id="parent-run",
    )


@pytest.mark.asyncio
async def test_submit_report_reloads_real_file_line_for_grounding(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("first\nactual line\n", encoding="utf-8")
    contract = TaskContract(
        task_id="child-report",
        objective="Submit grounded findings",
        acceptance_criteria=["Host attaches the real line"],
        allowed_tools={"submit_subagent_report"},
    )
    context = ToolContext(workspace=tmp_path, contract=contract)
    context.state["subagent_role"] = SubagentRole.REVIEW

    result = await ToolExecutor(build_builtin_registry()).execute(
        ToolCall(
            id="report",
            name="submit_subagent_report",
            arguments={
                "summary": "Found the relevant line.",
                "findings": [
                    {"path": "app.py", "line": 2, "observation": "This line is relevant."}
                ],
            },
        ),
        context,
    )

    assert result.ok
    report = context.state["subagent_report"]
    assert report.findings[0].snippet == "actual line"


@pytest.mark.asyncio
async def test_child_receives_only_read_tools_and_isolated_delegated_prompt(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="read",
                        name="read_file",
                        arguments={"path": "app.py"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="report",
                        name="submit_subagent_report",
                        arguments={
                            "summary": "The value is defined in app.py.",
                            "findings": [
                                {
                                    "path": "app.py",
                                    "line": 1,
                                    "observation": "The module defines value.",
                                }
                            ],
                        },
                    )
                ]
            ),
        ]
    )
    executor = ToolExecutor(build_builtin_registry())
    manager = ReadOnlySubagentManager(model=model, executor=executor)

    report = await manager.run(
        role=SubagentRole.EXPLORE,
        task="Find where value is defined",
        context_paths=["app.py"],
        max_turns=4,
        parent_context=parent_context(tmp_path),
    )

    assert report.status == "completed"
    assert report.findings[0].snippet == "value = 1"
    first_messages, first_tools = model.requests[0]
    tool_names = {tool["function"]["name"] for tool in first_tools}
    assert tool_names == {
        "read_file",
        "list_files",
        "search_text",
        "submit_subagent_report",
    }
    assert "delegate_readonly" not in tool_names
    assert "apply_text_patch" not in tool_names
    assert "Parent task content" not in str(first_messages)
    assert "Find where value is defined" in str(first_messages)


@pytest.mark.asyncio
async def test_child_cannot_call_write_or_recursive_delegate_even_if_model_requests_it(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("original\n", encoding="utf-8")
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="forbidden-write",
                        name="apply_text_patch",
                        arguments={
                            "path": "app.py",
                            "old_text": "original\n",
                            "new_text": "changed\n",
                        },
                    ),
                    ToolCall(
                        id="forbidden-recursion",
                        name="delegate_readonly",
                        arguments={"task": "Delegate again"},
                    ),
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="blocked-report",
                        name="submit_subagent_report",
                        arguments={
                            "status": "blocked",
                            "summary": "Forbidden actions were denied.",
                            "uncertainties": ["No further delegation is permitted"],
                        },
                    )
                ]
            ),
        ]
    )
    executor = ToolExecutor(build_builtin_registry())

    report = await ReadOnlySubagentManager(model=model, executor=executor).run(
        role=SubagentRole.REVIEW,
        task="Review without changes",
        context_paths=["app.py"],
        max_turns=4,
        parent_context=parent_context(tmp_path),
    )

    assert report.status == "blocked"
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "original\n"
    denied_results = str(model.requests[1][0])
    assert "not allowed by the task contract" in denied_results


@pytest.mark.asyncio
async def test_invalid_grounding_line_fails_report_tool(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("one line\n", encoding="utf-8")
    contract = TaskContract(
        task_id="invalid-grounding",
        objective="Reject invented line evidence",
        acceptance_criteria=["Line exists"],
        allowed_tools={"submit_subagent_report"},
    )
    context = ToolContext(workspace=tmp_path, contract=contract)
    context.state["subagent_role"] = SubagentRole.EXPLORE

    result = await ToolExecutor(build_builtin_registry()).execute(
        ToolCall(
            id="invalid-report",
            name="submit_subagent_report",
            arguments={
                "summary": "Invented",
                "findings": [{"path": "app.py", "line": 99, "observation": "Not real"}],
            },
        ),
        context,
    )

    assert not result.ok
    assert "outside the file" in result.output


def test_parent_visible_report_remains_valid_json_when_findings_are_trimmed() -> None:
    report = SubagentReport(
        role="review",
        status="completed",
        summary="Large report",
        findings=[
            SubagentFinding(
                path=f"src/file{index}.py",
                line=1,
                observation="x" * 1000,
                snippet="y" * 1000,
            )
            for index in range(10)
        ],
    )

    serialized, output, truncated = _bounded_report(report, limit=1200)

    import json

    assert json.loads(output) == serialized
    assert len(output) <= 1200
    assert truncated
    assert len(serialized["findings"]) < 10
