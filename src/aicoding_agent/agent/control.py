from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from aicoding_agent.persistence.base import validate_run_id


class ControlIntent(StrEnum):
    STEER = "steer"
    NEXT = "next"
    INTERRUPT = "interrupt"


class ControlMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=32, max_length=32)
    sequence: int = Field(ge=1)
    intent: ControlIntent
    content: str = Field(default="", max_length=20000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ControlEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_sequence: int = Field(ge=1)
    action: str = Field(pattern=r"^(queued|consumed)$")
    message: ControlMessage
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ControlStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._locks: dict[str, asyncio.Lock] = {}

    async def load(self, scope_id: str) -> list[ControlEvent]:
        path = self._path(scope_id)
        lock = self._locks.setdefault(scope_id, asyncio.Lock())
        async with lock:
            return await asyncio.to_thread(self._load, path)

    async def append(self, scope_id: str, event: ControlEvent) -> None:
        path = self._path(scope_id)
        lock = self._locks.setdefault(scope_id, asyncio.Lock())
        async with lock:
            existing = await asyncio.to_thread(self._load, path)
            if event.event_sequence != len(existing) + 1:
                raise ValueError("Control event sequence is not append-only")
            await asyncio.to_thread(self._append, path, event.model_dump(mode="json"))

    def _path(self, scope_id: str) -> Path:
        return self.root / validate_run_id(scope_id) / "control.jsonl"

    @staticmethod
    def _load(path: Path) -> list[ControlEvent]:
        if not path.is_file():
            return []
        events: list[ControlEvent] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            event = ControlEvent.model_validate_json(line)
            if event.event_sequence != len(events) + 1:
                raise ValueError(f"Invalid control event sequence at line {line_number}")
            events.append(event)
        return events

    @staticmethod
    def _append(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


class SteeringInbox:
    """Durable control queue. Messages are consumed only at Agent safe boundaries."""

    def __init__(self, *, store: ControlStore | None = None, scope_id: str = "default") -> None:
        self.store = store
        self.scope_id = validate_run_id(scope_id)
        self._events: list[ControlEvent] = []
        self._pending: list[ControlMessage] = []
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            if self._initialized:
                return
            self._events = await self.store.load(self.scope_id) if self.store else []
            queued: dict[str, ControlMessage] = {}
            consumed: set[str] = set()
            for event in self._events:
                if event.action == "queued":
                    queued[event.message.message_id] = event.message
                else:
                    consumed.add(event.message.message_id)
            self._pending = sorted(
                (message for key, message in queued.items() if key not in consumed),
                key=lambda message: message.sequence,
            )
            self._initialized = True

    async def submit(self, intent: ControlIntent, content: str = "") -> ControlMessage:
        await self.initialize()
        normalized = content.strip()
        if intent != ControlIntent.INTERRUPT and not normalized:
            raise ValueError(f"{intent} requires a non-empty message")
        async with self._lock:
            sequence = max((item.sequence for item in self._pending), default=0)
            sequence = (
                max(
                    sequence,
                    max((event.message.sequence for event in self._events), default=0),
                )
                + 1
            )
            message = ControlMessage(
                message_id=uuid4().hex,
                sequence=sequence,
                intent=intent,
                content=normalized,
            )
            await self._record("queued", message)
            self._pending.append(message)
            return message

    async def consume_current(self) -> list[ControlMessage]:
        return await self._consume({ControlIntent.STEER, ControlIntent.INTERRUPT})

    async def consume_next(self) -> ControlMessage | None:
        messages = await self._consume({ControlIntent.NEXT}, limit=1)
        return messages[0] if messages else None

    async def _consume(
        self,
        intents: set[ControlIntent],
        *,
        limit: int | None = None,
    ) -> list[ControlMessage]:
        await self.initialize()
        async with self._lock:
            selected = [message for message in self._pending if message.intent in intents]
            if limit is not None:
                selected = selected[:limit]
            selected_ids = {message.message_id for message in selected}
            for message in selected:
                await self._record("consumed", message)
            self._pending = [
                message for message in self._pending if message.message_id not in selected_ids
            ]
            return selected

    async def _record(self, action: str, message: ControlMessage) -> None:
        event = ControlEvent(
            event_sequence=len(self._events) + 1,
            action=action,
            message=message,
        )
        if self.store:
            await self.store.append(self.scope_id, event)
        self._events.append(event)

    @property
    def pending(self) -> tuple[ControlMessage, ...]:
        return tuple(self._pending)
