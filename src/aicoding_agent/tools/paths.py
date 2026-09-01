from __future__ import annotations

from pathlib import Path, PurePath

from aicoding_agent.tools.base import ToolError

PROTECTED_PARTS = {".git", ".venv", ".aicoding-agent", ".ssh", ".aws", ".kube"}
SENSITIVE_FILE_NAMES = {
    ".env",
    ".env.local",
    ".npmrc",
    ".pypirc",
    ".aicoding-agent.json",
    "env.txt",
    "id_ed25519",
    "id_rsa",
}


def is_protected_relative(relative: Path) -> bool:
    lowered_parts = {part.lower() for part in relative.parts}
    name = relative.name.lower()
    sensitive_env_variant = name.startswith(".env.") and name not in {
        ".env.example",
        ".env.sample",
        ".env.template",
    }
    return bool(
        lowered_parts & PROTECTED_PARTS or name in SENSITIVE_FILE_NAMES or sensitive_env_variant
    )


def resolve_workspace_path(
    workspace: Path,
    raw_path: str,
    *,
    allowed_prefixes: list[str],
    for_write: bool = False,
) -> Path:
    if not raw_path or Path(raw_path).is_absolute() or ":" in raw_path:
        raise ToolError("Path must be relative to the workspace")

    normalized = PurePath(raw_path.replace("\\", "/"))
    if any(part in {"", ".", ".."} for part in normalized.parts if part != "."):
        if ".." in normalized.parts:
            raise ToolError("Parent path traversal is not allowed")

    candidate = (workspace / Path(*normalized.parts)).resolve()
    try:
        relative = candidate.relative_to(workspace.resolve())
    except ValueError as exc:
        raise ToolError("Path resolves outside the workspace") from exc

    if is_protected_relative(relative):
        action = "Writes to" if for_write else "Access to"
        raise ToolError(f"{action} protected or secret-bearing workspace paths is not allowed")

    if "." not in allowed_prefixes:
        allowed = False
        for prefix in allowed_prefixes:
            prefix_path = Path(prefix.replace("\\", "/"))
            try:
                relative.relative_to(prefix_path)
                allowed = True
                break
            except ValueError:
                continue
        if not allowed:
            raise ToolError("Path is outside the task contract's allowed prefixes")
    return candidate
