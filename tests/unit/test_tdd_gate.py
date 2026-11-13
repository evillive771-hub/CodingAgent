from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from aicoding_agent.adapters.browser_tests import BrowserSuite, BrowserTestRunner
from aicoding_agent.domain.models import (
    BrowserVerificationContract,
    TaskContract,
    VerificationCommand,
)
from aicoding_agent.regression.gate import CommandSuite, RegressionGate
from aicoding_agent.regression.tdd import TddError, TddGate, TddSpec


class Budget:
    remaining_seconds = 30

    async def reserve_command(self, verification_id):
        pass

    async def reserve_browser(self, verification_id, max_runs):
        pass

    async def charge_tools(self, count):
        pass


def setup(tmp_path, *, reporter="pytest_junit", old_failure=False):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "counter.py").write_text("VALUE = -1\n", encoding="utf-8")
    host = tmp_path / "host"
    host.mkdir()
    script = host / "check.py"
    script.write_text(
        "import os,sys\nfrom pathlib import Path\n"
        "value=(Path(os.environ['REGRESSION_WORKSPACE'])/'counter.py').read_text()\n"
        "failure='VALUE = -1' in value\n"
        f"old_failure={old_failure!r}\n"
        "target='<failure message=\"negative counter\"/>' if failure else ''\n"
        "old='<failure message=\"old regression\"/>' if old_failure else ''\n"
        "report=next((arg.split('=',1)[1] for arg in sys.argv "
        "if arg.startswith('--junitxml=')),None)\n"
        "if report:\n"
        " Path(report).write_text('<testsuite><testcase classname=\"counter\" name=\"old\">'+old+"
        "'</testcase><testcase classname=\"counter\" name=\"clamp\">'+target+"
        "'</testcase></testsuite>')\n"
        "sys.exit(1 if failure or old_failure else 0)\n",
        encoding="utf-8",
    )
    suite = CommandSuite(
        id="counter", command=[sys.executable, str(script)], cwd=host, test_files=[script],
        expected_tests=["counter::old", "counter::clamp"], reporter=reporter,
    )
    contract = TaskContract(
        task_id="clamp", objective="Clamp negative values", acceptance_criteria=["Counter >= 0"],
        verification_commands=[VerificationCommand(id="counter", program="python")],
    )
    gate = RegressionGate(tmp_path / "evidence", command_suites=[suite])
    return baseline, script, contract, gate


async def red(baseline, contract, gate):
    return await TddGate(gate).verify_red(
        contract, baseline, 0, "registry", "red", Budget(),
        TddSpec(red_cases={"counter": ["counter::clamp"]}), set(),
    )


@pytest.mark.asyncio
async def test_valid_red_and_same_frozen_tests_green(tmp_path):
    baseline, _, contract, gate = setup(tmp_path)
    evidence = await red(baseline, contract, gate)
    tdd = TddGate(gate)
    assert await tdd.validate_red(evidence, contract, baseline)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "counter.py").write_text("VALUE = 0\n", encoding="utf-8")
    green = await gate.verify(contract, candidate, 0, "registry", "green", Budget())
    assert await tdd.validate_pair(evidence, green, contract, baseline, candidate)


@pytest.mark.asyncio
async def test_preexisting_pass_is_not_red(tmp_path):
    baseline, _, contract, gate = setup(tmp_path)
    (baseline / "counter.py").write_text("VALUE = 0\n", encoding="utf-8")
    with pytest.raises(TddError, match="RED") as error:
        await red(baseline, contract, gate)
    assert error.value.report.status == "passed"


@pytest.mark.asyncio
async def test_exit_code_only_cannot_prove_red(tmp_path):
    baseline, _, contract, gate = setup(tmp_path, reporter="exit_code")
    with pytest.raises(TddError, match="structured"):
        await red(baseline, contract, gate)


@pytest.mark.asyncio
async def test_unexpected_assertion_failure_is_not_valid_red(tmp_path):
    baseline, _, contract, gate = setup(tmp_path, old_failure=True)
    with pytest.raises(TddError, match="unexpected"):
        await red(baseline, contract, gate)


