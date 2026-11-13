from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from aicoding_agent.adapters.browser_tests import BrowserSuite, BrowserTestRunner
from aicoding_agent.domain.models import (
    BrowserVerificationContract,
    TaskContract,
    VerificationCommand,
)
from aicoding_agent.regression.gate import CommandSuite, RegressionGate, parse_junit
from aicoding_agent.regression.store import AcceptedBaselineStore, RegressionStoreError


class Budget:
    remaining_seconds = 30

    async def reserve_command(self, verification_id):
        pass

    async def reserve_browser(self, verification_id, max_runs):
        pass

    async def charge_tools(self, count):
        pass


def setup(tmp_path: Path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "interface.py").write_text("VALUE = 1\n")
    host = tmp_path / "host"
    host.mkdir()
    check = host / "check.py"
    check.write_text(
        "import os,sys\nfrom pathlib import Path\n"
        "text=(Path(os.environ['REGRESSION_WORKSPACE'])/'interface.py').read_text()\n"
        "sys.exit(0 if 'VALUE = 1' in text else 1)\n"
    )
    suite = CommandSuite(
        id="historical-api", command=[sys.executable, str(check)], cwd=host, test_files=[check],
    )
    command = VerificationCommand(id=suite.id, program="python", args=["-V"])
    contract = TaskContract(
        task_id="B", objective="Change UI", acceptance_criteria=["No regressions"],
        verification_commands=[command],
    )
    return candidate, host, suite, contract


@pytest.mark.asyncio
async def test_historical_interface_failure_cannot_be_accepted(tmp_path):
    candidate, _, suite, contract = setup(tmp_path)
    gate = RegressionGate(tmp_path / "evidence", command_suites=[suite])
    good = await gate.verify(contract, candidate, 1, "registry", "run", Budget())
    assert good.status == "passed"
    assert await gate.revalidate(good, candidate, contract, 1, "registry")
    (candidate / "interface.py").write_text("VALUE = 2\n")
    assert not await gate.revalidate(good, candidate, contract, 1, "registry")
    failed = await gate.verify(contract, candidate, 1, "registry", "run", Budget())
    assert failed.status == "assertion_failed"
    assert failed.tests[0].cases == {"historical-api": "assertion_failed"}


@pytest.mark.asyncio
async def test_browser_failure_blocks_passing_commands(tmp_path):
    candidate, host, suite, contract = setup(tmp_path)
    script = host / "browser.py"
    script.write_text(
        "import os,json,sys\nfrom pathlib import Path\n"
        "payload={'suites':[{'specs':[{'title':'login','tests':[{'expectedStatus':'passed',"
        "'status':'unexpected','results':[{'status':'failed',"
        "'error':{'message':'login broken'}}]}]}]}],"
        "'errors':[]}\n"
        "Path(os.environ['PLAYWRIGHT_JSON_OUTPUT_NAME']).write_text(json.dumps(payload))\n"
        "Path(os.environ['BROWSER_GUARD_PROOF']).write_text('guard')\nsys.exit(1)\n"
    )
    browser = BrowserTestRunner([BrowserSuite(
        id="login", command=[sys.executable, str(script)], cwd=host, test_files=[script],
        expected_tests=["login"], allowed_origins=["http://127.0.0.1:8080"],
    )], tmp_path / "browser-evidence")
    contract.browser_verifications = [BrowserVerificationContract(id="login-ui", suite_ref="login")]
    gate = RegressionGate(tmp_path / "evidence", browser, [suite])
    result = await gate.verify(contract, candidate, 1, "registry", "run", Budget())
    assert result.status == "assertion_failed"
    assert [item.status for item in result.tests] == ["passed", "assertion_failed"]


@pytest.mark.asyncio
async def test_report_or_fixed_test_tampering_invalidates_acceptance(tmp_path):
    candidate, host, suite, contract = setup(tmp_path)
    gate = RegressionGate(tmp_path / "evidence", command_suites=[suite])
    result = await gate.verify(contract, candidate, 1, "registry", "run", Budget())
    payload = json.loads(Path(result.report_path).read_text())
    payload["duration_seconds"] += 1
    Path(result.report_path).write_text(json.dumps(payload))
    assert not await gate.revalidate(result, candidate, contract, 1, "registry")
    Path(result.report_path).write_text(result.model_dump_json())
    (host / "check.py").write_text("print('weakened')")
    assert not await gate.revalidate(result, candidate, contract, 1, "registry")


@pytest.mark.parametrize("body,code", [
    ("", 0), ('<testcase classname="A" name="old"><skipped/></testcase>', 0),
    ('<testcase classname="A" name="wrong"/>', 0),
    ('<testcase classname="A" name="old"/>', 1),
    ('<testcase classname="A" name="old"><failure/></testcase>', 0),
])
def test_junit_rejects_zero_partial_skip_and_status_mismatch(tmp_path, body, code):
    path = tmp_path / "result.xml"
    path.write_text(f"<testsuite>{body}</testsuite>")
    with pytest.raises(ValueError):
        parse_junit(path, ["A::old"], code)


