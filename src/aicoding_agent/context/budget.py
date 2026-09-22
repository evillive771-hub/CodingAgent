from __future__ import annotations

import json
import math
from typing import Any

from pydantic import Field, model_validator

from aicoding_agent.domain.models import StrictModel


class ContextBudgetConfig(StrictModel):
    """Model-aware context limits used by automatic compaction."""

    context_window_tokens: int = Field(default=131_072, ge=8_192, le=4_000_000)
    max_output_tokens: int = Field(default=8_192, ge=256, le=131_072)
    summary_output_tokens: int = Field(default=20_000, ge=256, le=32_768)
    compact_buffer_tokens: int = Field(default=13_000, ge=512, le=200_000)
    warning_buffer_tokens: int = Field(default=20_000, ge=1_024, le=300_000)

    @model_validator(mode="after")
    def thresholds_must_fit_window(self) -> ContextBudgetConfig:
        if self.max_output_tokens >= self.context_window_tokens:
            raise ValueError("max_output_tokens must be smaller than the context window")
        if self.compact_threshold_tokens <= 0:
            raise ValueError("compact buffer leaves no usable context")
        if self.warning_threshold_tokens <= 0:
            raise ValueError("warning buffer leaves no usable context")
        if self.warning_buffer_tokens <= self.compact_buffer_tokens:
            raise ValueError("warning buffer must be larger than compact buffer")
        return self

    @property
    def reserved_output_tokens(self) -> int:
        return min(self.max_output_tokens, self.summary_output_tokens)

    @property
    def effective_context_tokens(self) -> int:
        return self.context_window_tokens - self.reserved_output_tokens

    @property
    def compact_threshold_tokens(self) -> int:
        return self.effective_context_tokens - self.compact_buffer_tokens

    @property
    def warning_threshold_tokens(self) -> int:
        return self.effective_context_tokens - self.warning_buffer_tokens


class ContextBudgetStatus(StrictModel):
    estimated_tokens: int = Field(ge=0)
    context_window_tokens: int = Field(ge=1)
    effective_context_tokens: int = Field(ge=1)
    warning_threshold_tokens: int = Field(ge=1)
    compact_threshold_tokens: int = Field(ge=1)
    last_prompt_tokens: int | None = Field(default=None, ge=0)
    warning: bool
    should_compact: bool


class ContextBudgetManager:
    def __init__(self, config: ContextBudgetConfig | None = None) -> None:
        self.config = config or ContextBudgetConfig()
        self._last_prompt_tokens: int | None = None

    def estimate_messages(self, messages: list[dict[str, Any]]) -> int:
        return estimate_message_tokens(messages)

    def observe_usage(self, usage: dict[str, int | float]) -> None:
        for key in ("prompt_tokens", "input_tokens"):
            value = usage.get(key)
            if isinstance(value, (int, float)) and value >= 0:
                self._last_prompt_tokens = int(value)
                return

    def status(self, messages: list[dict[str, Any]]) -> ContextBudgetStatus:
        estimated = self.estimate_messages(messages)
        config = self.config
        return ContextBudgetStatus(
            estimated_tokens=estimated,
            context_window_tokens=config.context_window_tokens,
            effective_context_tokens=config.effective_context_tokens,
            warning_threshold_tokens=config.warning_threshold_tokens,
            compact_threshold_tokens=config.compact_threshold_tokens,
            last_prompt_tokens=self._last_prompt_tokens,
            warning=estimated >= config.warning_threshold_tokens,
            should_compact=estimated >= config.compact_threshold_tokens,
        )


def estimate_message_tokens(messages: list[dict[str, Any]]) -> int:
    """Conservative tokenizer-free estimate suitable for provider-independent guards."""
    serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"), default=str)
    cjk = sum(_is_cjk(character) for character in serialized)
    non_cjk = len(serialized) - cjk
    structural_overhead = max(1, len(messages)) * 4
    return cjk + math.ceil(non_cjk / 4) + structural_overhead


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )
