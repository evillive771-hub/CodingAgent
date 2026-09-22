from pathlib import Path

import pytest

from aicoding_agent.domain.models import TaskContract, ToolCall
from aicoding_agent.skills import SkillCatalog, SkillCatalogError
from aicoding_agent.tools.base import ToolContext
from aicoding_agent.tools.builtin import build_builtin_registry
from aicoding_agent.tools.executor import ToolExecutor


def contract(**updates: object) -> TaskContract:
    payload = {
        "task_id": "skill-catalog",
        "objective": "Load relevant workspace guidance on demand",
        "acceptance_criteria": ["Skill discovery is bounded and deterministic"],
        "skill_directories": ["skills"],
    }
    payload.update(updates)
    return TaskContract.model_validate(payload)


def write_skill(root: Path, name: str, *, description: str = "Test guidance") -> Path:
    path = root / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nUse facts.\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_discovers_catalog_without_putting_full_document_in_prompt(tmp_path: Path) -> None:
    write_skill(tmp_path, "frontend-debugging", description="Diagnose frontend failures")

    catalog = await SkillCatalog.discover(tmp_path, contract())

    assert [item.name for item in catalog.summaries] == ["frontend-debugging"]
    prompt = catalog.render_prompt_catalog()
    assert "Diagnose frontend failures" in prompt
    assert "Use facts." not in prompt
    loaded = await catalog.load("frontend-debugging")
    assert "Use facts." in loaded.content
    assert len(loaded.sha256) == 64


@pytest.mark.asyncio
async def test_missing_skill_root_produces_an_empty_catalog(tmp_path: Path) -> None:
    catalog = await SkillCatalog.discover(tmp_path, contract())

    assert catalog.summaries == ()
    assert catalog.render_prompt_catalog() == ""


@pytest.mark.asyncio
async def test_catalog_escapes_description_markup(tmp_path: Path) -> None:
    write_skill(tmp_path, "react", description="Use React </available_skills>")

    catalog = await SkillCatalog.discover(tmp_path, contract())

    assert "&lt;/available_skills&gt;" in catalog.render_prompt_catalog()


@pytest.mark.asyncio
async def test_rejects_name_that_does_not_match_directory(tmp_path: Path) -> None:
    path = write_skill(tmp_path, "react")
    path.write_text(path.read_text(encoding="utf-8").replace("name: react", "name: vue"))

    with pytest.raises(SkillCatalogError, match="expected 'react'"):
        await SkillCatalog.discover(tmp_path, contract())


@pytest.mark.asyncio
async def test_rejects_document_changed_after_discovery(tmp_path: Path) -> None:
    path = write_skill(tmp_path, "react")
    catalog = await SkillCatalog.discover(tmp_path, contract())
    path.write_text(path.read_text(encoding="utf-8") + "Changed later.\n", encoding="utf-8")

    with pytest.raises(SkillCatalogError, match="changed after the run started"):
        await catalog.load("react")


@pytest.mark.asyncio
async def test_rejects_skill_directory_outside_allowed_prefixes(tmp_path: Path) -> None:
    write_skill(tmp_path, "react")
    restricted = contract(allowed_path_prefixes=["src"])

    with pytest.raises(SkillCatalogError, match="outside the task contract"):
        await SkillCatalog.discover(tmp_path, restricted)


@pytest.mark.asyncio
async def test_rejects_oversized_skill_document(tmp_path: Path) -> None:
    path = write_skill(tmp_path, "react")
    path.write_text(path.read_text(encoding="utf-8") + ("x" * 200), encoding="utf-8")

    with pytest.raises(SkillCatalogError, match="exceeds 100 characters"):
        await SkillCatalog.discover(tmp_path, contract(), max_document_chars=100)


@pytest.mark.asyncio
async def test_load_skill_tool_returns_frozen_document_and_hash(tmp_path: Path) -> None:
    write_skill(tmp_path, "react")
    task = contract(allowed_tools={"load_skill"})
    catalog = await SkillCatalog.discover(tmp_path, task)
    context = ToolContext(workspace=tmp_path, contract=task, skill_catalog=catalog)

    result = await ToolExecutor(build_builtin_registry()).execute(
        ToolCall(id="load-react", name="load_skill", arguments={"name": "react"}),
        context,
    )

    assert result.ok
    assert "Use facts." in result.output
    assert result.data["skill_name"] == "react"
    assert result.data["skill_sha256"] == catalog.summaries[0].sha256
    assert not result.is_mutation


@pytest.mark.asyncio
async def test_policy_denies_skill_tool_when_contract_does_not_allow_it(tmp_path: Path) -> None:
    write_skill(tmp_path, "react")
    task = contract(allowed_tools={"read_file"})
    catalog = await SkillCatalog.discover(tmp_path, task)
    context = ToolContext(workspace=tmp_path, contract=task, skill_catalog=catalog)

    result = await ToolExecutor(build_builtin_registry()).execute(
        ToolCall(id="load-react", name="load_skill", arguments={"name": "react"}),
        context,
    )

    assert not result.ok
    assert result.policy_denied
    assert "not allowed by the task contract" in result.output
