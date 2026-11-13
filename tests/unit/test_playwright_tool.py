import asyncio
import time
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from aicoding_agent.domain.models import TaskContract, ToolCall
from aicoding_agent.regression.budget import RegressionBudget
from aicoding_agent.tools.base import ToolContext
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor


class FakeResult(BaseModel):
    verification_id: str = "ui"
    attempt_id: str = "attempt"
    status: str
    exit_code: int = 0
    failures: list[dict[str, str]] = []
    evidence_refs: list[str] = []
    reason: str | None = None


class FakeRunner:
    def __init__(self, statuses: list[str], *, valid: bool = True) -> None:
        self.statuses = statuses
        self.valid = valid
        self.calls = 0
        self.prior_environment_failures = 0

    def has_suite(self, suite_ref: str) -> bool:
        return suite_ref == "counter-public"

    async def run(self, **kwargs: object) -> FakeResult:
        status = self.statuses[self.calls]
        self.calls += 1
        return FakeResult(status=status, failures=[{"message": "actual -1, expected 0"}])

    async def revalidate(self, workspace: Path, result: FakeResult) -> bool:
        return self.valid

    async def run_count(self, run_id: str, verification_id: str) -> int:
        return self.calls

    async def environment_failure_count(self, run_id: str, verification_id: str) -> int:
        return self.prior_environment_failures


def contract(**updates: object) -> TaskContract:
    payload = {
        "task_id": "browser-test",
        "objective": "Fix counter",
        "acceptance_criteria": ["Counter cannot become negative"],
        "allowed_tools": {"run_playwright", "create_file"},
        "browser_verifications": [{"id": "ui", "suite_ref": "counter-public"}],
    }
    payload.update(updates)
    return TaskContract.model_validate(payload)


async def execute(context: ToolContext, **arguments: str):
    return await ToolExecutor(build_builtin_registry()).execute(
        ToolCall(
            id="test-call", name="run_playwright", arguments=arguments or {"verification_id": "ui"}
        ),
        context,
    )


