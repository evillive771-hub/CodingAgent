"""Trusted opt-in host configuration, loaded outside model tool arguments."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pydantic import Field

from aicoding_agent.adapters.browser_tests import BrowserTestRunner
from aicoding_agent.domain.models import StrictModel
from aicoding_agent.regression.controller import RegressionController
from aicoding_agent.regression.gate import RegressionGate


class RegressionConfig(StrictModel):
    command_suites: Path | None = None
    infra_retries: int = Field(default=1, ge=0, le=2)
    require_both: bool = True
    require_tdd: bool = False


async def build_controller(
    config_path: Path, runtime_root: Path, browser_runner: BrowserTestRunner | None = None,
) -> RegressionController:
    config_path = config_path.resolve()
    payload = await asyncio.to_thread(config_path.read_text, encoding="utf-8")
    config = RegressionConfig.model_validate(json.loads(payload))
    root = runtime_root.resolve() / "regression"
    if config.command_suites is None:
        gate = RegressionGate(root / "evidence", browser_runner, require_both=config.require_both)
    else:
        gate = await RegressionGate.from_config(
            (config_path.parent / config.command_suites).resolve(),
            root / "evidence", browser_runner, require_both=config.require_both,
        )
    return RegressionController(root, gate, infra_retries=config.infra_retries,
                                require_tdd=config.require_tdd)
