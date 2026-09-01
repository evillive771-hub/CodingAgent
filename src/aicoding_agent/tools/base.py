from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from aicoding_agent.domain.models import TaskContract, ToolRisk

if TYPE_CHECKING:
    from aicoding_agent.agent.background import BackgroundTaskManager
    from aicoding_agent.agent.plan import PlanManager


class ToolError(RuntimeError):
    """A safe, model-facing tool failure."""


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output: str
    data: dict[str, Any] = Field(default_factory=dict)
    is_mutation: bool = False


EventEmitter = Callable[[str, dict[str, Any]], Awaitable[None]]


class ToolContext:
    def __init__(
        self,
        *,
        workspace: Path,
        contract: TaskContract,
        emit: EventEmitter | None = None,
        plan_manager: PlanManager | None = None,
        run_id: str = "run",
        background_manager: BackgroundTaskManager | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.contract = contract
        self.emit = emit
        self.plan_manager = plan_manager
        self.run_id = run_id
        self.background_manager = background_manager
        self.state: dict[str, Any] = {
            "operation_sequence": 0,
            "last_mutation_sequence": 0,
            "verification_success": {},
            "coding_outcome": None,
            "user_question": None,
        }

    async def next_operation(self) -> int:
        self.state["operation_sequence"] += 1
        return int(self.state["operation_sequence"])


class BaseTool[InputT: ToolInput](ABC):
    name: str
    description: str
    input_model: type[InputT]
    risk: ToolRisk
    concurrency_safe: bool = False

    def model_definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema(),
            },
        }

    @abstractmethod
    async def run(self, arguments: InputT, context: ToolContext) -> ToolExecution: ...


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool[Any]] = {}

    def register(self, tool: BaseTool[Any]) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool[Any] | None:
        return self._tools.get(name)

    def definitions(self, allowed_names: set[str] | None = None) -> list[dict[str, Any]]:
        tools = self._tools.values()
        if allowed_names is not None:
            tools = (tool for tool in tools if tool.name in allowed_names)
        return [tool.model_definition() for tool in tools]

    @property
    def names(self) -> set[str]:
        return set(self._tools)
