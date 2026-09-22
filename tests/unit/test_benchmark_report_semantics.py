"""Exercise the maintained benchmark reporter, without rewriting frozen reports."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture(scope="module")
def reporter() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[3]
        / "benchmark-runs"
        / "tools"
        / "scale_report.py"
    )
    if not path.is_file():
        pytest.skip("maintained benchmark reporter is not installed beside this checkout")
    spec = importlib.util.spec_from_file_location("maintained_scale_report", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def result(status: str, verifier: str, *, eligible: bool = True) -> dict:
    return {
        "agent_status": status,
        "verifier_status": verifier,
        "eligible": eligible,
        "execution_status": "recorded",
    }


@pytest.mark.parametrize(
    ("status", "false_negative", "completed"),
    [
        ("completed", 0, 1),
        ("submitted_unverified", 0, 0),
        ("failed", 1, 0),
        ("budget_exhausted", 1, 0),
        ("waiting_for_user", 0, 0),
        ("interrupted", 0, 0),
        ("missing_runtime_evidence", 0, 0),
    ],
)
def test_external_pass_does_not_promote_or_misclassify_agent_status(
    reporter: ModuleType, status: str, false_negative: int, completed: int
) -> None:
    metrics = reporter.metric_group([result(status, "pass")])

    assert metrics["pass_at_1"] == {"num": 1, "den": 1, "percent": 100.0}
    assert metrics["agent_clean_completion"]["num"] == completed
    assert metrics["false_negative"]["num"] == false_negative
    assert metrics["matrix"]["agent_other_verifier_pass"] == 1 - completed


def test_eligible_matrix_preserves_all_outcomes_without_false_completion(
    reporter: ModuleType,
) -> None:
    rows = [
        result(status, verifier)
        for status in ("completed", "submitted_unverified", "failed", "budget_exhausted")
        for verifier in ("pass", "fail")
    ] + [result("failed", "pass", eligible=False)]

    metrics = reporter.metric_group(rows)

    assert metrics["eligible"] == 8
    assert metrics["pass_at_1"] == {"num": 4, "den": 8, "percent": 50.0}
    assert metrics["false_negative"] == {"num": 2, "den": 8, "percent": 25.0}
    assert metrics["false_positive"] == {"num": 1, "den": 8, "percent": 12.5}
    assert metrics["completed_failure_rate"] == {"num": 1, "den": 2, "percent": 50.0}
    assert metrics["terminal_status_matrix"] == {
        "completed": {"verifier_pass": 1, "verifier_fail": 1},
        "submitted_unverified": {"verifier_pass": 1, "verifier_fail": 1},
        "other": {"verifier_pass": 2, "verifier_fail": 2},
    }
    assert metrics["matrix"] == {
        "agent_completed_verifier_pass": 1,
        "agent_completed_verifier_fail": 1,
        "agent_other_verifier_pass": 3,
        "agent_other_verifier_fail": 3,
    }


def test_report_explains_semantics_in_json_markdown_and_html(reporter: ModuleType) -> None:
    row = {
        **result("submitted_unverified", "pass"),
        "task_id": "sample-task",
        "benchmark": "web-bench",
        "project": "canvas",
        "sequence_index": 1,
        "batch": "sentinel",
    }
    summary = reporter.make_summary(
        {"experiment_id": "report-unit-test"},
        [row],
        {"expected": 1, "recorded": 1, "not_run": 0, "duplicate_task_ids": []},
        [],
    )

    assert summary["metric_semantics_version"] == 2
    assert "budget_exhausted" in summary["metric_definitions"]["false_negative"]
    assert summary["overall"]["false_negative"]["num"] == 0
    assert "不计为 completed" in reporter.render_markdown(summary)
    html = reporter.render_html(summary, [row])
    assert "Failure terminal &amp; pass / eligible" in html
    assert "neither completed nor a failure claim" in html