@pytest.mark.asyncio
async def test_environment_error_is_not_red(tmp_path):
    baseline, script, contract, gate = setup(tmp_path)
    script.write_text("raise RuntimeError('missing browser')\n", encoding="utf-8")
    with pytest.raises(TddError) as error:
        await red(baseline, contract, gate)
    assert error.value.status == "infra_error"


@pytest.mark.asyncio
async def test_red_baseline_and_report_tampering_invalidates_proof(tmp_path):
    baseline, _, contract, gate = setup(tmp_path)
    evidence = await red(baseline, contract, gate)
    path = evidence.red_report.report_path
    payload = json.loads(Path(path).read_text())
    payload["duration_seconds"] += 1
    Path(path).write_text(json.dumps(payload), encoding="utf-8")
    assert not await TddGate(gate).validate_red(evidence, contract, baseline)
    Path(path).write_text(evidence.red_report.model_dump_json(), encoding="utf-8")
    (baseline / "added.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert not await TddGate(gate).validate_red(evidence, contract, baseline)


@pytest.mark.asyncio
async def test_test_weakening_between_red_green_cannot_be_accepted(tmp_path):
    baseline, script, contract, gate = setup(tmp_path)
    evidence = await red(baseline, contract, gate)
    script.write_text(script.read_text().replace("failure='VALUE = -1' in value", "failure=False"))
    green = await gate.verify(contract, baseline, 0, "registry", "green", Budget())
    assert green.status == "passed"
    assert not await TddGate(gate).validate_pair(evidence, green, contract, baseline, baseline)


@pytest.mark.parametrize("cases", [{}, {"counter": []}, {"counter": ["x", "x"]}])
def test_red_targets_require_nonempty_unique_inventory(cases):
    with pytest.raises(ValidationError):
        TddSpec(red_cases=cases)


@pytest.mark.asyncio
async def test_unknown_red_case_is_rejected_before_execution(tmp_path):
    baseline, _, contract, gate = setup(tmp_path)
    with pytest.raises(TddError, match="inventory"):
        await TddGate(gate).verify_red(
            contract, baseline, 0, "registry", "red", Budget(),
            TddSpec(red_cases={"counter": ["counter::nonexistent"]}), set(),
        )


@pytest.mark.asyncio
async def test_browser_red_requires_command_history_pass_and_page_green(tmp_path):
    baseline, script, contract, gate = setup(tmp_path)
    (baseline / "counter.py").write_text("VALUE = 0\n", encoding="utf-8")
    (baseline / "page.js").write_text("CLAMP = false\n", encoding="utf-8")
    browser_script = script.parent / "browser.py"
    browser_script.write_text(
        "import os,json,sys\nfrom pathlib import Path\n"
        "failed='false' in (Path(os.environ['EVAL_PROJECT_ROOT'])/'page.js').read_text()\n"
        "result={'status':'failed' if failed else 'passed'}\n"
        "if failed: result['error']={'message':'negative page value'}\n"
        "payload={'suites':[{'specs':[{'title':'page clamp','tests':[{'expectedStatus':'passed',"
        "'status':'unexpected' if failed else 'expected','results':[result]}]}]}],'errors':[]}\n"
        "Path(os.environ['PLAYWRIGHT_JSON_OUTPUT_NAME']).write_text(json.dumps(payload))\n"
        "Path(os.environ['BROWSER_GUARD_PROOF']).write_text('guard')\n"
        "sys.exit(1 if failed else 0)\n",
        encoding="utf-8",
    )
    gate.browser_runner = BrowserTestRunner([BrowserSuite(
        id="page", command=[sys.executable, str(browser_script)], cwd=script.parent,
        test_files=[browser_script], expected_tests=["page clamp"],
        allowed_origins=["http://127.0.0.1:8080"],
    )], tmp_path / "browser-evidence")
    contract.browser_verifications = [BrowserVerificationContract(id="page", suite_ref="page")]
    tdd = TddGate(gate)
    evidence = await tdd.verify_red(
        contract, baseline, 0, "registry", "red", Budget(),
        TddSpec(red_cases={"page": ["page clamp"]}), {"counter"},
    )
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "counter.py").write_text("VALUE = 0\n", encoding="utf-8")
    (candidate / "page.js").write_text("CLAMP = true\n", encoding="utf-8")
    green = await gate.verify(contract, candidate, 0, "registry", "green", Budget())
    assert await tdd.validate_pair(evidence, green, contract, baseline, candidate)
    forged = green.model_copy(deep=True)
    forged.status = "assertion_failed"
    forged.tests[1].status = "assertion_failed"
    forged.tests[1].cases["page clamp"] = "assertion_failed"
    Path(forged.report_path).write_text(forged.model_dump_json(), encoding="utf-8")
    assert not await gate.revalidate(
        forged, candidate, contract, 0, "registry", allow_assertion_failure=True,
    )
    (candidate / "page.js").write_text("CLAMP = false\n", encoding="utf-8")
    failed = await gate.verify(contract, candidate, 0, "registry", "failed-green", Budget())
    assert not await tdd.validate_pair(evidence, failed, contract, baseline, candidate)


@pytest.mark.asyncio
async def test_red_test_process_cannot_modify_baseline(tmp_path):
    baseline, script, contract, gate = setup(tmp_path)
    script.write_text(
        script.read_text() + "\n",
        encoding="utf-8",
    )
    text = script.read_text()
    text = text.replace(
        "failure='VALUE = -1' in value",
        "(Path(os.environ['REGRESSION_WORKSPACE'])/'injected.py').write_text('new code')\n"
        "failure='VALUE = -1' in value",
    )
    script.write_text(text, encoding="utf-8")
    with pytest.raises(TddError) as error:
        await red(baseline, contract, gate)
    assert error.value.status == "infra_error"


@pytest.mark.asyncio
async def test_historical_test_cannot_be_designated_expected_red(tmp_path):
    baseline, _, contract, gate = setup(tmp_path)
    with pytest.raises(TddError, match="Historical"):
        await TddGate(gate).verify_red(
            contract, baseline, 0, "registry", "red", Budget(),
            TddSpec(red_cases={"counter": ["counter::clamp"]}), {"counter"},
        )


@pytest.mark.asyncio
async def test_red_proof_restores_in_new_gate_and_rejects_missing_log(tmp_path):
    baseline, _, contract, gate = setup(tmp_path)
    evidence = await red(baseline, contract, gate)
    restored = TddGate(RegressionGate(
        gate.evidence_root, command_suites=list(gate.command_suites.values()),
    ))
    assert await restored.validate_red(evidence, contract, baseline)
    Path(evidence.red_report.tests[0].evidence_refs[-1]).unlink()
    assert not await restored.validate_red(evidence, contract, baseline)


@pytest.mark.asyncio
async def test_red_proof_cannot_change_declared_task_or_target(tmp_path):
    baseline, _, contract, gate = setup(tmp_path)
    contract.tdd = TddSpec(red_cases={"counter": ["counter::clamp"]})
    evidence = await red(baseline, contract, gate)
    drifted = contract.model_copy(update={"objective": "A different requirement"})
    assert not await TddGate(gate).validate_red(evidence, drifted, baseline)
    drifted = contract.model_copy(update={
        "tdd": TddSpec(red_cases={"counter": ["counter::old"]}),
    })
    assert not await TddGate(gate).validate_red(evidence, drifted, baseline)


@pytest.mark.asyncio
async def test_report_metadata_cannot_relabel_passing_junit_as_red(tmp_path):
    baseline, _, contract, gate = setup(tmp_path)
    (baseline / "counter.py").write_text("VALUE = 0\n", encoding="utf-8")
    report = await gate.verify(contract, baseline, 0, "registry", "forged", Budget())
    assert report.status == "passed"
    report.status = "assertion_failed"
    report.tests[0].status = "assertion_failed"
    report.tests[0].exit_code = 1
    report.tests[0].cases["counter::clamp"] = "assertion_failed"
    Path(report.report_path).write_text(report.model_dump_json(), encoding="utf-8")
    assert not await gate.revalidate(
        report, baseline, contract, 0, "registry", allow_assertion_failure=True,
    )
