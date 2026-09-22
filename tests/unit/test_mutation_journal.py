from pathlib import Path

import pytest

from aicoding_agent.agent.mutation_journal import MutationJournal, MutationRecoveryError
from aicoding_agent.domain.models import TaskContract, ToolCall
from aicoding_agent.tools.base import ToolContext
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor


@pytest.mark.asyncio
async def test_journal_recovers_multiple_mutations_in_reverse_and_is_idempotent(
    tmp_path: Path,
) -> None:
    journal = MutationJournal(tmp_path / "journal")
    target = tmp_path / "value.txt"
    target.write_text("one\n", encoding="utf-8")
    before_first = await journal.capture(target)
    target.write_text("two\n", encoding="utf-8")
    await journal.record(
        run_id="run-1",
        tool_name="apply_text_patch",
        workspace=tmp_path,
        path=target,
        before=before_first,
    )
    before_second = await journal.capture(target)
    target.write_text("three\n", encoding="utf-8")
    await journal.record(
        run_id="run-2",
        tool_name="apply_text_patch",
        workspace=tmp_path,
        path=target,
        before=before_second,
    )

    result = await journal.recover_runs(workspace=tmp_path, run_ids=["run-1", "run-2"])

    assert target.read_text(encoding="utf-8") == "one\n"
    assert result.restored_files == ["value.txt"]
    repeated = await journal.recover_runs(workspace=tmp_path, run_ids=["run-1", "run-2"])
    assert repeated.already_completed

    target.write_text("external after recovery\n", encoding="utf-8")
    with pytest.raises(MutationRecoveryError, match="changed afterward"):
        await journal.recover_runs(workspace=tmp_path, run_ids=["run-1", "run-2"])


@pytest.mark.asyncio
async def test_journal_recovery_removes_file_created_by_agent(tmp_path: Path) -> None:
    journal = MutationJournal(tmp_path / "journal")
    target = tmp_path / "created.txt"
    before = await journal.capture(target)
    target.write_text("created\n", encoding="utf-8")
    await journal.record(
        run_id="create-run",
        tool_name="create_file",
        workspace=tmp_path,
        path=target,
        before=before,
    )

    result = await journal.recover_runs(workspace=tmp_path, run_ids=["create-run"])

    assert not target.exists()
    assert result.removed_files == ["created.txt"]


@pytest.mark.asyncio
async def test_recovery_rejects_unjournaled_external_edit_without_partial_changes(
    tmp_path: Path,
) -> None:
    journal = MutationJournal(tmp_path / "journal")
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("before first\n", encoding="utf-8")
    second.write_text("before second\n", encoding="utf-8")
    first_before = await journal.capture(first)
    second_before = await journal.capture(second)
    first.write_text("agent first\n", encoding="utf-8")
    second.write_text("agent second\n", encoding="utf-8")
    await journal.record(
        run_id="run",
        tool_name="apply_text_patch",
        workspace=tmp_path,
        path=first,
        before=first_before,
    )
    await journal.record(
        run_id="run",
        tool_name="apply_text_patch",
        workspace=tmp_path,
        path=second,
        before=second_before,
    )
    second.write_text("external edit\n", encoding="utf-8")

    with pytest.raises(MutationRecoveryError, match="not a journal boundary"):
        await journal.recover_runs(workspace=tmp_path, run_ids=["run"])

    assert first.read_text(encoding="utf-8") == "agent first\n"
    assert second.read_text(encoding="utf-8") == "external edit\n"


@pytest.mark.asyncio
async def test_executor_journals_file_tool_mutation(tmp_path: Path) -> None:
    target = tmp_path / "value.txt"
    target.write_text("before\n", encoding="utf-8")
    journal = MutationJournal(tmp_path / "journal")
    executor = ToolExecutor(build_builtin_registry(), mutation_journal=journal)
    context = ToolContext(
        workspace=tmp_path,
        run_id="journaled-run",
        contract=TaskContract(
            task_id="journal-test",
            objective="Journal a file patch",
            acceptance_criteria=["The write is reversible"],
            allowed_tools={"apply_text_patch"},
        ),
    )

    result = await executor.execute(
        ToolCall(
            id="patch",
            name="apply_text_patch",
            arguments={
                "path": "value.txt",
                "old_text": "before\n",
                "new_text": "after\n",
            },
        ),
        context,
    )

    assert result.ok
    records = await journal.load("journaled-run")
    assert len(records) == 1
    assert records[0].path == "value.txt"
    assert records[0].before.content == "before\n"
    assert records[0].after.content == "after\n"


@pytest.mark.asyncio
async def test_executor_rolls_back_when_journal_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "value.txt"
    target.write_text("before\n", encoding="utf-8")
    journal = MutationJournal(tmp_path / "journal")

    async def fail_record(**_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(journal, "record", fail_record)
    executor = ToolExecutor(build_builtin_registry(), mutation_journal=journal)
    context = ToolContext(
        workspace=tmp_path,
        run_id="failed-journal-run",
        contract=TaskContract(
            task_id="journal-failure-test",
            objective="Rollback an unjournaled change",
            acceptance_criteria=["No unjournaled mutation remains"],
            allowed_tools={"apply_text_patch"},
        ),
    )

    result = await executor.execute(
        ToolCall(
            id="patch",
            name="apply_text_patch",
            arguments={
                "path": "value.txt",
                "old_text": "before\n",
                "new_text": "after\n",
            },
        ),
        context,
    )

    assert not result.ok
    assert "rolled back" in result.output
    assert target.read_text(encoding="utf-8") == "before\n"


@pytest.mark.asyncio
async def test_recovery_refuses_runs_with_irreversible_effects(tmp_path: Path) -> None:
    journal = MutationJournal(tmp_path / "journal")
    await journal.record_irreversible_effect(
        run_id="process-run",
        tool_name="run_process",
        risk="process",
    )

    with pytest.raises(MutationRecoveryError, match="run_process"):
        await journal.recover_runs(workspace=tmp_path, run_ids=["process-run"])
