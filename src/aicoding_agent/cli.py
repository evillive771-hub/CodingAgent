from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from aicoding_agent.adapters.browser_tests import BrowserTestRunner
from aicoding_agent.adapters.build import ControlledArtifactBuilder
from aicoding_agent.adapters.deployment import DeploymentError, SftpNfsTestEnvironmentPublisher
from aicoding_agent.adapters.git import GitChangeInspector, LocalGitWorktreeManager
from aicoding_agent.adapters.playwright import PlaywrightBrowserVerifier
from aicoding_agent.agent.background import BackgroundJobStore, BackgroundTaskManager
from aicoding_agent.agent.control import ControlIntent, ControlStore, SteeringInbox
from aicoding_agent.agent.goal import ModelGoalEvaluator
from aicoding_agent.agent.hooks import AsyncHookBus
from aicoding_agent.agent.loop import AgentLoop
from aicoding_agent.agent.memory import MemoryError, MemoryManager, MemoryStore
from aicoding_agent.agent.mutation_journal import (
    MutationJournal,
    MutationRecoveryError,
)
from aicoding_agent.agent.observability import TelemetryRegistry
from aicoding_agent.agent.permission import (
    PermissionBroker,
    PermissionDecision,
    PermissionStore,
)
from aicoding_agent.agent.plan import PlanManager, PlanStore
from aicoding_agent.agent.subagent import ReadOnlySubagentManager
from aicoding_agent.agent.task_graph import TaskGraphManager, TaskGraphStore
from aicoding_agent.agent.team import (
    TeamLaunchManager,
    TeamMessageBus,
    TeamPlanApprovalManager,
    TeamProtocolError,
)
from aicoding_agent.chat.console import ChatEventSink
from aicoding_agent.chat.journal import SessionJournalError, SessionMessageJournal
from aicoding_agent.chat.session import ChatSession
from aicoding_agent.chat.store import ChatStoreError, ChatTranscriptStore
from aicoding_agent.config import ConfigurationError, ModelSettings
from aicoding_agent.context.archive import ContextEvidenceArchive
from aicoding_agent.context.budget import ContextBudgetConfig, ContextBudgetManager
from aicoding_agent.context.compaction import AgentContextCompactor, CompactionStore
from aicoding_agent.context.session_memory import (
    ConversationCompactionConfig,
    ConversationCompactor,
    ModelSessionCheckpointer,
    SessionMemoryStore,
)
from aicoding_agent.delivery_config import DeliveryConfigurationError, PhaseOneDeliveryConfig
from aicoding_agent.domain.models import TaskContract
from aicoding_agent.model import create_chat_model
from aicoding_agent.persistence.jsonl import AtomicCheckpointStore, JsonlEventSink
from aicoding_agent.regression.config import build_controller
from aicoding_agent.tools.base import ToolError
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor
from aicoding_agent.tools.file_lock import WorkspaceFileLockManager
from aicoding_agent.workflow.engine import DeliveryWorkflow, WorkflowError
from aicoding_agent.workflow.models import ApprovalRecord
from aicoding_agent.workflow.progress import SafeProgressEventSink

app = typer.Typer(
    no_args_is_help=True,
    help="Controlled AI coding and frontend delivery agent.",
)


def _load_contract(path: Path) -> TaskContract:
    try:
        return TaskContract.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise typer.BadParameter(f"Could not read task file: {exc}") from exc
    except ValueError as exc:
        raise typer.BadParameter(f"Invalid task contract: {exc}") from exc


def _context_budget(settings: ModelSettings) -> ContextBudgetManager:
    return ContextBudgetManager(
        ContextBudgetConfig(
            context_window_tokens=settings.context_window_tokens,
            max_output_tokens=settings.max_output_tokens,
            summary_output_tokens=min(settings.max_output_tokens, 20_000),
            compact_buffer_tokens=settings.context_compact_buffer_tokens,
            warning_buffer_tokens=settings.context_warning_buffer_tokens,
        )
    )


def _conversation_compaction_config(
    settings: ModelSettings,
) -> ConversationCompactionConfig:
    return ConversationCompactionConfig(
        min_recent_tokens=settings.session_memory_min_recent_tokens,
        max_recent_tokens=settings.session_memory_max_recent_tokens,
    )


