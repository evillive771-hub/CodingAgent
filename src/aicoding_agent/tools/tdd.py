from __future__ import annotations

import re
from pathlib import PurePosixPath

from pydantic import Field, field_validator, model_validator

from aicoding_agent.domain.models import ToolRisk
from aicoding_agent.tools.base import BaseTool, ToolContext, ToolError, ToolExecution, ToolInput


class ProposalFile(ToolInput):
    path: str = Field(min_length=1, max_length=240)
    content: str = Field(min_length=1, max_length=100000)

    @field_validator("path")
    @classmethod
    def test_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            "\\" in value
            or ":" in value
            or path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or path.parts[0] != "tests"
            or path.suffix not in {".py", ".js", ".ts", ".cjs", ".mjs", ".tsx", ".jsx"}
            or path.name == "conftest.py"
            or ".config." in path.name
            or not (path.name.startswith("test_") or ".test." in path.name or ".spec." in path.name)
        ):
            raise ValueError("Proposal paths must be test files under tests/, not configuration")
        return str(path)

    @field_validator("content")
    @classmethod
    def enabled_tests(cls, value: str) -> str:
        if re.search(
            r"\b(?:test|it|describe)\s*\.\s*(?:skip|only|fixme|fail)\b|pytest\.(?:skip|xfail)|pytest\.mark\.(?:skip|skipif|xfail)",
            value,
        ):
            raise ValueError("Test proposals cannot skip, focus, or disable assertions")
        return value


class TddProposal(ToolInput):
    files: list[ProposalFile] = Field(default_factory=list, max_length=20)
    red_cases: dict[str, list[str]] = Field(min_length=1, max_length=100)
    requirement_links: dict[str, int] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def bounded_proposal(self) -> TddProposal:
        if sum(len(item.content.encode("utf-8")) for item in self.files) > 200000:
            raise ValueError("Test proposal exceeds 200000 bytes")
        if len({item.path.casefold() for item in self.files}) != len(self.files):
            raise ValueError("Duplicate proposal test path")
        cases = [case for values in self.red_cases.values() for case in values]
        if not cases or any(not case.strip() or len(case) > 1000 for case in cases):
            raise ValueError("RED case names must be nonempty and bounded")
        if len(cases) > 200 or any(not key or len(key) > 256 for key in self.red_cases):
            raise ValueError("Too many RED cases or invalid suite ID")
        if any(value < 0 for value in self.requirement_links.values()):
            raise ValueError("Requirement indices cannot be negative")
        if not set(cases).issubset(self.requirement_links):
            raise ValueError("Every RED case requires an acceptance criterion link")
        return self


class SubmitTestProposalTool(BaseTool[TddProposal]):
    name = "submit_test_proposal"
    description = (
        "Submit test drafts and RED case IDs for host review; never changes business files."
    )
    input_model = TddProposal
    risk = ToolRisk.WRITE
    concurrency_safe = True

    async def run(self, arguments: TddProposal, context: ToolContext) -> ToolExecution:
        if not context.tdd_proposal_mode:
            raise ToolError("Test submission is only available in the host proposal stage")
        if any(
            index >= len(context.contract.acceptance_criteria)
            for index in arguments.requirement_links.values()
        ):
            raise ToolError("Unknown acceptance criterion index")
        if context.state.get("test_proposal") is not None:
            raise ToolError("A test proposal has already been submitted")
        context.state["test_proposal"] = arguments.model_dump(mode="json")
        return ToolExecution(
            output="Test proposal submitted for host validation and RED verification.",
            data={"proposal": context.state["test_proposal"]},
        )
