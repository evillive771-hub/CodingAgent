import asyncio
import sys
from pathlib import Path

import pytest

from aicoding_agent.domain.models import TaskContract, VerificationCommand
from aicoding_agent.regression.store import (
    AcceptedBaselineStore,
    BaselineConflict,
    RegressionStoreError,
    apply_delta,
    workspace_fingerprint,
)


def contract(task_id="a"):
    return TaskContract(task_id=task_id, objective="Preserve arithmetic",
                        acceptance_criteria=["Adds correctly"], verification_commands=[
                            VerificationCommand(id="arithmetic", program="python", args=["test.py"])
                        ])


async def report(store, candidate):
    baseline = await store.current()
    return {"status": "passed", "candidate_fingerprint": await workspace_fingerprint(candidate),
            "registry_fingerprint": baseline.registry_fingerprint,
            "tests": [{"id": "arithmetic", "status": "passed", "test_fingerprint": "fixed"}]}


async def test_snapshot_untracked_secret_exclusion_and_real_worktree(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "new.js").write_text("untracked", encoding="utf-8")
    (source / ".env.local").write_text("secret", encoding="utf-8")
    (source / "node_modules").mkdir()
    (source / "node_modules" / "big").write_text("dependency", encoding="utf-8")
    store = AcceptedBaselineStore(tmp_path / "runtime")
    baseline = await store.initialize(source)
    assert not baseline.verified
    candidate = await store.prepare_candidate(source, "run", 0, 0)
    assert (candidate / "new.js").read_text() == "untracked"
    assert not (candidate / ".env.local").exists()
    assert not (candidate / "node_modules").exists()
    assert (candidate / ".git").is_file()
    (candidate / "added.js").write_text("added", encoding="utf-8")
    accepted = await store.accept(candidate, contract(), await report(store, candidate), 0)
    assert accepted.verified and accepted.revision == accepted.registry_version == 1
    assert (Path(accepted.snapshot_path) / "added.js").read_text() == "added"
    assert not (source / "added.js").exists()
    assert (source / ".env.local").read_text() == "secret"


async def test_accept_conflict_and_stale_report_leave_baseline_unchanged(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "code.py").write_text("v1", encoding="utf-8")
    store = AcceptedBaselineStore(tmp_path / "runtime")
    await store.initialize(source)
    candidate = await store.prepare_candidate(source, "a", 0)
    evidence = await report(store, candidate)
    (candidate / "code.py").write_text("unverified", encoding="utf-8")
    with pytest.raises(RegressionStoreError, match="no longer matches"):
        await store.accept(candidate, contract(), evidence, 0)
    assert (await store.current()).revision == 0
    accepted = await store.accept(candidate, contract(), await report(store, candidate), 0)
    with pytest.raises(BaselineConflict):
        await AcceptedBaselineStore(store.root).accept(candidate, contract(), evidence, 0)
    assert await store.current() == accepted


async def test_historical_definitions_and_test_fingerprints_cannot_weaken(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "code.py").write_text("v1", encoding="utf-8")
    store = AcceptedBaselineStore(tmp_path / "runtime")
    await store.initialize(source)
    candidate = await store.prepare_candidate(source, "a", 0)
    await store.accept(candidate, contract(), await report(store, candidate), 0)
    changed = contract("b")
    changed.verification_commands[0].args = ["weakened.py"]
    with pytest.raises(RegressionStoreError, match="definition changed"):
        await store.effective_contract(changed)
    evidence = await report(store, candidate)
    evidence["tests"][0]["test_fingerprint"] = "weakened"
    with pytest.raises(RegressionStoreError, match="fingerprint changed"):
        await store.accept(candidate, contract("b"), evidence, 1)
    assert (await store.current()).revision == 1


async def test_partial_report_and_tampered_snapshot_fail_closed(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "code.py").write_text("v1", encoding="utf-8")
    store = AcceptedBaselineStore(tmp_path / "runtime")
    baseline = await store.initialize(source)
    candidate = await store.prepare_candidate(source, "a", 0)
    evidence = await report(store, candidate)
    evidence["tests"] = []
    with pytest.raises(RegressionStoreError, match="complete required"):
        await store.accept(candidate, contract(), evidence, 0)
    (Path(baseline.snapshot_path) / "code.py").write_text("tampered", encoding="utf-8")
    with pytest.raises(RegressionStoreError, match="snapshot was modified"):
        await store.prepare_candidate(source, "a", 1)


async def test_integration_delta_preserves_unrelated_edits_and_rejects_overlap(tmp_path):
    base, candidate, integration = [tmp_path / name for name in ("base", "cand", "integration")]
    for root in (base, candidate, integration):
        root.mkdir()
        (root / "a").write_text("old", encoding="utf-8")
        (root / "b").write_text("old", encoding="utf-8")
    (candidate / "a").write_text("candidate", encoding="utf-8")
    (integration / "b").write_text("other agent", encoding="utf-8")
    await apply_delta(base, candidate, integration)
    assert (integration / "a").read_text() == "candidate"
    assert (integration / "b").read_text() == "other agent"
    (integration / "a").write_text("conflict", encoding="utf-8")
    with pytest.raises(BaselineConflict):
        await apply_delta(base, candidate, integration)
    assert (integration / "a").read_text() == "conflict"


