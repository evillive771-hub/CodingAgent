import json
from pathlib import Path

import pytest

from aicoding_agent.domain.models import (
    Budget,
    DiagnosticCommand,
    DiagnosticParser,
    TaskContract,
    ToolCall,
)
from aicoding_agent.tools.base import ToolContext
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.diagnostics import parse_diagnostics
from aicoding_agent.tools.executor import ToolExecutor


def diagnostic_contract(command: DiagnosticCommand, **updates: object) -> TaskContract:
    payload = {
        "task_id": "diagnostics",
        "objective": "Collect structured frontend diagnostics",
        "acceptance_criteria": ["Diagnostics are bounded and contract controlled"],
        "allowed_tools": {"get_diagnostics"},
        "allowed_commands": {"python"},
        "diagnostic_commands": [command],
    }
    payload.update(updates)
    return TaskContract.model_validate(payload)


def test_parses_typescript_parenthesized_and_colon_formats(tmp_path: Path) -> None:
    command = DiagnosticCommand(
        id="types",
        program="python",
        parser=DiagnosticParser.TYPESCRIPT,
    )
    output = (
        "src/app.ts(4,9): error TS2322: Type 'string' is not assignable to type 'number'.\n"
        "src/view.tsx:7:3 - warning TS6133: 'value' is declared but never read.\n"
        "C:/outside/secret.ts(1,1): error TS1000: outside\n"
    )

    records = parse_diagnostics(
        command,
        stdout=output,
        stderr="",
        workspace=tmp_path,
        cwd=tmp_path,
        max_results=10,
    )

    assert [item.path for item in records] == ["src/app.ts", "src/view.tsx"]
    assert records[0].code == "TS2322"
    assert records[0].severity == "error"
    assert records[1].severity == "warning"


def test_parses_eslint_json_and_filters_outside_workspace(tmp_path: Path) -> None:
    inside = tmp_path / "src" / "app.js"
    outside = tmp_path.parent / "outside.js"
    output = json.dumps(
        [
            {
                "filePath": str(inside),
                "messages": [
                    {
                        "line": 2,
                        "column": 5,
                        "severity": 2,
                        "ruleId": "no-undef",
                        "message": "value is not defined",
                    }
                ],
            },
            {
                "filePath": str(outside),
                "messages": [{"line": 1, "column": 1, "severity": 2, "message": "outside"}],
            },
        ]
    )
    command = DiagnosticCommand(
        id="lint",
        program="python",
        parser=DiagnosticParser.ESLINT_JSON,
    )

    records = parse_diagnostics(
        command,
        stdout=output,
        stderr="",
        workspace=tmp_path,
        cwd=tmp_path,
        max_results=10,
    )

    assert len(records) == 1
    assert records[0].path == "src/app.js"
    assert records[0].code == "no-undef"


@pytest.mark.asyncio
async def test_tool_runs_only_frozen_diagnostic_and_returns_structured_failure(
    tmp_path: Path,
) -> None:
    (tmp_path / "diagnose.py").write_text(
        'print("src/app.ts(3,8): error TS2322: incompatible type")\nraise SystemExit(2)\n',
        encoding="utf-8",
    )
    command = DiagnosticCommand(
        id="types",
        program="python",
        args=["diagnose.py"],
        parser=DiagnosticParser.TYPESCRIPT,
    )
    contract = diagnostic_contract(command)

    result = await ToolExecutor(build_builtin_registry()).execute(
        ToolCall(
            id="diagnostic-1",
            name="get_diagnostics",
            arguments={"diagnostic_id": "types"},
        ),
        ToolContext(workspace=tmp_path, contract=contract),
    )

    assert result.ok
    assert result.data["exit_code"] == 2
    assert result.data["errors"] == 1
    assert result.data["diagnostics"][0]["path"] == "src/app.ts"
    assert result.data.get("verification_id") is None
    assert "errors=1" in result.output


@pytest.mark.asyncio
async def test_policy_denies_unknown_diagnostic_id(tmp_path: Path) -> None:
    command = DiagnosticCommand(id="types", program="python")
    contract = diagnostic_contract(command)

    result = await ToolExecutor(build_builtin_registry()).execute(
        ToolCall(
            id="diagnostic-unknown",
            name="get_diagnostics",
            arguments={"diagnostic_id": "other"},
        ),
        ToolContext(workspace=tmp_path, contract=contract),
    )

    assert not result.ok
    assert result.policy_denied
    assert "Unknown task-contract diagnostic ID" in result.output


@pytest.mark.asyncio
async def test_policy_denies_diagnostic_executable_outside_command_allowlist(
    tmp_path: Path,
) -> None:
    command = DiagnosticCommand(id="lint", program="node", args=["lint.js"])
    contract = diagnostic_contract(command)

    result = await ToolExecutor(build_builtin_registry()).execute(
        ToolCall(
            id="diagnostic-node",
            name="get_diagnostics",
            arguments={"diagnostic_id": "lint"},
        ),
        ToolContext(workspace=tmp_path, contract=contract),
    )

    assert not result.ok
    assert result.policy_denied
    assert "Diagnostic executable 'node' is not allowed" in result.output


@pytest.mark.asyncio
async def test_diagnostic_data_and_output_stay_within_contract_budget(tmp_path: Path) -> None:
    lines = "\n".join(f"src/file{index}.ts(1,1): error TS1000: {'x' * 900}" for index in range(100))
    (tmp_path / "many.py").write_text(
        f"print({lines!r})\nraise SystemExit(1)\n",
        encoding="utf-8",
    )
    command = DiagnosticCommand(
        id="many",
        program="python",
        args=["many.py"],
        parser=DiagnosticParser.TYPESCRIPT,
    )
    contract = diagnostic_contract(
        command,
        budget=Budget(max_tool_output_chars=4000),
    )

    result = await ToolExecutor(build_builtin_registry()).execute(
        ToolCall(
            id="diagnostic-many",
            name="get_diagnostics",
            arguments={"diagnostic_id": "many", "max_results": 100},
        ),
        ToolContext(workspace=tmp_path, contract=contract),
    )

    serialized_data = json.dumps(result.data, ensure_ascii=False)
    assert result.ok
    assert result.data["truncated"]
    assert len(result.output) <= 2000
    assert len(serialized_data) < 3000


def test_contract_rejects_shell_expression_as_diagnostic_program() -> None:
    with pytest.raises(ValueError, match="executable name"):
        DiagnosticCommand(id="unsafe", program="npm && echo", args=[])
