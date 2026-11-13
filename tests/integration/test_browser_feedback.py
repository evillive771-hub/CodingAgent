"""Exercise feedback using real subprocess/report/evidence handling, without a browser.

The synthetic reporter is trusted test infrastructure, not proof of real UI behavior.
The separate Counter sample supplies real Playwright tests.
"""

import asyncio
import json
import os
import shutil
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import ScriptedModel

from aicoding_agent.adapters.browser_tests import BrowserSuite, BrowserTestRunner
from aicoding_agent.agent.goal import GoalEvaluation, GoalEvaluationResult
from aicoding_agent.agent.loop import AgentLoop
from aicoding_agent.domain.models import (
    BrowserVerificationContract,
    Budget,
    ModelResponse,
    TaskContract,
    ToolCall,
)
from aicoding_agent.persistence.jsonl import AtomicCheckpointStore
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor

REPORTER = """
import json, os, sys
from pathlib import Path
workspace = Path(os.environ['BROWSER_WORKSPACE'])
passed = 'Math.max(0, value - 1)' in (workspace / 'counter.js').read_text()
status = 'passed' if passed else 'failed'
result = {'status': status}
if not passed:
    result['error'] = {'message': 'counter below zero', 'expected': '0', 'actual': '-1'}
payload = {'suites': [{'specs': [{'title': 'counter clamp', 'tests': [{
    'expectedStatus': 'passed', 'status': 'expected' if passed else 'unexpected',
    'results': [result]}]}]}], 'errors': []}
Path(os.environ['PLAYWRIGHT_JSON_OUTPUT_NAME']).write_text(json.dumps(payload))
# Synthetic guard proof ONLY for deterministic integration infrastructure.
Path(os.environ['BROWSER_GUARD_PROOF']).write_text('synthetic fixture, no browser')
sys.exit(0 if passed else 1)
"""


@pytest.fixture
def browser_fixture(tmp_path: Path) -> tuple[Path, BrowserTestRunner, TaskContract]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "counter.js").write_text("return value - 1;\n", encoding="utf-8")
    host = tmp_path / "host"
    host.mkdir()
    reporter = host / "reporter.py"
    reporter.write_text(REPORTER, encoding="utf-8")
    runner = BrowserTestRunner(
        [
            BrowserSuite(
                id="counter-public",
                command=[sys.executable, str(reporter)],
                cwd=host,
                test_files=[reporter],
                expected_tests=["counter clamp"],
                allowed_origins=["http://127.0.0.1:8080"],
            )
        ],
        evidence_root=tmp_path / "evidence",
    )
    contract = TaskContract(
        task_id="browser-feedback",
        objective="Clamp counter at zero",
        acceptance_criteria=["The fixed public test passes on the submitted code"],
        allowed_tools={"run_playwright", "apply_text_patch", "submit_result"},
        browser_verifications=[
            BrowserVerificationContract(
                id="counter-e2e",
                suite_ref="counter-public",
                timeout_seconds=5,
            )
        ],
    )
    return workspace, runner, contract


def call(name: str, identity: str, **arguments) -> ModelResponse:
    return ModelResponse(tool_calls=[ToolCall(id=identity, name=name, arguments=arguments)])


def verify(identity: str = "verify") -> ModelResponse:
    return call("run_playwright", identity, verification_id="counter-e2e")


def submit(identity: str = "submit", changed: bool = True) -> ModelResponse:
    return call(
        "submit_result",
        identity,
        summary="Counter validated",
        changed_files=["counter.js"] if changed else [],
    )


def patch(identity: str = "patch") -> ModelResponse:
    return call(
        "apply_text_patch",
        identity,
        path="counter.js",
        old_text="return value - 1;",
        new_text="return Math.max(0, value - 1);",
    )


def loop(model: ScriptedModel, runner: BrowserTestRunner | None = None) -> AgentLoop:
    return AgentLoop(
        model=model,
        executor=ToolExecutor(build_builtin_registry()),
        browser_runner=runner,
    )


def tool_data(model: ScriptedModel) -> list[dict]:
    messages = model.requests[-1][0]
    return [json.loads(message["content"]) for message in messages if message["role"] == "tool"]


