import asyncio
import json
from pathlib import Path

import pytest

from aicoding_agent.agent.permission import (
    PermissionBroker,
    PermissionDecision,
    PermissionStore,
)
from aicoding_agent.domain.models import TaskContract, ToolCall, ToolRisk
from aicoding_agent.tools.base import (
    BaseTool,
    ToolContext,
    ToolExecution,
    ToolInput,
    ToolRegistry,
)
from aicoding_agent.tools.executor import ToolExecutor


class ExternalLookupInput(ToolInput):
    query: str


class ExternalLookupTool(BaseTool[ExternalLookupInput]):
    name = "external_lookup"
    description = "Test-only external lookup"
    input_model = ExternalLookupInput
    risk = ToolRisk.EXTERNAL

    async def run(
        self,
        arguments: ExternalLookupInput,
        context: ToolContext,
    ) -> ToolExecution:
        return ToolExecution(output=f"looked up {arguments.query}")


def external_executor(
    broker: PermissionBroker | None = None,
) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(ExternalLookupTool())
    return ToolExecutor(registry, permission_authorizer=broker)


def external_context(tmp_path: Path, *, allowed: bool = True) -> ToolContext:
    task = TaskContract(
        task_id="permission-tool",
        objective="Test interactive permission",
        acceptance_criteria=["ASK is resolved by the host"],
        allowed_tools={"external_lookup"} if allowed else {"read_file"},
    )
    return ToolContext(workspace=tmp_path, contract=task, run_id="permission-run")


async def wait_for_request(broker: PermissionBroker) -> str:
    deadline = asyncio.get_running_loop().time() + 1
    while asyncio.get_running_loop().time() < deadline:
        if broker.pending:
            return broker.pending[0].request_id
        await asyncio.sleep(0.005)
    raise AssertionError("Permission request did not become pending")


@pytest.mark.asyncio
async def test_approve_once_unblocks_only_one_exact_request(tmp_path: Path) -> None:
    broker = PermissionBroker(
        store=PermissionStore(tmp_path),
        scope_id="session",
        timeout_seconds=1,
    )
    first = asyncio.create_task(
        broker.authorize(
            run_id="run-1",
            tool_name="external_lookup",
            risk=ToolRisk.EXTERNAL,
            arguments={"query": "one"},
            reason="External access requires confirmation",
        )
    )
    request_id = await wait_for_request(broker)

    await broker.resolve(request_id[:8], PermissionDecision.APPROVE_ONCE)

    assert await first is True
    second = asyncio.create_task(
        broker.authorize(
            run_id="run-2",
            tool_name="external_lookup",
            risk=ToolRisk.EXTERNAL,
            arguments={"query": "one"},
            reason="External access requires confirmation",
        )
    )
    second_id = await wait_for_request(broker)
    assert second_id != request_id
    await broker.resolve(second_id, PermissionDecision.DENY)
    assert await second is False


@pytest.mark.asyncio
async def test_session_approval_reuses_only_matching_tool_and_arguments(tmp_path: Path) -> None:
    store = PermissionStore(tmp_path)
    broker = PermissionBroker(store=store, scope_id="session", timeout_seconds=1)
    first = asyncio.create_task(
        broker.authorize(
            run_id="run-1",
            tool_name="external_lookup",
            risk=ToolRisk.EXTERNAL,
            arguments={"query": "same"},
            reason="Ask",
        )
    )
    request_id = await wait_for_request(broker)
    await broker.resolve(request_id, PermissionDecision.APPROVE_SESSION)
    assert await first is True

    restarted = PermissionBroker(store=store, scope_id="session", timeout_seconds=1)
    assert await restarted.authorize(
        run_id="run-2",
        tool_name="external_lookup",
        risk=ToolRisk.EXTERNAL,
        arguments={"query": "same"},
        reason="Ask",
    )
    assert restarted.pending == ()

    changed = asyncio.create_task(
        restarted.authorize(
            run_id="run-3",
            tool_name="external_lookup",
            risk=ToolRisk.EXTERNAL,
            arguments={"query": "different"},
            reason="Ask",
        )
    )
    changed_id = await wait_for_request(restarted)
    await restarted.resolve(changed_id, PermissionDecision.DENY)
    assert await changed is False


