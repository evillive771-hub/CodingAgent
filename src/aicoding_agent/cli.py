from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from aicoding_agent.agent.background import BackgroundJobStore, BackgroundTaskManager
from aicoding_agent.agent.control import ControlIntent, ControlStore, SteeringInbox
from aicoding_agent.agent.hooks import AsyncHookBus
from aicoding_agent.agent.loop import AgentLoop
from aicoding_agent.agent.plan import PlanManager, PlanStore
from aicoding_agent.chat.console import ChatEventSink
from aicoding_agent.chat.session import ChatSession
from aicoding_agent.chat.store import ChatStoreError, ChatTranscriptStore
from aicoding_agent.config import ConfigurationError, ModelSettings
from aicoding_agent.context.archive import ContextEvidenceArchive
from aicoding_agent.domain.models import TaskContract
from aicoding_agent.model.mimo import MimoChatModel
from aicoding_agent.persistence.jsonl import JsonlEventSink
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor

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
    """Validate local configuration; optionally run a live MiMo protocol probe."""
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
    model = MimoChatModel(settings.model_copy(update={"max_output_tokens": 512}))
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
) -> None:
    """Run the Coding Agent in a real workspace under a validated task contract."""
    contract = _load_contract(task)
    try:
        settings = ModelSettings.from_env_file(env_file)
    except ConfigurationError as exc:
        raise typer.BadParameter(str(exc), param_hint="--env-file") from exc
    result = asyncio.run(_run_agent(contract, workspace, settings))
    typer.echo(result.model_dump_json(indent=2))
    if result.status != "completed":
        raise typer.Exit(code=1)


async def _run_agent(
    contract: TaskContract,
    workspace: Path,
    settings: ModelSettings,
):
    model = MimoChatModel(settings)
    runtime_root = workspace / ".aicoding-agent"
    event_sink = JsonlEventSink(runtime_root / "runs")
    hook_bus = AsyncHookBus()
    background = BackgroundTaskManager(store=BackgroundJobStore(runtime_root / "background"))
    executor = ToolExecutor(build_builtin_registry(), hook_bus=hook_bus)
    agent = AgentLoop(
        model=model,
        executor=executor,
        event_sink=event_sink,
        hook_bus=hook_bus,
        context_archive=ContextEvidenceArchive(runtime_root / "evidence"),
        background_manager=background,
    )
    try:
        return await agent.run(contract, workspace)
    finally:
        await model.aclose()


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
    selected_runtime = (runtime_dir or (resolved_workspace / ".aicoding-agent")).resolve()
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
) -> None:
    workspace_key = hashlib.sha256(str(workspace).casefold().encode()).hexdigest()[:16]
    runtime_root = runtime_dir / "chat" / workspace_key
    persistent_events = JsonlEventSink(runtime_root / "events")
    event_sink = ChatEventSink(persistent_events, writer=typer.echo)
    model = MimoChatModel(settings)
    hook_bus = AsyncHookBus()
    executor = ToolExecutor(build_builtin_registry(), hook_bus=hook_bus)
    plan_manager = PlanManager(store=PlanStore(runtime_root / "sessions"), scope_id=session_id)
    control = SteeringInbox(store=ControlStore(runtime_root / "sessions"), scope_id=session_id)
    background = BackgroundTaskManager(
        store=BackgroundJobStore(runtime_root / "sessions" / session_id / "background")
    )
    agent = AgentLoop(
        model=model,
        executor=executor,
        event_sink=event_sink,
        hook_bus=hook_bus,
        context_archive=ContextEvidenceArchive(runtime_root / "evidence"),
    )
    session = ChatSession(
        agent=agent,
        workspace=workspace,
        profile=contract,
        store=ChatTranscriptStore(runtime_root / "sessions"),
        session_id=session_id,
        plan_manager=plan_manager,
        control=control,
        background_manager=background,
    )
    try:
        await session.initialize()
        typer.secho("AI Coding Agent Chat", fg=typer.colors.CYAN, bold=True)
        typer.echo(f"workspace: {workspace}")
        typer.echo(f"session: {session_id} ({len(session.history)} previous turn(s))")
        typer.echo(
            "Commands: /plan /steer <message> /next <message> /interrupt /status /history /exit\n"
        )
        prompt_session: PromptSession[str] = PromptSession()
        with patch_stdout(raw=True):
            await _interactive_chat(prompt_session, session, workspace, session_id)
    finally:
        await model.aclose()


async def _interactive_chat(
    prompt_session: PromptSession[str],
    session: ChatSession,
    workspace: Path,
    session_id: str,
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
            )
            if action == "exit":
                return
            if action.startswith("start:"):
                active = asyncio.create_task(session.send(action.removeprefix("start:")))
            continue

        prompt_task: asyncio.Task[str] | None = None
        if not exit_after_active:
            prompt_task = asyncio.create_task(
                prompt_session.prompt_async("Busy (/steer, /next, /interrupt) > ")
            )
        waiters: set[asyncio.Task[Any]] = {active}
        if prompt_task is not None:
            waiters.add(prompt_task)
        done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        if active in done:
            if prompt_task is not None and not prompt_task.done():
                prompt_task.cancel()
                await asyncio.gather(prompt_task, return_exceptions=True)
            turn = active.result()
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
            "/next queues a later turn and /interrupt stops at the next safe boundary."
        )
        return "none"
    if lowered == "/plan":
        typer.echo(session.plan_manager.render())
        return "none"
    if lowered == "/status":
        typer.echo(
            f"workspace={workspace}; session={session_id}; turns={len(session.history)}; "
            f"busy={busy}\n{session.background_manager.render()}"
        )
        return "none"
    if lowered == "/history":
        if not session.history:
            typer.echo("<empty conversation>")
        for turn in session.history:
            typer.echo(f"[{turn.turn_number}] You: {turn.user_message[:200]}")
            typer.echo(f"[{turn.turn_number}] Agent: {turn.assistant_message[:300]}")
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