@pytest.mark.asyncio
async def test_browser_failure_reaches_model_then_patch_pass_and_complete(browser_fixture) -> None:
    workspace, runner, contract = browser_fixture
    model = ScriptedModel([verify(), patch(), verify("verify-fixed"), submit()])
    result = await loop(model, runner).run(contract, workspace, run_id="counter-loop")

    assert result.status == "completed"
    feedback = tool_data(model)
    runs = [item for item in feedback if item.get("data", {}).get("verification_id")]
    assert [item["data"]["status"] for item in runs] == ["failed", "passed"]
    assert runs[0]["ok"] is True  # assertion failures must not trip the tool-error circuit
    failure = runs[0]["data"]["failures"][0]
    assert failure["expected"] == "0" and failure["actual"] == "-1"
    assert Path(runs[1]["data"]["evidence_refs"][0]).is_file()
    assert result.outcome.changed_files == ["counter.js"]


@pytest.mark.asyncio
async def test_patch_after_browser_success_requires_fresh_validation(browser_fixture) -> None:
    workspace, runner, contract = browser_fixture
    model = ScriptedModel(
        [
            patch(),
            verify(),
            call(
                "apply_text_patch",
                "later-edit",
                path="counter.js",
                old_text="return Math.max(0, value - 1);",
                new_text="return Math.max(0, value - 1); // clarified",
            ),
            submit("stale-submit"),
            verify("fresh-verify"),
            submit("fresh-submit"),
        ]
    )
    result = await loop(model, runner).run(contract, workspace, run_id="fresh-browser")
    assert result.status == "completed"
    assert len(model.requests) == 6
    assert "<completion_gate_blocked>" in str(model.requests[4][0])
    runs = [item["data"] for item in tool_data(model) if item.get("data", {}).get("attempt_id")]
    assert len(runs) == 2
    assert runs[0]["input_fingerprint"] != runs[1]["input_fingerprint"]


@pytest.mark.asyncio
async def test_required_browser_verification_cannot_use_read_only_exemption(
    browser_fixture,
) -> None:
    workspace, runner, contract = browser_fixture
    (workspace / "counter.js").write_text("return Math.max(0, value - 1);\n")
    model = ScriptedModel(
        [submit("early", changed=False), verify(), submit("verified", changed=False)]
    )
    result = await loop(model, runner).run(contract, workspace, run_id="read-only-browser")
    assert result.status == "completed"
    assert "counter-e2e" in str(model.requests[1][0])
    assert any(item.get("data", {}).get("status") == "passed" for item in tool_data(model))


@pytest.mark.asyncio
async def test_report_tampering_after_pass_blocks_completion_and_requires_rerun(
    browser_fixture,
) -> None:
    workspace, runner, contract = browser_fixture
    (workspace / "counter.js").write_text("return Math.max(0, value - 1);\n")

    class TamperingModel(ScriptedModel):
        async def complete(self, messages, tools):
            if len(self.requests) == 1:
                report = next(runner.evidence_root.rglob("report.json"))
                report.write_text("{}", encoding="utf-8")
            return await super().complete(messages, tools)

    model = TamperingModel(
        [verify(), submit("stale", changed=False), verify("rerun"), submit("valid", changed=False)]
    )
    result = await loop(model, runner).run(contract, workspace, run_id="tamper-browser")
    assert result.status == "completed"
    assert len(model.requests) == 4
    assert "counter-e2e" in str(model.requests[2][0])
    assert len(list(runner.evidence_root.rglob("report.json"))) == 2


@pytest.mark.asyncio
async def test_browser_attempt_budget_does_not_reset_across_model_turns(browser_fixture) -> None:
    workspace, runner, contract = browser_fixture
    contract.browser_verifications[0] = contract.browser_verifications[0].model_copy(
        update={"max_runs": 2},
    )
    model = ScriptedModel([verify("one"), verify("two"), verify("three")])
    result = await loop(model, runner).run(contract, workspace, run_id="browser-budget")
    assert result.status == "budget_exhausted"
    assert len(list(runner.evidence_root.rglob("report.json"))) == 2


@pytest.mark.asyncio
async def test_legacy_contract_keeps_tool_hidden_and_read_only_completion(tmp_path: Path) -> None:
    contract = TaskContract(
        task_id="legacy", objective="Explain code", acceptance_criteria=["Explain"]
    )
    model = ScriptedModel([submit(changed=False)])
    result = await loop(model).run(contract, tmp_path, run_id="legacy-browser")
    assert result.status == "completed"
    assert all(tool["function"]["name"] != "run_playwright" for tool in model.requests[0][1])


