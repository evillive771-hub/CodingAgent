from pathlib import Path

import pytest

from aicoding_agent.adapters.build import ControlledArtifactBuilder
from aicoding_agent.adapters.deployment import (
    DeviceCommandTemplates,
    RemoteCommandTemplate,
    SftpNfsDeploymentSettings,
    SftpNfsTestEnvironmentPublisher,
    SshEndpoint,
)
from aicoding_agent.adapters.fakes import FakeChangeInspector, FakeWorkspaceManager
from aicoding_agent.adapters.playwright import (
    BrowserCapture,
    PlaywrightBrowserVerifier,
    PlaywrightSettings,
)
from aicoding_agent.domain.models import (
    AgentResult,
    AgentStatus,
    BuildCommand,
    CodingOutcome,
    TaskContract,
)
from aicoding_agent.persistence.jsonl import AtomicCheckpointStore, JsonlEventSink
from aicoding_agent.workflow.engine import DeliveryWorkflow
from aicoding_agent.workflow.models import ApprovalRecord


class PhaseOneAgent:
    async def run(self, contract, workspace, *, run_id=None):
        del contract
        (workspace / "app.js").write_text("const ready = true;\n", encoding="utf-8")
        return AgentResult(
            status=AgentStatus.COMPLETED,
            run_id=run_id or "run",
            outcome=CodingOutcome(summary="Fixed configured bug", changed_files=["app.js"]),
        )


class PhaseOneRemoteTransport:
    def __init__(self) -> None:
        self.staged = 0
        self.commands: list[str] = []

    async def stage(self, endpoint, files, *, incoming_dir, release_dir):
        del endpoint, incoming_dir, release_dir
        assert files and files[0][0].read_text(encoding="utf-8") == "ready"
        self.staged += 1

    async def run(self, endpoint, command):
        del endpoint
        self.commands.append(command)
        if command.startswith("frontend-current"):
            return "/srv/frontend/releases/previous"
        return "healthy"


class PhaseOneBrowserSession:
    async def capture(self, url, settings, evidence_dir):
        del settings
        evidence_dir.mkdir(parents=True, exist_ok=True)
        screenshot = evidence_dir / "page.png"
        screenshot.write_bytes(b"png")
        return BrowserCapture(
            ok=True,
            url=url,
            status_code=200,
            evidence_refs=[str(screenshot)],
        )


def deployment_settings(tmp_path: Path) -> SftpNfsDeploymentSettings:
    known_hosts = tmp_path / "known_hosts"
    key = tmp_path / "key"
    known_hosts.write_text("host key", encoding="utf-8")
    key.write_text("private key fixture", encoding="utf-8")
    endpoint = SshEndpoint(
        host="test.internal",
        username="delivery",
        known_hosts=known_hosts,
        private_key=key,
    )
    return SftpNfsDeploymentSettings(
        storage=endpoint,
        device=endpoint,
        remote_root="/srv/frontend",
        public_base_url="https://test.internal/builds",
        commands=DeviceCommandTemplates(
            current_release=RemoteCommandTemplate(program="frontend-current"),
            activate=RemoteCommandTemplate(program="frontend-activate", args=["{candidate}"]),
            health_check=RemoteCommandTemplate(program="frontend-health"),
            rollback=RemoteCommandTemplate(program="frontend-restore", args=["{previous}"]),
        ),
    )


@pytest.mark.asyncio
async def test_configured_phase_one_bug_completes_without_git_or_teambition_write(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    runtime = tmp_path / "runtime"
    remote = PhaseOneRemoteTransport()
    workflow = DeliveryWorkflow(
        agent=PhaseOneAgent(),
        workspace_manager=FakeWorkspaceManager(runtime / "worktrees"),
        change_inspector=FakeChangeInspector("a" * 64),
        artifact_builder=ControlledArtifactBuilder(runtime / "evidence" / "build"),
        test_publisher=SftpNfsTestEnvironmentPublisher(
            deployment_settings(tmp_path),
            runtime / "evidence" / "deployment",
            transport=remote,
        ),
        browser_verifier=PlaywrightBrowserVerifier(
            PlaywrightSettings(allowed_hosts={"test.internal"}),
            runtime / "evidence" / "browser",
            session=PhaseOneBrowserSession(),
        ),
        checkpoints=AtomicCheckpointStore(runtime / "checkpoints"),
        event_sink=JsonlEventSink(runtime / "events"),
        finalize_external_changes=False,
    )
    contract = TaskContract(
        task_id="configured-bug-1",
        objective="Fix and deliver a configured frontend bug",
        acceptance_criteria=["Built page is healthy in the test environment"],
        target_environment="test",
        build_commands=[
            BuildCommand(
                id="build",
                program="python",
                args=[
                    "-c",
                    "from pathlib import Path; Path('dist').mkdir(); "
                    "Path('dist/index.html').write_text('ready', encoding='utf-8')",
                ],
            )
        ],
        artifact_paths=["dist"],
    )

    waiting = await workflow.execute(contract, source, run_id="configured-phase-one")
    completed = await workflow.decide(
        waiting.run_id,
        ApprovalRecord(
            decision="approved",
            subject_hash=waiting.approval_subject_hash or "",
            approver="frontend-owner",
        ),
    )

    assert waiting.stage == "awaiting_approval"
    assert completed.stage == "completed"
    assert completed.rollback_status == "not_requested"
    assert len(completed.evidence_refs) >= 5
    assert all(Path(path).is_file() for path in completed.evidence_refs)
    assert remote.staged == 1
    assert any(command.startswith("frontend-activate") for command in remote.commands)
    checkpoint = await workflow.checkpoints.load(waiting.run_id)
    assert checkpoint is not None
    assert checkpoint["publish_receipt"] is None
