"""Host proofs for a fixed requirement test's RED to GREEN transition."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import Field

from aicoding_agent.domain.models import StrictModel, TaskContract, TddSpec
from aicoding_agent.regression.gate import RegressionGate, RegressionReport
from aicoding_agent.regression.store import workspace_fingerprint


class TddEvidence(StrictModel):
    contract_fingerprint: str
    baseline_path: Path
    baseline_fingerprint: str
    red_report: RegressionReport
    red_cases: dict[str, list[str]]
    historical_ids: set[str] = Field(default_factory=set)
    registry_fingerprint: str
    test_fingerprint: str
    environment_fingerprint: str


class TddError(RuntimeError):
    def __init__(
        self, reason: str, report: RegressionReport | None = None, status: str = "invalid_red",
    ) -> None:
        super().__init__(reason[:2000])
        self.report = report
        self.status = status


class TddGate:
    """RED never grants acceptance; only an immutable RED/GREEN pair does."""

    def __init__(self, gate: RegressionGate) -> None:
        self.gate = gate

    @staticmethod
    def _contract_fingerprint(contract: TaskContract) -> str:
        payload = contract.model_dump(mode="json", exclude={"created_at"})
        payload["allowed_tools"] = sorted(contract.allowed_tools)
        payload["allowed_commands"] = sorted(contract.allowed_commands)
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _validate_spec(
        self, spec: TddSpec, contract: TaskContract, historical_ids: set[str],
    ) -> None:
        commands = {item.id: item for item in contract.verification_commands}
        browsers = {item.id: item for item in contract.browser_verifications if item.required}
        required = set(commands) | set(browsers)
        if not spec.red_cases:
            raise TddError("RED requires a resolved nonempty target case inventory")
        if (contract.tdd and contract.tdd.red_cases
                and contract.tdd.red_cases != spec.red_cases):
            raise TddError("RED targets differ from the declared task targets")
        if not historical_ids <= required:
            raise TddError("Historical test inventory is not present in the effective contract")
        if set(spec.red_cases) & historical_ids:
            raise TddError("Historical tests cannot be declared as expected RED failures")
        for verification_id, cases in spec.red_cases.items():
            if verification_id in commands:
                suite = self.gate.command_suites.get(verification_id)
                if suite is None or suite.reporter != "pytest_junit":
                    raise TddError("RED command targets require structured pytest_junit reports")
                expected = set(suite.expected_tests)
            elif verification_id in browsers:
                runner = self.gate.browser_runner
                reference = browsers[verification_id].suite_ref
                if runner is None or reference not in runner.suites:
                    raise TddError("RED browser targets require configured structured suites")
                expected = set(runner.suites[reference].expected_tests)
            else:
                raise TddError(
                    "RED verification inventory is not present in the effective contract",
                )
            if not set(cases) <= expected:
                raise TddError("RED targets are absent from the complete expected case inventory")

    @staticmethod
    def _valid_red_results(
        report: RegressionReport, targets: dict[str, list[str]], historical_ids: set[str],
    ) -> bool:
        if report.status != "assertion_failed" or report.reason:
            return False
        results = {item.id: item for item in report.tests}
        if len(results) != len(report.tests) or not set(targets) <= set(results):
            return False
        if not historical_ids <= set(results):
            return False
        for item in report.tests:
            if item.status not in {"passed", "assertion_failed"} or not item.cases:
                return False
            failures = {key for key, status in item.cases.items() if status == "assertion_failed"}
            if any(status not in {"passed", "assertion_failed"} for status in item.cases.values()):
                return False
            if failures != set(targets.get(item.id, [])):
                return False
            if (item.status == "assertion_failed") != bool(failures):
                return False
            if item.id in historical_ids and item.status != "passed":
                return False
        return True

    async def verify_red(
        self, contract: TaskContract, baseline: Path, registry_version: int,
        registry_fingerprint: str, run_id: str, budget: object, spec: TddSpec,
        historical_ids: set[str] | None = None,
    ) -> TddEvidence:
        historical_ids = historical_ids or set()
        self._validate_spec(spec, contract, historical_ids)
        before = await workspace_fingerprint(baseline)
        report = await self.gate.verify(
            contract, baseline, registry_version, registry_fingerprint, run_id, budget,
        )
        if report.status in {"infra_error", "timeout"}:
            raise TddError("RED verification environment failed", report, report.status)
        if (before != report.candidate_fingerprint
                or before != await workspace_fingerprint(baseline)):
            raise TddError("RED baseline code changed during verification", report)
        if not self._valid_red_results(report, spec.red_cases, historical_ids):
            status = "already_satisfied" if report.status == "passed" else "invalid_red"
            raise TddError(
                "RED did not show only the designated failures; unexpected result", report, status,
            )
        evidence = TddEvidence(
            contract_fingerprint=self._contract_fingerprint(contract),
            baseline_path=baseline.resolve(),
            baseline_fingerprint=before, red_report=report,
            red_cases=spec.red_cases, historical_ids=historical_ids,
            registry_fingerprint=registry_fingerprint,
            test_fingerprint=report.test_fingerprint,
            environment_fingerprint=report.environment_fingerprint,
        )
        if not await self.validate_red(evidence, contract, baseline):
            raise TddError("RED verification evidence is invalid or stale", report)
        return evidence

    async def validate_red(
        self, evidence: TddEvidence, contract: TaskContract, baseline: Path,
    ) -> bool:
        try:
            spec = TddSpec(red_cases=evidence.red_cases)
            self._validate_spec(spec, contract, evidence.historical_ids)
            report = evidence.red_report
            if (evidence.contract_fingerprint != self._contract_fingerprint(contract)
                    or evidence.baseline_path.resolve() != baseline.resolve()
                    or evidence.baseline_fingerprint != report.candidate_fingerprint
                    or evidence.registry_fingerprint != report.registry_fingerprint
                    or evidence.test_fingerprint != report.test_fingerprint
                    or evidence.environment_fingerprint != report.environment_fingerprint
                    or not self._valid_red_results(
                        report, evidence.red_cases, evidence.historical_ids,
                    )):
                return False
            return await self.gate.revalidate(
                report, baseline, contract, report.registry_version, report.registry_fingerprint,
                allow_assertion_failure=True,
            )
        except (OSError, ValueError, RuntimeError):
            return False

    async def validate_pair(
        self, evidence: TddEvidence, green_report: RegressionReport, contract: TaskContract,
        baseline: Path, candidate: Path,
    ) -> bool:
        if not await self.validate_red(evidence, contract, baseline):
            return False
        red = evidence.red_report
        if (green_report.status != "passed"
                or green_report.registry_version != red.registry_version
                or green_report.registry_fingerprint != evidence.registry_fingerprint
                or green_report.test_fingerprint != evidence.test_fingerprint
                or green_report.environment_fingerprint != evidence.environment_fingerprint):
            return False
        results = {item.id: item for item in green_report.tests}
        for suite_id, case_ids in evidence.red_cases.items():
            result = results.get(suite_id)
            if result is None or any(result.cases.get(case_id) != "passed" for case_id in case_ids):
                return False
        try:
            return await self.gate.revalidate(
                green_report, candidate, contract,
                green_report.registry_version, green_report.registry_fingerprint,
            )
        except (OSError, ValueError, RuntimeError):
            return False
