from __future__ import annotations

import json

from pydantic import Field

from aicoding_agent.context.history import HistoryError
from aicoding_agent.domain.models import ToolRisk
from aicoding_agent.tools.base import BaseTool, ToolContext, ToolError, ToolExecution, ToolInput


class HistoryFilterInput(ToolInput):
    window_id: str | None = Field(default=None, pattern=r"^W[0-9]{6}$")
    role: str | None = Field(default=None, max_length=32)
    tool_namespace: str | None = Field(default=None, max_length=100)
    tool_name: str | None = Field(default=None, max_length=100)
    agent_name: str | None = Field(default=None, max_length=100)
    recent_first: bool = True
    max_results: int = Field(default=20, ge=1, le=50)


class HistorySearchInput(HistoryFilterInput):
    query: str = Field(min_length=1, max_length=500)


class HistorySearchTool(BaseTool[HistorySearchInput]):
    name = "history_search"
    description = (
        "Literal substring search over immutable messages from previous context windows. "
        "Supports exact metadata filters and returns window/item references, not semantic matches."
    )
    input_model = HistorySearchInput
    risk = ToolRisk.READ
    concurrency_safe = True

    async def run(self, arguments: HistorySearchInput, context: ToolContext) -> ToolExecution:
        store, scope_id = _history_context(context)
        try:
            matches = await store.search_contents(
                scope_id,
                arguments.query,
                window_id=arguments.window_id,
                role=arguments.role,
                tool_namespace=arguments.tool_namespace,
                tool_name=arguments.tool_name,
                agent_name=arguments.agent_name,
                recent_first=arguments.recent_first,
                limit=arguments.max_results,
            )
        except HistoryError as exc:
            raise ToolError(str(exc)) from exc
        payload = [match.model_dump(mode="json") for match in matches]
        return _bounded_execution(payload, context, key="matches")


class HistoryListItemsTool(BaseTool[HistoryFilterInput]):
    name = "history_list_items"
    description = "List bounded item metadata from previous context windows without reading bodies."
    input_model = HistoryFilterInput
    risk = ToolRisk.READ
    concurrency_safe = True

    async def run(self, arguments: HistoryFilterInput, context: ToolContext) -> ToolExecution:
        store, scope_id = _history_context(context)
        try:
            items = await store.list_items(
                scope_id,
                window_id=arguments.window_id,
                role=arguments.role,
                tool_namespace=arguments.tool_namespace,
                tool_name=arguments.tool_name,
                agent_name=arguments.agent_name,
                recent_first=arguments.recent_first,
                limit=arguments.max_results,
            )
        except HistoryError as exc:
            raise ToolError(str(exc)) from exc
        payload = [
            {
                "window_id": item.window_id,
                "item_id": item.item_id,
                "role": item.role,
                "source_message_index": item.source_message_index,
                "tool_namespace": item.tool_namespace,
                "tool_name": item.tool_name,
                "agent_name": item.agent_name,
            }
            for item in items
        ]
        return _bounded_execution(payload, context, key="items")


class HistoryReadItemInput(ToolInput):
    window_id: str = Field(pattern=r"^W[0-9]{6}$")
    item_id: str = Field(pattern=r"^item-[0-9]{6}$")
    offset_chars: int = Field(default=0, ge=0, le=2_000_000)
    limit_chars: int = Field(default=20_000, ge=1, le=50_000)


class HistoryReadItemTool(BaseTool[HistoryReadItemInput]):
    name = "history_read_item"
    description = (
        "Read an exact archived history item by window_id and item_id, with bounded character "
        "paging. Use references from Notes or history_search."
    )
    input_model = HistoryReadItemInput
    risk = ToolRisk.READ
    concurrency_safe = True

    async def run(self, arguments: HistoryReadItemInput, context: ToolContext) -> ToolExecution:
        store, scope_id = _history_context(context)
        budget = context.contract.budget.max_tool_output_chars
        limit = min(arguments.limit_chars, max(1, budget - 500))
        try:
            item, content, has_more = await store.read_item(
                scope_id,
                window_id=arguments.window_id,
                item_id=arguments.item_id,
                offset_chars=arguments.offset_chars,
                limit_chars=limit,
            )
        except HistoryError as exc:
            raise ToolError(str(exc)) from exc
        payload = {
            "window_id": item.window_id,
            "item_id": item.item_id,
            "role": item.role,
            "offset_chars": arguments.offset_chars,
            "content": content,
            "has_more": has_more,
        }
        output = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        while len(output) > budget and payload["content"]:
            overflow = len(output) - budget
            payload["content"] = str(payload["content"])[: -max(1, overflow)]
            payload["has_more"] = True
            output = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return ToolExecution(output=output, data=payload)


def _history_context(context: ToolContext):
    if context.history_store is None or context.history_scope_id is None:
        raise ToolError("This Agent run has no context History store")
    return context.history_store, context.history_scope_id


def _bounded_execution(
    entries: list[dict[str, object]], context: ToolContext, *, key: str
) -> ToolExecution:
    budget = context.contract.budget.max_tool_output_chars
    kept: list[dict[str, object]] = []
    for entry in entries:
        candidate = json.dumps({key: [*kept, entry]}, ensure_ascii=False, separators=(",", ":"))
        if len(candidate) > budget:
            break
        kept.append(entry)
    payload = {"count": len(kept), key: kept}
    return ToolExecution(
        output=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        data=payload,
    )
