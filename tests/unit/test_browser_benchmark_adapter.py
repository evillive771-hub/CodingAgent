import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aicoding_agent.domain.models import TaskContract


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch):
    path = Path("D:/Github/benchmark-runs/tools/scale_webbench_runner.py")
    if not path.is_file():
        pytest.skip("External benchmark runner is not installed")
    monkeypatch.setenv("CODING_AGENT_BACKEND", "python")
    monkeypatch.delenv("CODING_AGENT_BROWSER_FEEDBACK", raising=False)
    spec = importlib.util.spec_from_file_location("browser_feedback_runner", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    suites = tmp_path / "public-suites.json"
    suites.write_text("[]", encoding="utf-8")
    mapping = tmp_path / "mapping.json"
    mapping.write_text(
        json.dumps(
            {
                "suites_config": str(suites),
                "tasks": {
                    "web-bench:canvas:task-1": [
                        {
                            "id": "public-ui",
                            "suite_ref": "public-canvas",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODING_AGENT_BROWSER_FEEDBACK", str(mapping))
    return suites


def test_feedback_is_opt_in_and_injects_only_matching_public_contract(
    runner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = {"id": "task-1", "description": "Draw a rectangle"}
    path = runner.build_contract(tmp_path, "canvas", task)
    legacy = json.loads(path.read_text(encoding="utf-8"))
    assert "browser_verifications" not in legacy
    configure(tmp_path, monkeypatch)
    path = runner.build_contract(tmp_path, "canvas", task)
    contract = TaskContract.model_validate_json(path.read_text(encoding="utf-8"))
    assert contract.browser_verifications[0].suite_ref == "public-canvas"
    assert "run_playwright" in contract.allowed_tools
    unmatched = runner.build_contract(
        tmp_path,
        "canvas",
        {
            "id": "task-2",
            "description": "Change color",
        },
    )
    assert "browser_verifications" not in json.loads(unmatched.read_text(encoding="utf-8"))


def test_python_launch_passes_host_suites_and_isolated_runtime(
    runner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suites = configure(tmp_path, monkeypatch)
    contract = runner.build_contract(
        tmp_path,
        "canvas",
        {
            "id": "task-1",
            "description": "Draw rectangle",
        },
    )
    captured = []

    def launch(command, **kwargs):
        captured.append(command)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(runner.subprocess, "run", launch)
    logs = tmp_path / "logs"
    result = runner.run_agent(contract, tmp_path / "workspace", logs)
    assert not result["infra_error"]
    command = captured[0]
    assert command[command.index("--browser-suites") + 1] == str(suites)
    assert command[command.index("--runtime-dir") + 1] == str(logs / "runtime")


def test_pi_backend_rejects_unimplemented_feedback(
    runner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(tmp_path, monkeypatch)
    runner.AGENT_BACKEND = "pi"
    with pytest.raises(ValueError, match="Python backend"):
        runner.build_contract(
            tmp_path,
            "canvas",
            {
                "id": "task-1",
                "description": "Draw rectangle",
            },
        )


def test_explicit_runtime_preserves_event_discovery(runner, tmp_path: Path) -> None:
    runtime = tmp_path / "logs" / "runtime"
    event_dir = runtime / "runs" / "run-123"
    event_dir.mkdir(parents=True)
    (event_dir / "events.jsonl").write_text(
        '{"event_type":"agent.completed","data":{}}\n',
        encoding="utf-8",
    )
    assert runner.run_ids(tmp_path, runtime) == {"run-123"}
    run_id, events = runner.latest_run_events(tmp_path, set(), runtime)
    assert run_id == "run-123"
    assert events[0]["event_type"] == "agent.completed"
    assert runner.run_ids(tmp_path) == set()
