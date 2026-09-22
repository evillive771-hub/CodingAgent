from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import Field

from aicoding_agent.context.budget import estimate_message_tokens
from aicoding_agent.domain.models import StrictModel
from aicoding_agent.persistence.base import validate_run_id


class HistoryError(RuntimeError):
    pass


class HistoryItem(StrictModel):
    scope_id: str = Field(min_length=1, max_length=128)
    window_id: str = Field(pattern=r"^W[0-9]{6}$")
    item_id: str = Field(pattern=r"^item-[0-9]{6}$")
    ordinal: int = Field(ge=1)
    source_message_index: int = Field(ge=0)
    role: str = Field(min_length=1, max_length=32)
    message: dict[str, Any]
    tool_namespace: str | None = Field(default=None, max_length=100)
    tool_name: str | None = Field(default=None, max_length=100)
    agent_name: str | None = Field(default=None, max_length=100)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def searchable_text(self) -> str:
        return json.dumps(self.message, ensure_ascii=False, sort_keys=True, default=str)


class HistoryWindow(StrictModel):
    scope_id: str = Field(min_length=1, max_length=128)
    window_id: str = Field(pattern=r"^W[0-9]{6}$")
    version: int = Field(ge=1)
    source_start_index: int = Field(ge=0)
    source_end_index: int = Field(ge=1)
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    item_count: int = Field(ge=1)
    source_tokens: int = Field(ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class HistorySearchMatch(StrictModel):
    window_id: str
    item_id: str
    role: str
    source_message_index: int
    preview: str
    match_start: int = Field(ge=0)
    tool_namespace: str | None = None
    tool_name: str | None = None
    agent_name: str | None = None


class HistoryStore:
    """Immutable context windows with lexical search and exact item reads."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._locks: dict[str, asyncio.Lock] = {}

    async def archive_window(
        self,
        scope_id: str,
        messages: list[dict[str, Any]],
        *,
        source_start_index: int,
    ) -> HistoryWindow:
        scope_id = validate_run_id(scope_id)
        if not messages:
            raise HistoryError("A history window must contain at least one message")
        digest = _messages_digest(messages)
        lock = self._locks.setdefault(scope_id, asyncio.Lock())
        async with lock:
            windows = await asyncio.to_thread(self._load_windows_sync, scope_id)
            for window in windows:
                if (
                    window.source_start_index == source_start_index
                    and window.source_end_index == source_start_index + len(messages)
                    and window.source_digest == digest
                ):
                    return window
            version = len(windows) + 1
            window_id = f"W{version:06d}"
            window = HistoryWindow(
                scope_id=scope_id,
                window_id=window_id,
                version=version,
                source_start_index=source_start_index,
                source_end_index=source_start_index + len(messages),
                source_digest=digest,
                item_count=len(messages),
                source_tokens=estimate_message_tokens(messages),
            )
            call_names: dict[str, str] = {}
            items: list[HistoryItem] = []
            for index, message in enumerate(messages):
                calls = message.get("tool_calls")
                if isinstance(calls, list):
                    for call in calls:
                        if not isinstance(call, dict) or not isinstance(call.get("id"), str):
                            continue
                        function = call.get("function")
                        if isinstance(function, dict) and isinstance(function.get("name"), str):
                            call_names[call["id"]] = function["name"]
                inferred_name = call_names.get(str(message.get("tool_call_id", "")))
                items.append(
                    _history_item(
                        window,
                        message,
                        ordinal=index + 1,
                        inferred_tool_name=inferred_name,
                    )
                )
            await asyncio.to_thread(self._write_window_sync, window, items)
            return window

    async def list_windows(self, scope_id: str) -> list[HistoryWindow]:
        scope_id = validate_run_id(scope_id)
        lock = self._locks.setdefault(scope_id, asyncio.Lock())
        async with lock:
            return await asyncio.to_thread(self._load_windows_sync, scope_id)

    async def list_items(
        self,
        scope_id: str,
        *,
        window_id: str | None = None,
        role: str | None = None,
        tool_namespace: str | None = None,
        tool_name: str | None = None,
        agent_name: str | None = None,
        recent_first: bool = True,
        limit: int = 20,
    ) -> list[HistoryItem]:
        scope_id = validate_run_id(scope_id)
        limit = max(1, min(limit, 100))
        lock = self._locks.setdefault(scope_id, asyncio.Lock())
        async with lock:
            items = await asyncio.to_thread(self._load_items_sync, scope_id, window_id)
        filtered = [
            item
            for item in items
            if (role is None or item.role == role)
            and (tool_namespace is None or item.tool_namespace == tool_namespace)
            and (tool_name is None or item.tool_name == tool_name)
            and (agent_name is None or item.agent_name == agent_name)
        ]
        filtered.sort(
            key=lambda item: (item.window_id, item.ordinal),
            reverse=recent_first,
        )
        return filtered[:limit]

    async def search_contents(
        self,
        scope_id: str,
        query: str,
        *,
        window_id: str | None = None,
        role: str | None = None,
        tool_namespace: str | None = None,
        tool_name: str | None = None,
        agent_name: str | None = None,
        recent_first: bool = True,
        limit: int = 20,
        preview_chars: int = 240,
    ) -> list[HistorySearchMatch]:
        needle = query.strip().casefold()
        if not needle:
            raise HistoryError("History search query cannot be empty")
        scope_id = validate_run_id(scope_id)
        lock = self._locks.setdefault(scope_id, asyncio.Lock())
        async with lock:
            candidates = await asyncio.to_thread(self._load_items_sync, scope_id, window_id)
        candidates = [
            item
            for item in candidates
            if (role is None or item.role == role)
            and (tool_namespace is None or item.tool_namespace == tool_namespace)
            and (tool_name is None or item.tool_name == tool_name)
            and (agent_name is None or item.agent_name == agent_name)
        ]
        candidates.sort(
            key=lambda item: (item.window_id, item.ordinal),
            reverse=recent_first,
        )
        matches: list[HistorySearchMatch] = []
        for item in candidates:
            position = item.searchable_text.casefold().find(needle)
            if position < 0:
                continue
            start = max(0, position - preview_chars // 3)
            end = min(len(item.searchable_text), start + preview_chars)
            preview = item.searchable_text[start:end]
            matches.append(
                HistorySearchMatch(
                    window_id=item.window_id,
                    item_id=item.item_id,
                    role=item.role,
                    source_message_index=item.source_message_index,
                    preview=preview,
                    match_start=position,
                    tool_namespace=item.tool_namespace,
                    tool_name=item.tool_name,
                    agent_name=item.agent_name,
                )
            )
            if len(matches) >= max(1, min(limit, 50)):
                break
        return matches

    async def read_item(
        self,
        scope_id: str,
        *,
        window_id: str,
        item_id: str,
        offset_chars: int = 0,
        limit_chars: int = 20_000,
    ) -> tuple[HistoryItem, str, bool]:
        scope_id = validate_run_id(scope_id)
        if offset_chars < 0:
            raise HistoryError("offset_chars cannot be negative")
        limit_chars = max(1, min(limit_chars, 50_000))
        lock = self._locks.setdefault(scope_id, asyncio.Lock())
        async with lock:
            items = await asyncio.to_thread(self._load_items_sync, scope_id, window_id)
        item = next((candidate for candidate in items if candidate.item_id == item_id), None)
        if item is None:
            raise HistoryError(f"History item not found: {window_id}:{item_id}")
        serialized = json.dumps(item.message, ensure_ascii=False, separators=(",", ":"))
        content = serialized[offset_chars : offset_chars + limit_chars]
        return item, content, offset_chars + len(content) < len(serialized)

    def _scope_root(self, scope_id: str) -> Path:
        return self.root / scope_id / "history"

    def _load_windows_sync(self, scope_id: str) -> list[HistoryWindow]:
        path = self._scope_root(scope_id) / "windows.jsonl"
        if not path.is_file():
            return []
        windows: list[HistoryWindow] = []
        try:
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                window = HistoryWindow.model_validate_json(line)
                if window.version != len(windows) + 1:
                    raise HistoryError(f"Invalid history window sequence at line {line_number}")
                windows.append(window)
        except (OSError, ValueError) as exc:
            if isinstance(exc, HistoryError):
                raise
            raise HistoryError(f"Could not load history windows: {type(exc).__name__}") from exc
        return windows

    def _load_items_sync(self, scope_id: str, window_id: str | None) -> list[HistoryItem]:
        windows = self._load_windows_sync(scope_id)
        selected = [
            window
            for window in windows
            if window_id is None or window.window_id == window_id
        ]
        if window_id is not None and not selected:
            raise HistoryError(f"History window not found: {window_id}")
        items: list[HistoryItem] = []
        for window in selected:
            path = self._scope_root(scope_id) / "windows" / f"{window.window_id}.jsonl"
            try:
                loaded = [
                    HistoryItem.model_validate_json(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            except (OSError, ValueError) as exc:
                raise HistoryError(
                    f"Could not load history window {window.window_id}: {type(exc).__name__}"
                ) from exc
            if len(loaded) != window.item_count or any(
                item.ordinal != index + 1 for index, item in enumerate(loaded)
            ):
                raise HistoryError(f"History window is incomplete: {window.window_id}")
            items.extend(loaded)
        return items

    def _write_window_sync(self, window: HistoryWindow, items: list[HistoryItem]) -> None:
        root = self._scope_root(window.scope_id)
        windows_root = root / "windows"
        windows_root.mkdir(parents=True, exist_ok=True)
        item_path = windows_root / f"{window.window_id}.jsonl"
        temporary = windows_root / f".{window.window_id}.{os.getpid()}.tmp"
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                for item in items:
                    handle.write(item.model_dump_json())
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, item_path)
        finally:
            temporary.unlink(missing_ok=True)
        index_path = root / "windows.jsonl"
        with index_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(window.model_dump_json())
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


def _history_item(
    window: HistoryWindow,
    message: dict[str, Any],
    *,
    ordinal: int,
    inferred_tool_name: str | None,
) -> HistoryItem:
    function = message.get("function") if isinstance(message.get("function"), dict) else {}
    tool_name = message.get("name") or function.get("name") or inferred_tool_name
    calls = message.get("tool_calls")
    if not tool_name and isinstance(calls, list) and len(calls) == 1:
        call = calls[0]
        call_function = call.get("function") if isinstance(call, dict) else None
        if isinstance(call_function, dict):
            tool_name = call_function.get("name")
    tool_namespace = message.get("tool_namespace")
    if not tool_namespace and isinstance(tool_name, str) and "." in tool_name:
        tool_namespace, tool_name = tool_name.rsplit(".", 1)
    return HistoryItem(
        scope_id=window.scope_id,
        window_id=window.window_id,
        item_id=f"item-{ordinal:06d}",
        ordinal=ordinal,
        source_message_index=window.source_start_index + ordinal - 1,
        role=str(message.get("role", "unknown")),
        message=message,
        tool_namespace=str(tool_namespace) if tool_namespace else None,
        tool_name=str(tool_name) if tool_name else None,
        agent_name=str(message.get("agent_name")) if message.get("agent_name") else None,
    )


def _messages_digest(messages: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        messages,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
