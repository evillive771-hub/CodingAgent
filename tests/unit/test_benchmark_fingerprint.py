from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

TOOLS = Path("D:/Github/benchmark-runs/tools")


@pytest.fixture(autouse=True)
def python_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    # These fixtures describe the Python repository, irrespective of runner defaults.
    monkeypatch.setenv("CODING_AGENT_BACKEND", "python")


def load_tool(name: str) -> ModuleType:
    path = TOOLS / f"{name}.py"
    if not path.is_file():
        pytest.skip("Separately maintained benchmark tools are not installed")
    spec = importlib.util.spec_from_file_location(f"fingerprint_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def create_repository(root: Path) -> None:
    (root / "src" / "package").mkdir(parents=True)
    (root / "skills" / "react-review").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='sample'\n", encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (root / "src" / "package" / "agent.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "skills" / "react-review" / "SKILL.md").write_text(
        "---\nname: react-review\n---\nReview evidence.\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "fingerprint@example.invalid"], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.name", "Fingerprint Test"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)


def fingerprints(root: Path) -> dict[str, str]:
    modules = {
        name: load_tool(name)
        for name in (
            "scale_webbench_runner",
            "scale_reactbench_runner",
            "scale_calibration",
            "calibration_gate",
            "scale_orchestrator",
        )
    }
    for name in ("scale_webbench_runner", "scale_reactbench_runner", "scale_calibration"):
        modules[name].CODING_AGENT = root
    return {
        "web": modules["scale_webbench_runner"].source_fingerprint()["source_sha256"],
        "react": modules["scale_reactbench_runner"].source_fingerprint()["source_sha256"],
        "calibration": modules["scale_calibration"].source_fingerprint()["source_sha256"],
        "gate": modules["calibration_gate"]._source_fingerprint(root)["source_sha256"],
        "orchestrator": modules["scale_orchestrator"].sut_source_sha(root),
    }


def test_all_experiment_components_hash_skill_guidance_consistently(tmp_path: Path) -> None:
    create_repository(tmp_path)
    before = fingerprints(tmp_path)
    assert len(set(before.values())) == 1

    skill = tmp_path / "skills" / "react-review" / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "Check refs.\n", encoding="utf-8")
    after = fingerprints(tmp_path)
    assert len(set(after.values())) == 1
    assert after["orchestrator"] != before["orchestrator"]


def test_transient_skill_caches_do_not_change_experiment_fingerprint(tmp_path: Path) -> None:
    create_repository(tmp_path)
    before = fingerprints(tmp_path)
    cache = tmp_path / "skills" / "react-review" / "__pycache__"
    cache.mkdir()
    (cache / "helper.cpython-312.pyc").write_bytes(b"transient")
    (tmp_path / "skills" / ".pytest_cache").mkdir()
    (tmp_path / "skills" / ".pytest_cache" / "state").write_text("state", encoding="utf-8")
    assert fingerprints(tmp_path) == before
