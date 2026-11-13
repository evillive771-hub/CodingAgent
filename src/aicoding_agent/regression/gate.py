"""Host final acceptance of the complete command and browser regression set."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from aicoding_agent.adapters.browser_tests import (
    BrowserTestRunner,
    _environment,
    _redact,
    _sha256,
    parse_playwright_report,
)
from aicoding_agent.domain.models import TaskContract

Status = Literal["passed", "assertion_failed", "infra_error", "timeout"]


class CommandSuite(BaseModel):
    """Trusted host configuration. Test code never resides in the editable candidate."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    command: list[str] = Field(min_length=1)
    cwd: Path
    test_files: list[Path] = Field(min_length=1)
    expected_tests: list[str] = Field(default_factory=list)
    reporter: Literal["exit_code", "pytest_junit"] = "exit_code"
    timeout_seconds: float = Field(default=120, gt=0, le=1800)


class RegressionTestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    kind: Literal["command", "browser"]
    status: Status
    exit_code: int | None = None
    duration_seconds: float = 0
    cases: dict[str, str] = Field(default_factory=dict)
    reason: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    browser_result: dict[str, Any] | None = None
    test_fingerprint: str = ""


class RegressionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    attempt_id: str
    status: Status = "infra_error"
    candidate_fingerprint: str = ""
    registry_version: int
    registry_fingerprint: str
    test_fingerprint: str = ""
    environment_fingerprint: str = ""
    tests: list[RegressionTestResult] = Field(default_factory=list)
    reason: str | None = None
    duration_seconds: float = 0
    report_path: str = ""
    evidence_hashes: dict[str, str] = Field(default_factory=dict)

    @property
    def env_fingerprint(self) -> str:
        return self.environment_fingerprint


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _legacy_test_inputs(candidate: Path, command: Any) -> list[Any]:
    """Conservative legacy protection; immutable host suites remain the stronger option."""
    files: set[Path] = set()
    for directory, directories, names in os.walk(candidate, followlinks=False):
        directories[:] = [name for name in directories if name not in {
            ".git", "node_modules", ".venv", "__pycache__", ".aicoding-agent", ".next",
            "playwright-report", "test-results", "evidence",
        }]
        base = Path(directory)
        relative_parts = base.relative_to(candidate).parts
        for name in names:
            if (any(part in {"test", "tests", "__tests__"} for part in relative_parts)
                    or name.startswith(("test_", "conftest.", "pytest.", "jest.config.",
                                        "vitest.config.", "playwright.config."))
                    or re.search(r"(?:^|\.)(?:test|spec)\.", name)):
                files.add(base / name)
    cwd = (candidate / command.cwd).resolve()
    for argument in command.args:
        if argument.startswith("-"):
            continue
        path = (cwd / argument).resolve()
        if path.is_file() and candidate in path.parents:
            if path.suffix in {".py", ".js", ".ts", ".mjs", ".cjs"}:
                # Explicit verification scripts may have arbitrary names.
                content = path.read_bytes()
                if re.search(rb"\b(?:assert|test|describe|it)\b", content):
                    files.add(path)
    values = [[str(file.relative_to(candidate)), _sha256(file)] for file in sorted(files)]
    package = cwd / "package.json"
    if package.is_file():
        if package.is_symlink():
            raise ValueError("Package test configuration cannot be a symlink")
        payload = json.loads(package.read_text(encoding="utf-8"))
        values.append(["package.json:scripts", payload.get("scripts", {})])
    return values


