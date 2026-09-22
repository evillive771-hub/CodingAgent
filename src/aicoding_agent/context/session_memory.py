from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import Field, model_validator

from aicoding_agent.context.budget import ContextBudgetManager, estimate_message_tokens
from aicoding_agent.context.builder import message_groups
from aicoding_agent.context.history import HistoryStore
from aicoding_agent.domain.models import StrictModel
from aicoding_agent.model.base import ChatModel, ModelError, ModelPromptTooLongError
from aicoding_agent.persistence.base import validate_run_id


class SessionMemoryError(RuntimeError):
    pass


class SessionMemoryStrategy(StrEnum):
    ROLLOVER = "rollover"
    FULL = "full"
    INCREMENTAL = "incremental"  # Legacy persisted-record compatibility only.


class SessionMemorySnapshot(StrictModel):
    """A small task checkpoint and index into an immutable history window."""

    version: int = Field(ge=1)
    scope_id: str = Field(min_length=1, max_length=128)
    strategy: SessionMemoryStrategy
    summary: str = Field(min_length=1, max_length=60_000)
    covered_message_count: int = Field(ge=1)
    source_start_index: int = Field(default=0, ge=0)
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_tokens: int = Field(ge=0)
    summary_tokens: int = Field(ge=0)
    history_window_id: str | None = Field(default=None, pattern=r"^W[0-9]{6}$")
    history_item_refs: list[str] = Field(default_factory=list, max_length=1_000)
    focus: str = Field(default="", max_length=2_000)
    lossy_fallback: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def notes(self) -> str:
        return self.summary