@pytest.mark.asyncio
async def test_persistence_redacts_and_bounds_argument_preview(tmp_path: Path) -> None:
    broker = PermissionBroker(
        store=PermissionStore(tmp_path),
        scope_id="redaction",
        timeout_seconds=1,
    )
    pending = asyncio.create_task(
        broker.authorize(
            run_id="run-secret",
            tool_name="external_lookup",
            risk=ToolRisk.EXTERNAL,
            arguments={"api_key": "real-secret", "query": "x" * 2000},
            reason="Ask",
        )
    )
    request_id = await wait_for_request(broker)
    request = broker.pending[0]
    assert request.argument_preview["api_key"] == "<redacted>"
    assert len(request.argument_preview["query"]) < 600
    await broker.resolve(request_id, PermissionDecision.DENY)
    assert await pending is False

    persisted = (tmp_path / "redaction" / "permissions.jsonl").read_text(encoding="utf-8")
    assert "real-secret" not in persisted
    assert len(json.loads(persisted.splitlines()[0])["request"]["argument_preview"]["query"]) < 600


@pytest.mark.asyncio
async def test_restart_denies_an_orphaned_pending_request(tmp_path: Path) -> None:
    store = PermissionStore(tmp_path)
    broker = PermissionBroker(store=store, scope_id="restart", timeout_seconds=60)
    pending = asyncio.create_task(
        broker.authorize(
            run_id="run-before-restart",
            tool_name="external_lookup",
            risk=ToolRisk.EXTERNAL,
            arguments={"query": "one"},
            reason="Ask",
        )
    )
    await wait_for_request(broker)
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)

    restarted = PermissionBroker(store=store, scope_id="restart", timeout_seconds=1)
    await restarted.initialize()

    assert restarted.pending == ()
    events = await store.load("restart")
    assert [event.action for event in events] == ["requested", "resolved"]
    assert events[-1].resolution is not None
    assert events[-1].resolution.decision == PermissionDecision.DENY


@pytest.mark.asyncio
async def test_executor_fails_closed_without_interactive_authorizer(tmp_path: Path) -> None:
    result = await external_executor().execute(
        ToolCall(
            id="external-1",
            name="external_lookup",
            arguments={"query": "status"},
        ),
        external_context(tmp_path),
    )

    assert not result.ok
    assert result.policy_denied
    assert "Policy ask" in result.output


@pytest.mark.asyncio
async def test_executor_runs_ask_tool_only_after_exact_approval(tmp_path: Path) -> None:
    broker = PermissionBroker(timeout_seconds=1)
    execution = asyncio.create_task(
        external_executor(broker).execute(
            ToolCall(
                id="external-2",
                name="external_lookup",
                arguments={"query": "status"},
            ),
            external_context(tmp_path),
        )
    )
    request_id = await wait_for_request(broker)
    assert not execution.done()

    await broker.resolve(request_id, PermissionDecision.APPROVE_ONCE)
    result = await execution

    assert result.ok
    assert result.output == "looked up status"


@pytest.mark.asyncio
async def test_permission_cannot_override_contract_deny(tmp_path: Path) -> None:
    broker = PermissionBroker(timeout_seconds=1)

    result = await external_executor(broker).execute(
        ToolCall(
            id="external-3",
            name="external_lookup",
            arguments={"query": "status"},
        ),
        external_context(tmp_path, allowed=False),
    )

    assert not result.ok
    assert result.policy_denied
    assert broker.pending == ()
    assert "not allowed by the task contract" in result.output