@pytest.mark.asyncio
async def test_required_browser_environment_error_is_not_code_failure(tmp_path):
    candidate, _, suite, contract = setup(tmp_path)
    contract.browser_verifications = [BrowserVerificationContract(id="ui", suite_ref="absent")]
    gate = RegressionGate(tmp_path / "evidence", command_suites=[suite])
    result = await gate.verify(contract, candidate, 1, "registry", "run", Budget())
    assert result.status == "infra_error"
    assert "runner" in result.reason


@pytest.mark.asyncio
async def test_real_pytest_junit_identifies_old_feature_regression(tmp_path):
    candidate, host, _, contract = setup(tmp_path)
    fixed = host / "test_old.py"
    fixed.write_text(
        "import os\nfrom pathlib import Path\n"
        "def test_api():\n"
        "    source = (Path(os.environ['REGRESSION_WORKSPACE']) / 'interface.py').read_text()\n"
        "    assert 'VALUE = 1' in source, 'Historical interface changed'\n"
    )
    suite = CommandSuite(
        id="historical-api", command=[sys.executable, "-m", "pytest", str(fixed), "-q"],
        cwd=host, test_files=[fixed], reporter="pytest_junit",
        expected_tests=["test_old::test_api"],
    )
    gate = RegressionGate(tmp_path / "evidence", command_suites=[suite])
    passed = await gate.verify(contract, candidate, 1, "registry", "run", Budget())
    assert passed.status == "passed", passed.model_dump()
    (candidate / "interface.py").write_text("VALUE = 2\n")
    failed = await gate.verify(contract, candidate, 1, "registry", "run", Budget())
    assert failed.status == "assertion_failed", failed.model_dump()
    assert failed.tests[0].cases == {"test_old::test_api": "assertion_failed"}


@pytest.mark.asyncio
async def test_legacy_historical_test_weakening_cannot_advance_baseline(tmp_path):
    candidate, _, _, contract = setup(tmp_path)
    test = candidate / "test_old.py"
    test.write_text("assert True\n")
    contract.verification_commands = [VerificationCommand(
        id="legacy", program=Path(sys.executable).name, args=["test_old.py"],
    )]
    store = AcceptedBaselineStore(tmp_path / "store")
    baseline = await store.initialize(candidate)
    gate = RegressionGate(tmp_path / "evidence")
    first = await gate.verify(contract, candidate, baseline.registry_version,
                              baseline.registry_fingerprint, "first", Budget())
    assert first.status == "passed"
    baseline = await store.accept(candidate, contract, first, baseline.revision)
    test.write_text("assert 1 == 1\n")
    second = await gate.verify(contract, candidate, baseline.registry_version,
                               baseline.registry_fingerprint, "second", Budget())
    assert second.status == "passed"
    with pytest.raises(RegressionStoreError):
        await store.accept(candidate, contract, second, baseline.revision)


@pytest.mark.asyncio
async def test_restored_browser_runner_revalidates_persisted_evidence(tmp_path):
    candidate, host, suite, contract = setup(tmp_path)
    script = host / "browser_pass.py"
    script.write_text(
        "import os,json\nfrom pathlib import Path\n"
        "payload={'suites':[{'specs':[{'title':'login','tests':[{'expectedStatus':'passed',"
        "'status':'expected','results':[{'status':'passed'}]}]}]}],'errors':[]}\n"
        "Path(os.environ['PLAYWRIGHT_JSON_OUTPUT_NAME']).write_text(json.dumps(payload))\n"
        "Path(os.environ['BROWSER_GUARD_PROOF']).write_text('guard')\n"
    )
    browser_suite = BrowserSuite(
        id="login", command=[sys.executable, str(script)], cwd=host, test_files=[script],
        expected_tests=["login"], allowed_origins=["http://127.0.0.1:8080"],
    )
    contract.browser_verifications = [BrowserVerificationContract(id="ui", suite_ref="login")]
    first_runner = BrowserTestRunner([browser_suite], tmp_path / "browser-evidence")
    first_gate = RegressionGate(tmp_path / "evidence", first_runner, [suite])
    report = await first_gate.verify(contract, candidate, 1, "registry", "run", Budget())
    assert report.status == "passed"
    restored = RegressionGate(tmp_path / "evidence", BrowserTestRunner(
        [browser_suite], tmp_path / "browser-evidence",
    ), [suite])
    assert await restored.revalidate(report, candidate, contract, 1, "registry")
    browser_report = next(Path(path) for path in report.tests[1].evidence_refs
                          if Path(path).name == "report.json")
    browser_report.write_text("{}")
    assert not await restored.revalidate(report, candidate, contract, 1, "registry")


@pytest.mark.asyncio
async def test_strict_frontend_gate_refuses_missing_browser_even_with_passing_commands(tmp_path):
    candidate, _, suite, contract = setup(tmp_path)
    gate = RegressionGate(tmp_path / "evidence", command_suites=[suite], require_both=True)
    report = await gate.verify(contract, candidate, 1, "registry", "strict", Budget())
    assert report.status == "infra_error"
    assert "command and browser" in report.reason
    assert not await gate.revalidate(report, candidate, contract, 1, "registry")