class SessionMemoryStore:
    """Append-only Notes checkpoints; raw messages live in the sibling HistoryStore."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._locks: dict[str, asyncio.Lock] = {}

    async def load(self, scope_id: str) -> list[SessionMemorySnapshot]:
        scope_id = validate_run_id(scope_id)
        lock = self._locks.setdefault(scope_id, asyncio.Lock())
        async with lock:
            return await asyncio.to_thread(self._load_sync, self._path(scope_id))

    async def append(self, snapshot: SessionMemorySnapshot) -> Path:
        scope_id = validate_run_id(snapshot.scope_id)
        path = self._path(scope_id)
        lock = self._locks.setdefault(scope_id, asyncio.Lock())
        async with lock:
            existing = await asyncio.to_thread(self._load_sync, path)
            expected = len(existing) + 1
            if snapshot.version != expected:
                raise SessionMemoryError(
                    f"Expected session memory version {expected}, received {snapshot.version}"
                )
            if existing and snapshot.covered_message_count < existing[-1].covered_message_count:
                raise SessionMemoryError("Session memory coverage cannot move backwards")
            await asyncio.to_thread(self._append_sync, path, snapshot)
        return path

    def _path(self, scope_id: str) -> Path:
        return self.root / scope_id / "session-memory.jsonl"

    @staticmethod
    def _load_sync(path: Path) -> list[SessionMemorySnapshot]:
        if not path.is_file():
            return []
        snapshots: list[SessionMemorySnapshot] = []
        try:
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                snapshot = SessionMemorySnapshot.model_validate_json(line)
                if snapshot.version != len(snapshots) + 1:
                    raise SessionMemoryError(
                        f"Invalid session memory sequence at line {line_number}"
                    )
                snapshots.append(snapshot)
        except (OSError, ValueError) as exc:
            if isinstance(exc, SessionMemoryError):
                raise
            raise SessionMemoryError(
                f"Could not load session memory: {type(exc).__name__}"
            ) from exc
        return snapshots

    @staticmethod
    def _append_sync(path: Path, snapshot: SessionMemorySnapshot) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(snapshot.model_dump_json())
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


class SessionSummarizer(Protocol):
    async def summarize(
        self,
        messages: list[dict[str, Any]],
        *,
        previous_summary: str | None,
        focus: str,
        strategy: SessionMemoryStrategy,
    ) -> str: ...


class ModelSessionCheckpointer:
    """Isolated, no-tools checkpoint writer used before a context rollover."""

    def __init__(self, model: ChatModel) -> None:
        self.model = model

    async def checkpoint(
        self,
        messages: list[dict[str, Any]],
        *,
        previous_notes: str | None,
        history_refs: list[str],
        focus: str,
    ) -> str:
        payload = json.dumps(messages, ensure_ascii=False, separators=(",", ":"), default=str)
        prompt = (
            "Write a compact checkpoint for a coding-agent context rollover. Use exactly these "
            "Markdown headings: Goal, Decisions, Progress, Current Problem, Important History, "
            "Next. Keep only current working state. Under Important History, cite supplied "
            "window:item references where exact evidence may be needed. Never invent results.\n"
            f"User focus: {focus or '<none>'}\n"
            f"Previous checkpoint (state only):\n{previous_notes or '<none>'}\n"
            f"Available original-history references: {', '.join(history_refs)}\n"
            f"Messages moving out of the current window:\n{payload}"
        )
        response = await self.model.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "You are an isolated checkpoint writer with no tools. Return only factual "
                        "Notes; original history remains separately readable."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            [],
        )
        notes = response.content.strip()
        if not notes:
            raise SessionMemoryError("The checkpoint model returned empty content")
        return notes[:60_000]

    async def summarize(
        self,
        messages: list[dict[str, Any]],
        *,
        previous_summary: str | None,
        focus: str,
        strategy: SessionMemoryStrategy,
    ) -> str:
        del strategy
        return await self.checkpoint(
            messages,
            previous_notes=previous_summary,
            history_refs=[],
            focus=focus,
        )


# Public compatibility alias for callers built against ADR-0021's earlier draft.
ModelSessionSummarizer = ModelSessionCheckpointer


class SessionMemoryManager:
    def __init__(
        self,
        *,
        scope_id: str,
        summarizer: SessionSummarizer,
        store: SessionMemoryStore | None = None,
        history_store: HistoryStore | None = None,
        max_consecutive_failures: int = 3,
    ) -> None:
        self.scope_id = validate_run_id(scope_id)
        self.summarizer = summarizer
        self.store = store
        self.history_store = history_store or (HistoryStore(store.root) if store else None)
        self.max_consecutive_failures = max_consecutive_failures
        self.history: list[SessionMemorySnapshot] = []
        self.consecutive_failures = 0
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            if self._initialized:
                return
            self.history = await self.store.load(self.scope_id) if self.store else []
            self._initialized = True

    @property
    def current(self) -> SessionMemorySnapshot | None:
        return self.history[-1] if self.history else None

    async def compact_prefix(
        self,
        messages: list[dict[str, Any]],
        *,
        covered_message_count: int,
        focus: str = "",
        force_full: bool = False,
        history_messages: list[dict[str, Any]] | None = None,
    ) -> SessionMemorySnapshot:
        """Rotate a prefix into History and persist Notes pointing to exact items."""
        await self.initialize()
        focus = focus.strip()
        if len(focus) > 2_000:
            raise ValueError("Session memory focus exceeds 2000 characters")
        if self.consecutive_failures >= self.max_consecutive_failures:
            raise SessionMemoryError("Session memory circuit breaker is open")
        if not 1 <= covered_message_count <= len(messages):
            raise ValueError("covered_message_count must select a non-empty message prefix")
        raw_messages = messages if history_messages is None else history_messages
        if len(raw_messages) != len(messages):
            raise ValueError("history_messages must align one-to-one with messages")

        async with self._lock:
            previous = self.current
            prefix = raw_messages[:covered_message_count]
            source_digest = _messages_digest(prefix)
            continuous = (
                previous is not None
                and previous.covered_message_count < covered_message_count
                and previous.source_digest
                == _messages_digest(raw_messages[: previous.covered_message_count])
            )
            if continuous and not force_full:
                source_start = previous.covered_message_count
                strategy = SessionMemoryStrategy.ROLLOVER
                previous_notes = previous.notes
            else:
                source_start = 0
                strategy = SessionMemoryStrategy.FULL
                previous_notes = None
            source = messages[source_start:covered_message_count]
            raw_source = raw_messages[source_start:covered_message_count]
            try:
                window_id, refs = await self._archive(raw_source, source_start=source_start)
                notes, dropped = await self._checkpoint_with_ptl_fallback(
                    source,
                    previous_notes=previous_notes,
                    history_refs=refs,
                    focus=focus,
                    strategy=strategy,
                )
            except (ModelError, SessionMemoryError, ValueError) as first_error:
                if strategy == SessionMemoryStrategy.FULL:
                    self.consecutive_failures += 1
                    raise SessionMemoryError(
                        f"Full session memory rollover failed: {first_error}"
                    ) from first_error
                try:
                    source_start = 0
                    source = messages[:covered_message_count]
                    raw_source = raw_messages[:covered_message_count]
                    window_id, refs = await self._archive(raw_source, source_start=0)
                    notes, dropped = await self._checkpoint_with_ptl_fallback(
                        source,
                        previous_notes=None,
                        history_refs=refs,
                        focus=focus,
                        strategy=SessionMemoryStrategy.FULL,
                    )
                    strategy = SessionMemoryStrategy.FULL
                except (ModelError, SessionMemoryError, ValueError) as fallback_error:
                    self.consecutive_failures += 1
                    raise SessionMemoryError(
                        f"Context rollover and full checkpoint failed: {fallback_error}"
                    ) from fallback_error

            redacted_notes = _redact_session_secrets(notes)
            snapshot = SessionMemorySnapshot(
                version=len(self.history) + 1,
                scope_id=self.scope_id,
                strategy=strategy,
                summary=redacted_notes,
                covered_message_count=covered_message_count,
                source_start_index=source_start + dropped,
                source_digest=source_digest,
                source_tokens=estimate_message_tokens(prefix),
                summary_tokens=estimate_message_tokens(
                    [{"role": "user", "content": redacted_notes}]
                ),
                history_window_id=window_id,
                history_item_refs=refs[dropped:],
                focus=focus,
                lossy_fallback=dropped > 0,
            )
            if self.store:
                await self.store.append(snapshot)
            self.history.append(snapshot)
            self.consecutive_failures = 0
            return snapshot

    async def _archive(
        self, messages: list[dict[str, Any]], *, source_start: int
    ) -> tuple[str, list[str]]:
        if self.history_store is None:
            window_id = f"W{len(self.history) + 1:06d}"
        else:
            window = await self.history_store.archive_window(
                self.scope_id,
                messages,
                source_start_index=source_start,
            )
            window_id = window.window_id
        refs = [f"{window_id}:item-{index + 1:06d}" for index in range(len(messages))]
        return window_id, refs

    async def _checkpoint_with_ptl_fallback(
        self,
        messages: list[dict[str, Any]],
        *,
        previous_notes: str | None,
        history_refs: list[str],
        focus: str,
        strategy: SessionMemoryStrategy,
    ) -> tuple[str, int]:
        candidate = list(messages)
        candidate_refs = list(history_refs)
        dropped_total = 0
        last_error: Exception | None = None
        for _attempt in range(3):
            try:
                checkpoint = getattr(self.summarizer, "checkpoint", None)
                if checkpoint is not None:
                    notes = await checkpoint(
                        candidate,
                        previous_notes=previous_notes,
                        history_refs=candidate_refs,
                        focus=focus,
                    )
                else:
                    notes = await self.summarizer.summarize(
                        candidate,
                        previous_summary=previous_notes,
                        focus=focus,
                        strategy=strategy,
                    )
                if not notes.strip():
                    raise SessionMemoryError("The checkpoint writer returned empty content")
                return notes, dropped_total
            except (ModelError, SessionMemoryError, ValueError) as exc:
                last_error = exc
                if isinstance(exc, ModelError) and not isinstance(exc, ModelPromptTooLongError):
                    raise
                groups = message_groups(candidate)
                if len(groups) < 2:
                    break
                drop_groups = max(1, len(groups) // 5)
                dropped = sum(len(group) for group in groups[:drop_groups])
                dropped_total += dropped
                candidate = [message for group in groups[drop_groups:] for message in group]
                candidate_refs = candidate_refs[dropped:]
        raise SessionMemoryError(f"Checkpoint retry budget exhausted: {last_error}")


class ConversationCompactionConfig(StrictModel):
    min_recent_tokens: int = Field(default=10_000, ge=0, le=200_000)
    max_recent_tokens: int = Field(default=40_000, ge=256, le=500_000)
    min_recent_messages: int = Field(default=5, ge=1, le=200)
    max_recent_messages: int = Field(default=24, ge=1, le=1_000)

    @model_validator(mode="after")
    def maximums_must_cover_minimums(self) -> ConversationCompactionConfig:
        if self.max_recent_tokens < self.min_recent_tokens:
            raise ValueError("max_recent_tokens must be >= min_recent_tokens")
        if self.max_recent_messages < self.min_recent_messages:
            raise ValueError("max_recent_messages must be >= min_recent_messages")
        return self


class ConversationContext(StrictModel):
    messages: list[dict[str, Any]]
    summary: str | None = None
    snapshot_version: int | None = Field(default=None, ge=1)
    history_window_id: str | None = None
    compacted: bool = False
    strategy: SessionMemoryStrategy | None = None
    raw_tokens: int = Field(ge=0)
    context_tokens: int = Field(ge=0)
    consecutive_failures: int = Field(default=0, ge=0)
    error: str | None = Field(default=None, max_length=2_000)

    @property
    def notes(self) -> str | None:
        return self.summary


class ConversationCompactor:
    """Compatibility facade implementing context rotation, not repeated summaries."""

    def __init__(
        self,
        *,
        summarizer: SessionSummarizer,
        budget: ContextBudgetManager,
        store: SessionMemoryStore | None = None,
        history_store: HistoryStore | None = None,
        config: ConversationCompactionConfig | None = None,
        auto_enabled: bool = True,
    ) -> None:
        self.summarizer = summarizer
        self.budget = budget
        self.store = store
        self.history_store = history_store or (HistoryStore(store.root) if store else None)
        self.config = config or ConversationCompactionConfig()
        self.auto_enabled = auto_enabled
        self._managers: dict[str, SessionMemoryManager] = {}

    async def prepare(
        self,
        messages: list[dict[str, Any]],
        *,
        scope_id: str,
        force: bool = False,
        force_full: bool = False,
        focus: str = "",
        history_messages: list[dict[str, Any]] | None = None,
    ) -> ConversationContext:
        manager = self._managers.get(scope_id)
        if manager is None:
            manager = SessionMemoryManager(
                scope_id=scope_id,
                summarizer=self.summarizer,
                store=self.store,
                history_store=self.history_store,
            )
            self._managers[scope_id] = manager
        await manager.initialize()
        raw_tokens = self.budget.estimate_messages(
            messages if history_messages is None else history_messages
        )
        current = manager.current
        visible_start = current.covered_message_count if current else 0
        visible = messages[visible_start:]
        visible_tokens = self.budget.estimate_messages(visible)
        notes_tokens = current.summary_tokens if current else 0
        legacy_checkpoint = current is not None and current.history_window_id is None
        requires_full = force_full or legacy_checkpoint
        should_rollover = force or legacy_checkpoint or (
            self.auto_enabled
            and notes_tokens + visible_tokens >= self.budget.config.compact_threshold_tokens
        )
        if not should_rollover:
            return _conversation_context(current, visible, raw_tokens, visible_tokens, manager)

        covered = _select_prefix_to_compact(messages, self.config)
        if current is not None:
            covered = max(covered, current.covered_message_count)
        if covered <= 0 or (
            current is not None
            and covered == current.covered_message_count
            and not requires_full
            and not focus.strip()
        ):
            return _conversation_context(current, visible, raw_tokens, visible_tokens, manager)
        try:
            snapshot = await manager.compact_prefix(
                messages,
                covered_message_count=covered,
                focus=focus,
                force_full=requires_full,
                history_messages=history_messages,
            )
        except SessionMemoryError as exc:
            context = _conversation_context(current, visible, raw_tokens, visible_tokens, manager)
            return context.model_copy(update={"error": str(exc)})
        recent = messages[snapshot.covered_message_count :]
        return ConversationContext(
            messages=recent,
            summary=snapshot.notes,
            snapshot_version=snapshot.version,
            history_window_id=snapshot.history_window_id,
            compacted=True,
            strategy=snapshot.strategy,
            raw_tokens=raw_tokens,
            context_tokens=snapshot.summary_tokens + self.budget.estimate_messages(recent),
            consecutive_failures=manager.consecutive_failures,
        )

    async def status(self, messages: list[dict[str, Any]], *, scope_id: str) -> ConversationContext:
        return await self.prepare(messages, scope_id=scope_id)


def _conversation_context(
    current: SessionMemorySnapshot | None,
    visible: list[dict[str, Any]],
    raw_tokens: int,
    visible_tokens: int,
    manager: SessionMemoryManager,
) -> ConversationContext:
    return ConversationContext(
        messages=visible,
        summary=current.notes if current else None,
        snapshot_version=current.version if current else None,
        history_window_id=current.history_window_id if current else None,
        compacted=current is not None,
        strategy=current.strategy if current else None,
        raw_tokens=raw_tokens,
        context_tokens=(current.summary_tokens if current else 0) + visible_tokens,
        consecutive_failures=manager.consecutive_failures,
    )


def _select_prefix_to_compact(
    messages: list[dict[str, Any]], config: ConversationCompactionConfig
) -> int:
    groups = message_groups(messages)
    if len(groups) < 2:
        return 0
    kept_tokens = 0
    kept_messages = 0
    keep_from_group = len(groups)
    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        group_tokens = estimate_message_tokens(group)
        group_messages = len(group)
        satisfies_minimum = (
            kept_tokens >= config.min_recent_tokens and kept_messages >= config.min_recent_messages
        )
        exceeds_maximum = (
            kept_tokens + group_tokens > config.max_recent_tokens
            or kept_messages + group_messages > config.max_recent_messages
        )
        if satisfies_minimum and exceeds_maximum:
            break
        keep_from_group = index
        kept_tokens += group_tokens
        kept_messages += group_messages
    if keep_from_group == 0:
        keep_from_group = len(groups) - 1
    return sum(len(group) for group in groups[:keep_from_group])


def _messages_digest(messages: Sequence[dict[str, Any]]) -> str:
    payload = json.dumps(
        list(messages),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


SESSION_SECRET_PATTERNS = (
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?"
        r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:sk|ak)-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:ghp|github_pat|glpat)-[A-Za-z0-9_-]{12,}\b", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[^\s,;]+",
        re.IGNORECASE,
    ),
)


def _redact_session_secrets(notes: str) -> str:
    redacted = notes
    for pattern in SESSION_SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted
