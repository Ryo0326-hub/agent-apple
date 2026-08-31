from __future__ import annotations

from pathlib import Path

import pytest

from thetatrap.errors import ConfigurationError
from thetatrap.settings import load_settings, validate_environment_pair


def test_valid_development_settings_are_redacted(valid_env_file: Path) -> None:
    settings = load_settings(valid_env_file)
    summary = settings.redacted_summary()
    assert settings.environment == "development"
    assert summary["alpaca_credentials_present"] is True
    assert summary["featherless_key_present"] is False
    assert "dev-key" not in str(summary)
    assert "dev-secret" not in str(summary)


def test_generic_env_filename_is_rejected(tmp_path: Path, valid_env_text: str) -> None:
    path = tmp_path / ".env"
    path.write_text(valid_env_text, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="generic .env"):
        load_settings(path)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("ALPACA_PAPER_TRADE=true", "ALPACA_PAPER_TRADE=false", "must remain true"),
        (
            "THETATRAP_READ_ONLY=true",
            "THETATRAP_READ_ONLY=false",
            "must be opposites",
        ),
    ],
)
def test_unsafe_modes_are_rejected(
    tmp_path: Path, valid_env_text: str, old: str, new: str, message: str
) -> None:
    path = tmp_path / ".env.dev"
    path.write_text(valid_env_text.replace(old, new), encoding="utf-8")
    with pytest.raises(ConfigurationError, match=message):
        load_settings(path)


def test_development_execution_requires_explicit_inverse_flags(
    tmp_path: Path, valid_env_text: str
) -> None:
    path = tmp_path / ".env.dev"
    path.write_text(
        valid_env_text.replace("THETATRAP_READ_ONLY=true", "THETATRAP_READ_ONLY=false")
        .replace(
            "THETATRAP_EXECUTION_ENABLED=false",
            "THETATRAP_EXECUTION_ENABLED=true",
        ),
        encoding="utf-8",
    )
    settings = load_settings(path)
    assert settings.execution_enabled is True
    assert settings.read_only is False


def test_competition_requires_schema_hash(tmp_path: Path, valid_env_text: str) -> None:
    competition = valid_env_text.replace(
        "THETATRAP_ENVIRONMENT=development", "THETATRAP_ENVIRONMENT=competition"
    )
    path = tmp_path / ".env.competition"
    path.write_text(competition, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="MCP_EXPECTED_SCHEMA_HASH"):
        load_settings(path)


def test_environment_pair_requires_distinct_identity(
    tmp_path: Path, valid_env_text: str
) -> None:
    dev = tmp_path / ".env.dev"
    competition = tmp_path / ".env.competition"
    dev.write_text(valid_env_text, encoding="utf-8")
    competition.write_text(
        valid_env_text.replace(
            "THETATRAP_ENVIRONMENT=development", "THETATRAP_ENVIRONMENT=competition"
        ).replace(
            "THETATRAP_MCP_EXPECTED_SCHEMA_HASH=",
            "THETATRAP_MCP_EXPECTED_SCHEMA_HASH=abc123",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="account IDs must differ"):
        validate_environment_pair(dev, competition)


def test_environment_pair_accepts_separate_roles(
    tmp_path: Path, valid_env_text: str
) -> None:
    dev = tmp_path / ".env.dev"
    competition = tmp_path / ".env.competition"
    dev.write_text(valid_env_text, encoding="utf-8")
    competition.write_text(
        valid_env_text.replace(
            "THETATRAP_ENVIRONMENT=development", "THETATRAP_ENVIRONMENT=competition"
        )
        .replace("dev-account-id", "competition-account-id")
        .replace("dev-key", "competition-key")
        .replace("dev-secret", "competition-secret")
        .replace("dev.sqlite3", "competition.sqlite3")
        .replace(
            "THETATRAP_MCP_EXPECTED_SCHEMA_HASH=",
            "THETATRAP_MCP_EXPECTED_SCHEMA_HASH=abc123",
        ),
        encoding="utf-8",
    )
    loaded_dev, loaded_competition = validate_environment_pair(dev, competition)
    assert loaded_dev.environment == "development"
    assert loaded_competition.environment == "competition"


def test_account_discovery_can_validate_keys_before_uuid_is_known(
    tmp_path: Path, valid_env_text: str
) -> None:
    path = tmp_path / ".env.dev"
    path.write_text(
        valid_env_text.replace("dev-account-id", "replace_account_uuid"),
        encoding="utf-8",
    )
    settings = load_settings(path)

    settings.require_alpaca_credentials()
    with pytest.raises(ConfigurationError, match="EXPECTED_ACCOUNT_ID"):
        settings.require_mcp_credentials()
    with pytest.raises(ConfigurationError, match="FEATHERLESS_API_KEY"):
        settings.require_featherless_credentials()
