from pathlib import Path

import pytest
from pydantic import ValidationError

from aicoding_agent.agent.loop import AgentLoop
from aicoding_agent.domain.models import ModelResponse, TaskContract, ToolCall
from aicoding_agent.tools.base import ToolContext
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor
from aicoding_agent.tools.tdd import SubmitTestProposalTool, TddProposal


def proposal():
    return {
        "files": [
            {
                "path": "tests/test_counter.py",
                "content": "def test_clamp():\n    assert decrement(0) == 0\n",
            }
        ],
        "red_cases": {"unit": ["test_clamp"]},
        "requirement_links": {"test_clamp": 0},
    }


@pytest.mark.parametrize(
    "path",
    [
        "../src.py",
        "src/counter.py",
        "tests/conftest.py",
        "tests/playwright.config.ts",
        "C:/tests/test.py",
    ],
)
def test_proposal_cannot_overwrite_business_or_configuration(path):
    data = proposal()
    data["files"][0]["path"] = path
    with pytest.raises(ValidationError):
        TddProposal.model_validate(data)


@pytest.mark.parametrize(
    "content",
    [
        "test.skip('x', () => {});",
        "test.only('x', () => {});",
        "@pytest.mark.skip\ndef test_x(): pass",
    ],
)
def test_proposal_cannot_disable_tests(content):
    data = proposal()
    data["files"][0]["content"] = content
    with pytest.raises(ValidationError):
        TddProposal.model_validate(data)


async def test_proposal_submission_requires_host_stage_and_does_not_write(tmp_path):
    contract = TaskContract(task_id="tdd", objective="Clamp", acceptance_criteria=["Clamp"])
    context = ToolContext(workspace=tmp_path, contract=contract)
    tool = SubmitTestProposalTool()
    with pytest.raises(Exception, match="proposal stage"):
        await tool.run(TddProposal.model_validate(proposal()), context)
    context.tdd_proposal_mode = True
    result = await tool.run(TddProposal.model_validate(proposal()), context)
    assert result.data["proposal"]["red_cases"] == {"unit": ["test_clamp"]}
    assert not (tmp_path / "tests").exists()


class Model:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tools):
        assert {item["function"]["name"] for item in tools} == {
            "read_file",
            "list_files",
            "search_text",
            "submit_test_proposal",
        }
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="write",
                        name="create_file",
                        arguments={"path": "counter.py", "content": "broken"},
                    )
                ]
            )
        assert messages[-1]["role"] == "tool"
        assert "Unknown tool" in messages[-1]["content"]
        return ModelResponse(
            tool_calls=[ToolCall(id="proposal", name="submit_test_proposal", arguments=proposal())]
        )


async def test_proposal_loop_never_exposes_or_executes_implementation_tools(tmp_path: Path):
    (tmp_path / "counter.py").write_text("original", encoding="utf-8")
    model = Model()
    loop = AgentLoop(model=model, executor=ToolExecutor(build_builtin_registry()))
    contract = TaskContract(task_id="tdd", objective="Clamp", acceptance_criteria=["Clamp"])
    result = await loop.propose_tests(contract, tmp_path, run_id="proposal")
    assert result.red_cases == {"unit": ["test_clamp"]}
    assert (tmp_path / "counter.py").read_text() == "original"
