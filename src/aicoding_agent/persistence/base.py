from __future__ import annotations

import re
from typing import Any, Protocol

RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("run_id contains unsupported characters or exceeds 128 characters")
    return run_id


class EventSink(Protocol):
    async def emit(self, run_id: str, event_type: str, data: dict[str, Any]) -> None: ...


class NullEventSink:
    async def emit(self, run_id: str, event_type: str, data: dict[str, Any]) -> None:
        return None
