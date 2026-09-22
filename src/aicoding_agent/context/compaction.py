from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import Field

from aicoding_agent.context.archive import ContextEvidenceArchive
from aicoding_agent.context.budget import ContextBudgetManager
from aicoding_agent.context.builder import (
    compact_messages,
    message_groups,
    micro_compact_messages,
    protected_group_indexes,
)
from aicoding_agent.context.session_memory import (
    ConversationCompactor,
    SessionMemoryStrategy,
)
from aicoding_agent.domain.models import StrictModel
from aicoding_agent.persistence.base import validate_run_id


class CompactionRecord(StrictModel):
    version: int = Field(ge=1)
    scope_id: str = Field(min_length=1, max_length=128)
    boundary_id: str | None = Field(default=None, min_length=32, max_length=32)
    strategy: SessionMemoryStrategy | None = None
    summary_version: int | None = Field(default=None, ge=1)
    history_window_id: str | None = Field(default=None, pattern=r"^W[0-9]{6}$")
    raw_tokens: int = Field(ge=0)
    archived_tokens: int = Field(default=0, ge=0)
    model_tokens: int = Field(ge=0)
    raw_messages: int = Field(ge=0)
    model_messages: int = Field(ge=0)
    compacted: bool
    archive_compacted: bool = False
    semantic_compacted: bool = False
    fallback_used: bool = False
    error: str | None = Field(default=None, max_length=2_000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CompactionStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._locks: dict[str, asyncio.Lock] = {}

    async def load(self, scope_id: str) -> list[CompactionRecord]:
        scope_id = validate_run_id(scope_id)
        lock = self._locks.setdefault(scope_id, asyncio.Lock())
        async with lock:
            return await asyncio.to_thread(self._load_sync, self._path(scope_id))

    async def append(self, record: CompactionRecord) -> Path:
        scope_id = validate_run_id(record.scope_id)
        path = self._path(scope_id)
        lock = self._locks.setdefault(scope_id, asyncio.Lock())
        async with lock:
            existing = await asyncio.to_thread(self._load_sync, path)
            if record.version != len(existing) + 1:
                raise ValueError("Compaction record version is not append-only")
            await asyncio.to_thread(self._append_sync, path, record)
        return path

    def _path(self, scope_id: str) -> Path:
        return self.root / scope_id / "compactions.jsonl"

    @staticmethod
    def _load_sync(path: Path) -> list[CompactionRecord]:
        if not path.is_file():
            return []
        records: list[CompactionRecord] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            record = CompactionRecord.model_validate_json(line)
            if record.version != len(records) + 1:
                raise ValueError(f"Invalid compaction sequence at line {line_number}")
            records.append(record)
        return records

    @staticmethod
    def _append_sync(path: Path, record: CompactionRecord) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(record.model_dump_json())
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


class PreparedAgentContext(StrictModel):
    messages: list[dict[str, Any]]
    record: CompactionRecord


class AgentContextCompactor:
    """Archive evidence, rotate old messages to History, and rebuild the working set."""

    def __init__(
        self,
        *,
        conversation_compactor: ConversationCompactor,
        budget: ContextBudgetManager,
        archive: ContextEvidenceArchive | None = None,
        store: CompactionStore | None = None,
        fallback_max_chars: int = 240_000,
    ) -> None:
        self.conversation_compactor = conversation_compactor
        self.budget = budget
        self.archive = archive
        self.store = store
        self.fallback_max_chars = fallback_max_chars
        self._versions: dict[str, int] = {}

    async def prepare(
        self,
        messages: list[dict[str, Any]],
        *,
        run_id: str,
        force: bool = False,
        focus: str = "",
    ) -> PreparedAgentContext:
        run_id = validate_run_id(run_id)
        raw_tokens = self.budget.estimate_messages(messages)
        archived = (
            await self.archive.archive_messages(messages, run_id=run_id)
            if self.archive is not None
            else list(messages)
        )
        archived_tokens = self.budget.estimate_messages(archived)
        source_messages = (
            micro_compact_messages(archived)
            if self.conversation_compactor.auto_enabled and self.budget.status(archived).warning
            else archived
        )
        context = await self.conversation_compactor.prepare(
            source_messages,
            scope_id=run_memory_scope(run_id),
            force=force,
            focus=focus,
            history_messages=messages,
        )
        fallback_used = False
        if context.summary is None:
            model_messages = context.messages
        else:
            model_messages = _rebuild_context(
                source_messages,
                recent=context.messages,
                notes=context.summary,
                boundary_id=uuid4().hex,
                notes_version=context.snapshot_version,
                history_window_id=context.history_window_id,
            )
        if context.error is not None and context.summary is None:
            fallback_used = True
            model_messages = compact_messages(
                source_messages,
                max_chars=self.fallback_max_chars,
            )

        model_tokens = self.budget.estimate_messages(model_messages)
        boundary_id = _boundary_id(model_messages)
        version = await self._next_version(run_id)
        record = CompactionRecord(
            version=version,
            scope_id=run_id,
            boundary_id=boundary_id,
            strategy=context.strategy,
            summary_version=context.snapshot_version,
            history_window_id=context.history_window_id,
            raw_tokens=raw_tokens,
            archived_tokens=archived_tokens,
            model_tokens=model_tokens,
            raw_messages=len(messages),
            model_messages=len(model_messages),
            compacted=context.compacted or model_tokens < raw_tokens,
            archive_compacted=archived_tokens < raw_tokens,
            semantic_compacted=context.compacted or model_tokens < archived_tokens,
            fallback_used=fallback_used,
            error=context.error,
        )
        if self.store is not None:
            await self.store.append(record)
        return PreparedAgentContext(messages=model_messages, record=record)

    async def _next_version(self, run_id: str) -> int:
        if run_id not in self._versions:
            existing = await self.store.load(run_id) if self.store is not None else []
            self._versions[run_id] = len(existing)
        self._versions[run_id] += 1
        return self._versions[run_id]


def _rebuild_context(
    messages: list[dict[str, Any]],
    *,
    recent: list[dict[str, Any]],
    notes: str,
    boundary_id: str,
    notes_version: int | None,
    history_window_id: str | None,
) -> list[dict[str, Any]]:
    system = [message for message in messages if message.get("role") == "system"]
    non_system = [message for message in messages if message.get("role") != "system"]
    groups = message_groups(non_system)
    protected_indexes = protected_group_indexes(groups)
    protected = [
        message
        for index, group in enumerate(groups)
        if index in protected_indexes
        for message in group
    ]
    boundary = {
        "role": "user",
        "content": (
            f'<new_context id="{boundary_id}" notes_version="{notes_version or 0}" '
            f'history_window="{history_window_id or "none"}" />'
        ),
    }
    memory = {
        "role": "user",
        "content": (
            "<notes>\n"
            "Checkpoint and History index only. Use history_search/history_read_item for exact "
            "archived evidence. Current files, contract and host evidence remain authoritative.\n"
            f"{notes}\n</notes>"
        ),
    }
    protected_signatures = {_message_signature(message) for message in protected}
    recent_without_protected = [
        message
        for message in recent
        if message.get("role") != "system"
        and _message_signature(message) not in protected_signatures
    ]
    return system + [boundary, memory] + recent_without_protected + protected


def _message_signature(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, sort_keys=True, default=str)


def _boundary_id(messages: list[dict[str, Any]]) -> str | None:
    for message in messages:
        content = str(message.get("content", ""))
        if content.startswith('<new_context id="'):
            return content.split('"', 2)[1]
    return None


def run_memory_scope(run_id: str) -> str:
    candidate = f"run-{run_id}"
    if len(candidate) <= 128:
        return candidate
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    return f"run-{digest}"
