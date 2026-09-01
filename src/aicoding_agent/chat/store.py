from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from pydantic import ValidationError

from aicoding_agent.chat.models import ChatTurn

SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class ChatStoreError(RuntimeError):
    pass


def validate_session_id(session_id: str) -> str:
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError("session_id must be 1-64 letters, digits, dots, underscores, or dashes")
    return session_id


class ChatTranscriptStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._locks: dict[str, asyncio.Lock] = {}

    async def load(self, session_id: str) -> list[ChatTurn]:
        path = self._path(session_id)
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            return await asyncio.to_thread(self._load, path)

    async def append(self, session_id: str, turn: ChatTurn) -> Path:
        path = self._path(session_id)
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            existing = await asyncio.to_thread(self._load, path)
            expected_turn = len(existing) + 1
            if turn.turn_number != expected_turn:
                raise ChatStoreError(
                    f"Expected chat turn {expected_turn}, received {turn.turn_number}"
                )
            await asyncio.to_thread(self._append, path, turn.model_dump(mode="json"))
            return path

    def _path(self, session_id: str) -> Path:
        validated = validate_session_id(session_id)
        return self.root / validated / "turns.jsonl"

    @staticmethod
    def _load(path: Path) -> list[ChatTurn]:
        if not path.is_file():
            return []
        turns: list[ChatTurn] = []
        try:
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                turn = ChatTurn.model_validate_json(line)
                if turn.turn_number != len(turns) + 1:
                    raise ChatStoreError(
                        f"Chat transcript sequence is invalid at line {line_number}"
                    )
                turns.append(turn)
        except (OSError, ValueError, ValidationError, json.JSONDecodeError) as exc:
            if isinstance(exc, ChatStoreError):
                raise
            raise ChatStoreError(f"Could not load chat transcript: {type(exc).__name__}") from exc
        return turns

    @staticmethod
    def _append(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
