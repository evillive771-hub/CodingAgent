import sys
from pathlib import Path

import pytest

from aicoding_agent.agent.background import (
    BackgroundJobStore,
    BackgroundStatus,
    BackgroundTaskManager,
)


@pytest.mark.asyncio
async def test_background_job_completes_persists_and_notifies_once(tmp_path: Path) -> None:
    store = BackgroundJobStore(tmp_path / "jobs")
    manager = BackgroundTaskManager(store=store)
    job = await manager.start(
        program=sys.executable,
        args=["-c", "print('finished')"],
        cwd=tmp_path,
        env={},
        timeout_seconds=10,
        verification_id="tests",
    )

    await manager.wait_for_change(10)
    notifications = await manager.collect_completed()

    assert len(notifications) == 1
    assert notifications[0].job_id == job.job_id
    assert notifications[0].status == BackgroundStatus.COMPLETED
    assert notifications[0].exit_code == 0
    assert "finished" in notifications[0].output
    assert await manager.collect_completed() == []

    restored = BackgroundTaskManager(store=store)
    await restored.initialize()
    assert restored.jobs[job.job_id].notification_delivered is True


@pytest.mark.asyncio
async def test_running_job_is_marked_interrupted_after_host_restart(tmp_path: Path) -> None:
    store = BackgroundJobStore(tmp_path / "jobs")
    from aicoding_agent.agent.background import BackgroundJob

    job = BackgroundJob(
        job_id="a" * 32,
        program="node",
        cwd=str(tmp_path),
        status=BackgroundStatus.RUNNING,
        timeout_seconds=30,
    )
    await store.append(job)

    restored = BackgroundTaskManager(store=store)
    await restored.initialize()

    assert restored.jobs[job.job_id].status == BackgroundStatus.INTERRUPTED
    notifications = await restored.collect_completed()
    assert notifications[0].job_id == job.job_id
