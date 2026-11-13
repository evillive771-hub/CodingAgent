from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from aicoding_agent import cli
from aicoding_agent.config import ModelSettings
from aicoding_agent.domain.models import AgentResult, AgentStatus, TaskContract


@pytest.mark.parametrize("command", ["run", "chat", "deliver-phase-one"])
def test_cli_exposes_host_regression_config(command):
    result = CliRunner().invoke(cli.app, [command, "--help"])
    assert result.exit_code == 0
    assert "regression-config" in result.output


@pytest.mark.asyncio
async def test_configured_agent_forwards_shared_retry_budget(tmp_path, monkeypatch):
    budget = object()
    settings = object()
    observed = {}

    async def fake_run(contract, workspace, selected_settings, **kwargs):
        observed.update(kwargs)
        assert selected_settings is settings
        return AgentResult(status=AgentStatus.COMPLETED, run_id=kwargs["run_id"])

    monkeypatch.setattr(cli, "_run_agent", fake_run)
    agent = cli._ConfiguredCodingAgent(settings, tmp_path / "runtime")
    contract = TaskContract(task_id="retry", objective="Fix", acceptance_criteria=["Pass"])
    await agent.run(contract, tmp_path, run_id="retry-run", shared_budget=budget)
    assert observed["shared_budget"] is budget
    assert observed["run_id"] == "retry-run"


async def test_configured_agent_forwards_isolated_proposal_mode(tmp_path, monkeypatch):
    budget = object()
    settings = object()
    observed = {}

    async def fake_run(contract, workspace, selected_settings, **kwargs):
        observed.update(kwargs)
        assert selected_settings is settings
        return "proposal"

    monkeypatch.setattr(cli, "_run_agent", fake_run)
    agent = cli._ConfiguredCodingAgent(settings, tmp_path / "runtime")
    contract = TaskContract(task_id="draft", objective="Fix", acceptance_criteria=["Pass"])
    result = await agent.propose_tests(contract, tmp_path, run_id="draft", shared_budget=budget)
    assert result == "proposal"
    assert observed["proposal_only"] is True
    assert observed["shared_budget"] is budget


async def test_direct_proposal_uses_stage_instead_of_normal_agent(tmp_path, monkeypatch):
    observed = {}

    class Model:
        async def aclose(self):
            observed["closed"] = observed.get("closed", 0) + 1

    class Agent:
        async def propose_tests(self, contract, workspace, **kwargs):
            observed.update(kwargs)
            return "draft"

        async def run(self, *args, **kwargs):
            pytest.fail("Proposal mode called the coding loop")

    monkeypatch.setattr(cli, "create_chat_model", lambda _: Model())
    monkeypatch.setattr(cli, "AgentLoop", lambda **kwargs: Agent())
    settings = ModelSettings(
        api_key="test-key", model="test-model", base_url="https://model.example/v1"
    )
    contract = TaskContract(task_id="draft", objective="Fix", acceptance_criteria=["Pass"])
    result = await cli._run_agent(
        contract, tmp_path, settings, runtime_root=tmp_path / "runtime",
        proposal_only=True, run_id="draft",
    )
    assert result == "draft"
    assert observed["closed"] == 4


def test_run_command_forwards_regression_config(tmp_path, monkeypatch):
    contract = TaskContract(task_id="gate", objective="Fix", acceptance_criteria=["Pass"])
    task = tmp_path / "task.json"
    task.write_text(contract.model_dump_json(), encoding="utf-8")
    config = tmp_path / "regression.json"
    config.write_text("{}", encoding="utf-8")
    env = tmp_path / "model.txt"
    env.write_text("unused", encoding="utf-8")
    observed = {}

    async def fake_run(contract, workspace, settings, **kwargs):
        observed.update(kwargs)
        return AgentResult(status=AgentStatus.COMPLETED, run_id="run")

    monkeypatch.setattr(cli.ModelSettings, "from_env_file", lambda path: object())
    monkeypatch.setattr(cli, "_run_agent", fake_run)
    result = CliRunner().invoke(cli.app, [
        "run", "--task", str(task), "--workspace", str(tmp_path), "--env-file", str(env),
        "--regression-config", str(config),
    ])
    assert result.exit_code == 0, result.output
    assert observed["regression_config"] == Path(config)


@pytest.mark.asyncio
async def test_direct_run_adopts_only_controller_accepted_code(tmp_path, monkeypatch):
    observed = {}

    class Model:
        async def aclose(self):
            pass

    class Controller:
        async def run(self, contract, workspace, **kwargs):
            observed.update(kwargs)
            return SimpleNamespace(agent_result=AgentResult(
                status=AgentStatus.COMPLETED, run_id=kwargs["run_id"]
            ))

    async def controller_factory(*args, **kwargs):
        return Controller()

    monkeypatch.setattr(cli, "build_controller", controller_factory)
    monkeypatch.setattr(cli, "create_chat_model", lambda _: Model())
    monkeypatch.setattr(cli, "AgentLoop", lambda **kwargs: object())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = ModelSettings(
        api_key="test-key", model="test-model", base_url="https://model.example/v1"
    )
    contract = TaskContract(task_id="adopt", objective="Fix", acceptance_criteria=["Pass"])
    result = await cli._run_agent(
        contract, workspace, settings, runtime_root=tmp_path / "runtime",
        regression_config=tmp_path / "host-config.json", run_id="adopt-run",
    )
    assert result.status == AgentStatus.COMPLETED
    assert observed["adopt"] is True
