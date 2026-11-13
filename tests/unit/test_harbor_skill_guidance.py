from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from aicoding_agent.domain.models import TaskContract, ToolCall
from aicoding_agent.skills import SkillCatalog
from aicoding_agent.tools.base import ToolContext
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor

_PROJECT_ROOT = Path(__file__).parents[2]
_ADAPTER_PATH = _PROJECT_ROOT.parent / "benchmark-runs" / "harbor_coding_agent.py"


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch):
    if not _ADAPTER_PATH.is_file():
        pytest.skip("The optional maintained Harbor adapter is not installed")
    monkeypatch.setenv("CODING_AGENT_BACKEND", "python")
    # Harbor itself runs in a separate environment; exercise the adapter without installing it.
    for name, symbol in (
        ("harbor.agents.base", "BaseAgent"),
        ("harbor.environments.base", "BaseEnvironment"),
        ("harbor.models.agent.context", "AgentContext"),
    ):
        module = types.ModuleType(name)
        setattr(module, symbol, object)
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("harbor_guidance_test", _ADAPTER_PATH)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_guidance_is_excluded_from_product_diffs(adapter, tmp_path: Path) -> None:
    product = tmp_path / "index.tsx"
    product.write_text("original component", encoding="utf-8")
    business_skill = tmp_path / "skills" / "business" / "SKILL.md"
    business_skill.parent.mkdir(parents=True)
    business_skill.write_text("business guidance", encoding="utf-8")
    before = adapter._workspace_files(tmp_path)

    adapter._install_review_guidance(_PROJECT_ROOT, tmp_path)

    assert adapter._workspace_files(tmp_path) == before
    assert business_skill.read_text(encoding="utf-8") == "business guidance"
    guidance = tmp_path / adapter._GUIDANCE_DIRECTORY / "react-review" / "SKILL.md"
    guidance.write_text("changed local guidance", encoding="utf-8")
    product.write_text("fixed component", encoding="utf-8")
    after = adapter._workspace_files(tmp_path)
    assert [path for path, digest in after.items() if before.get(path) != digest] == [
        "index.tsx"
    ]
    assert set(before) - set(after) == set()


def test_missing_guidance_is_an_explicit_configuration_error(adapter, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(FileNotFoundError, match="Required host review guidance is missing"):
        adapter._install_review_guidance(tmp_path / "missing-source", workspace)
    assert list(workspace.iterdir()) == []


def test_guidance_install_rejects_existing_task_content(adapter, tmp_path: Path) -> None:
    existing = tmp_path / adapter._GUIDANCE_DIRECTORY
    existing.mkdir()
    content = existing / "keep.txt"
    content.write_text("existing task content", encoding="utf-8")
    with pytest.raises(FileExistsError):
        adapter._install_review_guidance(_PROJECT_ROOT, tmp_path)
    assert content.read_text(encoding="utf-8") == "existing task content"


def test_runtime_summary_preserves_per_task_latency_percentiles(adapter, tmp_path: Path) -> None:
    events = tmp_path / "runtime" / "runs" / "run-1" / "events.jsonl"
    events.parent.mkdir(parents=True)
    records = [
        {"event_type": "model.response", "data": {"duration_ms": 100}},
        {"event_type": "model.response", "data": {"duration_ms": 300}},
        {"event_type": "tool.finished", "data": {"duration_ms": 20}},
        {
            "run_id": "run-1",
            "event_type": "agent.completed",
            "data": {"status": "completed", "turns": 2, "tool_calls": 1},
        },
    ]
    events.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    summary = adapter._runtime_summary(tmp_path / "runtime", tmp_path / "archive.zip")
    latency = summary["runtime"]["latency_ms"]
    assert latency["model"] == {"n": 2, "p50": 200.0, "p90": 280.0, "p95": 290.0}
    assert latency["tool"] == {"n": 1, "p50": 20.0, "p90": 20.0, "p95": 20.0}
    assert latency["goal"] == {"n": 0, "p50": None, "p90": None, "p95": None}


@pytest.mark.asyncio
async def test_adapter_contract_can_load_guidance_without_uploading_it(
    adapter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Environment:
        async def download_dir_with_exclusions(self, *, source_dir, target_dir, exclude):
            assert source_dir == "/app"
            assert adapter._GUIDANCE_DIRECTORY in exclude
            target_dir.mkdir()
            (target_dir / "index.tsx").write_text("original component", encoding="utf-8")

        async def exec(self, command):
            pytest.fail(f"Unchanged product should not trigger container mutation: {command}")

        async def upload_file(self, source, target):
            pytest.fail(f"Host guidance must not be uploaded to {target}")

    checked = []

    async def start_process(*args, **kwargs):
        task_path = Path(args[args.index("--task") + 1])
        workspace = Path(args[args.index("--workspace") + 1])
        task = TaskContract.model_validate(json.loads(task_path.read_text(encoding="utf-8")))
        assert task.skill_directories == [adapter._GUIDANCE_DIRECTORY]
        catalog = await SkillCatalog.discover(workspace, task)
        assert [item.name for item in catalog.summaries] == ["react-review"]
        result = await ToolExecutor(build_builtin_registry()).execute(
            ToolCall(id="review", name="load_skill", arguments={"name": "react-review"}),
            ToolContext(workspace=workspace, contract=task, skill_catalog=catalog),
        )
        assert result.ok, result.output
        document = await catalog.load("react-review")
        assert document.content == (
            _PROJECT_ROOT / "skills" / "react-review" / "SKILL.md"
        ).read_text(encoding="utf-8")
        assert result.data["skill_sha256"] == document.sha256
        checked.append(True)

        async def communicate():
            return b"", b""

        return SimpleNamespace(returncode=0, communicate=communicate)

    monkeypatch.setattr(adapter, "_STAGING_ROOT", tmp_path / "staging")
    monkeypatch.setattr(adapter.asyncio, "create_subprocess_exec", start_process)
    agent = object.__new__(adapter.CodingAgentHarborAdapter)
    agent.coding_agent_root = _PROJECT_ROOT
    agent.env_file = tmp_path / "unused-env.txt"
    agent.logs_dir = tmp_path / "logs"
    agent.logs_dir.mkdir()
    agent.session_id = "guidance-test"
    context = SimpleNamespace(metadata=None)

    await agent.run("Review this component without changing behavior", Environment(), context)

    assert checked == [True]
    assert context.metadata == {"exit_code": 0, "changed_files": [], "deleted_files": []}
    assert list((tmp_path / "staging").iterdir()) == []
