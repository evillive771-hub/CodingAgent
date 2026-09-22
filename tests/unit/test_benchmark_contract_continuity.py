"""Contract boundary tests for the optional, separately maintained benchmark runner."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from aicoding_agent.domain.models import TaskContract


@pytest.fixture
def runner() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[3]
        / "benchmark-runs"
        / "tools"
        / "scale_webbench_runner.py"
    )
    if not path.is_file():
        pytest.skip("Separately maintained benchmark runner is not installed")
    spec = importlib.util.spec_from_file_location("contract_continuity_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("current_index", [0, 1, 2, 20])
def test_contract_contains_only_current_and_prior_public_requirements(
    runner: ModuleType, tmp_path: Path, current_index: int
) -> None:
    tasks = [
        {
            "id": task_id,
            "description": f"Public requirement number {index}.",
            "oracle": f"Hidden solution number {index}.",
            "tests": f"Private assertion number {index}.",
        }
        for index, task_id in enumerate(["init", *runner.FORMAL_TASK_IDS])
    ]
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    # The parser owns ordering, so input file order cannot expose future tasks.
    (project_dir / "tasks.jsonl").write_text(
        "\n".join(json.dumps(task) for task in reversed(tasks)), encoding="utf-8"
    )
    ordered_tasks = runner.parse_tasks("canvas", project_dir)
    current = ordered_tasks[current_index]
    path = runner.build_contract(
        tmp_path, "canvas", current, project_tasks=ordered_tasks
    )
    contract = TaskContract.model_validate_json(path.read_text(encoding="utf-8"))

    assert contract.objective == current["description"]
    assert contract.acceptance_criteria[0] == current["description"]
    assert len(contract.acceptance_criteria) == current_index + 1
    serialized = path.read_text(encoding="utf-8")
    for previous in ordered_tasks[:current_index]:
        assert previous["description"] in serialized
    for future in ordered_tasks[current_index + 1 :]:
        assert future["description"] not in serialized
    assert "Hidden solution" not in serialized
    assert "Private assertion" not in serialized
    if current_index:
        assert "unless a later requirement explicitly changes it" in serialized
    assert not contract.verification_commands  # No invented verification evidence.


def test_standalone_contract_remains_compatible(runner: ModuleType, tmp_path: Path) -> None:
    task = {"id": "task-2", "description": "Make the canvas responsive."}
    path = runner.build_contract(tmp_path, "canvas", task)
    contract = TaskContract.model_validate_json(path.read_text(encoding="utf-8"))
    assert contract.objective == task["description"]
    assert contract.acceptance_criteria == [task["description"]]


@pytest.mark.parametrize("occurrences", [0, 2])
def test_ambiguous_sequence_fails_before_writing_contract(
    runner: ModuleType, tmp_path: Path, occurrences: int
) -> None:
    task = {"id": "task-2", "description": "Make the canvas responsive."}
    with pytest.raises(ValueError, match="exactly once"):
        runner.build_contract(tmp_path, "canvas", task, project_tasks=[task] * occurrences)
    assert not (tmp_path / "contracts").exists()


def test_resume_respects_absolute_boundary_before_new_task_limit(runner: ModuleType) -> None:
    tasks = [{"id": task_id} for task_id in ["init", *runner.FORMAL_TASK_IDS]]
    pending = runner.select_pending_tasks(
        tasks,
        "canvas",
        {"web-bench:canvas:init", "web-bench:canvas:task-1"},
        through_task="task-3",
        max_tasks=4,
    )
    assert pending == [(2, tasks[2]), (3, tasks[3])]


def test_completed_boundary_does_not_advance_into_future_tasks(runner: ModuleType) -> None:
    tasks = [{"id": task_id} for task_id in ["init", *runner.FORMAL_TASK_IDS]]
    completed = {f"web-bench:canvas:{task['id']}" for task in tasks[:4]}
    assert runner.select_pending_tasks(
        tasks, "canvas", completed, through_task="task-3", max_tasks=4
    ) == []


def test_max_tasks_still_counts_new_work_when_no_absolute_boundary(runner: ModuleType) -> None:
    tasks = [{"id": task_id} for task_id in ["init", *runner.FORMAL_TASK_IDS]]
    completed = {"web-bench:canvas:init", "web-bench:canvas:task-1"}
    assert runner.select_pending_tasks(tasks, "canvas", completed, max_tasks=2) == [
        (2, tasks[2]), (3, tasks[3])
    ]
    assert runner.select_pending_tasks(tasks, "canvas", completed, max_tasks=0) == []


def test_missing_boundary_is_rejected_even_when_no_tasks_requested(runner: ModuleType) -> None:
    tasks = [{"id": task_id} for task_id in runner.FORMAL_TASK_IDS]
    with pytest.raises(ValueError, match="'init'.*react"):
        runner.select_pending_tasks(tasks, "react", set(), through_task="init", max_tasks=0)
