"""Durable host coordinator for candidate validation and baseline promotion."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from aicoding_agent.agent.mutation_journal import FileSnapshot, MutationJournal
from aicoding_agent.domain.models import (
    AgentResult,
    AgentStatus,
    StrictModel,
    TaskContract,
    TddSpec,
    canonical_task_contract_payload,
)
from aicoding_agent.persistence.base import validate_run_id
from aicoding_agent.persistence.jsonl import AtomicCheckpointStore
from aicoding_agent.regression.budget import RegressionBudget, RegressionBudgetExceeded
from aicoding_agent.regression.gate import RegressionGate, RegressionReport
from aicoding_agent.regression.models import AcceptedBaseline
from aicoding_agent.regression.store import (
    AcceptedBaselineStore,
    BaselineConflict,
    RegressionStoreError,
    apply_delta,
    source_manifest,
    workspace_fingerprint,
)
from aicoding_agent.regression.tdd import TddError, TddEvidence, TddGate
from aicoding_agent.tools.file_lock import WorkspaceFileLockManager
from aicoding_agent.tools.tdd import TddProposal


class RegressionOutcome(StrictModel):
    status: Literal["accepted", "validated", "blocked"]
    run_id: str
    workspace: Path
    agent_result: AgentResult
    effective_contract: TaskContract
    original_contract: TaskContract
    base: AcceptedBaseline
    report: RegressionReport | None = None
    baseline_revision: int | None = None
    reason: str | None = None
    tdd_evidence: TddEvidence | None = None
    combined: bool = False


class CandidateRun(StrictModel):
    contract: TaskContract
    contract_hash: str
    source: Path
    source_fingerprint: str
    base: AcceptedBaseline
    effective_contract: TaskContract
    attempt: int = 0
    workspace: Path | None = None
    phase: Literal["prepare", "test_design", "red_verify", "coding", "verify",
                   "green_verify", "finished"] = "prepare"
    agent_result: AgentResult | None = None
    report: RegressionReport | None = None
    infra_retries_used: int = 0
    adopt: bool = False
    accept: bool = True
    outcome: RegressionOutcome | None = None
    failed_reports: list[str] = Field(default_factory=list)
    failure_summary: str = ""
    budget_run_id: str | None = None
    adoption_before: dict[str, FileSnapshot] = Field(default_factory=dict)
    adoption_started: bool = False
    tdd_evidence: TddEvidence | None = None
    combined: bool = False


def _contract_hash(contract: TaskContract) -> str:
    data = canonical_task_contract_payload(contract)
    data.pop("created_at", None)
    if data.get("tdd") is None:
        data.pop("tdd", None)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class RegressionController:
    def __init__(
        self, root: Path, gate: RegressionGate, infra_retries: int = 1, require_tdd: bool = False,
    ) -> None:
        if not 0 <= infra_retries <= 2:
            raise ValueError("Environment retries must be between zero and two")
        self.root = root.resolve()
        self.gate = gate
        self.store = AcceptedBaselineStore(self.root / "baseline")
        self.checkpoints = AtomicCheckpointStore(self.root / "runs")
        self.locks = WorkspaceFileLockManager()
        self.infra_retries = infra_retries
        self.require_tdd = require_tdd
        self.tdd_gate = TddGate(gate)

    async def _save(self, run_id: str, state: CandidateRun) -> None:
        await self.checkpoints.save(run_id, state.model_dump(mode="json"))

    async def _event(self, run_id: str, event: str, **values: Any) -> None:
        # Append-only, scoped to the idempotent coordinator run; never log prompts/secrets.
        path = self.root / "events.jsonl"
        payload = json.dumps({"run_id": run_id, "event": event, **values}) + "\n"
        async with self.locks.acquire(self.root, path, owner=run_id):
            def append() -> None:
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(payload)
            await asyncio.to_thread(append)

    async def _finish(
        self, run_id: str, state: CandidateRun, status: str, reason: str | None = None,
        revision: int | None = None,
    ) -> RegressionOutcome:
        result = state.agent_result or AgentResult(status=AgentStatus.BLOCKED, run_id=run_id)
        if status == "blocked":
            result = result.model_copy(update={
                "status": AgentStatus.BLOCKED, "reason": reason,
                "final_text": f"Regression acceptance blocked: {reason}",
            })
        elif status == "accepted":
            result = result.model_copy(update={"status": AgentStatus.COMPLETED})
        budget = await RegressionBudget.initialize(
            self.root / "budgets", state.budget_run_id or run_id, state.contract.budget,
        )
        counts = await budget.refresh()
        result = result.model_copy(update={
            "run_id": run_id, "turns": counts.turns, "tool_calls": counts.tool_calls,
        })
        outcome = RegressionOutcome(
            status=status, run_id=run_id, workspace=state.workspace or state.source,
            agent_result=result, effective_contract=state.effective_contract,
            original_contract=state.contract, base=state.base, report=state.report,
            baseline_revision=revision, reason=reason,
            tdd_evidence=state.tdd_evidence,
            combined=state.combined,
        )
        state.phase, state.outcome = "finished", outcome
        await self._save(run_id, state)
        await self._event(run_id, f"regression.{status}", revision=revision, reason=reason)
        return outcome

    async def validate_outcome(self, outcome: RegressionOutcome) -> bool:
        report = outcome.report
        if outcome.original_contract.tdd is not None or self.require_tdd:
            if outcome.tdd_evidence is None or report is None:
                return False
            if outcome.combined:
                return await self._validate_combined_tdd(
                    outcome.tdd_evidence, report, outcome.original_contract, outcome.workspace,
                )
            return await self.tdd_gate.validate_pair(
                outcome.tdd_evidence, report, outcome.effective_contract,
                outcome.tdd_evidence.baseline_path, outcome.workspace,
            )
        return bool(report and await self.gate.revalidate(
            report, outcome.workspace, outcome.effective_contract,
            report.registry_version, report.registry_fingerprint,
        ))

    async def validate_checkpoint(self, run_id: str) -> bool:
        payload = await self.checkpoints.load(run_id)
        if payload is None:
            return False
        state = CandidateRun.model_validate(payload)
        return bool(state.outcome and state.outcome.status == "accepted"
                    and await self.validate_outcome(state.outcome))

    async def run(
        self, contract: TaskContract, workspace: Path, *, run_id: str, agent: Any,
        agent_kwargs: dict[str, Any] | None = None, accept: bool = True, adopt: bool = False,
        adoption_journal: MutationJournal | None = None,
    ) -> RegressionOutcome:
        validate_run_id(run_id)
        workspace = workspace.resolve()
        async with self.locks.acquire(
            self.root, self.root / "runs" / run_id / "coordinator-lease", owner=run_id,
        ):
            payload = await self.checkpoints.load(run_id)
            if payload is None:
                base = await self.store.initialize(workspace)
                state = CandidateRun(
                    contract=contract, contract_hash=_contract_hash(contract), source=workspace,
                    source_fingerprint=await workspace_fingerprint(workspace), base=base,
                    effective_contract=await self.store.effective_contract(contract),
                    accept=accept, adopt=adopt,
                    budget_run_id=run_id,
                )
                await self._save(run_id, state)
            else:
                state = CandidateRun.model_validate(payload)
                if (_contract_hash(state.contract) != _contract_hash(contract)
                        or state.source != workspace
                        or state.accept != accept or state.adopt != adopt):
                    raise ValueError("A resumed run cannot change its contract, source or mode")
            if state.adoption_started and state.outcome is None:
                current = await self.store.current()
                if current.revision == state.base.revision:
                    # Publication did not happen; undo an interrupted source copy before
                    # budget/time checks. Unrelated user edits are never overwritten.
                    assert state.workspace is not None
                    await apply_delta(state.workspace, Path(state.base.snapshot_path), state.source)
                    state.adoption_started = False
                    await self._save(run_id, state)
            if state.outcome:
                if (state.outcome.status != "blocked"
                        and not await self.validate_outcome(state.outcome)):
                    return await self._finish(run_id, state, "blocked", "Saved evidence is stale")
                return state.outcome
            budget = await RegressionBudget.initialize(
                self.root / "budgets", run_id, state.contract.budget,
            )
            if state.adopt and state.phase == "prepare" and state.attempt == 0:
                if state.source_fingerprint != state.base.candidate_fingerprint:
                    return await self._finish(run_id, state, "blocked",
                                              "Source differs from the accepted baseline")
            try:
                return await self._execute(run_id, state, agent, agent_kwargs or {}, budget,
                                           adoption_journal)
            except (RegressionBudgetExceeded, RegressionStoreError, OSError, TddError) as exc:
                if isinstance(exc, TddError) and exc.report:
                    state.report = exc.report
                return await self._finish(run_id, state, "blocked", str(exc))

    async def _execute(
        self, run_id: str, state: CandidateRun, agent: Any, kwargs: dict[str, Any],
        budget: RegressionBudget,
        adoption_journal: MutationJournal | None,
    ) -> RegressionOutcome:
        while True:
            if budget.remaining_seconds <= 0:
                raise RegressionBudgetExceeded("Task total time budget exhausted")
            if state.phase == "prepare":
                state.workspace = await self.store.prepare_candidate(
                    state.source, run_id, state.attempt, state.base.revision,
                )
                state.phase = (
                    "test_design" if state.contract.tdd and state.contract.tdd.propose_tests else
                    "red_verify" if (state.contract.tdd or self.require_tdd) else "coding"
                )
                await self._save(run_id, state)
                await self._event(run_id, "candidate.created", attempt=state.attempt,
                                  workspace=str(state.workspace))
            assert state.workspace is not None
            if state.phase in {"red_verify", "test_design"}:
                async def update_tdd_phase(phase: str) -> None:
                    state.phase = phase
                    await self._save(run_id, state)

                state.tdd_evidence = await self.prepare_tdd(
                    state.contract, state.workspace, run_id=run_id,
                    shared_budget=budget, agent=agent,
                    effective_contract=state.effective_contract,
                    stage_callback=update_tdd_phase,
                )
                resolved = await self._tdd_contract(run_id)
                if resolved is not None:
                    state.effective_contract = resolved
                state.phase = "coding"
                await self._save(run_id, state)
            if state.tdd_evidence and not await self.tdd_gate.validate_red(
                state.tdd_evidence, state.effective_contract, state.tdd_evidence.baseline_path,
            ):
                raise TddError("RED proof changed before coding or acceptance")
            if state.phase == "coding":
                budget.phase = "coding"
                options = {
                    **kwargs, "run_id": f"{run_id}-a{state.attempt}", "shared_budget": budget,
                    "tdd_authorized": state.tdd_evidence is not None,
                }
                if state.tdd_evidence:
                    options["conversation_history"] = [
                        *options.get("conversation_history", []),
                        {"role": "user", "content": "Host verified RED on the untouched baseline. "
                         "Fix these exact failing cases without modifying tests: " +
                         json.dumps(state.tdd_evidence.red_cases) + "\n" +
                         await self._feedback(state.tdd_evidence.red_report)},
                    ]
                if options.get("message_journal") is not None:
                    options["message_journal"] = _JournalProxy(options["message_journal"], run_id)
                if state.attempt:
                    # Fresh coding context after baseline rollback; retain host/user permissions.
                    options["resume_messages"] = None
                    feedback = {
                        "role": "user",
                        "content": "Previous candidate failed host regression. Restart from the "
                        "accepted baseline; preserve historical behavior. Failure evidence: "
                        + state.failure_summary + "\n" + "\n".join(state.failed_reports[-3:]),
                    }
                    options["conversation_history"] = [
                        *options.get("conversation_history", []), feedback,
                    ]
                signature = inspect.signature(agent.run)
                if not any(p.kind == inspect.Parameter.VAR_KEYWORD
                           for p in signature.parameters.values()):
                    options = {k: v for k, v in options.items() if k in signature.parameters}
                async with asyncio.timeout(budget.remaining_seconds):
                    state.agent_result = await agent.run(
                        state.effective_contract, state.workspace, **options,
                    )
                if state.agent_result.status not in {
                    AgentStatus.COMPLETED, AgentStatus.SUBMITTED_UNVERIFIED,
                }:
                    return await self._finish(
                        run_id, state, "blocked", state.agent_result.reason or
                        f"Coding ended with {state.agent_result.status}",
                    )
                state.phase = "green_verify" if state.tdd_evidence else "verify"
                await self._save(run_id, state)
            budget.phase = "green_verify"
            state.report = await self.gate.verify(
                state.effective_contract, state.workspace, state.base.registry_version,
                state.base.registry_fingerprint, run_id, budget,
            )
            await self._save(run_id, state)
            if state.report.status == "passed":
                if not await self.validate_outcome(RegressionOutcome(
                    status="validated", run_id=run_id, workspace=state.workspace,
                    agent_result=state.agent_result, effective_contract=state.effective_contract,
                    original_contract=state.contract, base=state.base, report=state.report,
                    tdd_evidence=state.tdd_evidence,
                )):
                    return await self._finish(run_id, state, "blocked", "Verification became stale")
                if not state.accept:
                    return await self._finish(run_id, state, "validated")
                return await self._accept(run_id, state, adoption_journal)
            state.failed_reports.append(state.report.report_path)
            state.failure_summary = await self._feedback(state.report)
            if (state.report.status == "infra_error"
                    and state.infra_retries_used < self.infra_retries):
                state.infra_retries_used += 1
                await self._save(run_id, state)
                await self._event(run_id, "environment.retry", count=state.infra_retries_used)
                continue
            if state.report.status == "assertion_failed" and state.attempt < 1:
                state.attempt += 1
                state.phase, state.workspace, state.agent_result = "prepare", None, None
                await self._save(run_id, state)
                await self._event(run_id, "candidate.retry_from_baseline", attempt=state.attempt)
                continue
            return await self._finish(
                run_id, state, "blocked", state.report.reason or
                "; ".join(f"{test.id}: {test.status} {test.reason or ''}"
                          for test in state.report.tests if test.status != "passed")
                or state.report.status,
            )

    async def _feedback(self, report: RegressionReport) -> str:
        parts = []
        for test in report.tests:
            if test.status == "passed":
                continue
            parts.append(f"{test.id}: {test.status}; {test.reason or ''}; cases={test.cases}")
            for ref in test.evidence_refs:
                if Path(ref).suffix == ".log":
                    def tail(path: Path = Path(ref)) -> str:
                        with path.open("rb") as stream:
                            stream.seek(max(0, path.stat().st_size - 3000))
                            return stream.read(3000).decode("utf-8", errors="replace")
                    parts.append(await asyncio.to_thread(tail))
        return "\n".join(parts)[:12000]

    async def _accept(
        self, run_id: str, state: CandidateRun, adoption_journal: MutationJournal | None = None,
    ) -> RegressionOutcome:
        assert state.workspace and state.report
        current = await self.store.current()
        if state.tdd_evidence:
            valid = await self._validate_combined_tdd(
                state.tdd_evidence, state.report, state.contract, state.workspace,
            ) if state.combined else await self.tdd_gate.validate_pair(
                state.tdd_evidence, state.report, state.effective_contract,
                state.tdd_evidence.baseline_path, state.workspace,
            )
            if not valid:
                raise TddError("RED/GREEN proof is stale before promotion")
        registry = await self.store.registry()
        registry.adding(state.contract, {test.id: test.test_fingerprint
                                        for test in state.report.tests})
        # Recover a crash between atomic baseline publication and run checkpoint publication.
        if current.revision == state.base.revision + 1:
            if (current.candidate_fingerprint == state.report.candidate_fingerprint
                    and any(task.task_id == state.contract.task_id
                            for task in registry.state.tasks)):
                return await self._finish(run_id, state, "accepted", revision=current.revision)
        if current.revision != state.base.revision:
            raise BaselineConflict("Integration revision advanced during candidate verification")
        async def adopt() -> None:
            assert state.workspace is not None
            base_files, new_files, current_files = await asyncio.gather(
                source_manifest(Path(state.base.snapshot_path)),
                source_manifest(state.workspace), source_manifest(state.source),
            )
            # A resumed interrupted copy may contain either side of each known change.
            # Unrelated files, including new user files, must still match the original source.
            for name in base_files.keys() | new_files.keys() | current_files.keys():
                allowed = {base_files.get(name)}
                if state.adoption_started:
                    allowed.add(new_files.get(name))
                if current_files.get(name) not in allowed:
                    raise BaselineConflict("Source changed while the candidate was being verified")
            if adoption_journal and not state.adoption_started:
                for name in base_files.keys() | new_files.keys():
                    if base_files.get(name) != new_files.get(name):
                        state.adoption_before[name] = await adoption_journal.capture(
                            Path(state.base.snapshot_path) / name,
                        )
            state.adoption_started = True
            await self._save(run_id, state)
            copy_task = asyncio.create_task(apply_delta(
                Path(state.base.snapshot_path), state.workspace, state.source,
            ))
            try:
                await asyncio.shield(copy_task)
                if await workspace_fingerprint(state.source) != state.report.candidate_fingerprint:
                    raise BaselineConflict("Source changed during adoption")
                if adoption_journal:
                    existing = await adoption_journal.load(run_id)
                    recorded = {record.path for record in existing
                                if record.tool_name == "regression_accept"}
                    for name, snapshot in state.adoption_before.items():
                        if name not in recorded:
                            await adoption_journal.record(
                                run_id=run_id, tool_name="regression_accept",
                                workspace=state.source,
                                path=state.source / name, before=snapshot,
                            )
            except BaseException:
                # Finish the thread-backed copy before rollback, including cancellation.
                await asyncio.gather(copy_task, return_exceptions=True)
                await asyncio.shield(apply_delta(
                    state.workspace, Path(state.base.snapshot_path), state.source,
                ))
                state.adoption_started = False
                await self._save(run_id, state)
                raise
        baseline = await self.store.accept(
            state.workspace, state.contract, state.report, state.base.revision,
            before_publish=adopt if state.adopt else None,
        )
        return await self._finish(run_id, state, "accepted", revision=baseline.revision)

    async def integrate(self, outcome: RegressionOutcome) -> RegressionOutcome:
        """Merge a separately validated worker candidate, then verify the frozen combination."""
        run_id = outcome.run_id + "-integration"
        validate_run_id(run_id)
        async with self.locks.acquire(
            self.root, self.root / "runs" / run_id / "coordinator-lease", owner=run_id,
        ):
            payload = await self.checkpoints.load(run_id)
            if payload:
                state = CandidateRun.model_validate(payload)
                if state.outcome:
                    return state.outcome if await self.validate_outcome(state.outcome) else (
                        await self._finish(run_id, state, "blocked", "Saved integration is stale")
                    )
            else:
                base = await self.store.current()
                state = CandidateRun(
                    contract=outcome.original_contract,
                    contract_hash=_contract_hash(outcome.original_contract),
                    source=Path(base.source_workspace),
                    source_fingerprint=await workspace_fingerprint(
                        Path(base.source_workspace),
                    ), base=base, effective_contract=await self.store.effective_contract(
                        outcome.original_contract,
                    ), agent_result=outcome.agent_result,
                    budget_run_id=outcome.run_id,
                    tdd_evidence=outcome.tdd_evidence,
                    combined=True,
                )
                await self._save(run_id, state)
            budget = await RegressionBudget.initialize(
                self.root / "budgets", outcome.run_id, state.contract.budget,
            )
            if outcome.status != "validated" or not await self.validate_outcome(outcome):
                return await self._finish(run_id, state, "blocked", "Worker evidence is stale")
            try:
                if state.workspace is None:
                    state.workspace = await self.store.prepare_candidate(
                        state.source, run_id, 0, state.base.revision,
                    )
                    await apply_delta(Path(outcome.base.snapshot_path), outcome.workspace,
                                      state.workspace)
                    state.phase = "verify"
                    await self._save(run_id, state)
                state.report = await self.gate.verify(
                    state.effective_contract, state.workspace, state.base.registry_version,
                    state.base.registry_fingerprint, run_id, budget,
                )
                await self._save(run_id, state)
                if state.report.status != "passed":
                    return await self._finish(
                        run_id, state, "blocked", "Combined regression failed",
                    )
                if not await self.gate.revalidate(
                    state.report, state.workspace, state.effective_contract,
                    state.base.registry_version, state.base.registry_fingerprint,
                ):
                    return await self._finish(
                        run_id, state, "blocked", "Combination evidence is stale",
                    )
                return await self._accept(run_id, state)
            except (RegressionStoreError, RegressionBudgetExceeded, OSError, TddError) as exc:
                return await self._finish(run_id, state, "blocked", str(exc))

    async def verify_combination(
        self, *, workspace: Path, source_workspace: Path, contract: TaskContract,
        run_id: str, expected_revision: str, budget_run_id: str | None = None,
        tdd_evidence: TddEvidence | None = None,
    ) -> RegressionReport:
        """Called under the coordinator's integration lock on its temporary combination."""
        validate_run_id(run_id)
        await self.store.initialize(source_workspace)
        base = await self.store.current()
        effective = await self.store.effective_contract(contract)
        budget = await self.budget_for(budget_run_id or run_id, contract)
        budget.phase = "green_verify"
        report = await self.gate.verify(
            effective, workspace, base.registry_version, base.registry_fingerprint, run_id, budget,
        )
        if contract.tdd or self.require_tdd:
            if tdd_evidence is None or not await self._validate_combined_tdd(
                tdd_evidence, report, contract, workspace,
            ):
                raise TddError("Combined version lacks a valid RED/GREEN pair")
        await self._event(run_id, "combination.verified", status=report.status,
                          integration_revision=expected_revision, report=report.report_path)
        return report

    async def budget_for(self, run_id: str, contract: TaskContract) -> RegressionBudget:
        return await RegressionBudget.initialize(self.root / "budgets", run_id, contract.budget)

    async def initialize_source(self, workspace: Path) -> AcceptedBaseline:
        return await self.store.initialize(workspace)

    async def resolved_tdd_contract(self, run_id: str, contract: TaskContract) -> TaskContract:
        return await self._tdd_contract(run_id) or contract

    async def validate_combination(
        self, *, workspace: Path, contract: TaskContract, report: RegressionReport,
    ) -> bool:
        base = await self.store.current()
        try:
            registry = await self.store.registry()
            registry.adding(contract, {test.id: test.test_fingerprint for test in report.tests})
        except RegressionStoreError:
            return False
        return await self.gate.revalidate(
            report, workspace, await self.store.effective_contract(contract),
            base.registry_version, base.registry_fingerprint,
        )

    async def record_combination_acceptance(
        self, *, workspace: Path, contract: TaskContract, report: RegressionReport, run_id: str,
        before_publish: Callable[[], Awaitable[None]] | None = None,
        tdd_evidence: TddEvidence | None = None,
    ) -> AcceptedBaseline:
        effective = await self.store.effective_contract(contract)
        base = await self.store.current()
        if contract.tdd or self.require_tdd:
            if tdd_evidence is None or not await self._validate_combined_tdd(
                tdd_evidence, report, contract, workspace,
            ):
                raise TddError("Combined TDD evidence is stale before acceptance")
        if not await self.gate.revalidate(
            report, workspace, effective, base.registry_version, base.registry_fingerprint,
        ):
            raise RegressionStoreError("Combined regression evidence is stale")
        accepted = await self.store.accept(
            workspace, contract, report, base.revision, before_publish=before_publish,
        )
        await self._event(run_id, "combination.accepted", revision=accepted.revision)
        return accepted

    async def _validate_combined_tdd(
        self, evidence: TddEvidence, report: RegressionReport, contract: TaskContract,
        workspace: Path,
    ) -> bool:
        # The combination may add historical suites; validate the original pair's
        # suite identities and target cases separately from the complete newer Gate.
        red_contract = await self._tdd_contract(evidence.red_report.run_id.removesuffix("-red"))
        if red_contract is None or not await self.tdd_gate.validate_red(
            evidence, red_contract, evidence.baseline_path,
        ):
            return False
        old = {test.id: test for test in evidence.red_report.tests}
        new = {test.id: test for test in report.tests}
        if report.environment_fingerprint != evidence.environment_fingerprint:
            # Additional executables change the aggregate environment; each original
            # suite is still checked against RED below, and gate checks the full environment.
            if not set(old) <= set(new):
                return False
        for test_id, result in old.items():
            if (test_id not in new or new[test_id].status != "passed"
                    or new[test_id].test_fingerprint != result.test_fingerprint):
                return False
        return await self.gate.revalidate(
            report, workspace, await self.store.effective_contract(contract),
            report.registry_version, report.registry_fingerprint,
        )

    async def _tdd_contract(self, run_id: str) -> TaskContract | None:
        data = await AtomicCheckpointStore(self.root / "tdd").load(run_id)
        return TaskContract.model_validate(data["contract"]) if data else None

    async def prepare_tdd(
        self, contract: TaskContract, workspace: Path, *, run_id: str,
        shared_budget: RegressionBudget | None = None, agent: Any = None,
        effective_contract: TaskContract | None = None,
        stage_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> TddEvidence | None:
        if contract.tdd is None:
            if self.require_tdd:
                raise TddError("This host requires a TDD contract with RED target cases")
            return None
        if not self.store.manifest_path.exists():
            await self.store.initialize(workspace)
        validate_run_id(run_id)
        budget = shared_budget or await self.budget_for(run_id, contract)
        saved = AtomicCheckpointStore(self.root / "tdd")
        effective = effective_contract or await self.store.effective_contract(contract)
        data = await saved.load(run_id)
        if data:
            if data.get("request_hash", _contract_hash(contract)) != _contract_hash(contract):
                raise TddError("A resumed TDD phase cannot replace its task contract")
            effective = TaskContract.model_validate(data["contract"])
        if data and data.get("evidence"):
            evidence = TddEvidence.model_validate(data["evidence"])
            if not await self.tdd_gate.validate_red(evidence, effective, evidence.baseline_path):
                raise TddError("Saved RED proof is stale")
            return evidence
        if data is None:
            snapshot = self.root / "tdd-baselines" / run_id
            files = await source_manifest(workspace)
            def capture() -> None:
                snapshot.mkdir(parents=True, exist_ok=True)
                for name in files:
                    destination = snapshot / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(workspace / name, destination)
            await asyncio.to_thread(capture)
            if await workspace_fingerprint(snapshot) != await workspace_fingerprint(workspace):
                raise TddError("Baseline changed while preparing RED")
            base = await self.store.current()
            data = {"contract": effective.model_dump(mode="json"), "baseline": str(snapshot),
                    "request_hash": _contract_hash(contract),
                    "registry_version": base.registry_version,
                    "registry_fingerprint": base.registry_fingerprint, "infra_retries_used": 0}
            await saved.save(run_id, data)
        if contract.tdd.propose_tests:
            if stage_callback:
                await stage_callback("test_design")
            budget.phase = "test_design"
            if not data.get("proposal"):
                if agent is None or not hasattr(agent, "propose_tests"):
                    raise TddError("The selected agent cannot submit test proposals")
                slot_context = self._proposal_context(contract)
                draft_contract = contract.model_copy(update={
                    "objective": (contract.objective + "\nHost test proposal contract:\n"
                                  + slot_context),
                })
                proposal = await agent.propose_tests(
                    draft_contract, workspace, run_id=run_id + "-proposal", shared_budget=budget,
                )
                data["proposal"] = proposal.model_dump(mode="json")
                await saved.save(run_id, data)
            proposal = TddProposal.model_validate(data["proposal"])
            await self._freeze_proposal(contract, proposal)
            effective = effective.model_copy(update={
                "tdd": TddSpec(red_cases=proposal.red_cases, propose_tests=True),
            })
            data["contract"] = effective.model_dump(mode="json")
            await saved.save(run_id, data)
        if stage_callback:
            await stage_callback("red_verify")
        data["phase"] = "red_verify"
        await saved.save(run_id, data)
        spec = effective.tdd
        assert spec is not None
        registry = await self.store.registry()
        historical = {test.id for task in registry.state.tasks
                      for test in [*task.commands, *task.browsers]}
        while True:
            budget.phase = "red_verify"
            try:
                proof = await self.tdd_gate.verify_red(
                    effective, Path(data["baseline"]), data["registry_version"],
                    data["registry_fingerprint"], run_id + "-red", budget, spec, historical,
                )
                data["evidence"] = proof.model_dump(mode="json")
                await saved.save(run_id, data)
                await self._event(run_id, "tdd.red_verified", report=proof.red_report.report_path)
                return proof
            except TddError as exc:
                if (exc.status == "infra_error"
                        and data["infra_retries_used"] < self.infra_retries):
                    data["infra_retries_used"] += 1
                    await saved.save(run_id, data)
                    continue
                raise

    def _proposal_context(self, contract: TaskContract) -> str:
        slots, inventories = set(), {}
        for verification in [*contract.verification_commands, *contract.browser_verifications]:
            suite = self.gate.command_suites.get(verification.id)
            if suite is None and self.gate.browser_runner and hasattr(verification, "suite_ref"):
                suite = self.gate.browser_runner.suites.get(verification.suite_ref)
            if suite is None:
                continue
            inventories[verification.id] = suite.expected_tests
            for file in suite.test_files:
                target = file if file.is_absolute() else suite.cwd / file
                try:
                    name = target.resolve().relative_to(suite.cwd.resolve()).as_posix()
                except ValueError:
                    continue
                if name.startswith("tests/"):
                    slots.add(name)
        return json.dumps({"test_slots": sorted(slots), "case_inventory": inventories,
                           "workspace_variable": "REGRESSION_WORKSPACE"})[:12000]

    async def _freeze_proposal(self, contract: TaskContract, proposal: TddProposal) -> None:
        if any(index >= len(contract.acceptance_criteria)
               for index in proposal.requirement_links.values()):
            raise TddError("Proposal names an unknown acceptance criterion")
        spec = contract.tdd
        if spec and spec.red_cases and spec.red_cases != proposal.red_cases:
            raise TddError("Proposal cannot replace host-declared RED cases")
        current_ids = {test.id for test in contract.verification_commands}
        current_ids.update(test.id for test in contract.browser_verifications if test.required)
        if not set(proposal.red_cases) <= current_ids:
            raise TddError("Proposal cannot introduce unconfigured test runners")
        destinations: dict[str, Path] = {}
        registry = await self.store.registry()
        historical_ids = {test.id for task in registry.state.tasks
                          for test in [*task.commands, *task.browsers]}
        if set(proposal.red_cases) & historical_ids:
            raise TddError("Proposal cannot alter historical tests")
        for test_id in proposal.red_cases:
            suite = self.gate.command_suites.get(test_id)
            if suite is None and self.gate.browser_runner:
                verification = next(
                    (v for v in contract.browser_verifications if v.id == test_id), None,
                )
                suite = (self.gate.browser_runner.suites.get(verification.suite_ref)
                         if verification else None)
            if suite is None:
                raise TddError("Proposal requires a host-configured external test suite")
            for file in suite.test_files:
                path = (file if file.is_absolute() else suite.cwd / file).resolve()
                try:
                    name = path.relative_to(suite.cwd.resolve()).as_posix()
                except ValueError:
                    continue
                if name.startswith("tests/"):
                    if name in destinations and destinations[name] != path:
                        raise TddError("Proposal file matches multiple host test destinations")
                    destinations[name] = path
        for file in proposal.files:
            if file.path not in destinations:
                raise TddError("Proposal file is outside host-declared test slots")
            destination = destinations[file.path]
            def freeze(destination: Path = destination, content: str = file.content) -> None:
                if any(parent.is_symlink() for parent in destination.parents):
                    raise TddError("Proposal target has a symbolic-link parent")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    if destination.read_text(encoding="utf-8") != content:
                        raise TddError("Proposal cannot overwrite an existing frozen test")
                    return
                with destination.open("x", encoding="utf-8", newline="") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            await asyncio.to_thread(freeze)


class _JournalProxy:
    """Separate candidate mutation IDs while preserving the existing chat recovery journal."""
    def __init__(self, journal: Any, run_id: str) -> None:
        self.journal, self.run_id = journal, run_id

    async def append_messages(self, session_id: str, *, run_id: str, **kwargs: Any) -> Any:
        return await self.journal.append_messages(session_id, run_id=self.run_id, **kwargs)