@pytest.mark.asyncio
async def test_shared_browser_budget_survives_candidate_workspace_retry(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    limits = contract(browser_verifications=[{
        "id": "ui", "suite_ref": "counter-public", "max_runs": 1,
    }])
    budget = await RegressionBudget.initialize(tmp_path / "budget", "shared", limits.budget)
    first_runner = FakeRunner(["passed"])
    context = ToolContext(workspace=first, contract=limits, browser_runner=first_runner,
                          shared_budget=budget)
    result = await execute(context)
    assert result.data["status"] == "passed"
    resumed = await RegressionBudget.initialize(tmp_path / "budget", "shared", limits.budget)
    next_runner = FakeRunner(["passed"])
    retry_context = ToolContext(workspace=second, contract=limits, browser_runner=next_runner,
                                shared_budget=resumed)
    result = await execute(retry_context)
    assert result.data["status"] == "budget_exhausted"
    assert next_runner.calls == 0


@pytest.mark.asyncio
async def test_environment_retry_is_charged_to_shared_tools_and_browser_runs(tmp_path):
    limits = contract()
    budget = await RegressionBudget.initialize(tmp_path / "budget", "shared", limits.budget)
    runner = FakeRunner(["infra_error", "passed"])
    context = ToolContext(workspace=tmp_path, contract=limits, browser_runner=runner,
                          shared_budget=budget)
    result = await execute(context)
    assert result.data["status"] == "passed"
    state = await budget.refresh()
    assert state.browser_runs["ui"] == 2
    assert state.tool_calls == 1  # The first tool invocation is charged by AgentLoop.


def test_contract_preserves_legacy_and_rejects_duplicate_ids() -> None:
    assert contract(browser_verifications=[]).browser_verifications == []
    with pytest.raises(ValidationError, match="unique"):
        contract(verification_commands=[{"id": "ui", "program": "node"}])


@pytest.mark.asyncio
async def test_failure_feedback_is_normal_result_not_false_success(tmp_path: Path) -> None:
    context = ToolContext(
        workspace=tmp_path, contract=contract(), browser_runner=FakeRunner(["failed"])
    )
    context.state["verification_success"]["ui"] = 1
    result = await execute(context)
    assert result.ok
    assert result.data["status"] == "failed"
    assert "actual -1" in result.data["failures"][0]["message"]
    assert "ui" not in context.state["verification_success"]


@pytest.mark.asyncio
async def test_unknown_or_extra_parameters_do_not_run_host(tmp_path: Path) -> None:
    runner = FakeRunner(["passed"])
    context = ToolContext(workspace=tmp_path, contract=contract(), browser_runner=runner)
    result = await execute(context, verification_id="hidden")
    assert result.policy_denied
    result = await execute(context, verification_id="ui", url="http://malicious")
    assert not result.ok
    assert runner.calls == 0


@pytest.mark.asyncio
async def test_passed_evidence_invalidated_by_later_write(tmp_path: Path) -> None:
    context = ToolContext(
        workspace=tmp_path, contract=contract(), browser_runner=FakeRunner(["passed"])
    )
    executor = ToolExecutor(build_builtin_registry())
    result = await execute(context)
    assert result.ok
    assert context.state["verification_success"]["ui"] == 1
    result = await executor.execute(
        ToolCall(
            id="write",
            name="create_file",
            arguments={
                "path": "counter.js",
                "content": "broken",
            },
        ),
        context,
    )
    assert result.is_mutation
    assert "ui" not in context.state["verification_success"]


@pytest.mark.asyncio
async def test_changed_evidence_cannot_pass_and_recovery_is_bounded(tmp_path: Path) -> None:
    runner = FakeRunner(["passed", "passed"], valid=False)
    context = ToolContext(workspace=tmp_path, contract=contract(), browser_runner=runner)
    result = await execute(context)
    assert result.data["status"] == "infra_error"
    assert context.state["browser_terminal_status"] == "failed"
    assert runner.calls == 2
    assert not context.state["verification_success"]


@pytest.mark.asyncio
async def test_infra_retry_charged_and_success_can_recover(tmp_path: Path) -> None:
    runner = FakeRunner(["infra_error", "passed"])
    context = ToolContext(workspace=tmp_path, contract=contract(), browser_runner=runner)
    result = await execute(context)
    assert result.data["status"] == "passed"
    assert context.state["browser_verification_runs"]["ui"] == 2
    assert context.state["verification_success"]["ui"] == 1


@pytest.mark.asyncio
async def test_run_and_elapsed_budgets_prevent_new_execution(tmp_path: Path) -> None:
    runner = FakeRunner(["passed"])
    context = ToolContext(
        workspace=tmp_path,
        contract=contract(
            browser_verifications=[{"id": "ui", "suite_ref": "counter-public", "max_runs": 1}],
        ),
        browser_runner=runner,
    )
    await execute(context)
    result = await execute(context)
    assert result.data["status"] == "budget_exhausted"
    assert runner.calls == 1
    context = ToolContext(
        workspace=tmp_path,
        contract=contract(),
        browser_runner=runner,
        deadline_at=time.monotonic() - 1,
    )
    result = await execute(context)
    assert result.data["status"] == "budget_exhausted"
    assert runner.calls == 1


@pytest.mark.asyncio
async def test_other_executor_cannot_write_during_browser_validation(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class WaitingRunner(FakeRunner):
        async def run(self, **kwargs: object) -> FakeResult:
            entered.set()
            await release.wait()
            return await super().run(**kwargs)

    browser_context = ToolContext(
        workspace=tmp_path, contract=contract(), browser_runner=WaitingRunner(["passed"])
    )
    legacy_context = ToolContext(
        workspace=tmp_path, contract=contract(browser_verifications=[]), run_id="legacy-writer"
    )
    browser_task = asyncio.create_task(execute(browser_context))
    await asyncio.wait_for(entered.wait(), timeout=3)
    writer_task = asyncio.create_task(
        ToolExecutor(build_builtin_registry()).execute(
            ToolCall(
                id="other-write",
                name="create_file",
                arguments={
                    "path": "counter.js",
                    "content": "changed",
                },
            ),
            legacy_context,
        )
    )
    try:
        await asyncio.sleep(0.1)
        assert not writer_task.done()
        assert not (tmp_path / "counter.js").exists()
    finally:
        release.set()
        browser_result, writer_result = await asyncio.wait_for(
            asyncio.gather(browser_task, writer_task), timeout=3
        )
    assert browser_result.ok
    assert writer_result.is_mutation


@pytest.mark.asyncio
async def test_resume_does_not_replenish_browser_budget(tmp_path: Path) -> None:
    runner = FakeRunner(["passed"])
    task = contract(
        browser_verifications=[
            {
                "id": "ui",
                "suite_ref": "counter-public",
                "max_runs": 1,
            }
        ]
    )
    await execute(ToolContext(workspace=tmp_path, contract=task, browser_runner=runner))
    fresh_context = ToolContext(workspace=tmp_path, contract=task, browser_runner=runner)
    result = await execute(fresh_context)
    assert result.data["status"] == "budget_exhausted"
    assert runner.calls == 1


@pytest.mark.asyncio
async def test_resume_does_not_grant_another_environment_recovery(tmp_path: Path) -> None:
    runner = FakeRunner(["infra_error", "passed"])
    runner.prior_environment_failures = 1
    context = ToolContext(workspace=tmp_path, contract=contract(), browser_runner=runner)
    result = await execute(context)
    assert result.data["status"] == "infra_error"
    assert runner.calls == 1
    assert context.state["browser_terminal_status"] == "failed"


@pytest.mark.asyncio
async def test_tool_message_metadata_is_bounded_without_duplicating_report(tmp_path: Path) -> None:
    import json

    class LargeRunner(FakeRunner):
        async def run(self, **kwargs: object) -> FakeResult:
            self.calls += 1
            return FakeResult(
                status="failed",
                failures=[{"message": "x" * 30_000}],
                evidence_refs=["D:/" + "e" * 10_000] * 20,
                reason="y" * 30_000,
            )

    context = ToolContext(
        workspace=tmp_path,
        contract=contract(
            budget={"max_tool_output_chars": 1000},
        ),
        browser_runner=LargeRunner(["failed"]),
    )
    result = await execute(context)
    assert len(result.output) + len(json.dumps(result.data, ensure_ascii=False)) <= 1000
    assert result.data["summary_truncated"]
    assert len(context.state["browser_verification_results"]["ui"].reason) == 30_000
