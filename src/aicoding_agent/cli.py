from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from aicoding_agent.agent.loop import AgentLoop
from aicoding_agent.chat.console import ChatEventSink
from aicoding_agent.chat.session import ChatSession
from aicoding_agent.chat.store import ChatStoreError, ChatTranscriptStore
from aicoding_agent.config import ConfigurationError, ModelSettings
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
    event_sink = JsonlEventSink(workspace / ".aicoding-agent" / "runs")
    executor = ToolExecutor(build_builtin_registry())
    agent = AgentLoop(model=model, executor=executor, event_sink=event_sink)
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
    agent = AgentLoop(
        model=model,
        executor=ToolExecutor(build_builtin_registry()),
        event_sink=event_sink,
    )
    session = ChatSession(
        agent=agent,
        workspace=workspace,
        profile=contract,
        store=ChatTranscriptStore(runtime_root / "sessions"),
        session_id=session_id,
    )
    try:
        await session.initialize()
        typer.secho("AI Coding Agent Chat", fg=typer.colors.CYAN, bold=True)
        typer.echo(f"workspace: {workspace}")
        typer.echo(f"session: {session_id} ({len(session.history)} previous turn(s))")
        typer.echo("Type a request naturally. Commands: /help /status /history /exit\n")
        while True:
            try:
                query = await asyncio.to_thread(input, "You > ")
            except EOFError:
                break
            command = query.strip().lower()
            if command in {"/exit", "/quit", "exit", "quit"}:
                break
            if command == "/help":
                typer.echo(
                    "Natural language starts an Agent turn. /status shows session state; "
                    "/history shows prior messages; /exit closes the chat."
                )
                continue
            if command == "/status":
                typer.echo(
                    f"workspace={workspace}; session={session_id}; turns={len(session.history)}"
                )
                continue
            if command == "/history":
                if not session.history:
                    typer.echo("<empty conversation>")
                for turn in session.history:
                    typer.echo(f"[{turn.turn_number}] You: {turn.user_message[:200]}")
                    typer.echo(f"[{turn.turn_number}] Agent: {turn.assistant_message[:300]}")
                continue
            if not query.strip():
                continue
            turn = await session.send(query)
            typer.secho("\nAgent >", fg=typer.colors.GREEN, bold=True)
            typer.echo(turn.assistant_message)
            if turn.status not in {"completed", "waiting_for_user"}:
                typer.echo(f"[turn status: {turn.status}; reason: {turn.reason or 'unknown'}]")
            typer.echo()
    finally:
        await model.aclose()


if __name__ == "__main__":
    app()
