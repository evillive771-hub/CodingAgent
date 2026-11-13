"""One durable budget for coding, host verification and baseline retries."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import Field

from aicoding_agent.domain.models import Budget, StrictModel
from aicoding_agent.persistence.base import validate_run_id
from aicoding_agent.persistence.jsonl import AtomicCheckpointStore
from aicoding_agent.tools.file_lock import WorkspaceFileLockManager


class RegressionBudgetExceeded(RuntimeError):
    pass


class BudgetState(StrictModel):
    limits: Budget
    deadline_epoch: float
    turns: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    browser_runs: dict[str, int] = Field(default_factory=dict)
    command_runs: dict[str, int] = Field(default_factory=dict)
    phase_counts: dict[str, int] = Field(default_factory=dict)


class RegressionBudget:
    def __init__(self, root: Path, run_id: str, state: BudgetState) -> None:
        self.root = root.resolve()
        self.run_id = validate_run_id(run_id)
        self.state = state
        self.store = AtomicCheckpointStore(self.root)
        self.locks = WorkspaceFileLockManager()
        self.phase = "unspecified"

    @classmethod
    async def initialize(cls, root: Path, run_id: str, limits: Budget) -> RegressionBudget:
        validate_run_id(run_id)
        store = AtomicCheckpointStore(root)
        locks = WorkspaceFileLockManager()
        async with locks.acquire(root, root / run_id / "budget-lease", owner=run_id):
            payload = await store.load(run_id)
            if payload is None:
                state = BudgetState(
                    limits=limits,
                    deadline_epoch=datetime.now(UTC).timestamp() + limits.max_elapsed_seconds,
                )
                await store.save(run_id, state.model_dump(mode="json"))
            else:
                state = BudgetState.model_validate(payload)
                if state.limits != limits:
                    raise ValueError("A resumed regression run cannot replace its budget")
        return cls(root, run_id, state)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.state.deadline_epoch - datetime.now(UTC).timestamp())

    @property
    def deadline_at(self) -> float:
        return time.monotonic() + self.remaining_seconds

    async def _reserve(self, kind: str, count: int = 1, test_id: str = "", limit: int = 0) -> None:
        if count < 1:
            raise ValueError("Budget charges must be positive")
        async with self.locks.acquire(
            self.root, self.root / self.run_id / "budget-lease", owner=self.run_id
        ):
            payload = await self.store.load(self.run_id)
            if payload is None:
                raise ValueError("Regression budget checkpoint disappeared")
            self.state = BudgetState.model_validate(payload)
            if self.remaining_seconds <= 0:
                raise RegressionBudgetExceeded("Task total time budget exhausted")
            if kind == "turn":
                if self.state.turns + count > self.state.limits.max_turns:
                    raise RegressionBudgetExceeded("Task total model-turn budget exhausted")
                self.state.turns += count
            elif kind == "tool":
                if self.state.tool_calls + count > self.state.limits.max_tool_calls:
                    raise RegressionBudgetExceeded("Task total tool-call budget exhausted")
                self.state.tool_calls += count
            else:
                counts = self.state.browser_runs if kind == "browser" else self.state.command_runs
                if kind == "browser" and counts.get(test_id, 0) + count > limit:
                    raise RegressionBudgetExceeded(f"Browser execution budget exhausted: {test_id}")
                counts[test_id] = counts.get(test_id, 0) + count
            key = f"{self.phase}:{kind}:{test_id}"
            self.state.phase_counts[key] = self.state.phase_counts.get(key, 0) + count
            await self.store.save(self.run_id, self.state.model_dump(mode="json"))

    async def charge_turn(self) -> None:
        await self._reserve("turn")

    async def charge_tools(self, count: int = 1) -> None:
        await self._reserve("tool", count)

    async def reserve_browser(self, verification_id: str, max_runs: int) -> None:
        await self._reserve("browser", test_id=verification_id, limit=max_runs)

    async def reserve_command(self, verification_id: str, *, charge_tool: bool = True) -> None:
        if charge_tool:
            await self.charge_tools()
        await self._reserve("command", test_id=verification_id)

    async def refresh(self) -> BudgetState:
        payload = await self.store.load(self.run_id)
        if payload is None:
            raise ValueError("Regression budget checkpoint disappeared")
        self.state = BudgetState.model_validate(payload)
        return self.state.model_copy(deep=True)

    async def wait(self, seconds: float) -> None:
        """Bound a host environment recovery delay by the same task deadline."""
        if seconds >= self.remaining_seconds:
            raise RegressionBudgetExceeded("Task total time budget exhausted")
        await asyncio.sleep(seconds)
