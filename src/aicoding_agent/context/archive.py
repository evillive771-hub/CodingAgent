from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from aicoding_agent.context.builder import compact_messages
from aicoding_agent.persistence.base import validate_run_id


class ContextEvidenceArchive:
    """Persist large tool outputs before replacing them with bounded evidence references."""

    def __init__(self, root: Path, *, large_output_chars: int = 8000) -> None:
        if large_output_chars < 1000:
            raise ValueError("large_output_chars must be at least 1000")
        self.root = root.resolve()
        self.large_output_chars = large_output_chars
        self._locks: dict[str, asyncio.Lock] = {}

    async def compact(
        self,
        messages: list[dict[str, Any]],
        *,
        run_id: str,
        max_chars: int,
        recent_messages: int = 24,
    ) -> list[dict[str, Any]]:
        validate_run_id(run_id)
        copied = await self.archive_messages(messages, run_id=run_id)
        return compact_messages(
            copied,
            max_chars=max_chars,
            recent_messages=recent_messages,
        )

    async def archive_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        run_id: str,
    ) -> list[dict[str, Any]]:
        """Persist raw context and replace only oversized tool results with references."""
        await self.persist_transcript(run_id, messages)
        copied = copy.deepcopy(messages)
        for message in copied:
            if message.get("role") != "tool":
                continue
            content = message.get("content")
            if not isinstance(content, str) or len(content) <= self.large_output_chars:
                continue
            tool_call_id = str(message.get("tool_call_id", "unknown"))
            path, digest = await self.persist(run_id, tool_call_id, content)
            preview_size = min(1000, self.large_output_chars // 4)
            omitted = max(0, len(content) - preview_size * 2)
            message["content"] = (
                f"{content[:preview_size]}\n"
                f"... <{omitted} characters archived> ...\n"
                f"{content[-preview_size:]}\n"
                f'<evidence path="{path}" sha256="{digest}" />'
            )
        return copied

    async def persist_transcript(self, run_id: str, messages: list[dict[str, Any]]) -> Path:
        validate_run_id(run_id)
        target = self.root / run_id / "transcript.json"
        lock = self._locks.setdefault(str(target), asyncio.Lock())
        async with lock:
            await asyncio.to_thread(self._write_json_snapshot, target, messages)
        return target

    async def persist(self, run_id: str, tool_call_id: str, content: str) -> tuple[str, str]:
        validate_run_id(run_id)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        call_digest = hashlib.sha256(tool_call_id.encode("utf-8")).hexdigest()[:16]
        relative = Path(run_id) / "tool-results" / f"{call_digest}-{digest[:16]}.txt"
        target = self.root / relative
        lock = self._locks.setdefault(str(target), asyncio.Lock())
        async with lock:
            await asyncio.to_thread(self._write_once, target, content)
        return relative.as_posix(), digest

    @staticmethod
    def _write_once(target: Path, content: str) -> None:
        if target.is_file():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(f".{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)

    @staticmethod
    def _write_json_snapshot(target: Path, messages: list[dict[str, Any]]) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(f".{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(messages, handle, ensure_ascii=False, indent=2, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