def parse_junit(path: Path, expected: list[str], exit_code: int) -> dict[str, str]:
    """Fail closed on missing, partial, skipped, duplicated or inconsistent reports."""
    if not expected or len(expected) != len(set(expected)):
        raise ValueError("JUnit suites require a unique complete expected test inventory")
    if path.stat().st_size > 8_000_000 or path.is_symlink():
        raise ValueError("Invalid JUnit report size or symlink")
    payload = path.read_bytes()
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise ValueError("DTD reports are prohibited")
    root = ET.fromstring(payload)
    cases: dict[str, str] = {}
    for case in root.iter("testcase"):
        key = f"{case.get('classname', '')}::{case.get('name', '')}"
        if key in cases or not case.get("name"):
            raise ValueError("Duplicate or unnamed JUnit case")
        if case.find("skipped") is not None:
            raise ValueError("Required tests were skipped")
        if case.find("error") is not None:
            cases[key] = "infra_error"
        elif case.find("failure") is not None:
            cases[key] = "assertion_failed"
        else:
            cases[key] = "passed"
    if set(cases) != set(expected):
        raise ValueError("JUnit report does not contain the complete expected test IDs")
    passing = all(status == "passed" for status in cases.values())
    if (exit_code == 0) != passing or exit_code not in {0, 1}:
        raise ValueError("JUnit results disagree with process exit status")
    return cases


