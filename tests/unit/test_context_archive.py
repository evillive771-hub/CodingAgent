import json
from pathlib import Path

import pytest

from aicoding_agent.context.archive import ContextEvidenceArchive


@pytest.mark.asyncio
async def test_large_tool_output_is_archived_before_context_replacement(tmp_path: Path) -> None:
    content = "begin-" + ("x" * 9000) + "-end"
    archive = ContextEvidenceArchive(tmp_path / "evidence", large_output_chars=2000)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call-1", "type": "function"}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": content},
    ]

    compacted = await archive.compact(messages, run_id="run-1", max_chars=20000)

    archived_message = compacted[-1]["content"]
    assert "characters archived" in archived_message
    assert "<evidence path=" in archived_message
    evidence_files = list((tmp_path / "evidence" / "run-1" / "tool-results").glob("*.txt"))
    assert len(evidence_files) == 1
    assert evidence_files[0].read_text(encoding="utf-8") == content
    assert messages[-1]["content"] == content
    transcript = json.loads(
        (tmp_path / "evidence" / "run-1" / "transcript.json").read_text(encoding="utf-8")
    )
    assert transcript == messages
