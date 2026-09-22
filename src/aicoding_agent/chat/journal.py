from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import Field

from aicoding_agent.chat.store import validate_session_id
from aicoding_agent.domain.models import AgentResult, StrictModel
from aicoding_agent.persistence.base import validate_run_id


class SessionJournalError(RuntimeError):
    pass


class JournalEventType(StrEnum):
    RUN_STARTED = "run_started"
    MESSAGE = "message"
    RUN_FINISHED = "run_finished"


class SessionJournalEntry(StrictModel):
    sequence: int = Field(ge=1)
    event_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    previous_event_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    session_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=128)
    event_type: JournalEventType
    payload: dict[str, Any]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RecoverableRun(StrictModel):
    session_id: str
    run_id: str
    turn_number: int = Field(ge=1)
    objective: str = Field(min_length=1, max_length=20_000)
    messages: list[dict[str, Any]]
    unresolved_tool_call_ids: list[str] = Field(default_factory=list)
    safe_to_resume: bool


class SessionMessageJournal:
    """Append-only message-level log used to recover an interrupted Agent run."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._locks: dict[str, asyncio.Lock] = {}

    async def load(self, session_id: str) -> list[SessionJournalEntry]:
        session_id = validate_session_id(session_id)
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            return await asyncio.to_thread(self._load_sync, self._path(session_id))

    async def start_run(
        self,
        session_id: str,
        *,
        run_id: str,
        turn_number: int,
        objective: str,
    ) -> SessionJournalEntry:
        created = await self._append_payloads(
            session_id,
            run_id=run_id,
            entries=[
                (
                    JournalEventType.RUN_STARTED,
                    {"turn_number": turn_number, "objective": objective},
                )
            ],
        )
        return created[0]

    async def append_messages(
        self,
        session_id: str,
        *,
        run_id: str,
        messages: list[dict[str, Any]],
        api_round_id: str,
    ) -> list[SessionJournalEntry]:
        if not messages:
            return []
        return await self._append_payloads(
            session_id,
            run_id=run_id,
            entries=[
                (
                    JournalEventType.MESSAGE,
                    {"api_round_id": api_round_id, "message": message},
                )
                for message in messages
            ],
        )

    async def finish_run(
        self,
        session_id: str,
        *,
        run_id: str,
        result: AgentResult,
    ) -> SessionJournalEntry:
        created = await self._append_payloads(
            session_id,
            run_id=run_id,
            entries=[
                (
                    JournalEventType.RUN_FINISHED,
                    {"result": result.model_dump(mode="json")},
                )
            ],
        )
        return created[0]

    async def latest_recoverable(self, session_id: str) -> RecoverableRun | None:
        session_id = validate_session_id(session_id)
        entries = await self.load(session_id)
        started: dict[str, SessionJournalEntry] = {}
        finished: set[str] = set()
        for entry in entries:
            if entry.event_type == JournalEventType.RUN_STARTED:
                started[entry.run_id] = entry
            elif entry.event_type == JournalEventType.RUN_FINISHED:
                finished.add(entry.run_id)
        incomplete = [entry for run_id, entry in started.items() if run_id not in finished]
        if not incomplete:
            return None
        selected = max(incomplete, key=lambda item: item.sequence)
        payload = selected.payload
        messages = [
            entry.payload["message"]
            for entry in entries
            if entry.run_id == selected.run_id
            and entry.event_type == JournalEventType.MESSAGE
            and isinstance(entry.payload.get("message"), dict)
        ]
        sanitized, unresolved = sanitize_recovered_messages(messages)
        return RecoverableRun(
            session_id=session_id,
            run_id=selected.run_id,
            turn_number=int(payload["turn_number"]),
            objective=str(payload["objective"]),
            messages=sanitized,
            unresolved_tool_call_ids=sorted(unresolved),
            safe_to_resume=not unresolved,
        )

    async def _append_payloads(
        self,
        session_id: str,
        *,
        run_id: str,
        entries: list[tuple[JournalEventType, dict[str, Any]]],
    ) -> list[SessionJournalEntry]:
        session_id = validate_session_id(session_id)
        run_id = validate_run_id(run_id)
        path = self._path(session_id)
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            existing = await asyncio.to_thread(self._load_sync, path)
            sequence = len(existing)
            previous_event_id = existing[-1].event_id if existing else None
            created: list[SessionJournalEntry] = []
            for event_type, payload in entries:
                sequence += 1
                entry = SessionJournalEntry(
                    sequence=sequence,
                    event_id=uuid4().hex,
                    previous_event_id=previous_event_id,
                    session_id=session_id,
                    run_id=run_id,
                    event_type=event_type,
                    payload=payload,
                )
                created.append(entry)
                previous_event_id = entry.event_id
            await asyncio.to_thread(self._append_sync, path, created)
            return created

    def _path(self, session_id: str) -> Path:
        return self.root / session_id / "message-journal.jsonl"

    @staticmethod
    def _load_sync(path: Path) -> list[SessionJournalEntry]:
        if not path.is_file():
            return []
        entries: list[SessionJournalEntry] = []
        previous_event_id: str | None = None
        try:
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                entry = SessionJournalEntry.model_validate_json(line)
                if entry.sequence != len(entries) + 1:
                    raise SessionJournalError(f"Invalid journal sequence at line {line_number}")
                if entry.previous_event_id != previous_event_id:
                    raise SessionJournalError(f"Broken journal chain at line {line_number}")
                entries.append(entry)
                previous_event_id = entry.event_id
        except (OSError, ValueError) as exc:
            if isinstance(exc, SessionJournalError):
                raise
            raise SessionJournalError(
                f"Could not load message journal: {type(exc).__name__}"
            ) from exc
        return entries

    @staticmethod
    def _append_sync(path: Path, entries: list[SessionJournalEntry]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            for entry in entries:
                handle.write(entry.model_dump_json())
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


def sanitize_recovered_messages(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Remove malformed fragments and report ambiguous, unresolved tool calls."""
    tool_call_ids: set[str] = set()
    tool_result_ids: set[str] = set()
    filtered: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role not in {"system", "user", "assistant", "tool"}:
            continue
        if role == "assistant":
            calls = message.get("tool_calls") or []
            content = message.get("content")
            reasoning = message.get("reasoning_content")
            if not calls and not str(content or "").strip():
                # Thinking-only and blank assistant fragments are unsafe API history.
                if reasoning is not None or content is None or not str(content).strip():
                    continue
            for call in calls:
                if isinstance(call, dict) and isinstance(call.get("id"), str):
                    tool_call_ids.add(call["id"])
        elif role == "tool":
            tool_call_id = message.get("tool_call_id")
            if not isinstance(tool_call_id, str):
                continue
            tool_result_ids.add(tool_call_id)
        filtered.append(message)

    known_calls: set[str] = set()
    without_orphans: list[dict[str, Any]] = []
    for message in filtered:
        if message.get("role") == "assistant":
            known_calls.update(
                call["id"]
                for call in message.get("tool_calls") or []
                if isinstance(call, dict) and isinstance(call.get("id"), str)
            )
        if message.get("role") == "tool" and message.get("tool_call_id") not in known_calls:
            continue
        without_orphans.append(message)

    unresolved = tool_call_ids - tool_result_ids
    if without_orphans and without_orphans[-1].get("role") == "tool" and not unresolved:
        without_orphans.append(
            {
                "role": "user",
                "content": (
                    "<resume_after_interrupt>Continue from the completed tool result. "
                    "Do not repeat completed side effects.</resume_after_interrupt>"
                ),
            }
        )
    return without_orphans, unresolved


def close_ambiguous_tool_calls(
    messages: list[dict[str, Any]], unresolved_tool_call_ids: list[str]
) -> list[dict[str, Any]]:
    """Create protocol-paired unknown-status results without replaying a tool."""
    closed = list(messages)
    for tool_call_id in unresolved_tool_call_ids:
        closed.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": json.dumps(
                    {
                        "ok": False,
                        "output": (
                            "Execution status is unknown after host interruption. The host did "
                            "not replay this tool. Inspect current state before any new mutation."
                        ),
                        "data": {"recovery_status": "ambiguous_not_replayed"},
                        "policy_denied": False,
                    },
                    ensure_ascii=False,
                ),
            }
        )
    closed.append(
        {
            "role": "user",
            "content": (
                "<ambiguous_resume>One or more tool calls had unknown completion state. They were "
                "not replayed. Inspect current files and external state before continuing."
                "</ambiguous_resume>"
            ),
        }
    )
    return closed
