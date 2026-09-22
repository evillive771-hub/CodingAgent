from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import Field, model_validator

from aicoding_agent.domain.events import redact
from aicoding_agent.domain.models import StrictModel, ToolRisk
from aicoding_agent.persistence.base import validate_run_id

PermissionEmitter = Callable[[str, dict[str, Any]], Awaitable[None]]


class PermissionDecision(StrEnum):
    APPROVE_ONCE = "approve_once"
    APPROVE_SESSION = "approve_session"
    DENY = "deny"


class PermissionRequest(StrictModel):
    request_id: str = Field(min_length=32, max_length=32)
    sequence: int = Field(ge=1)
    run_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1, max_length=128)
    risk: ToolRisk
    argument_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    argument_preview: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=2000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PermissionResolution(StrictModel):
    request_id: str = Field(min_length=32, max_length=32)
    decision: PermissionDecision
    reason: str = Field(default="", max_length=2000)
    resolved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PermissionEvent(StrictModel):
    event_sequence: int = Field(ge=1)
    action: str = Field(pattern=r"^(requested|resolved)$")
    request: PermissionRequest
    resolution: PermissionResolution | None = None

    @model_validator(mode="after")
    def resolution_matches_action(self) -> PermissionEvent:
        if self.action == "requested" and self.resolution is not None:
            raise ValueError("Requested permission events cannot contain a resolution")
        if self.action == "resolved":
            if self.resolution is None:
                raise ValueError("Resolved permission events require a resolution")
            if self.resolution.request_id != self.request.request_id:
                raise ValueError("Permission resolution does not match its request")
        return self


