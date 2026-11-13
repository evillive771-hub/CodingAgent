import asyncio
import sys

import pytest

from aicoding_agent.agent.mutation_journal import MutationJournal
from aicoding_agent.chat.session import ChatSession
from aicoding_agent.chat.store import ChatTranscriptStore
from aicoding_agent.domain.models import AgentResult, AgentStatus, TaskContract, VerificationCommand
from aicoding_agent.regression.controller import RegressionController
from aicoding_agent.regression.gate import CommandSuite, RegressionGate


class Agent:
    def __init__(self, values):
        self.values, self.calls = values, []

    async def run(self, contract, workspace, *, run_id, shared_budget, **kwargs):
        await shared_budget.charge_turn()
        self.calls.append((workspace, kwargs))
        (workspace / "value.txt").write_text(self.values.pop(0), encoding="utf-8")
        return AgentResult(status=AgentStatus.COMPLETED, run_id=run_id, final_text="Changed value")


def setup(tmp_path, values):
    source, host = tmp_path / "source", tmp_path / "host"
    source.mkdir()
    host.mkdir()
    (source / "value.txt").write_text("initial", encoding="utf-8")
    (source / ".env.local").write_text("preserved", encoding="utf-8")
    test = host / "verify.py"
    test.write_text(
        "import os,pathlib\n"
        "value=(pathlib.Path(os.environ['REGRESSION_WORKSPACE'])/'value.txt').read_text()\n"
        "assert value != 'bad'\n", encoding="utf-8",
    )
    contract = TaskContract(task_id="profile", objective="Fix", acceptance_criteria=["Valid value"],
                            verification_commands=[VerificationCommand(id="value", program="python",
                                                                       args=["verify.py"])])
    gate = RegressionGate(tmp_path / "evidence", command_suites=[CommandSuite(
        id="value", cwd=host, command=[sys.executable, str(test)], test_files=[test],
    )])
    agent = Agent(values)
    journal = MutationJournal(tmp_path / "mutations")
    controller = RegressionController(tmp_path / "runtime", gate)
    session = ChatSession(agent=agent, workspace=source, profile=contract,
                          store=ChatTranscriptStore(tmp_path / "transcript"),
                          mutation_journal=journal, regression_controller=controller)
    return session, source, agent, journal, controller


async def test_chat_only_adopts_passed_code_and_keeps_rewind_journal(tmp_path):
    session, source, agent, journal, controller = setup(tmp_path, ["accepted", "bad", "bad"])
    first = await session.send("Set a valid value")
    assert first.status == AgentStatus.COMPLETED
    assert (source / "value.txt").read_text() == "accepted"
    assert (source / ".env.local").read_text() == "preserved"
    records = await journal.load(first.run_id)
    assert records[0].before.content == "initial"
    assert records[0].after.content == "accepted"
    second = await session.send("Try another change")
    assert second.status == AgentStatus.BLOCKED
    assert (source / "value.txt").read_text() == "accepted"
    assert (await controller.store.current()).revision == 1
    assert agent.calls[2][1]["conversation_history"][-1]["content"].find("AssertionError") >= 0
    await journal.recover_runs(workspace=source, run_ids=[first.run_id])
    assert (source / "value.txt").read_text() == "initial"


async def test_external_source_edit_blocks_adoption_and_baseline_advance(tmp_path):
    session, source, agent, _, controller = setup(tmp_path, ["accepted"])
    original = agent.run

    async def changing_source(*args, **kwargs):
        result = await original(*args, **kwargs)
        (source / "user.txt").write_text("user edit", encoding="utf-8")
        return result

    agent.run = changing_source
    turn = await session.send("Change value")
    assert turn.status == AgentStatus.BLOCKED
    assert (source / "value.txt").read_text() == "initial"
    assert (source / "user.txt").read_text() == "user edit"
    assert (await controller.store.current()).revision == 0


async def test_crash_during_adoption_restores_source_even_when_budget_expired(
    tmp_path, monkeypatch,
):
    session, source, agent, _, controller = setup(tmp_path, ["accepted"])
    original_accept = controller.store.accept

    async def crash(candidate, contract, report, expected_revision, before_publish=None):
        # Simulate process death after durable copy intent, without executing cleanup.
        state = await controller.checkpoints.load("crash")
        state["adoption_started"] = True
        await controller.checkpoints.save("crash", state)
        (source / "value.txt").write_text("accepted", encoding="utf-8")
        raise asyncio.CancelledError

    monkeypatch.setattr(controller.store, "accept", crash)
    with pytest.raises(asyncio.CancelledError):
        await controller.run(session.profile, source, run_id="crash", agent=agent, adopt=True)
    assert (source / "value.txt").read_text() == "accepted"
    budget = await controller.budget_for("crash", session.profile)
    payload = await budget.store.load("crash")
    payload["deadline_epoch"] = 0
    await budget.store.save("crash", payload)
    monkeypatch.setattr(controller.store, "accept", original_accept)
    resumed = await controller.run(session.profile, source, run_id="crash", agent=agent, adopt=True)
    assert resumed.status == "blocked"
    assert (source / "value.txt").read_text() == "initial"
    assert (await controller.store.current()).revision == 0
    assert len(agent.calls) == 1
