from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aicoding_agent.adapters.browser_tests import (
    BrowserSuite,
    BrowserTestError,
    BrowserTestRunner,
    parse_playwright_report,
)


def report(status: str = "passed", *, title: str = "counter stays nonnegative") -> dict:
    return {"suites": [{"specs": [{"title": title, "tests": [{
        "expectedStatus": "passed", "status": "expected" if status == "passed" else "unexpected",
        "results": [{"status": status, "error": {"message": "Expected zero; actual -1",
                    "expected": 0, "actual": -1}}],
    }]}]}], "errors": []}


def fixture_runner(tmp_path: Path, *, script: str | None = None):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "counter.js").write_text("value - 1", encoding="utf-8")
    host = tmp_path / "host"
    host.mkdir()
    executable = host / "fixed_reporter.py"
    executable.write_text(script or (
        "import json, os\n"
        "from pathlib import Path\n"
        f"payload={report()!r}\n"
        "Path(os.environ['PLAYWRIGHT_JSON_OUTPUT_NAME']).write_text(json.dumps(payload))\n"
        "Path(os.environ['BROWSER_GUARD_PROOF']).write_text('guarded')\n"
    ), encoding="utf-8")
    suite = BrowserSuite(
        id="counter", command=[sys.executable, str(executable)], cwd=host,
        test_files=[executable], expected_tests=["counter stays nonnegative"],
        allowed_origins=["http://127.0.0.1:8080"],
    )
    verification = SimpleNamespace(id="e2e", suite_ref="counter", max_failures_returned=5)
    return workspace, BrowserTestRunner([suite], tmp_path / "evidence"), verification


def test_report_parser_returns_assertion_evidence_without_inventing_it():
    parsed = parse_playwright_report(report("failed"), ["counter stays nonnegative"], 1)
    assert parsed["status"] == "failed"
    assert parsed["executed"] == 1
    assert parsed["failures"][0].expected == 0
    assert parsed["failures"][0].actual == -1


@pytest.mark.parametrize("case", ["zero", "missing", "skipped", "retry", "only", "xfail",
                                     "global_error", "exit_mismatch", "duplicate"])
def test_parser_rejects_false_success(case):
    payload = report()
    test = payload["suites"][0]["specs"][0]["tests"][0]
    exit_code = 0
    if case == "zero":
        payload["suites"] = []
    elif case == "missing":
        payload["suites"][0]["specs"][0]["title"] = "different test"
    elif case == "skipped":
        test["results"][0]["status"] = "skipped"
    elif case == "retry":
        test["results"].append({"status": "passed"})
    elif case == "only":
        payload["suites"][0]["specs"][0]["only"] = True
    elif case == "xfail":
        test["expectedStatus"] = "failed"
    elif case == "global_error":
        payload["errors"] = [{"message": "Setup failed"}]
    elif case == "exit_mismatch":
        exit_code = 1
    elif case == "duplicate":
        test_copy = json.loads(json.dumps(test))
        payload["suites"][0]["specs"][0]["tests"].append(test_copy)
    with pytest.raises(BrowserTestError):
        parse_playwright_report(payload, ["counter stays nonnegative"], exit_code)


def test_report_failure_is_bounded_and_secrets_redacted():
    payload = report("failed")
    payload["suites"][0]["specs"][0]["tests"][0]["results"][0]["error"] = {
        "message": "api_key=supersecret " + "x" * 6000,
    }
    parsed = parse_playwright_report(payload, ["counter stays nonnegative"], 1)
    failure = parsed["failures"][0]
    assert len(failure.message) == 4000
    assert "supersecret" not in failure.message
    assert failure.expected is None


@pytest.mark.parametrize("origin", ["https://example.org", "http://user:pass@localhost:80"])
def test_suite_rejects_nonlocal_and_credential_targets(tmp_path, origin):
    with pytest.raises(ValueError):
        BrowserSuite(id="bad", command=["node"], cwd=tmp_path, test_files=[tmp_path / "a"],
                     expected_tests=["a"], allowed_origins=[origin])


async def test_real_subprocess_report_and_stale_input_invalidation(tmp_path):
    workspace, runner, verification = fixture_runner(tmp_path)
    result = await runner.run(workspace=workspace, verification=verification,
                              run_id="test", timeout_seconds=5)
    assert result.status == "passed", result.reason
    assert await runner.revalidate(workspace, result)
    assert await runner.run_count("test", "e2e") == 1
    (workspace / "counter.js").write_text("Math.max(0, value - 1)", encoding="utf-8")
    assert not await runner.revalidate(workspace, result)