class RegressionGate:
    def __init__(
        self, evidence_root: Path, browser_runner: BrowserTestRunner | None = None,
        command_suites: list[CommandSuite] | None = None,
        require_both: bool = False,
    ) -> None:
        self.evidence_root = evidence_root.resolve()
        entries = command_suites or []
        if len({suite.id for suite in entries}) != len(entries):
            raise ValueError("Duplicate host command suite IDs")
        self.command_suites = {suite.id: suite for suite in entries}
        self.browser_runner = browser_runner
        self.require_both = require_both
        self._processes = BrowserTestRunner([], self.evidence_root)
        self._locks: dict[str, asyncio.Lock] = {}

    @classmethod
    async def from_config(
        cls, path: Path, evidence_root: Path,
        browser_runner: BrowserTestRunner | None = None,
        require_both: bool = False,
    ) -> RegressionGate:
        path = path.resolve()
        payload = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
        if set(payload) != {"suites"}:
            raise ValueError("Command configuration must contain only suites")
        suites = []
        for item in payload["suites"]:
            values = dict(item)
            values["cwd"] = (path.parent / values["cwd"]).resolve()
            values["test_files"] = [
                (values["cwd"] / file).resolve() for file in values["test_files"]
            ] + [path]
            suites.append(CommandSuite.model_validate(values))
        return cls(evidence_root, browser_runner, suites, require_both=require_both)

    def _test_fingerprint(self, contract: TaskContract, candidate: Path) -> str:
        values: list[Any] = [
            [item.model_dump(mode="json") for item in contract.verification_commands],
            [item.model_dump(mode="json") for item in contract.browser_verifications],
        ]
        for command in contract.verification_commands:
            suite = self.command_suites.get(command.id)
            if suite is None:
                values.append(_legacy_test_inputs(candidate, command))
                continue
            cwd = suite.cwd.resolve()
            if cwd == candidate or candidate in cwd.parents or not cwd.is_dir():
                raise ValueError("Command suites must reside outside the candidate")
            if suite.reporter == "pytest_junit" and not suite.expected_tests:
                raise ValueError("Pytest suite requires expected test IDs")
            arguments = suite.command[1:]
            if arguments[:2] == ["-m", "pytest"]:
                arguments = arguments[2:]
            if any(arg in {"-k", "-m", "--lf", "--last-failed", "--ff"}
                   or arg.startswith(("-k=", "-m=", "--deselect", "--ignore"))
                   for arg in arguments):
                raise ValueError("Focused command selection is prohibited")
            values.append(suite.model_dump(mode="json"))
            for file in suite.test_files:
                target = file if file.is_absolute() else cwd / file
                if target.is_symlink() or candidate in target.resolve().parents:
                    raise ValueError("Fixed command tests must be outside the candidate")
                values.append([str(target.resolve()), _sha256(target)])
        required = [item for item in contract.browser_verifications if item.required]
        if required:
            if self.browser_runner is None:
                raise ValueError("Required browser suites have no host runner")
            for verification in required:
                suite = self.browser_runner.suites[verification.suite_ref]
                values.append(self.browser_runner._suite_fingerprint(suite, candidate))
        return _digest(values)

    def _environment_fingerprint(self, contract: TaskContract) -> str:
        programs = [self.command_suites[item.id].command[0]
                    if item.id in self.command_suites else item.program
                    for item in contract.verification_commands]
        if self.browser_runner:
            for item in contract.browser_verifications:
                if item.required:
                    suite = self.browser_runner.suites[item.suite_ref]
                    programs.extend([suite.command[0], *(suite.preview_command[:1])])
        binaries = []
        for program in programs:
            resolved = shutil.which(program)
            if resolved is None:
                raise ValueError(f"Missing verification executable: {Path(program).name}")
            path = Path(resolved).resolve()
            stat = path.stat()
            binaries.append((str(path), stat.st_size, stat.st_mtime_ns))
        return _digest([_environment(), binaries])

    async def _command(
        self, command: Any, candidate: Path, directory: Path, timeout_seconds: float,
    ) -> RegressionTestResult:
        suite = self.command_suites.get(command.id)
        cwd = suite.cwd if suite else (candidate / command.cwd).resolve()
        if not suite and cwd != candidate and candidate not in cwd.parents:
            raise ValueError("Command working directory escapes candidate")
        argv = list(suite.command if suite else command.argv)
        report_path = directory / f"{command.id}.xml"
        if suite and suite.reporter == "pytest_junit":
            if any(arg.startswith("--junit") for arg in argv):
                raise ValueError("Host owns JUnit output path")
            argv.append(f"--junitxml={report_path}")
        environment = _environment()
        environment.update(EVAL_PROJECT_ROOT=str(candidate), REGRESSION_WORKSPACE=str(candidate))
        result = RegressionTestResult(id=command.id, kind="command", status="infra_error")
        started = time.monotonic()
        process = None
        drain = None
        try:
            effective_timeout = min(timeout_seconds, suite.timeout_seconds if suite else 120)
            async with asyncio.timeout(effective_timeout):
                process = await self._processes._spawn(argv, cwd, environment)
                drain = asyncio.create_task(self._processes._drain(process))
                result.exit_code = await self._processes._wait_bounded(process, directory)
                if suite and suite.reporter == "pytest_junit":
                    result.cases = await asyncio.to_thread(
                        parse_junit, report_path, suite.expected_tests, result.exit_code,
                    )
                    result.evidence_refs.append(str(report_path))
                    statuses = set(result.cases.values())
                    result.status = ("infra_error" if "infra_error" in statuses else
                                     "assertion_failed" if "assertion_failed" in statuses
                                     else "passed")
                else:
                    result.status = "passed" if result.exit_code == 0 else "assertion_failed"
                    result.cases = {command.id: result.status}
        except TimeoutError:
            result.status, result.reason = "timeout", "Command timed out"
        except (OSError, ValueError, ET.ParseError) as exc:
            result.reason = _redact(str(exc))
        finally:
            if process:
                await asyncio.shield(self._processes._terminate(process))
            output = await drain if drain else ""
            log = directory / f"{command.id}.log"
            await asyncio.to_thread(log.write_text, output, encoding="utf-8")
            result.evidence_refs.append(str(log))
            result.duration_seconds = time.monotonic() - started
        return result

    async def verify(
        self, contract: TaskContract, candidate: Path, registry_version: int,
        registry_fingerprint: str, run_id: str, budget: Any,
    ) -> RegressionReport:
        from aicoding_agent.regression.store import workspace_fingerprint

        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", run_id):
            raise ValueError("Invalid run ID")
        candidate = candidate.resolve()
        report = RegressionReport(
            run_id=run_id, attempt_id=uuid4().hex, registry_version=registry_version,
            registry_fingerprint=registry_fingerprint,
        )
        directory = self.evidence_root / run_id / report.attempt_id
        if directory == candidate or candidate in directory.parents:
            raise ValueError("Regression evidence must be outside candidate")
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=False)
        started = time.monotonic()
        async with self._locks.setdefault(str(candidate), asyncio.Lock()):
            try:
                report.candidate_fingerprint = await workspace_fingerprint(candidate)
                report.test_fingerprint = await asyncio.to_thread(
                    self._test_fingerprint, contract, candidate,
                )
                report.environment_fingerprint = await asyncio.to_thread(
                    self._environment_fingerprint, contract,
                )
                if self.require_both and (
                    not contract.verification_commands
                    or not any(item.required for item in contract.browser_verifications)
                ):
                    raise ValueError(
                        "Frontend acceptance requires command and browser verifications"
                    )
                if not contract.verification_commands and not any(
                    item.required for item in contract.browser_verifications
                ):
                    raise ValueError("Acceptance requires at least one required verification")
                for command in contract.verification_commands:
                    await budget.reserve_command(command.id)
                    report.tests.append(await self._command(
                        command, candidate, directory, budget.remaining_seconds,
                    ))
                    single = contract.model_copy(update={
                        "verification_commands": [command], "browser_verifications": [],
                    })
                    report.tests[-1].test_fingerprint = await asyncio.to_thread(
                        self._test_fingerprint, single, candidate,
                    )
                for verification in contract.browser_verifications:
                    if not verification.required:
                        continue
                    await budget.reserve_browser(verification.id, verification.max_runs)
                    await budget.charge_tools(1)
                    assert self.browser_runner is not None
                    result = await self.browser_runner.run(
                        workspace=candidate, verification=verification, run_id=run_id,
                        timeout_seconds=min(budget.remaining_seconds, verification.timeout_seconds),
                    )
                    expected_cases = self.browser_runner.suites[
                        verification.suite_ref
                    ].expected_tests
                    failed_cases = {failure.test for failure in result.failures}
                    if result.status in {"passed", "failed"}:
                        # Model feedback can be truncated; final host accounting must not be.
                        browser_report = next(
                            Path(ref) for ref in result.evidence_refs
                            if Path(ref).name == "report.json"
                        )
                        browser_payload = json.loads(await asyncio.to_thread(
                            browser_report.read_text, encoding="utf-8",
                        ))
                        full_result = parse_playwright_report(
                            browser_payload, expected_cases, result.exit_code,
                            max_failures=len(expected_cases),
                        )
                        failed_cases = {failure.test for failure in full_result["failures"]}
                    browser_cases = {
                        title: "assertion_failed" if title in failed_cases else (
                            "passed" if result.status in {"passed", "failed"} else result.status
                        )
                        for title in expected_cases
                    }
                    report.tests.append(RegressionTestResult(
                        id=verification.id, kind="browser",
                        status="assertion_failed" if result.status == "failed" else result.status,
                        exit_code=result.exit_code, duration_seconds=result.duration_seconds,
                        reason=result.reason, evidence_refs=result.evidence_refs,
                        browser_result=result.model_dump(mode="json"),
                        test_fingerprint=result.suite_fingerprint,
                        cases=browser_cases,
                    ))
                states = {item.status for item in report.tests}
                report.status = next((state for state in (
                    "timeout", "infra_error", "assertion_failed",
                ) if state in states), "passed")
                if (await workspace_fingerprint(candidate) != report.candidate_fingerprint
                        or await asyncio.to_thread(self._test_fingerprint, contract, candidate)
                        != report.test_fingerprint
                        or await asyncio.to_thread(self._environment_fingerprint, contract)
                        != report.environment_fingerprint):
                    raise ValueError("Verification inputs changed during regression execution")
            except (OSError, ValueError, KeyError, RuntimeError) as exc:
                report.status = ("timeout" if type(exc).__name__ == "RegressionBudgetExceeded"
                                 else "infra_error")
                report.reason = _redact(str(exc))
            finally:
                report.duration_seconds = time.monotonic() - started
                report.report_path = str(directory / "regression-report.json")
                for item in report.tests:
                    for ref in item.evidence_refs:
                        path = Path(ref)
                        if path.is_file():
                            report.evidence_hashes[ref] = await asyncio.to_thread(_sha256, path)
                await asyncio.to_thread(
                    Path(report.report_path).write_text, report.model_dump_json(), encoding="utf-8",
                )
        return report

    async def revalidate(
        self, report: RegressionReport, candidate: Path, contract: TaskContract,
        registry_version: int, registry_fingerprint: str,
        *, allow_assertion_failure: bool = False,
    ) -> bool:
        from aicoding_agent.regression.store import workspace_fingerprint

        try:
            candidate = candidate.resolve()
            if self.require_both and (
                not contract.verification_commands
                or not any(item.required for item in contract.browser_verifications)
            ):
                return False
            allowed = {"passed", "assertion_failed"} if allow_assertion_failure else {"passed"}
            if (report.status not in allowed or report.registry_version != registry_version
                    or report.registry_fingerprint != registry_fingerprint):
                return False
            expected = (self.evidence_root / report.run_id / report.attempt_id
                        / "regression-report.json")
            if Path(report.report_path).resolve() != expected:
                return False
            payload = await asyncio.to_thread(expected.read_text, encoding="utf-8")
            if RegressionReport.model_validate_json(payload) != report:
                return False
            if (await workspace_fingerprint(candidate) != report.candidate_fingerprint
                    or await asyncio.to_thread(self._test_fingerprint, contract, candidate)
                    != report.test_fingerprint
                    or await asyncio.to_thread(self._environment_fingerprint, contract)
                    != report.environment_fingerprint):
                return False
            required = {item.id for item in contract.verification_commands} | {
                item.id for item in contract.browser_verifications if item.required
            }
            if {item.id for item in report.tests} != required or len(report.tests) != len(required):
                return False
            commands = {item.id for item in contract.verification_commands}
            browsers = {item.id: item for item in contract.browser_verifications if item.required}
            for item in report.tests:
                if item.status not in allowed or not item.evidence_refs:
                    return False
                if item.kind != ("command" if item.id in commands else "browser"):
                    return False
                for ref in item.evidence_refs:
                    evidence_hash = await asyncio.to_thread(_sha256, Path(ref))
                    if evidence_hash != report.evidence_hashes.get(ref):
                        return False
                suite = self.command_suites.get(item.id) if item.kind == "command" else None
                if suite and suite.reporter == "pytest_junit":
                    junit = expected.parent / f"{item.id}.xml"
                    if str(junit) not in item.evidence_refs:
                        return False
                    parsed_cases = await asyncio.to_thread(
                        parse_junit, junit, suite.expected_tests,
                        item.exit_code if item.exit_code is not None else -1,
                    )
                    expected_status = (
                        "assertion_failed" if "assertion_failed" in parsed_cases.values()
                        else "passed"
                    )
                    if item.cases != parsed_cases or item.status != expected_status:
                        return False
                if item.kind == "browser":
                    if not item.browser_result:
                        return False
                    if self.browser_runner is None:
                        return False
                    verification = browsers[item.id]
                    if (item.browser_result.get("verification_id") != item.id
                            or item.browser_result.get("suite_ref") != verification.suite_ref
                            or item.browser_result.get("exit_code") != item.exit_code):
                        return False
                    # Frozen runner caches are process-local; report/config hashes above are
                    # the authority when restoring a gate in a new host process.
                    await self.browser_runner.freeze(candidate)
                    if not await self.browser_runner.revalidate(
                        candidate, item.browser_result, allow_failed=allow_assertion_failure,
                    ):
                        return False
                    browser_suite = self.browser_runner.suites[verification.suite_ref]
                    browser_path = (
                        self.browser_runner.evidence_root / item.browser_result["run_id"]
                        / item.browser_result["attempt_id"] / "report.json"
                    )
                    browser_payload = json.loads(await asyncio.to_thread(
                        browser_path.read_text, encoding="utf-8",
                    ))
                    parsed = parse_playwright_report(
                        browser_payload, browser_suite.expected_tests,
                        item.exit_code if item.exit_code is not None else -1,
                        max_failures=len(browser_suite.expected_tests),
                    )
                    failed = {failure.test for failure in parsed["failures"]}
                    parsed_cases = {
                        case: "assertion_failed" if case in failed else "passed"
                        for case in browser_suite.expected_tests
                    }
                    expected_status = (
                        "assertion_failed" if parsed["status"] == "failed" else "passed"
                    )
                    if item.cases != parsed_cases or item.status != expected_status:
                        return False
            expected_status = (
                "assertion_failed"
                if any(item.status == "assertion_failed" for item in report.tests)
                else "passed"
            )
            if report.status != expected_status:
                return False
            return True
        except (OSError, ValueError, KeyError, RuntimeError, ET.ParseError):
            return False