class PermissionStore:
    """Append-only permission decisions for one interactive session."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._locks: dict[str, asyncio.Lock] = {}

    async def load(self, scope_id: str) -> list[PermissionEvent]:
        path = self._path(scope_id)
        lock = self._locks.setdefault(scope_id, asyncio.Lock())
        async with lock:
            return await asyncio.to_thread(self._load, path)

    async def append(self, scope_id: str, event: PermissionEvent) -> None:
        path = self._path(scope_id)
        lock = self._locks.setdefault(scope_id, asyncio.Lock())
        async with lock:
            existing = await asyncio.to_thread(self._load, path)
            if event.event_sequence != len(existing) + 1:
                raise ValueError("Permission event sequence is not append-only")
            await asyncio.to_thread(self._append, path, event.model_dump(mode="json"))

    def _path(self, scope_id: str) -> Path:
        return self.root / validate_run_id(scope_id) / "permissions.jsonl"

    @staticmethod
    def _load(path: Path) -> list[PermissionEvent]:
        if not path.is_file():
            return []
        events: list[PermissionEvent] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            event = PermissionEvent.model_validate_json(line)
            if event.event_sequence != len(events) + 1:
                raise ValueError(f"Invalid permission event sequence at line {line_number}")
            events.append(event)
        return events

    @staticmethod
    def _append(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


class PermissionBroker:
    """Wait for an interactive decision without changing the host policy verdict."""

    def __init__(
        self,
        *,
        store: PermissionStore | None = None,
        scope_id: str = "default",
        timeout_seconds: float = 900,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Permission timeout must be positive")
        self.store = store
        self.scope_id = validate_run_id(scope_id)
        self.timeout_seconds = timeout_seconds
        self._events: list[PermissionEvent] = []
        self._pending: dict[str, PermissionRequest] = {}
        self._waiters: dict[str, asyncio.Future[PermissionDecision]] = {}
        self._emitters: dict[str, PermissionEmitter] = {}
        self._session_approvals: set[tuple[str, str]] = set()
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            if self._initialized:
                return
            self._events = await self.store.load(self.scope_id) if self.store else []
            requested: dict[str, PermissionRequest] = {}
            resolved: set[str] = set()
            for event in self._events:
                requested[event.request.request_id] = event.request
                if event.resolution is not None:
                    resolved.add(event.request.request_id)
                    if event.resolution.decision == PermissionDecision.APPROVE_SESSION:
                        self._session_approvals.add(
                            (event.request.tool_name, event.request.argument_hash)
                        )
            stale = [request for key, request in requested.items() if key not in resolved]
            for request in stale:
                await self._record_resolution(
                    request,
                    PermissionDecision.DENY,
                    reason="Host restarted before the permission was decided",
                )
            self._initialized = True

    async def authorize(
        self,
        *,
        run_id: str,
        tool_name: str,
        risk: ToolRisk,
        arguments: dict[str, Any],
        reason: str,
        emit: PermissionEmitter | None = None,
    ) -> bool:
        await self.initialize()
        argument_hash = _argument_hash(arguments)
        async with self._lock:
            if (tool_name, argument_hash) in self._session_approvals:
                if emit is not None:
                    await emit(
                        "permission.reused",
                        {"tool_name": tool_name, "argument_hash": argument_hash},
                    )
                return True
            sequence = 1 + max(
                (event.request.sequence for event in self._events),
                default=0,
            )
            request = PermissionRequest(
                request_id=uuid4().hex,
                sequence=sequence,
                run_id=validate_run_id(run_id),
                tool_name=tool_name,
                risk=risk,
                argument_hash=argument_hash,
                argument_preview=_bounded_preview(redact(arguments)),
                reason=reason,
            )
            await self._record_request(request)
            waiter = asyncio.get_running_loop().create_future()
            self._pending[request.request_id] = request
            self._waiters[request.request_id] = waiter
            if emit is not None:
                self._emitters[request.request_id] = emit
        if emit is not None:
            await emit("permission.requested", request.model_dump(mode="json"))
        try:
            decision = await asyncio.wait_for(
                asyncio.shield(waiter),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            try:
                await self.resolve(
                    request.request_id,
                    PermissionDecision.DENY,
                    reason="Permission request timed out",
                    emit=emit,
                )
            except ValueError:
                decision = await waiter
                return decision in {
                    PermissionDecision.APPROVE_ONCE,
                    PermissionDecision.APPROVE_SESSION,
                }
            return False
        return decision in {
            PermissionDecision.APPROVE_ONCE,
            PermissionDecision.APPROVE_SESSION,
        }

    async def resolve(
        self,
        request_id: str,
        decision: PermissionDecision,
        *,
        reason: str = "",
        emit: PermissionEmitter | None = None,
    ) -> PermissionResolution:
        await self.initialize()
        async with self._lock:
            request = self._find_pending(request_id)
            resolution = await self._record_resolution(request, decision, reason=reason)
            self._pending.pop(request.request_id, None)
            waiter = self._waiters.pop(request.request_id, None)
            request_emitter = self._emitters.pop(request.request_id, None)
            if decision == PermissionDecision.APPROVE_SESSION:
                self._session_approvals.add((request.tool_name, request.argument_hash))
        selected_emitter = emit or request_emitter
        try:
            if selected_emitter is not None:
                await selected_emitter(
                    "permission.resolved",
                    {
                        "request_id": request.request_id,
                        "tool_name": request.tool_name,
                        "decision": decision,
                    },
                )
        finally:
            if waiter is not None and not waiter.done():
                waiter.set_result(decision)
        return resolution

    def _find_pending(self, request_id: str) -> PermissionRequest:
        exact = self._pending.get(request_id)
        if exact is not None:
            return exact
        matches = [request for key, request in self._pending.items() if key.startswith(request_id)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(f"Unknown pending permission request: {request_id}")
        raise ValueError(f"Permission request prefix is ambiguous: {request_id}")

    async def _record_request(self, request: PermissionRequest) -> None:
        event = PermissionEvent(
            event_sequence=len(self._events) + 1,
            action="requested",
            request=request,
        )
        if self.store is not None:
            await self.store.append(self.scope_id, event)
        self._events.append(event)

    async def _record_resolution(
        self,
        request: PermissionRequest,
        decision: PermissionDecision,
        *,
        reason: str,
    ) -> PermissionResolution:
        resolution = PermissionResolution(
            request_id=request.request_id,
            decision=decision,
            reason=reason,
        )
        event = PermissionEvent(
            event_sequence=len(self._events) + 1,
            action="resolved",
            request=request,
            resolution=resolution,
        )
        if self.store is not None:
            await self.store.append(self.scope_id, event)
        self._events.append(event)
        return resolution

    @property
    def pending(self) -> tuple[PermissionRequest, ...]:
        return tuple(sorted(self._pending.values(), key=lambda request: request.sequence))

    def render(self) -> str:
        if not self._pending:
            return "<no pending permissions>"
        return "\n".join(
            f"{request.request_id[:8]} {request.tool_name} {request.risk} "
            f"args={json.dumps(request.argument_preview, ensure_ascii=False, sort_keys=True)}"
            for request in self.pending
        )


def _argument_hash(arguments: dict[str, Any]) -> str:
    encoded = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounded_preview(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return "<nested value omitted>"
    if isinstance(value, dict):
        items = list(value.items())
        preview = {
            str(key)[:100]: _bounded_preview(item, depth=depth + 1) for key, item in items[:50]
        }
        if len(items) > 50:
            preview["<omitted>"] = f"{len(items) - 50} fields"
        return preview
    if isinstance(value, list):
        preview = [_bounded_preview(item, depth=depth + 1) for item in value[:20]]
        if len(value) > 20:
            preview.append(f"<{len(value) - 20} items omitted>")
        return preview
    if isinstance(value, str) and len(value) > 500:
        return f"{value[:250]}... <{len(value) - 500} characters omitted> ...{value[-250:]}"
    return value