async def test_report_tampering_is_rejected(tmp_path):
    workspace, runner, verification = fixture_runner(tmp_path)
    result = await runner.run(workspace=workspace, verification=verification,
                              run_id="test", timeout_seconds=5)
    assert result.status == "passed"
    Path(result.evidence_refs[0]).write_text(json.dumps(report("failed")), encoding="utf-8")
    assert not await runner.revalidate(workspace, result)


async def test_runtime_lock_changes_do_not_invalidate_source(tmp_path):
    workspace, runner, verification = fixture_runner(tmp_path)
    runtime = workspace / ".aicoding-agent"
    runtime.mkdir()
    lock = runtime / "browser-workspace-guard"
    lock.write_text("first", encoding="utf-8")
    result = await runner.run(workspace=workspace, verification=verification,
                              run_id="test", timeout_seconds=5)
    lock.write_text("second", encoding="utf-8")
    assert result.status == "passed"
    assert await runner.revalidate(workspace, result)


async def test_dependency_tree_is_pruned_before_fingerprinting(tmp_path, monkeypatch):
    workspace, runner, verification = fixture_runner(tmp_path)
    dependencies = workspace / "node_modules" / "nested"
    dependencies.mkdir(parents=True)
    (dependencies / "giant.js").write_text("ignored", encoding="utf-8")
    original_walk = os.walk
    visited = []

    def checked_walk(*args, **kwargs):
        for entry in original_walk(*args, **kwargs):
            visited.append(Path(entry[0]))
            yield entry

    monkeypatch.setattr(os, "walk", checked_walk)
    result = await runner.run(workspace=workspace, verification=verification,
                              run_id="test", timeout_seconds=5)
    assert result.status == "passed"
    assert dependencies not in visited


async def test_source_hashing_budget_is_bounded(tmp_path):
    workspace, runner, _ = fixture_runner(tmp_path)
    runner.max_input_bytes = 2
    with pytest.raises(BrowserTestError, match="hashing size budget"):
        await asyncio.to_thread(runner._input_fingerprint, workspace, runner.suites["counter"])


async def test_artifact_size_monitor_stops_running_test(tmp_path):
    script = (
        "import os,time\nfrom pathlib import Path\n"
        "path=Path(os.environ['PLAYWRIGHT_OUTPUT_DIR']); path.mkdir()\n"
        "(path/'huge-artifact').write_bytes(b'x'*2_000_000)\n"
        "time.sleep(60)\n"
    )
    workspace, runner, verification = fixture_runner(tmp_path, script=script)
    runner.max_evidence_bytes = 1_000_000
    result = await runner.run(workspace=workspace, verification=verification,
                              run_id="test", timeout_seconds=10)
    assert result.status == "infra_error"
    assert "size budget" in result.reason
    assert result.duration_seconds < 5


async def test_frozen_suite_cannot_be_refrozen_after_edit(tmp_path):
    workspace, runner, _ = fixture_runner(tmp_path)
    await runner.freeze(workspace)
    suite = runner.suites["counter"]
    suite.test_files[0].write_text("print('edited')", encoding="utf-8")
    with pytest.raises(BrowserTestError, match="changed"):
        await runner.freeze(workspace)


async def test_missing_report_and_missing_guard_are_infrastructure_errors(tmp_path):
    workspace, runner, verification = fixture_runner(tmp_path, script="print('no report')")
    result = await runner.run(workspace=workspace, verification=verification,
                              run_id="test", timeout_seconds=5)
    assert result.status == "infra_error"
    assert result.report_hash == ""


async def test_timeout_kills_owned_process_and_persists_attempt(tmp_path):
    workspace, runner, verification = fixture_runner(tmp_path, script="import time; time.sleep(60)")
    result = await runner.run(workspace=workspace, verification=verification,
                              run_id="test", timeout_seconds=0.1)
    assert result.status == "timeout"
    assert result.duration_seconds < 10
    assert await runner.run_count("test", "e2e") == 1
    assert await runner.environment_failure_count("test", "e2e") == 1