async def test_private_snapshot_never_stages_user_repository(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    async def git(*args):
        process = await asyncio.create_subprocess_exec(
            "git", "-C", str(source), *args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, error = await process.communicate()
        assert process.returncode == 0, error.decode()
        return output

    await git("init", "--quiet")
    (source / "tracked.py").write_text("original", encoding="utf-8")
    await git("add", "tracked.py")
    await git("-c", "user.name=Test", "-c", "user.email=test@localhost",
              "commit", "--quiet", "-m", "Initial")
    (source / "tracked.py").write_text("staged user work", encoding="utf-8")
    await git("add", "tracked.py")
    (source / "tracked.py").write_text("unstaged user work", encoding="utf-8")
    (source / "untracked.py").write_text("new user file", encoding="utf-8")
    head, index = await git("rev-parse", "HEAD"), await git("diff", "--cached")
    store = AcceptedBaselineStore(tmp_path / "runtime")
    await store.initialize(source)
    candidate = await store.prepare_candidate(source, "a", 0)
    assert (candidate / "tracked.py").read_text() == "unstaged user work"
    assert (candidate / "untracked.py").read_text() == "new user file"
    assert await git("rev-parse", "HEAD") == head
    assert await git("diff", "--cached") == index


async def test_concurrent_accept_compare_and_swap_has_single_winner(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "code").write_text("source", encoding="utf-8")
    store = AcceptedBaselineStore(tmp_path / "runtime")
    await store.initialize(source)
    candidates = [await store.prepare_candidate(source, "a", index) for index in range(2)]
    reports = [await report(store, candidate) for candidate in candidates]
    results = await asyncio.gather(*[
        AcceptedBaselineStore(store.root).accept(candidate, contract(), evidence, 0)
        for candidate, evidence in zip(candidates, reports, strict=True)
    ], return_exceptions=True)
    assert sum(isinstance(value, BaselineConflict) for value in results) == 1
    assert (await store.current()).revision == 1


async def test_crashed_transaction_lock_is_reusable(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "code").write_text("source", encoding="utf-8")
    store = AcceptedBaselineStore(tmp_path / "runtime")
    await store.initialize(source)
    script = """
import asyncio, sys
from pathlib import Path
from aicoding_agent.tools.file_lock import WorkspaceFileLockManager
async def main():
    root = Path(sys.argv[1])
    async with WorkspaceFileLockManager().acquire(root, root / 'accepted.json'):
        print('locked', flush=True)
        await asyncio.sleep(3600)
asyncio.run(main())
"""
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, str(store.root), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert (await asyncio.wait_for(child.stdout.readline(), 10)).strip() == b"locked"
    finally:
        child.kill()
        await child.communicate()
    candidate = await store.prepare_candidate(source, "a", 0)
    accepted = await store.accept(candidate, contract(), await report(store, candidate), 0)
    assert accepted.revision == 1


async def test_store_cannot_be_reused_for_another_source_project(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "code").write_text("first", encoding="utf-8")
    store = AcceptedBaselineStore(tmp_path / "runtime")
    await store.initialize(first)
    with pytest.raises(RegressionStoreError, match="another source"):
        await store.initialize(second)


async def test_worktree_preserves_line_endings_despite_project_attributes(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / ".gitattributes").write_text("*.js text eol=lf\n", encoding="utf-8")
    original = b"first\r\nsecond\r\n"
    (source / "code.js").write_bytes(original)
    store = AcceptedBaselineStore(tmp_path / "runtime")
    await store.initialize(source)
    candidate = await store.prepare_candidate(source, "a", 0)
    assert (candidate / "code.js").read_bytes() == original


async def test_promotion_callback_runs_only_for_compare_and_swap_winner(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "code").write_text("original", encoding="utf-8")
    store = AcceptedBaselineStore(tmp_path / "runtime")
    await store.initialize(source)
    candidates = [await store.prepare_candidate(source, "a", index) for index in range(2)]
    for index, candidate in enumerate(candidates):
        (candidate / "code").write_text(str(index), encoding="utf-8")
    callback_calls = []

    async def promote(index):
        async def adopt():
            callback_calls.append(index)
            await asyncio.sleep(0.1)
            (source / "code").write_text(str(index), encoding="utf-8")
        candidate = candidates[index]
        evidence = await report(store, candidate)
        return await AcceptedBaselineStore(store.root).accept(
            candidate, contract(), evidence, 0, before_publish=adopt,
        )

    results = await asyncio.gather(promote(0), promote(1), return_exceptions=True)
    assert sum(isinstance(result, BaselineConflict) for result in results) == 1
    assert len(callback_calls) == 1
    baseline = await store.current()
    assert (source / "code").read_bytes() == (Path(baseline.snapshot_path) / "code").read_bytes()


async def test_promotion_callback_failure_never_publishes_baseline(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "code").write_text("original", encoding="utf-8")
    store = AcceptedBaselineStore(tmp_path / "runtime")
    original = await store.initialize(source)
    candidate = await store.prepare_candidate(source, "a", 0)

    async def fails():
        raise OSError("adoption interrupted")

    with pytest.raises(OSError, match="adoption interrupted"):
        await store.accept(candidate, contract(), await report(store, candidate), 0,
                           before_publish=fails)
    assert await store.current() == original
