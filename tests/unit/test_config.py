from pathlib import Path

from aicoding_agent.config import ModelSettings, read_env_file


def test_env_file_uses_last_duplicate_without_exposing_secret(tmp_path: Path) -> None:
    path = tmp_path / "settings.env"
    path.write_text(
        "MODEL_PROVIDER_TEXT=mimo\n"
        "MODEL_PROVIDER_TEXT=mimo\n"
        "MODEL_PROVIDER_TEXT_MODEL=mimo-v2.5-pro\n"
        "MIMO_API_KEY=top-secret\n"
        "MIMO_BASE_URL=https://example.invalid/v1\n",
        encoding="utf-8",
    )

    values = read_env_file(path)
    settings = ModelSettings.from_env_file(path)

    assert values["MODEL_PROVIDER_TEXT"] == "mimo"
    assert settings.model == "mimo-v2.5-pro"
    assert "top-secret" not in repr(settings)
