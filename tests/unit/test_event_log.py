import json
from pathlib import Path

import pytest

from aicoding_agent.persistence.jsonl import AtomicCheckpointStore, JsonlEventSink


@pytest.mark.asyncio
async def test_event_log_redacts_secrets_and_checkpoint_round_trips(tmp_path: Path) -> None:
    sink = JsonlEventSink(tmp_path / "events")
    await sink.emit(
        "run-1",
        "test",
        {"api_key": "secret-value", "nested": {"authorization": "Bearer secret"}, "ok": 1},
    )
    line = (tmp_path / "events" / "run-1" / "events.jsonl").read_text(encoding="utf-8")
    payload = json.loads(line)
    assert "secret-value" not in line
    assert payload["data"]["api_key"] == "<redacted>"

    restarted_sink = JsonlEventSink(tmp_path / "events")
    await restarted_sink.emit("run-1", "after-restart", {"ok": 2})
    lines = (
        (tmp_path / "events" / "run-1" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    )
    assert json.loads(lines[-1])["sequence"] == 2

    store = AtomicCheckpointStore(tmp_path / "checkpoints")
    await store.save("run-1", {"stage": "coding", "count": 2})
    assert await store.load("run-1") == {"stage": "coding", "count": 2}


@pytest.mark.asyncio
async def test_persistence_rejects_unsafe_run_ids(tmp_path: Path) -> None:
    sink = JsonlEventSink(tmp_path / "events")
    store = AtomicCheckpointStore(tmp_path / "checkpoints")

    with pytest.raises(ValueError, match="run_id"):
        await sink.emit("../outside", "test", {})
    with pytest.raises(ValueError, match="run_id"):
        await store.save("bad/run", {"stage": "created"})
    with pytest.raises(ValueError, match="run_id"):
        await store.load("..\\outside")

    assert not (tmp_path / "outside").exists()
