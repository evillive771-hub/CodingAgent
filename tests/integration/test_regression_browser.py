"""Opt-in real Chromium acceptance tests; use installed runtimes without downloads."""
import json
import os
import shutil
import socket
import sys
from pathlib import Path

import pytest

from aicoding_agent.adapters.browser_tests import BrowserSuite, BrowserTestRunner
from aicoding_agent.domain.models import (
    AgentResult,
    AgentStatus,
    BrowserVerificationContract,
    TaskContract,
    VerificationCommand,
)
from aicoding_agent.regression.controller import RegressionController
from aicoding_agent.regression.gate import CommandSuite, RegressionGate


class EditingAgent:
    def __init__(self, fixes: list[bool]):
        self.fixes = fixes
        self.calls = 0
        self.initial_code: list[str] = []
        self.feedback: list[str] = []

    async def run(self, contract, workspace, *, run_id, shared_budget, **kwargs):
        await shared_budget.charge_turn()
        source = workspace / "counter.js"
        code = source.read_text(encoding="utf-8")
        self.initial_code.append(code)
        self.feedback.append(str(kwargs.get("conversation_history", [])))
        fix = self.fixes[min(self.calls, len(self.fixes) - 1)]
        self.calls += 1
        source.write_text(code.replace("return value - 1;", "return Math.max(0, value - 1);")
                          if fix else code, encoding="utf-8")
        return AgentResult(status=AgentStatus.COMPLETED, run_id=run_id)


@pytest.fixture
def chromium_lab(tmp_path):
    runtime = os.environ.get("CODINGAGENT_PLAYWRIGHT_TEST_PACKAGE")
    if not runtime:
        pytest.skip("Set CODINGAGENT_PLAYWRIGHT_TEST_PACKAGE to installed @playwright/test")
    package = Path(runtime).resolve()
    node = shutil.which("node")
    assert node and (package / "cli.js").is_file(), "Selected Playwright runtime is missing"
    sample = Path(__file__).parents[2] / "examples" / "browser_feedback"
    source = tmp_path / "source"
    shutil.copytree(sample / "workspace", source)
    host = tmp_path / "host"
    host.mkdir()
    for name in ("counter.spec.cjs", "playwright.config.cjs"):
        content = (sample / "host" / name).read_text(encoding="utf-8")
        (host / name).write_text(
            content.replace("'@playwright/test'", json.dumps(package.as_posix())), encoding="utf-8",
        )
    with socket.socket() as allocator:
        allocator.bind(("127.0.0.1", 0))
        port = allocator.getsockname()[1]
    suite = BrowserSuite(
        id="counter", cwd=(package.parent.parent / "playwright").resolve(),
        command=[node, str(package / "cli.js"), "test",
                 f"--config={host / 'playwright.config.cjs'}", "--forbid-only"],
        test_files=[host / "counter.spec.cjs", host / "playwright.config.cjs"],
        expected_tests=["counter cannot decrement below zero",
                        "increment and decrement preserve positive values"],
        preview_command=[sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        preview_url=f"http://127.0.0.1:{port}/",
    )
    fixed_command = host / "check.py"
    fixed_command.write_text("assert 2 + 2 == 4\n", encoding="utf-8")
    command = CommandSuite(id="unit", command=[sys.executable, str(fixed_command)],
                           cwd=host, test_files=[fixed_command])
    contract = TaskContract(
        task_id="counter-real-regression", objective="Clamp counter at zero",
        acceptance_criteria=["Commands and both real browser cases pass"],
        verification_commands=[VerificationCommand(id="unit", program="python", args=["-V"])],
        browser_verifications=[BrowserVerificationContract(
            id="ui", suite_ref="counter", timeout_seconds=45, max_runs=6,
        )],
    )
    def controller():
        runner = BrowserTestRunner([suite], tmp_path / "browser-evidence")
        gate = RegressionGate(tmp_path / "evidence", runner, [command])
        return RegressionController(tmp_path / "runtime", gate)
    return source, contract, controller


@pytest.mark.asyncio
async def test_real_browser_regression_retry_accept_and_restored_resume(chromium_lab):
    source, contract, create_controller = chromium_lab
    controller = create_controller()
    agent = EditingAgent([False, True])
    outcome = await controller.run(contract, source, run_id="real-ui", agent=agent)
    assert outcome.status == "accepted", outcome.reason
    assert agent.calls == 2
    assert all("return value - 1;" in code for code in agent.initial_code)
    assert "ui: assertion_failed" in agent.feedback[1]
    reports = [json.loads(path.read_text(encoding="utf-8"))
               for path in controller.gate.evidence_root.rglob(
        "regression-report.json",
    )]
    failed = next(report for report in reports if report["status"] == "assertion_failed")
    assert [test["status"] for test in failed["tests"]] == ["passed", "assertion_failed"]
    assert outcome.report.tests[1].cases == {
        "counter cannot decrement below zero": "passed",
        "increment and decrement preserve positive values": "passed",
    }
    restored = create_controller()
    resumed = await restored.run(contract, source, run_id="real-ui", agent=agent)
    assert resumed.status == "accepted"
    assert resumed.baseline_revision == outcome.baseline_revision
    assert agent.calls == 2


@pytest.mark.asyncio
async def test_real_browser_repeated_regression_blocks_baseline(chromium_lab):
    source, contract, create_controller = chromium_lab
    controller = create_controller()
    agent = EditingAgent([False])
    outcome = await controller.run(contract, source, run_id="real-block", agent=agent)
    assert outcome.status == "blocked", outcome.reason
    assert agent.calls == 2
    baseline = await controller.store.current()
    assert baseline.revision == 0
    assert "return value - 1;" in (
        Path(baseline.snapshot_path) / "counter.js"
    ).read_text(encoding="utf-8")
    assert [test.status for test in outcome.report.tests] == ["passed", "assertion_failed"]