@pytest.mark.asyncio
async def test_cancellation_of_browser_tool_propagates_without_passing_evidence(
    browser_fixture,
) -> None:
    workspace, runner, contract = browser_fixture
    host = runner.suites["counter-public"].cwd
    (host / "reporter.py").write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    running = asyncio.create_task(
        loop(ScriptedModel([verify()]), runner).run(
            contract,
            workspace,
            run_id="cancel-browser",
        )
    )
    for _ in range(500):
        if list(runner.evidence_root.rglob("network-guard.cjs")):
            break
        await asyncio.sleep(0.01)
    assert not running.done()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert not list(runner.evidence_root.rglob("report.json"))


@pytest.mark.asyncio
async def test_browser_timeout_is_bounded_and_never_completed(browser_fixture) -> None:
    workspace, runner, contract = browser_fixture
    host = runner.suites["counter-public"].cwd
    (host / "reporter.py").write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    contract.browser_verifications[0] = contract.browser_verifications[0].model_copy(
        update={"max_runs": 1, "timeout_seconds": 0.1},
    )
    contract.budget = Budget(max_turns=3)
    result = await loop(ScriptedModel([verify()]), runner).run(
        contract,
        workspace,
        run_id="timeout-browser",
    )
    assert result.status == "failed"
    assert "timeout" in result.reason
    assert not list(runner.evidence_root.rglob("report.json"))


@pytest.mark.asyncio
async def test_real_counter_browser_feedback_with_existing_runtime(
    tmp_path: Path,
) -> None:
    """Opt-in real Chromium smoke; never downloads a runtime during pytest."""
    runtime = os.environ.get("CODINGAGENT_PLAYWRIGHT_TEST_PACKAGE")
    if not runtime:
        pytest.skip("Set CODINGAGENT_PLAYWRIGHT_TEST_PACKAGE to an existing @playwright/test")
    package = Path(runtime).resolve()
    node = shutil.which("node")
    assert node and (package / "cli.js").is_file(), "The explicitly selected runtime is missing"
    sample = Path(__file__).parents[2] / "examples" / "browser_feedback"
    workspace = tmp_path / "workspace"
    shutil.copytree(sample / "workspace", workspace)
    host = tmp_path / "host"
    host.mkdir()
    package_literal = json.dumps(package.as_posix())
    for name in ("counter.spec.cjs", "playwright.config.cjs"):
        source = (sample / "host" / name).read_text(encoding="utf-8")
        (host / name).write_text(
            source.replace("'@playwright/test'", package_literal),
            encoding="utf-8",
        )
    with socket.socket() as allocation:
        allocation.bind(("127.0.0.1", 0))
        port = allocation.getsockname()[1]
    # pnpm stores playwright-core next to playwright rather than the test package.
    playwright = (package.parent.parent / "playwright").resolve()
    runner = BrowserTestRunner(
        [
            BrowserSuite(
                id="counter-public",
                cwd=playwright,
                command=[
                    node,
                    str(package / "cli.js"),
                    "test",
                    f"--config={host / 'playwright.config.cjs'}",
                    "--forbid-only",
                ],
                test_files=[host / "counter.spec.cjs", host / "playwright.config.cjs"],
                expected_tests=[
                    "counter cannot decrement below zero",
                    "increment and decrement preserve positive values",
                ],
                preview_command=[
                    sys.executable,
                    "-m",
                    "http.server",
                    str(port),
                    "--bind",
                    "127.0.0.1",
                ],
                preview_url=f"http://127.0.0.1:{port}/",
            )
        ],
        evidence_root=tmp_path / "evidence",
    )
    contract = TaskContract(
        task_id="real-counter-browser",
        objective="Clamp counter to zero",
        acceptance_criteria=["Both public UI tests pass"],
        allowed_tools={"run_playwright", "apply_text_patch", "submit_result"},
        browser_verifications=[
            BrowserVerificationContract(
                id="counter-e2e",
                suite_ref="counter-public",
                timeout_seconds=30,
            )
        ],
    )
    model = ScriptedModel([verify(), patch(), verify("fixed"), submit()])
    result = await loop(model, runner).run(contract, workspace, run_id="real-counter")
    assert result.status == "completed", result.reason
    results = [item["data"] for item in tool_data(model) if item.get("data", {}).get("attempt_id")]
    assert [item["status"] for item in results] == ["failed", "passed"]
    assert all(item["executed"] == 2 for item in results)
    assert all(Path(item["evidence_refs"][0]).is_file() for item in results)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["max_turns", "max_tool_calls"])
