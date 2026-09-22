from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import Field

from aicoding_agent.domain.models import StrictModel


class MemoryError(RuntimeError):
    pass


class MemorySource(StrEnum):
    USER = "user"
    AGENT_PROPOSAL = "agent_proposal"


class MemoryStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class MemoryEntry(StrictModel):
    memory_id: str = Field(min_length=32, max_length=32)
    topic: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=2000)
    source: MemorySource
    source_run_id: str | None = Field(default=None, min_length=1, max_length=128)
    status: MemoryStatus
    ttl_days: int = Field(ge=1, le=3650)
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    approved_at: datetime | None = None
    approved_by: str | None = Field(default=None, max_length=64)


class MemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.path = self.root / "memory.jsonl"
        self._lock = asyncio.Lock()

    async def load(self) -> list[MemoryEntry]:
        async with self._lock:
            return await asyncio.to_thread(self._load_sync)

    async def append(self, entry: MemoryEntry) -> None:
        async with self._lock:
            await asyncio.to_thread(self._append_sync, entry)

    def _load_sync(self) -> list[MemoryEntry]:
        if not self.path.is_file():
            return []
        latest: dict[str, MemoryEntry] = {}
        try:
            for line_number, line in enumerate(
                self.path.read_text(encoding="utf-8").splitlines(),
                start=1,
            ):
                if not line.strip():
                    continue
                entry = MemoryEntry.model_validate_json(line)
                previous = latest.get(entry.memory_id)
                if previous is not None:
                    if entry.version != previous.version or entry.updated_at < previous.updated_at:
                        raise MemoryError(f"Invalid memory update at line {line_number}")
                latest[entry.memory_id] = entry
        except (OSError, ValueError) as exc:
            if isinstance(exc, MemoryError):
                raise
            raise MemoryError(f"Could not load curated memory: {type(exc).__name__}") from exc
        return list(latest.values())

    def _append_sync(self, entry: MemoryEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(entry.model_dump_json())
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


class MemoryManager:
    def __init__(
        self,
        *,
        store: MemoryStore | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self._now = now or (lambda: datetime.now(UTC))
        self._entries: dict[str, MemoryEntry] = {}
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            if self._initialized:
                return
            entries = await self.store.load() if self.store is not None else []
            self._entries = {entry.memory_id: entry for entry in entries}
            self._initialized = True

    async def propose(
        self,
        *,
        topic: str,
        content: str,
        ttl_days: int,
        source_run_id: str,
    ) -> MemoryEntry:
        return await self._create(
            topic=topic,
            content=content,
            ttl_days=ttl_days,
            source=MemorySource.AGENT_PROPOSAL,
            status=MemoryStatus.PENDING,
            source_run_id=source_run_id,
        )

    async def add_user_memory(
        self,
        *,
        topic: str,
        content: str,
        ttl_days: int,
    ) -> MemoryEntry:
        return await self._create(
            topic=topic,
            content=content,
            ttl_days=ttl_days,
            source=MemorySource.USER,
            status=MemoryStatus.APPROVED,
            source_run_id=None,
        )

    async def approve(self, memory_id_prefix: str) -> MemoryEntry:
        return await self._resolve(memory_id_prefix, MemoryStatus.APPROVED)

    async def reject(self, memory_id_prefix: str) -> MemoryEntry:
        return await self._resolve(memory_id_prefix, MemoryStatus.REJECTED)

    async def recall(self, query: str, *, limit: int = 5) -> list[MemoryEntry]:
        await self.initialize()
        normalized_query = _normalize(query)
        if not normalized_query:
            raise MemoryError("Memory query cannot be empty")
        async with self._lock:
            entries = tuple(self._entries.values())
        now = self._utc_now()
        active_by_topic: dict[str, MemoryEntry] = {}
        for entry in entries:
            if entry.status != MemoryStatus.APPROVED or entry.expires_at <= now:
                continue
            topic_key = _normalize(entry.topic)
            previous = active_by_topic.get(topic_key)
            if previous is None or entry.version > previous.version:
                active_by_topic[topic_key] = entry
        query_terms = set(normalized_query.split())

        def score(entry: MemoryEntry) -> tuple[int, int, datetime]:
            topic = _normalize(entry.topic)
            content = _normalize(entry.content)
            exact = 10 if normalized_query in topic or normalized_query in content else 0
            overlap = sum(term in topic or term in content for term in query_terms)
            return exact, overlap, entry.updated_at

        matches = [entry for entry in active_by_topic.values() if score(entry)[:2] != (0, 0)]
        return sorted(matches, key=score, reverse=True)[: max(1, min(limit, 10))]

    def render(self) -> str:
        if not self._entries:
            return "<no curated memory>"
        now = self._utc_now()
        lines: list[str] = []
        for entry in sorted(
            self._entries.values(),
            key=lambda item: (item.topic.casefold(), item.version, item.updated_at),
        ):
            status = "expired" if entry.expires_at <= now else entry.status.value
            preview = " ".join(entry.content.split())[:160]
            lines.append(
                f"[{entry.memory_id[:8]}] {entry.topic} v{entry.version} "
                f"{status} source={entry.source.value} expires={entry.expires_at.date()}: {preview}"
            )
        return "\n".join(lines)

    @property
    def entries(self) -> tuple[MemoryEntry, ...]:
        return tuple(self._entries.values())

    async def _create(
        self,
        *,
        topic: str,
        content: str,
        ttl_days: int,
        source: MemorySource,
        status: MemoryStatus,
        source_run_id: str | None,
    ) -> MemoryEntry:
        await self.initialize()
        topic = _validate_text(topic, field="topic", max_length=100)
        content = _validate_text(content, field="content", max_length=2000)
        if not 1 <= ttl_days <= 3650:
            raise MemoryError("Memory TTL must be between 1 and 3650 days")
        _reject_secrets(topic)
        _reject_secrets(content)
        async with self._lock:
            version = 1 + max(
                (
                    entry.version
                    for entry in self._entries.values()
                    if _normalize(entry.topic) == _normalize(topic)
                ),
                default=0,
            )
            now = self._utc_now()
            entry = MemoryEntry(
                memory_id=uuid4().hex,
                topic=topic,
                version=version,
                content=content,
                source=source,
                source_run_id=source_run_id,
                status=status,
                ttl_days=ttl_days,
                created_at=now,
                updated_at=now,
                expires_at=now + timedelta(days=ttl_days),
                approved_at=now if status == MemoryStatus.APPROVED else None,
                approved_by="user" if status == MemoryStatus.APPROVED else None,
            )
            if self.store is not None:
                await self.store.append(entry)
            self._entries[entry.memory_id] = entry
            return entry

    async def _resolve(
        self,
        memory_id_prefix: str,
        status: MemoryStatus,
    ) -> MemoryEntry:
        await self.initialize()
        prefix = memory_id_prefix.strip().lower()
        if len(prefix) < 6 or not re.fullmatch(r"[0-9a-f]+", prefix):
            raise MemoryError("Memory ID prefix must contain at least 6 hexadecimal characters")
        async with self._lock:
            matches = [
                entry for memory_id, entry in self._entries.items() if memory_id.startswith(prefix)
            ]
            if len(matches) != 1:
                raise MemoryError("Memory ID prefix is missing or ambiguous")
            current = matches[0]
            if current.status != MemoryStatus.PENDING:
                raise MemoryError("Only pending Agent proposals can be approved or rejected")
            now = self._utc_now()
            updated = current.model_copy(
                update={
                    "status": status,
                    "updated_at": now,
                    "approved_at": now if status == MemoryStatus.APPROVED else None,
                    "approved_by": "user" if status == MemoryStatus.APPROVED else None,
                }
            )
            if self.store is not None:
                await self.store.append(updated)
            self._entries[updated.memory_id] = updated
            return updated

    def _utc_now(self) -> datetime:
        value = self._now()
        return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:sk|ak)-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:ghp|github_pat|glpat)-[A-Za-z0-9_-]{12,}\b", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_ -]?key|access[_ -]?token|password|secret)\b\s*[:=]\s*\S{6,}",
        re.IGNORECASE,
    ),
)


def _reject_secrets(value: str) -> None:
    if any(pattern.search(value) for pattern in SECRET_PATTERNS):
        raise MemoryError("Curated memory rejected content that looks like a secret")


def _validate_text(value: str, *, field: str, max_length: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise MemoryError(f"Memory {field} cannot be empty")
    if len(normalized) > max_length:
        raise MemoryError(f"Memory {field} exceeds {max_length} characters")
    return normalized


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())