async def test_preview_is_started_and_owned_service_is_stopped(tmp_path):
    workspace, runner, verification = fixture_runner(tmp_path)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    old = runner.suites["counter"]
    suite = old.model_copy(update={
        "preview_command": [sys.executable, "-m", "http.server", str(port),
                            "--bind", "127.0.0.1"],
        "preview_url": f"http://127.0.0.1:{port}",
    })
    runner.suites["counter"] = suite
    result = await runner.run(workspace=workspace, verification=verification,
                              run_id="test", timeout_seconds=5)
    assert result.status == "passed", result.reason
    with socket.socket() as probe:
        probe.settimeout(0.5)
        assert probe.connect_ex(("127.0.0.1", port)) != 0


async def test_cleanup_stops_owned_descendant_heartbeat(tmp_path):
    heartbeat = tmp_path / "heartbeat"
    child_code = (
        "import time\nfrom pathlib import Path\n"
        f"path=Path({str(heartbeat)!r})\n"
        "while True:\n path.write_text(str(time.time())); time.sleep(0.02)\n"
    )
    script = (
        "import subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable,'-c',{child_code!r}])\n"
        "time.sleep(60)\n"
    )
    workspace, runner, verification = fixture_runner(tmp_path, script=script)
    result = await runner.run(workspace=workspace, verification=verification,
                              run_id="test", timeout_seconds=0.5)
    assert result.status == "timeout"
    assert heartbeat.is_file()
    stopped = heartbeat.read_text()
    await asyncio.sleep(0.1)
    assert heartbeat.read_text() == stopped


async def test_cancellation_persists_incomplete_attempt(tmp_path):
    workspace, runner, verification = fixture_runner(tmp_path, script="import time; time.sleep(60)")
    task = asyncio.create_task(runner.run(workspace=workspace, verification=verification,
                                         run_id="test", timeout_seconds=60))
    for _ in range(100):
        if await runner.run_count("test", "e2e"):
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await runner.run_count("test", "e2e") == 1
    receipts = list((runner.evidence_root / "test").glob("*/result.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())["reason"] == "Browser verification interrupted"


async def test_model_credentials_not_inherited_by_test_process(tmp_path, monkeypatch):
    monkeypatch.setenv("MIMO_API_KEY", "do-not-inherit")
    monkeypatch.setenv("NODE_OPTIONS", "untrusted preload")
    script = (
        "import os\n"
        "assert 'MIMO_API_KEY' not in os.environ\n"
        "assert 'untrusted' not in os.environ['NODE_OPTIONS']\n"
        "print('sanitized')\n"
    )
    workspace, runner, verification = fixture_runner(tmp_path, script=script)
    result = await runner.run(workspace=workspace, verification=verification,
                              run_id="test", timeout_seconds=5)
    assert "sanitized" in Path(result.evidence_refs[1]).read_text()
    assert result.exit_code == 0


async def test_suite_config_is_frozen_and_relative_paths_resolved(tmp_path):
    workspace, original, _ = fixture_runner(tmp_path)
    config = tmp_path / "suites.json"
    suite = original.suites["counter"].model_dump(mode="json")
    suite["cwd"] = "host"
    suite["test_files"] = ["fixed_reporter.py"]
    config.write_text(json.dumps({"suites": [suite]}), encoding="utf-8")
    runner = await BrowserTestRunner.from_config(config, tmp_path / "other-evidence")
    await runner.freeze(workspace)
    config.write_text(config.read_text() + "\n", encoding="utf-8")
    with pytest.raises(BrowserTestError, match="changed"):
        await runner.freeze(workspace)


async def test_model_owned_suite_or_evidence_directory_is_rejected(tmp_path):
    workspace, runner, _ = fixture_runner(tmp_path)
    runner.evidence_root = workspace / "evidence"
    with pytest.raises(BrowserTestError, match="outside"):
        await runner.freeze(workspace)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
async def test_timeout_terminates_descendant_not_only_parent(tmp_path):
    pid_file = tmp_path / "child-pid"
    script = (
        "import subprocess,sys,time\n"
        "from pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        f"Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    workspace, runner, verification = fixture_runner(tmp_path, script=script)
    result = await runner.run(workspace=workspace, verification=verification,
                              run_id="test", timeout_seconds=0.3)
    assert result.status == "timeout"
    pid = int(pid_file.read_text())
    # A killed orphan may briefly remain a zombie; neither state can serve traffic.
    proc_stat = Path(f"/proc/{pid}/stat")
    assert not proc_stat.exists() or proc_stat.read_text().split()[2] == "Z"
