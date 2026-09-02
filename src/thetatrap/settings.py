"""Role-specific, fail-closed runtime configuration."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from thetatrap.data_profile import (
    BASIC_OPTION_FEED,
    BASIC_STOCK_FEED,
    MarketDataProfile,
    require_basic_indicative_profile,
)
from thetatrap.errors import ConfigurationError


EnvironmentName = Literal["development", "competition", "replay"]


class StrategyProfile(StrEnum):
    """Explicit runtime strategy selection; earnings remains the safe default."""

    EARNINGS = "earnings"
    INTRADAY_CANARY = "intraday_canary"

REQUIRED_MCP_TOOLSETS = (
    "account",
    "trading",
    "assets",
    "stock-data",
    "options-data",
    "news",
)
REQUIRED_MCP_TOOLSETS_VALUE = ",".join(REQUIRED_MCP_TOOLSETS)
PLACEHOLDER_PREFIX = "replace_"


class RuntimeSettings(BaseSettings):
    """Settings loaded from one explicit role file or the process environment."""

    model_config = SettingsConfigDict(
        env_file=None,
        env_file_encoding="utf-8",
        extra="forbid",
        case_sensitive=True,
    )

    environment: EnvironmentName = Field(validation_alias="THETATRAP_ENVIRONMENT")
    expected_account_id: str = Field(validation_alias="THETATRAP_EXPECTED_ACCOUNT_ID")
    database_path: Path = Field(validation_alias="THETATRAP_DATABASE_PATH")
    read_only: bool = Field(default=True, validation_alias="THETATRAP_READ_ONLY")
    execution_enabled: bool = Field(
        default=False, validation_alias="THETATRAP_EXECUTION_ENABLED"
    )
    mcp_expected_schema_hash: str | None = Field(
        default=None, validation_alias="THETATRAP_MCP_EXPECTED_SCHEMA_HASH"
    )
    mcp_server_command: str | None = Field(
        default=None, validation_alias="THETATRAP_MCP_SERVER_COMMAND"
    )
    log_level: str = Field(default="INFO", validation_alias="THETATRAP_LOG_LEVEL")
    worker_interval_seconds: int = Field(
        default=60,
        ge=10,
        le=300,
        validation_alias="THETATRAP_WORKER_INTERVAL_SECONDS",
    )
    strategy_profile: StrategyProfile = Field(
        default=StrategyProfile.EARNINGS,
        validation_alias="THETATRAP_STRATEGY_PROFILE",
    )

    alpaca_api_key: SecretStr = Field(validation_alias="ALPACA_API_KEY")
    alpaca_secret_key: SecretStr = Field(validation_alias="ALPACA_SECRET_KEY")
    alpaca_paper_trade: bool = Field(default=True, validation_alias="ALPACA_PAPER_TRADE")
    alpaca_toolsets: str = Field(
        default=REQUIRED_MCP_TOOLSETS_VALUE, validation_alias="ALPACA_TOOLSETS"
    )
    alpaca_stock_feed: str = Field(
        default=BASIC_STOCK_FEED, validation_alias="ALPACA_STOCK_FEED"
    )
    alpaca_option_feed: str = Field(
        default=BASIC_OPTION_FEED, validation_alias="ALPACA_OPTION_FEED"
    )

    featherless_api_key: SecretStr | None = Field(
        default=None, validation_alias="FEATHERLESS_API_KEY"
    )
    featherless_base_url: str = Field(
        default="https://api.featherless.ai/v1", validation_alias="FEATHERLESS_BASE_URL"
    )
    featherless_primary_model: str = Field(
        default="Qwen/Qwen3-Coder-Next", validation_alias="FEATHERLESS_PRIMARY_MODEL"
    )
    featherless_fallback_model: str = Field(
        default="Qwen/Qwen3-32B",
        validation_alias="FEATHERLESS_FALLBACK_MODEL",
    )
    timezone: str = Field(default="America/New_York", validation_alias="TZ")

    @field_validator("expected_account_id", "mcp_expected_schema_hash", mode="before")
    @classmethod
    def strip_optional_strings(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("alpaca_toolsets")
    @classmethod
    def validate_toolsets(cls, value: str) -> str:
        actual = tuple(part.strip() for part in value.split(",") if part.strip())
        if actual != REQUIRED_MCP_TOOLSETS:
            raise ValueError(
                "ALPACA_TOOLSETS must exactly equal " + REQUIRED_MCP_TOOLSETS_VALUE
            )
        return value

    @model_validator(mode="after")
    def enforce_runtime_safety(self) -> "RuntimeSettings":
        if not self.alpaca_paper_trade:
            raise ValueError("ALPACA_PAPER_TRADE must remain true")
        if self.read_only == self.execution_enabled:
            raise ValueError(
                "THETATRAP_READ_ONLY and THETATRAP_EXECUTION_ENABLED must be opposites"
            )
        if self.environment == "replay" and self.execution_enabled:
            raise ValueError("replay mode can never enable broker execution")
        if self.timezone != "America/New_York":
            raise ValueError("TZ must be America/New_York")
        if self.environment == "competition" and not self.mcp_expected_schema_hash:
            raise ValueError("competition mode requires THETATRAP_MCP_EXPECTED_SCHEMA_HASH")
        require_basic_indicative_profile(
            stock_feed=self.alpaca_stock_feed,
            option_feed=self.alpaca_option_feed,
        )
        return self

    def market_data_profile(self) -> MarketDataProfile:
        """Return the validated, public-safe competition data profile."""

        return require_basic_indicative_profile(
            stock_feed=self.alpaca_stock_feed,
            option_feed=self.alpaca_option_feed,
        )

    def market_data_status(self) -> dict[str, object]:
        return self.market_data_profile().status()

    def require_alpaca_credentials(self) -> None:
        required = {
            "ALPACA_API_KEY": self.alpaca_api_key.get_secret_value(),
            "ALPACA_SECRET_KEY": self.alpaca_secret_key.get_secret_value(),
        }
        invalid = [
            name
            for name, value in required.items()
            if not value or value.lower().startswith(PLACEHOLDER_PREFIX)
        ]
        if invalid:
            raise ConfigurationError("replace placeholder values for: " + ", ".join(invalid))

    def require_mcp_credentials(self) -> None:
        self.require_alpaca_credentials()
        if not self.expected_account_id or self.expected_account_id.lower().startswith(
            PLACEHOLDER_PREFIX
        ):
            raise ConfigurationError(
                "replace placeholder values for: THETATRAP_EXPECTED_ACCOUNT_ID"
            )

    def require_featherless_credentials(self) -> None:
        value = (
            self.featherless_api_key.get_secret_value().strip()
            if self.featherless_api_key is not None
            else ""
        )
        if not value or value.lower().startswith(PLACEHOLDER_PREFIX):
            raise ConfigurationError("replace placeholder values for: FEATHERLESS_API_KEY")

    def redacted_summary(self) -> dict[str, object]:
        return {
            "environment": self.environment,
            "expected_account_suffix": account_suffix(self.expected_account_id),
            "database_path": str(self.database_path),
            "read_only": self.read_only,
            "execution_enabled": self.execution_enabled,
            "strategy_profile": self.strategy_profile.value,
            "alpaca_paper_trade": self.alpaca_paper_trade,
            "alpaca_toolsets": self.alpaca_toolsets,
            "market_data_profile": self.market_data_status(),
            "mcp_expected_schema_hash": self.mcp_expected_schema_hash,
            "alpaca_credentials_present": all(
                value and not value.lower().startswith(PLACEHOLDER_PREFIX)
                for value in (
                    self.alpaca_api_key.get_secret_value(),
                    self.alpaca_secret_key.get_secret_value(),
                )
            ),
            "featherless_key_present": bool(
                self.featherless_api_key
                and (
                    value := self.featherless_api_key.get_secret_value().strip()
                )
                and not value.lower().startswith(PLACEHOLDER_PREFIX)
            ),
        }


def account_suffix(account_id: str, visible: int = 6) -> str:
    if not account_id:
        return "missing"
    if len(account_id) <= visible:
        return "*" * len(account_id)
    return "…" + account_id[-visible:]


def load_settings(env_file: str | Path | None = None) -> RuntimeSettings:
    path: Path | None = None
    if env_file is not None:
        path = Path(env_file).expanduser().resolve()
        if path.name == ".env":
            raise ConfigurationError(
                "generic .env files are rejected; use .env.dev or .env.competition"
            )
        if not path.is_file():
            raise ConfigurationError(f"environment file not found: {path}")
    try:
        return RuntimeSettings(_env_file=path, _env_file_encoding="utf-8")  # type: ignore[call-arg]
    except Exception as exc:
        if isinstance(exc, ConfigurationError):
            raise
        raise ConfigurationError(str(exc)) from exc


def validate_environment_pair(
    dev_file: str | Path, competition_file: str | Path
) -> tuple[RuntimeSettings, RuntimeSettings]:
    dev = load_settings(dev_file)
    competition = load_settings(competition_file)
    dev.require_mcp_credentials()
    competition.require_mcp_credentials()
    if dev.environment != "development":
        raise ConfigurationError("development file must declare development environment")
    if competition.environment != "competition":
        raise ConfigurationError("competition file must declare competition environment")
    if dev.expected_account_id == competition.expected_account_id:
        raise ConfigurationError("development and competition account IDs must differ")
    if dev.alpaca_api_key.get_secret_value() == competition.alpaca_api_key.get_secret_value():
        raise ConfigurationError("development and competition API keys must differ")
    if (
        dev.alpaca_secret_key.get_secret_value()
        == competition.alpaca_secret_key.get_secret_value()
    ):
        raise ConfigurationError("development and competition secret keys must differ")
    if dev.database_path.resolve() == competition.database_path.resolve():
        raise ConfigurationError("development and competition database paths must differ")
    return dev, competition
