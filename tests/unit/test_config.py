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


def test_optional_context_and_resume_settings_are_loaded(tmp_path: Path) -> None:
    path = tmp_path / "settings.env"
    path.write_text(
        "MODEL_PROVIDER_TEXT=mimo\n"
        "MODEL_PROVIDER_TEXT_MODEL=mimo-v2.5-pro\n"
        "MIMO_API_KEY=test-key\n"
        "MIMO_BASE_URL=https://example.invalid/v1\n"
        "MODEL_CONTEXT_WINDOW_TOKENS=200000\n"
        "MODEL_SUMMARY_MAX_OUTPUT_TOKENS=6000\n"
        "AUTO_COMPACT=false\n"
        "AUTO_RESUME=ask\n",
        encoding="utf-8",
    )

    settings = ModelSettings.from_env_file(path)

    assert settings.context_window_tokens == 200_000
    assert settings.summary_max_output_tokens == 6_000
    assert settings.auto_compact is False
    assert settings.auto_resume == "ask"


def test_deepseek_provider_uses_provider_specific_key_and_default_url(tmp_path: Path) -> None:
    path = tmp_path / "deepseek.env"
    path.write_text(
        "MODEL_PROVIDER_TEXT=deepseek\n"
        "DEEPSEEK_TEXT_MODEL=deepseek-flash\n"
        "DEEPSEEK_API_KEY=deepseek-secret\n",
        encoding="utf-8",
    )

    settings = ModelSettings.from_env_file(path)

    assert settings.provider == "deepseek"
    assert settings.model == "deepseek-flash"
    assert settings.base_url == "https://api.deepseek.com"
    assert "deepseek-secret" not in repr(settings)
