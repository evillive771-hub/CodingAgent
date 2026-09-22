from typing import TYPE_CHECKING, Any

from aicoding_agent.tools.base import BaseTool, ToolContext, ToolRegistry

if TYPE_CHECKING:
    from aicoding_agent.tools.executor import ToolExecutor


def build_builtin_registry() -> ToolRegistry:
    from aicoding_agent.tools.builtin import build_builtin_registry as build

    return build()


def __getattr__(name: str) -> Any:
    if name == "ToolExecutor":
        from aicoding_agent.tools.executor import ToolExecutor

        return ToolExecutor
    raise AttributeError(name)

__all__ = [
    "BaseTool",
    "ToolContext",
    "ToolExecutor",
    "ToolRegistry",
    "build_builtin_registry",
]