@app.command()
def validate_task(
    task: Annotated[Path, typer.Option(exists=True, dir_okay=False, readable=True)],
) -> None:
    """Validate a task contract without calling a model or changing a workspace."""
    contract = _load_contract(task)
    typer.echo(
        json.dumps(
            {
                "valid": True,
                "task_id": contract.task_id,
                "source": contract.source,
                "verification_ids": [item.id for item in contract.verification_commands],
                "browser_verification_ids": [item.id for item in contract.browser_verifications],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command()
def doctor(
    env_file: Annotated[
        Path,
        typer.Option(exists=True, dir_okay=False, readable=True, help="Model environment file."),
    ] = Path("env.txt"),
    live: Annotated[
        bool,
        typer.Option(help="Make a small multi-turn Tool Calling request using the configured API."),
    ] = False,
) -> None:
    """Validate local configuration; optionally probe the configured model provider."""
    try:
        settings = ModelSettings.from_env_file(env_file)
    except ConfigurationError as exc:
        typer.echo(f"configuration: failed ({exc})", err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(f"configuration: ok; provider={settings.provider}; model={settings.model}")
    typer.echo("api_key: present and redacted")
    if not live:
        typer.echo("live_protocol_probe: skipped")
        return
    asyncio.run(_live_protocol_probe(settings))


async def _live_protocol_probe(settings: ModelSettings) -> None:
    model = create_chat_model(settings.model_copy(update={"max_output_tokens": 512}))
    tools = [
        {
            "type": "function",
            "function": {
                "name": "echo_probe",
                "description": "Return a supplied nonce for an API compatibility check.",
                "parameters": {
                    "type": "object",
                    "properties": {"nonce": {"type": "string"}},
                    "required": ["nonce"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "You are an API protocol test."},
        {
            "role": "user",
            "content": "Call echo_probe exactly once with nonce 'agent-probe-2026'.",
        },
    ]
    try:
        first = await model.complete(messages, tools)
        if len(first.tool_calls) != 1 or first.tool_calls[0].name != "echo_probe":
            raise RuntimeError("model did not return the requested echo_probe tool call")
        call = first.tool_calls[0]
        messages.append(
            AgentLoop._assistant_message(first.content, first.reasoning_content, [call])
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(
                    {"ok": True, "nonce": call.arguments.get("nonce")}, ensure_ascii=False
                ),
            }
        )
        messages.append({"role": "user", "content": "Reply with TOOL_LOOP_OK and no tool call."})
        second = await model.complete(messages, tools)
        if second.tool_calls:
            raise RuntimeError("model unexpectedly called another tool in the probe")
    except Exception as exc:
        typer.echo(f"live_protocol_probe: failed ({type(exc).__name__}: {exc})", err=True)
        raise typer.Exit(code=3) from exc
    finally:
        await model.aclose()
    typer.echo("live_protocol_probe: ok; multi-turn tool history accepted")


@app.command("run")
def run_agent(
    task: Annotated[Path, typer.Option(exists=True, dir_okay=False, readable=True)],
    workspace: Annotated[Path, typer.Option(exists=True, file_okay=False, readable=True)],
    env_file: Annotated[
        Path,
        typer.Option(exists=True, dir_okay=False, readable=True, help="Model environment file."),
    ] = Path("env.txt"),
    browser_suites: Annotated[
        Path | None,
        typer.Option(
            exists=True, dir_okay=False, readable=True, help="Trusted host browser suites JSON."
        ),
    ] = None,
    runtime_dir: Annotated[
        Path | None,
        typer.Option(help="Runtime evidence and logs; browser evidence must be external."),
    ] = None,
    regression_config: Annotated[
        Path | None,
        typer.Option(exists=True, dir_okay=False, readable=True, help="Host regression gate JSON."),
    ] = None,
) -> None:
    """Run the Coding Agent in a real workspace under a validated task contract."""
    contract = _load_contract(task)
    try:
        settings = ModelSettings.from_env_file(env_file)
    except ConfigurationError as exc:
        raise typer.BadParameter(str(exc), param_hint="--env-file") from exc
    try:
        result = asyncio.run(
            _run_agent(
                contract,
                workspace,
                settings,
                browser_suites=browser_suites,
                runtime_root=runtime_dir,
                regression_config=regression_config,
            )
        )
    except (ValueError, OSError, RuntimeError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--browser-suites") from exc
    typer.echo(result.model_dump_json(indent=2))
    if result.status != "completed":
        raise typer.Exit(code=1)


async def _run_agent(
    contract: TaskContract,
    workspace: Path,
    settings: ModelSettings,
    *,
    run_id: str | None = None,
    runtime_root: Path | None = None,
    progress_writer: Callable[[str], None] | None = None,
    browser_suites: Path | None = None,
    regression_config: Path | None = None,
    shared_budget: Any = None,
    proposal_only: bool = False,
    tdd_authorized: bool = False,
):
    runtime_root = runtime_root or (
        workspace.resolve().parent / ".aicoding-agent-runtime" / workspace.name
        if browser_suites is not None or regression_config is not None
        else workspace / ".aicoding-agent"
    )
    browser_runner = (
        await BrowserTestRunner.from_config(browser_suites, runtime_root / "evidence" / "browser")
        if browser_suites is not None
        else None
    )
    model = create_chat_model(settings)
    evaluator_model = create_chat_model(settings.model_copy(update={"max_output_tokens": 2048}))
    subagent_model = create_chat_model(settings)
    summary_model = create_chat_model(
        settings.model_copy(update={"max_output_tokens": settings.summary_max_output_tokens})
    )
    workspace_key = hashlib.sha256(str(workspace.resolve()).casefold().encode()).hexdigest()[:16]
    persistent_events = JsonlEventSink(runtime_root / "runs")
    event_sink = (
        SafeProgressEventSink(persistent_events, writer=progress_writer)
        if progress_writer is not None
        else persistent_events
    )
    hook_bus = AsyncHookBus()
    background = BackgroundTaskManager(store=BackgroundJobStore(runtime_root / "background"))
    mutation_journal = MutationJournal(runtime_root / "mutations")
    memory_manager = MemoryManager(store=MemoryStore(runtime_root / "memory" / workspace_key))
    team_launch_manager = TeamLaunchManager(runtime_root / "teams")
    executor = ToolExecutor(
        build_builtin_registry(),
        hook_bus=hook_bus,
        mutation_journal=mutation_journal,
    )
    subagent_manager = ReadOnlySubagentManager(model=subagent_model, executor=executor)
    context_budget = _context_budget(settings)
    conversation_compactor = ConversationCompactor(
        summarizer=ModelSessionCheckpointer(summary_model),
        budget=context_budget,
        store=SessionMemoryStore(runtime_root / "session-memory"),
        config=_conversation_compaction_config(settings),
        auto_enabled=settings.auto_compact,
    )
    context_archive = ContextEvidenceArchive(runtime_root / "evidence")
    context_compactor = AgentContextCompactor(
        conversation_compactor=conversation_compactor,
        budget=context_budget,
        archive=context_archive,
        store=CompactionStore(runtime_root / "compactions"),
    )
    agent = AgentLoop(
        model=model,
        executor=executor,
        event_sink=event_sink,
        hook_bus=hook_bus,
        context_archive=context_archive,
        context_compactor=context_compactor,
        background_manager=background,
        goal_evaluator=ModelGoalEvaluator(evaluator_model),
        subagent_manager=subagent_manager,
        memory_manager=memory_manager,
        team_launch_manager=team_launch_manager,
        browser_runner=browser_runner,
    )
    try:
        if proposal_only:
            return await agent.propose_tests(
                contract, workspace, run_id=run_id, shared_budget=shared_budget
            )
        if regression_config is not None:
            controller = await build_controller(
                regression_config, runtime_root, browser_runner=browser_runner
            )
            outcome = await controller.run(
                contract, workspace, run_id=run_id or uuid4().hex, agent=agent, adopt=True
            )
            return outcome.agent_result
        return await agent.run(contract, workspace, run_id=run_id, shared_budget=shared_budget,
                               tdd_authorized=tdd_authorized)
    finally:
        await model.aclose()
        await evaluator_model.aclose()
        await subagent_model.aclose()
        await summary_model.aclose()


class _ConfiguredCodingAgent:
    def __init__(
        self,
        settings: ModelSettings,
        runtime_root: Path,
        *,
        progress_writer: Callable[[str], None] | None = None,
        browser_suites: Path | None = None,
    ) -> None:
        self.settings = settings
        self.runtime_root = runtime_root
        self.progress_writer = progress_writer
        self.browser_suites = browser_suites

    async def propose_tests(
        self,
        contract: TaskContract,
        workspace: Path,
        *,
        run_id: str | None = None,
        shared_budget: Any = None,
    ):
        return await _run_agent(
            contract, workspace, self.settings, run_id=run_id,
            runtime_root=self.runtime_root, progress_writer=self.progress_writer,
            browser_suites=self.browser_suites, shared_budget=shared_budget,
            proposal_only=True,
        )

    async def run(
        self,
        contract: TaskContract,
        workspace: Path,
        *,
        run_id: str | None = None,
        shared_budget: Any = None,
        tdd_authorized: bool = False,
    ):
        return await _run_agent(
            contract,
            workspace,
            self.settings,
            run_id=run_id,
            runtime_root=self.runtime_root,
            progress_writer=self.progress_writer,
            browser_suites=self.browser_suites,
            shared_budget=shared_budget,
            tdd_authorized=tdd_authorized,
        )


@app.command("deliver-phase-one")
def deliver_phase_one(
    task: Annotated[Path, typer.Option(exists=True, dir_okay=False, readable=True)],
    repository: Annotated[Path, typer.Option(exists=True, file_okay=False, readable=True)],
    delivery_config: Annotated[Path, typer.Option(exists=True, dir_okay=False, readable=True)],
    env_file: Annotated[
        Path,
        typer.Option(exists=True, dir_okay=False, readable=True, help="Model environment file."),
    ] = Path("env.txt"),
    run_id: Annotated[str | None, typer.Option(help="Stable workflow run identifier.")] = None,
    resume: Annotated[
        bool, typer.Option(help="Resume an existing run; requires --run-id.")
    ] = False,
    regression_config: Annotated[
        Path | None,
        typer.Option(exists=True, dir_okay=False, readable=True, help="Host regression gate JSON."),
    ] = None,
    browser_suites: Annotated[
        Path | None,
        typer.Option(
            exists=True, dir_okay=False, readable=True, help="Trusted host browser suites JSON."
        ),
    ] = None,
) -> None:
    """Run build, test deployment, Playwright evidence, and human approval without Git/TB writes."""
    contract = _load_contract(task)
    if resume and run_id is None:
        raise typer.BadParameter("--resume requires --run-id", param_hint="--resume")
    if contract.target_environment != "test":
        raise typer.BadParameter(
            "Phase one requires target_environment='test'", param_hint="--task"
        )
    missing_sections = [
        name
        for name, values in (
            ("verification_commands", contract.verification_commands),
            ("build_commands", contract.build_commands),
            ("artifact_paths", contract.artifact_paths),
        )
        if not values
    ]
    if not contract.repository_url:
        missing_sections.append("repository_url")
    if missing_sections:
        raise typer.BadParameter(
            "Phase one task requires non-empty " + ", ".join(missing_sections),
            param_hint="--task",
        )
    try:
        model_settings = ModelSettings.from_env_file(env_file)
        delivery_settings = PhaseOneDeliveryConfig.from_json_file(delivery_config)
    except (ConfigurationError, DeliveryConfigurationError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    runtime_root = delivery_settings.runtime_root.resolve()
    repository_root = repository.resolve()
    if runtime_root == repository_root or repository_root in runtime_root.parents:
        raise typer.BadParameter(
            "runtime_root must be outside the source repository", param_hint="--delivery-config"
        )
    try:
        result = asyncio.run(
            _deliver_phase_one_interactive(
                contract,
                repository_root,
                model_settings,
                delivery_settings,
                run_id=run_id,
                resume=resume,
                browser_suites=browser_suites,
                regression_config=regression_config,
            )
        )
    except (ToolError, WorkflowError) as exc:
        typer.echo(f"phase_one_delivery: failed ({exc})", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(result.model_dump_json(indent=2))
    if result.stage != "completed":
        raise typer.Exit(code=1)


async def _deliver_phase_one_interactive(
    contract: TaskContract,
    repository: Path,
    model_settings: ModelSettings,
    delivery_settings: PhaseOneDeliveryConfig,
    *,
    run_id: str | None,
    resume: bool,
    browser_suites: Path | None = None,
    regression_config: Path | None = None,
):
    runtime = delivery_settings.runtime_root
    runtime.mkdir(parents=True, exist_ok=True)
    effective_run_id = run_id or uuid4().hex
    publisher = SftpNfsTestEnvironmentPublisher(
        delivery_settings.deployment,
        runtime / "evidence" / "deployment",
    )
    regression_browser_runner = (
        await BrowserTestRunner.from_config(
            browser_suites, runtime / "evidence" / "regression-browser"
        )
        if browser_suites is not None
        else None
    )
    regression_controller = (
        await build_controller(regression_config, runtime, browser_runner=regression_browser_runner)
        if regression_config is not None
        else None
    )
    workflow = DeliveryWorkflow(
        agent=_ConfiguredCodingAgent(
            model_settings,
            runtime / "agent",
            progress_writer=typer.echo,
            browser_suites=browser_suites,
        ),
        workspace_manager=LocalGitWorktreeManager(runtime / "worktrees"),
        change_inspector=GitChangeInspector(),
        artifact_builder=ControlledArtifactBuilder(runtime / "evidence" / "build"),
        test_publisher=publisher,
        browser_verifier=PlaywrightBrowserVerifier(
            delivery_settings.browser,
            runtime / "evidence" / "browser",
        ),
        checkpoints=AtomicCheckpointStore(runtime / "checkpoints"),
        event_sink=SafeProgressEventSink(
            JsonlEventSink(runtime / "workflow-events"),
            writer=typer.echo,
        ),
        finalize_external_changes=False,
        regression_controller=regression_controller,
    )
    device_lock_target = runtime / "device-leases" / f"{publisher.device_id}.lock"
    device_locks = WorkspaceFileLockManager(timeout_seconds=10)
    async with device_locks.acquire(
        runtime,
        device_lock_target,
        owner=f"delivery:{effective_run_id}",
    ):
        typer.echo(f"workflow_run_id={effective_run_id}")
        waiting = (
            await workflow.resume(effective_run_id)
            if resume
            else await workflow.execute(contract, repository, run_id=effective_run_id)
        )
        if waiting.stage != "awaiting_approval":
            return waiting
        checkpoint = await workflow.checkpoints.load(waiting.run_id)
        if checkpoint is None:
            raise RuntimeError("Workflow checkpoint disappeared before approval")
        typer.secho("\nApproval package (Git and Teambition will not be changed):", bold=True)
        typer.echo(json.dumps(checkpoint["approval_package"], ensure_ascii=False, indent=2))
        decision = typer.prompt("Decision [approve/reject]", default="reject").strip().lower()
        if decision not in {"approve", "reject"}:
            raise typer.BadParameter("Decision must be approve or reject")
        approver = typer.prompt("Approver").strip()
        comment = typer.prompt("Comment", default="").strip()
        approval = ApprovalRecord(
            decision="approved" if decision == "approve" else "rejected",
            subject_hash=waiting.approval_subject_hash or "",
            approver=approver,
            comment=comment,
        )
        return await workflow.decide(waiting.run_id, approval)


@app.command("delivery-device-status")
def delivery_device_status(
    delivery_config: Annotated[
        Path,
        typer.Option(exists=True, dir_okay=False, readable=True),
    ],
) -> None:
    """Show whether a phase-one test device is available or isolated."""
    try:
        settings = PhaseOneDeliveryConfig.from_json_file(delivery_config)
        publisher = SftpNfsTestEnvironmentPublisher(
            settings.deployment,
            settings.runtime_root / "evidence" / "deployment",
        )
        status = publisher.isolation_status()
    except (DeliveryConfigurationError, DeploymentError) as exc:
        typer.echo(f"device_status: failed ({exc})", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(status.model_dump_json(indent=2))
    if status.isolated:
        raise typer.Exit(code=1)


@app.command("delivery-device-recover")
def delivery_device_recover(
    delivery_config: Annotated[
        Path,
        typer.Option(exists=True, dir_okay=False, readable=True),
    ],
    approver: Annotated[str, typer.Option(help="Human approving device recovery.")],
    comment: Annotated[str, typer.Option(help="Reason and recovery action taken.")],
) -> None:
    """Clear device isolation only after a live SSH health check and human attribution."""
    try:
        settings = PhaseOneDeliveryConfig.from_json_file(delivery_config)
        status = asyncio.run(_recover_delivery_device(settings, approver, comment))
    except (DeliveryConfigurationError, DeploymentError, ToolError) as exc:
        typer.echo(f"device_recovery: failed ({exc})", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(status.model_dump_json(indent=2))


async def _recover_delivery_device(
    settings: PhaseOneDeliveryConfig,
    approver: str,
    comment: str,
):
    runtime = settings.runtime_root.resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    publisher = SftpNfsTestEnvironmentPublisher(
        settings.deployment,
        runtime / "evidence" / "deployment",
    )
    lock_target = runtime / "device-leases" / f"{publisher.device_id}.lock"
    locks = WorkspaceFileLockManager(timeout_seconds=10)
    async with locks.acquire(
        runtime,
        lock_target,
        owner=f"device-recovery:{approver[:64]}",
    ):
        return await publisher.clear_isolation(approved_by=approver, comment=comment)


@app.command("chat")
def chat_agent(
    workspace: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, readable=True, help="Coding workspace."),
    ] = Path("."),
    profile: Annotated[
        Path | None,
        typer.Option(
            exists=True,
            dir_okay=False,
            readable=True,
            help="Task policy profile; defaults to <workspace>/.aicoding-agent.json.",
        ),
    ] = None,
    env_file: Annotated[
        Path,
        typer.Option(exists=True, dir_okay=False, readable=True, help="Model environment file."),
    ] = Path("env.txt"),
    session_id: Annotated[
        str,
        typer.Option("--session", help="Conversation name used to resume prior turns."),
    ] = "default",
    runtime_dir: Annotated[
        Path | None,
        typer.Option(
            help=(
                "Runtime logs and transcripts; defaults to the protected "
                "<workspace>/.aicoding-agent directory."
            )
        ),
    ] = None,
    browser_suites: Annotated[
        Path | None,
        typer.Option(
            exists=True, dir_okay=False, readable=True, help="Trusted host browser suites JSON."
        ),
    ] = None,
    regression_config: Annotated[
        Path | None,
        typer.Option(exists=True, dir_okay=False, readable=True, help="Host regression gate JSON."),
    ] = None,
) -> None:
    """Start a persistent Claude Code-style conversational coding session."""
    _configure_console_output()
    resolved_workspace = workspace.resolve()
    selected_profile = profile or (resolved_workspace / ".aicoding-agent.json")
    if not selected_profile.is_file():
        raise typer.BadParameter(
            "No chat profile found; provide --profile or add .aicoding-agent.json to the workspace",
            param_hint="--profile",
        )
    contract = _load_contract(selected_profile)
    default_runtime = (
        resolved_workspace.parent / ".aicoding-agent-runtime" / resolved_workspace.name
        if browser_suites is not None or regression_config is not None
        else resolved_workspace / ".aicoding-agent"
    )
    selected_runtime = (runtime_dir or default_runtime).resolve()
    if selected_runtime.is_relative_to(resolved_workspace):
        relative_runtime = selected_runtime.relative_to(resolved_workspace)
        if not relative_runtime.parts or relative_runtime.parts[0] != ".aicoding-agent":
            raise typer.BadParameter(
                "Runtime data inside the workspace must be under .aicoding-agent",
                param_hint="--runtime-dir",
            )
    try:
        settings = ModelSettings.from_env_file(env_file)
    except ConfigurationError as exc:
        raise typer.BadParameter(str(exc), param_hint="--env-file") from exc
    try:
        asyncio.run(
            _chat_loop(
                contract=contract,
                workspace=resolved_workspace,
                settings=settings,
                session_id=session_id,
                runtime_dir=selected_runtime,
                browser_suites=browser_suites,
                regression_config=regression_config,
            )
        )
    except (ValueError, ChatStoreError) as exc:
        typer.echo(f"chat: failed ({exc})", err=True)
        raise typer.Exit(code=2) from exc


def _configure_console_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="replace")


async def _chat_loop(
    *,
    contract: TaskContract,
    workspace: Path,
    settings: ModelSettings,
    session_id: str,
    runtime_dir: Path,
    browser_suites: Path | None = None,
    regression_config: Path | None = None,
) -> None:
    workspace_key = hashlib.sha256(str(workspace).casefold().encode()).hexdigest()[:16]
    runtime_root = runtime_dir / "chat" / workspace_key
    browser_runner = (
        await BrowserTestRunner.from_config(browser_suites, runtime_root / "evidence" / "browser")
        if browser_suites is not None
        else None
    )
    persistent_events = JsonlEventSink(runtime_root / "events")
    telemetry = TelemetryRegistry()
    event_sink = ChatEventSink(
        persistent_events,
        writer=typer.echo,
        telemetry=telemetry,
    )
    model = create_chat_model(settings)
    evaluator_model = create_chat_model(settings.model_copy(update={"max_output_tokens": 2048}))
    subagent_model = create_chat_model(settings)
    summary_model = create_chat_model(
        settings.model_copy(update={"max_output_tokens": settings.summary_max_output_tokens})
    )
    hook_bus = AsyncHookBus()
    permission_broker = PermissionBroker(
        store=PermissionStore(runtime_root / "sessions"),
        scope_id=session_id,
    )
    mutation_journal = MutationJournal(runtime_root / "mutations")
    memory_manager = MemoryManager(store=MemoryStore(runtime_dir / "memory" / workspace_key))
    team_launch_manager = TeamLaunchManager(runtime_root / "teams")
    team_message_bus = TeamMessageBus(runtime_root / "teams")
    team_plan_approval_manager = TeamPlanApprovalManager(
        runtime_root / "teams",
        message_bus=team_message_bus,
    )
    executor = ToolExecutor(
        build_builtin_registry(),
        hook_bus=hook_bus,
        permission_authorizer=permission_broker,
        mutation_journal=mutation_journal,
    )
    subagent_manager = ReadOnlySubagentManager(model=subagent_model, executor=executor)
    context_budget = _context_budget(settings)
    session_memory_store = SessionMemoryStore(runtime_root / "session-memory")
    conversation_compactor = ConversationCompactor(
        summarizer=ModelSessionCheckpointer(summary_model),
        budget=context_budget,
        store=session_memory_store,
        config=_conversation_compaction_config(settings),
        auto_enabled=settings.auto_compact,
    )
    context_archive = ContextEvidenceArchive(runtime_root / "evidence")
    context_compactor = AgentContextCompactor(
        conversation_compactor=conversation_compactor,
        budget=context_budget,
        archive=context_archive,
        store=CompactionStore(runtime_root / "compactions"),
    )
    message_journal = SessionMessageJournal(runtime_root / "sessions")
    plan_manager = PlanManager(store=PlanStore(runtime_root / "sessions"), scope_id=session_id)
    task_graph_manager = TaskGraphManager(
        store=TaskGraphStore(runtime_root / "sessions"),
        scope_id=session_id,
    )
    control = SteeringInbox(store=ControlStore(runtime_root / "sessions"), scope_id=session_id)
    background = BackgroundTaskManager(
        store=BackgroundJobStore(runtime_root / "sessions" / session_id / "background")
    )
    agent = AgentLoop(
        model=model,
        executor=executor,
        event_sink=event_sink,
        hook_bus=hook_bus,
        context_archive=context_archive,
        context_compactor=context_compactor,
        goal_evaluator=ModelGoalEvaluator(evaluator_model),
        subagent_manager=subagent_manager,
        memory_manager=memory_manager,
        team_launch_manager=team_launch_manager,
        browser_runner=browser_runner,
    )
    regression_controller = (
        await build_controller(regression_config, runtime_root, browser_runner=browser_runner)
        if regression_config is not None
        else None
    )
    session = ChatSession(
        agent=agent,
        workspace=workspace,
        profile=contract,
        store=ChatTranscriptStore(runtime_root / "sessions"),
        session_id=session_id,
        plan_manager=plan_manager,
        task_graph_manager=task_graph_manager,
        control=control,
        background_manager=background,
        mutation_journal=mutation_journal,
        memory_manager=memory_manager,
        conversation_compactor=conversation_compactor,
        message_journal=message_journal,
        team_launch_manager=team_launch_manager,
        team_plan_approval_manager=team_plan_approval_manager,
        team_message_bus=team_message_bus,
        regression_controller=regression_controller,
    )
    try:
        await session.initialize()
        typer.secho("AI Coding Agent Chat", fg=typer.colors.CYAN, bold=True)
        typer.echo(f"workspace: {workspace}")
        typer.echo(f"session: {session_id} ({len(session.history)} previous turn(s))")
        typer.echo(
            "Commands: /plan /tasks /new-context [focus] /context /resume /steer <message> "
            "/next <message> /interrupt /permissions "
            "/allow <id> [session] /deny <id> /fork <session> [turn] "
            "/rewind <session> <turn> /memory /remember <topic> <days> <text> "
            "/memory-approve <id> /memory-reject <id> /metrics /status /history /exit\n"
            "Team: /team <team-id> /team-approve <team-id> <proposal-id> "
            "/team-cancel <team-id> <proposal-id> [feedback]\n"
            "Plans: /team-plan <team-id> <worker> /team-plan-approve "
            "<team-id> <worker> <request-id> [feedback] /team-plan-reject ...\n"
            "Runtime: /team-send <team-id> <worker> <message> "
            "/team-shutdown <team-id> <worker> /task-assign <task-id> <worker> "
            "/task-retry <task-id> [worker]\n"
        )
        recoverable = await session.recoverable_run()
        if recoverable is not None:
            if settings.auto_resume == "safe" and recoverable.safe_to_resume:
                typer.echo(f"[resume] continuing interrupted run {recoverable.run_id}")
                recovered_turn = await session.resume()
                typer.secho("\nAgent >", fg=typer.colors.GREEN, bold=True)
                typer.echo(recovered_turn.assistant_message)
                typer.echo()
            elif settings.auto_resume != "disabled":
                reason = (
                    "safe to resume"
                    if recoverable.safe_to_resume
                    else "unresolved tool execution requires inspection"
                )
                typer.echo(
                    f"[resume] interrupted run {recoverable.run_id} detected ({reason}); "
                    "use /resume when appropriate"
                )
        prompt_session: PromptSession[str] = PromptSession()
        with patch_stdout(raw=True):
            await _interactive_chat(
                prompt_session,
                session,
                workspace,
                session_id,
                permission_broker,
                telemetry,
            )
    finally:
        await model.aclose()
        await evaluator_model.aclose()
        await subagent_model.aclose()
        await summary_model.aclose()


async def _interactive_chat(
    prompt_session: PromptSession[str],
    session: ChatSession,
    workspace: Path,
    session_id: str,
    permission_broker: PermissionBroker,
    telemetry: TelemetryRegistry,
) -> None:
    active: asyncio.Task[Any] | None = None
    exit_after_active = False
    while True:
        if active is None:
            queued = await session.control.consume_next()
            if queued is not None:
                typer.echo(f"[next] {queued.content}")
                active = asyncio.create_task(session.send(queued.content))
                continue
            try:
                query = await prompt_session.prompt_async("You > ")
            except (EOFError, KeyboardInterrupt):
                return
            action = await _handle_chat_input(
                query,
                session=session,
                workspace=workspace,
                session_id=session_id,
                busy=False,
                permission_broker=permission_broker,
                telemetry=telemetry,
            )
            if action == "exit":
                return
            if action.startswith("start:"):
                active = asyncio.create_task(session.send(action.removeprefix("start:")))
            continue

        prompt_task: asyncio.Task[str] | None = None
        if not exit_after_active:
            prompt_task = asyncio.create_task(
                prompt_session.prompt_async("Busy (/steer, /next, /interrupt, /allow, /deny) > ")
            )
        waiters: set[asyncio.Task[Any]] = {active}
        if prompt_task is not None:
            waiters.add(prompt_task)
        done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        if active in done:
            if prompt_task is not None and not prompt_task.done():
                prompt_task.cancel()
                await asyncio.gather(prompt_task, return_exceptions=True)
            try:
                turn = active.result()
            except (SessionJournalError, ValueError) as exc:
                typer.echo(f"[session] turn could not start: {exc}")
                active = None
                if exit_after_active:
                    return
                continue
            typer.secho("\nAgent >", fg=typer.colors.GREEN, bold=True)
            typer.echo(turn.assistant_message)
            if turn.status not in {"completed", "waiting_for_user"}:
                typer.echo(f"[turn status: {turn.status}; reason: {turn.reason or 'unknown'}]")
            typer.echo()
            active = None
            if exit_after_active:
                return
            continue

        if prompt_task is None:
            continue
        try:
            query = prompt_task.result()
        except (EOFError, KeyboardInterrupt):
            await session.control.submit(ControlIntent.INTERRUPT)
            exit_after_active = True
            continue
        action = await _handle_chat_input(
            query,
            session=session,
            workspace=workspace,
            session_id=session_id,
            busy=True,
            permission_broker=permission_broker,
            telemetry=telemetry,
        )
        if action == "exit":
            await session.control.submit(ControlIntent.INTERRUPT)
            exit_after_active = True


async def _handle_chat_input(
    query: str,
    *,
    session: ChatSession,
    workspace: Path,
    session_id: str,
    busy: bool,
    permission_broker: PermissionBroker | None = None,
    telemetry: TelemetryRegistry | None = None,
) -> str:
    text = query.strip()
    lowered = text.lower()
    if not text:
        return "none"
    if lowered in {"/exit", "/quit", "exit", "quit"}:
        return "exit"
    if lowered == "/help":
        typer.echo(
            "Natural text starts a turn; while busy it steers the current turn. "
            "/next queues a later turn and /interrupt stops at the next safe boundary. "
            "Policy ASK requests use /allow <id> [session] or /deny <id>. "
            "/fork creates an independent conversation branch; /rewind also restores "
            "journaled file changes after the selected turn. "
            "/memory lists curated memory; /remember adds an explicitly approved entry, "
            "and Agent proposals require /memory-approve. "
            "/new-context rotates old messages into History and writes Notes; /context shows "
            "its token state, and /resume "
            "continues a safely journaled interrupted run. "
            "/metrics shows the current or most recent run counters."
            " Team proposals require /team-approve before a Coordinator can start."
        )
        return "none"
    if lowered == "/plan":
        typer.echo(session.plan_manager.render())
        return "none"
    if lowered == "/tasks":
        typer.echo(session.task_graph_manager.render())
        return "none"
    if lowered.startswith("/team "):
        team_id = text[len("/team ") :].strip()
        try:
            proposal = await session.team_status(team_id)
        except (ValueError, TeamProtocolError) as exc:
            typer.echo(f"[team] status failed: {exc}")
            return "none"
        if proposal is None:
            typer.echo("<no team proposal>")
        else:
            typer.echo(proposal.model_dump_json(indent=2))
        return "none"
    if lowered.startswith("/team-approve "):
        if busy:
            typer.echo("[team] approval requires an idle Lead Agent")
            return "none"
        parts = text.split(maxsplit=2)
        if len(parts) != 3:
            typer.echo("usage: /team-approve <team-id> <proposal-id>")
            return "none"
        try:
            proposal = await session.approve_team(parts[1], parts[2])
        except (ValueError, TeamProtocolError) as exc:
            typer.echo(f"[team] approval failed: {exc}")
            return "none"
        typer.echo(f"[team] approved {proposal.team_id} proposal {proposal.proposal_id}")
        return "none"
    if lowered.startswith("/team-cancel "):
        if busy:
            typer.echo("[team] cancellation requires an idle Lead Agent")
            return "none"
        parts = text.split(maxsplit=3)
        if len(parts) < 3:
            typer.echo("usage: /team-cancel <team-id> <proposal-id> [feedback]")
            return "none"
        try:
            proposal = await session.cancel_team(
                parts[1],
                parts[2],
                feedback=parts[3] if len(parts) == 4 else "",
            )
        except (ValueError, TeamProtocolError) as exc:
            typer.echo(f"[team] cancellation failed: {exc}")
            return "none"
        typer.echo(f"[team] cancelled {proposal.team_id} proposal {proposal.proposal_id}")
        return "none"
    if lowered.startswith("/team-plan "):
        parts = text.split(maxsplit=2)
        if len(parts) != 3:
            typer.echo("usage: /team-plan <team-id> <worker>")
            return "none"
        try:
            approval = await session.team_plan_status(parts[1], parts[2])
        except (ValueError, TeamProtocolError) as exc:
            typer.echo(f"[team] plan status failed: {exc}")
            return "none"
        typer.echo(approval.model_dump_json(indent=2) if approval else "<no plan request>")
        return "none"
    for prefix, approve in (
        ("/team-plan-approve ", True),
        ("/team-plan-reject ", False),
    ):
        if lowered.startswith(prefix):
            if busy:
                typer.echo("[team] plan decisions require an idle Lead Agent")
                return "none"
            parts = text.split(maxsplit=4)
            if len(parts) < 4:
                typer.echo(f"usage: {prefix.strip()} <team-id> <worker> <request-id> [feedback]")
                return "none"
            try:
                approval = await session.respond_team_plan(
                    team_id=parts[1],
                    worker=parts[2],
                    request_id=parts[3],
                    approve=approve,
                    feedback=parts[4] if len(parts) == 5 else "",
                )
            except (ValueError, TeamProtocolError) as exc:
                typer.echo(f"[team] plan decision failed: {exc}")
                return "none"
            typer.echo(f"[team] plan {approval.status} for {approval.worker}/{approval.task_id}")
            return "none"
    if lowered.startswith("/team-send "):
        parts = text.split(maxsplit=3)
        if len(parts) != 4:
            typer.echo("usage: /team-send <team-id> <worker> <message>")
            return "none"
        try:
            message = await session.send_team_message(
                team_id=parts[1],
                worker=parts[2],
                content=parts[3],
            )
        except (ValueError, TeamProtocolError) as exc:
            typer.echo(f"[team] send failed: {exc}")
            return "none"
        typer.echo(f"[team] queued direct message {message.message_id}")
        return "none"
    if lowered.startswith("/team-shutdown "):
        parts = text.split(maxsplit=2)
        if len(parts) != 3:
            typer.echo("usage: /team-shutdown <team-id> <worker>")
            return "none"
        try:
            message = await session.request_team_shutdown(team_id=parts[1], worker=parts[2])
        except (ValueError, TeamProtocolError) as exc:
            typer.echo(f"[team] shutdown request failed: {exc}")
            return "none"
        typer.echo(f"[team] queued shutdown request {message.request_id}")
        return "none"
    if lowered.startswith("/task-assign "):
        parts = text.split(maxsplit=2)
        if len(parts) != 3:
            typer.echo("usage: /task-assign <task-id> <worker>")
            return "none"
        try:
            task = await session.task_graph_manager.assign_pending(parts[1], parts[2])
        except ValueError as exc:
            typer.echo(f"[task] assignment failed: {exc}")
            return "none"
        typer.echo(f"[task] {task.task_id} assigned to {task.preferred_owner}")
        return "none"
    if lowered.startswith("/task-retry "):
        parts = text.split(maxsplit=2)
        if len(parts) < 2:
            typer.echo("usage: /task-retry <task-id> [worker]")
            return "none"
        try:
            task = await session.task_graph_manager.retry_blocked(
                parts[1],
                owner=parts[2] if len(parts) == 3 else None,
            )
        except ValueError as exc:
            typer.echo(f"[task] retry failed: {exc}")
            return "none"
        typer.echo(
            f"[task] {task.task_id} requeued"
            + (f" for {task.preferred_owner}" if task.preferred_owner else "")
        )
        return "none"
    if lowered == "/status":
        typer.echo(
            f"workspace={workspace}; session={session_id}; turns={len(session.history)}; "
            f"busy={busy}\n{session.background_manager.render()}"
        )
        if permission_broker is not None:
            typer.echo(permission_broker.render())
        return "none"
    if lowered == "/permissions":
        typer.echo(
            permission_broker.render()
            if permission_broker is not None
            else "<interactive permissions unavailable>"
        )
        return "none"
    if lowered == "/metrics":
        typer.echo(telemetry.render_latest() if telemetry is not None else "<no Agent metrics>")
        return "none"
    if lowered == "/context":
        typer.echo(await session.context_status())
        return "none"
    if lowered in {"/resume", "/resume --force", "/resume --discard"}:
        if busy:
            typer.echo("[resume] /resume requires an idle Agent")
            return "none"
        try:
            if lowered == "/resume --discard":
                turn = await session.discard_recovery()
            else:
                turn = await session.resume(allow_ambiguous=lowered == "/resume --force")
        except SessionJournalError as exc:
            typer.echo(f"[resume] failed: {exc}")
            return "none"
        typer.secho("Agent >", fg=typer.colors.GREEN, bold=True)
        typer.echo(turn.assistant_message)
        return "none"
    is_new_context = lowered == "/new-context" or lowered.startswith("/new-context ")
    is_compact_alias = lowered == "/compact" or lowered.startswith("/compact ")
    if is_new_context or is_compact_alias:
        if busy:
            typer.echo("[context] /new-context requires an idle Agent")
            return "none"
        command = "/new-context" if is_new_context else "/compact"
        focus = text[len(command) :].strip()
        full = False
        if focus.lower() == "--full" or focus.lower().startswith("--full "):
            full = True
            focus = focus[len("--full") :].strip()
        try:
            context = await session.new_context(focus, full=full)
        except ValueError as exc:
            typer.echo(f"[context] rollover failed: {exc}")
            return "none"
        if context.error:
            typer.echo(f"[context] rollover fallback: {context.error}")
        else:
            typer.echo(
                f"[context] rolled_over={context.compacted}; "
                f"notes=v{context.snapshot_version or 0}; "
                f"history_window={context.history_window_id or 'none'}; "
                f"strategy={context.strategy or 'none'}; "
                f"tokens={context.raw_tokens}->{context.context_tokens}"
            )
        return "none"
    if lowered == "/memory":
        typer.echo(
            session.memory_manager.render()
            if session.memory_manager is not None
            else "<curated memory unavailable>"
        )
        return "none"
    if lowered.startswith("/remember"):
        if session.memory_manager is None:
            typer.echo("[memory] curated memory is unavailable")
            return "none"
        parts = text.split(maxsplit=3)
        if len(parts) != 4:
            typer.echo("usage: /remember <topic> <ttl-days> <content>")
            return "none"
        try:
            entry = await session.memory_manager.add_user_memory(
                topic=parts[1],
                ttl_days=int(parts[2]),
                content=parts[3],
            )
        except (ValueError, MemoryError) as exc:
            typer.echo(f"[memory] add failed: {exc}")
            return "none"
        typer.echo(f"[memory] approved {entry.memory_id[:8]} {entry.topic} v{entry.version}")
        return "none"
    for prefix, approve in (("/memory-approve ", True), ("/memory-reject ", False)):
        if lowered.startswith(prefix):
            if session.memory_manager is None:
                typer.echo("[memory] curated memory is unavailable")
                return "none"
            memory_id = text[len(prefix) :].strip()
            try:
                entry = (
                    await session.memory_manager.approve(memory_id)
                    if approve
                    else await session.memory_manager.reject(memory_id)
                )
            except MemoryError as exc:
                typer.echo(f"[memory] decision failed: {exc}")
                return "none"
            typer.echo(f"[memory] {entry.status.value} {entry.memory_id[:8]}")
            return "none"
    if lowered.startswith("/allow "):
        if permission_broker is None:
            typer.echo("[permission] interactive permissions are unavailable")
            return "none"
        parts = text.split()
        if len(parts) not in {2, 3} or (len(parts) == 3 and parts[2].lower() != "session"):
            typer.echo("usage: /allow <request-id> [session]")
            return "none"
        decision = (
            PermissionDecision.APPROVE_SESSION
            if len(parts) == 3
            else PermissionDecision.APPROVE_ONCE
        )
        try:
            await permission_broker.resolve(parts[1], decision)
        except ValueError as exc:
            typer.echo(f"[permission] {exc}")
        return "none"
    if lowered.startswith("/deny "):
        if permission_broker is None:
            typer.echo("[permission] interactive permissions are unavailable")
            return "none"
        parts = text.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            typer.echo("usage: /deny <request-id>")
            return "none"
        try:
            await permission_broker.resolve(parts[1].strip(), PermissionDecision.DENY)
        except ValueError as exc:
            typer.echo(f"[permission] {exc}")
        return "none"
    if lowered == "/history":
        if not session.history:
            typer.echo("<empty conversation>")
        for turn in session.history:
            typer.echo(f"[{turn.turn_number}] You: {turn.user_message[:200]}")
            typer.echo(f"[{turn.turn_number}] Agent: {turn.assistant_message[:300]}")
        return "none"
    if lowered.startswith("/fork"):
        if busy:
            typer.echo("[session] /fork requires an idle Agent")
            return "none"
        parts = text.split()
        if len(parts) not in {2, 3}:
            typer.echo("usage: /fork <new-session> [through-turn]")
            return "none"
        try:
            through_turn = int(parts[2]) if len(parts) == 3 else None
            cloned = await session.fork(parts[1], through_turn=through_turn)
        except (ValueError, ChatStoreError) as exc:
            typer.echo(f"[session] fork failed: {exc}")
            return "none"
        typer.echo(
            f"[session] forked {len(cloned)} turn(s) to {parts[1]}; "
            f"resume with --session {parts[1]}"
        )
        return "none"
    if lowered.startswith("/rewind"):
        if busy:
            typer.echo("[session] /rewind requires an idle Agent")
            return "none"
        parts = text.split()
        if len(parts) != 3:
            typer.echo("usage: /rewind <new-session> <through-turn>")
            return "none"
        try:
            through_turn = int(parts[2])
            cloned, recovery = await session.rewind(
                parts[1],
                through_turn=through_turn,
            )
        except (ValueError, ChatStoreError, MutationRecoveryError) as exc:
            typer.echo(f"[session] rewind failed: {exc}")
            return "none"
        typer.echo(
            f"[session] rewound to turn {through_turn} as {parts[1]} "
            f"({len(cloned)} retained; restored={len(recovery.restored_files)}; "
            f"removed={len(recovery.removed_files)}); resume with --session {parts[1]}"
        )
        return "none"
    if lowered == "/interrupt":
        if busy:
            await session.control.submit(ControlIntent.INTERRUPT)
            typer.echo("[control] interrupt queued for the next safe boundary")
        else:
            typer.echo("[control] no Agent turn is running")
        return "none"
    for prefix, intent in (("/steer ", ControlIntent.STEER), ("/next ", ControlIntent.NEXT)):
        if lowered.startswith(prefix):
            content = text[len(prefix) :].strip()
            if intent == ControlIntent.STEER and not busy:
                typer.echo("[control] /steer requires a running Agent turn")
                return "none"
            await session.control.submit(intent, content)
            typer.echo(f"[control] {intent} queued")
            return "none"
    if busy:
        await session.control.submit(ControlIntent.STEER, text)
        typer.echo("[control] steering queued")
        return "none"
    return f"start:{text}"


if __name__ == "__main__":
    app()