async def test_resume_same_run_cannot_reset_coding_budget(browser_fixture, limit: str) -> None:
    workspace, runner, contract = browser_fixture
    contract.budget = contract.budget.model_copy(update={limit: 1})
    responses = [verify()] if limit == "max_turns" else [verify(), verify("over-limit")]
    first = await loop(ScriptedModel(responses), runner).run(
        contract,
        workspace,
        run_id=f"resume-{limit}",
    )
    assert first.status == "budget_exhausted"
    resumed_model = ScriptedModel([] if limit == "max_turns" else [verify("resumed")])
    resumed = await loop(resumed_model, runner).run(
        contract,
        workspace,
        run_id=f"resume-{limit}",
    )
    assert resumed.status == "budget_exhausted"
    if limit == "max_turns":
        assert resumed_model.requests == []
    assert len(list(runner.evidence_root.rglob("report.json"))) == 1


@pytest.mark.asyncio
async def test_resume_rejects_replacement_contract_budget_before_model_call(
    browser_fixture,
) -> None:
    workspace, runner, contract = browser_fixture
    contract.budget = contract.budget.model_copy(update={"max_turns": 1})
    await loop(ScriptedModel([verify()]), runner).run(contract, workspace, run_id="resume-contract")
    contract.budget = contract.budget.model_copy(update={"max_turns": 20})
    model = ScriptedModel([])
    result = await loop(model, runner).run(contract, workspace, run_id="resume-contract")
    assert result.status == "failed"
    assert "cannot replace" in result.reason
    assert not model.requests


@pytest.mark.asyncio
async def test_resume_cannot_treat_historical_pass_as_current_success(browser_fixture) -> None:
    workspace, runner, contract = browser_fixture
    (workspace / "counter.js").write_text("return Math.max(0, value - 1);\n")
    first = await loop(ScriptedModel([verify(), submit(changed=False)]), runner).run(
        contract,
        workspace,
        run_id="resume-evidence",
    )
    assert first.status == "completed"
    model = ScriptedModel(
        [
            submit("historical", changed=False),
            verify("new-attempt"),
            submit("current", changed=False),
        ]
    )
    resumed = await loop(model, runner).run(
        contract,
        workspace,
        run_id="resume-evidence",
        resume_messages=[{"role": "user", "content": "Previous verification: passed"}],
    )
    assert resumed.status == "completed"
    assert "<completion_gate_blocked>" in str(model.requests[1][0])
    assert len(list(runner.evidence_root.rglob("report.json"))) == 2


@pytest.mark.asyncio
async def test_resumed_remaining_deadline_cancels_slow_model(browser_fixture) -> None:
    workspace, runner, contract = browser_fixture

    class InterruptedModel:
        async def complete(self, messages, tools):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await loop(InterruptedModel(), runner).run(contract, workspace, run_id="model-deadline")
    store = AtomicCheckpointStore(runner.evidence_root / "budgets")
    checkpoint = await store.load("model-deadline")
    assert checkpoint is not None
    # Simulate recovering an interrupted run close to its original deadline.
    checkpoint["deadline_epoch"] = datetime.now(UTC).timestamp() + 0.2
    await store.save("model-deadline", checkpoint)
    cancelled = asyncio.Event()

    class SlowModel:
        async def complete(self, messages, tools):
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()

    result = await loop(SlowModel(), runner).run(contract, workspace, run_id="model-deadline")
    assert result.status == "budget_exhausted"
    assert "during model call" in result.reason
    assert cancelled.is_set()
    assert await runner.run_count("model-deadline", "counter-e2e") == 0


@pytest.mark.asyncio
async def test_goal_receives_host_confirmed_browser_summary(browser_fixture) -> None:
    workspace, runner, contract = browser_fixture

    class Goal:
        async def evaluate(self, goal):
            assert "browser:counter-e2e" in goal.host_evidence_ids
            evidence = json.loads(goal.available_evidence["browser:counter-e2e"])
            assert evidence["status"] == "passed"
            assert evidence["executed"] == evidence["passed"] == 1
            assert all(Path(ref).is_file() for ref in evidence["evidence_refs"])
            return GoalEvaluationResult(
                evaluation=GoalEvaluation(status="complete", reason="Public test confirmed"),
                attempts=1,
            )

    agent = loop(ScriptedModel([patch(), verify(), submit()]), runner)
    agent.goal_evaluator = Goal()
    result = await agent.run(contract, workspace, run_id="goal-browser-evidence")
    assert result.status == "completed"
