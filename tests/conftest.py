from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def valid_env_text(tmp_path: Path) -> str:
    return "\n".join(
        [
            "THETATRAP_ENVIRONMENT=development",
            "THETATRAP_EXPECTED_ACCOUNT_ID=dev-account-id",
            f"THETATRAP_DATABASE_PATH={tmp_path / 'dev.sqlite3'}",
            "THETATRAP_READ_ONLY=true",
            "THETATRAP_EXECUTION_ENABLED=false",
            "THETATRAP_MCP_EXPECTED_SCHEMA_HASH=",
            "THETATRAP_LOG_LEVEL=INFO",
            "THETATRAP_WORKER_INTERVAL_SECONDS=60",
            "ALPACA_API_KEY=dev-key",
            "ALPACA_SECRET_KEY=dev-secret",
            "ALPACA_PAPER_TRADE=true",
            "ALPACA_TOOLSETS=account,trading,assets,stock-data,options-data,news",
            "ALPACA_STOCK_FEED=iex",
            "ALPACA_OPTION_FEED=indicative",
            "FEATHERLESS_API_KEY=",
            "FEATHERLESS_BASE_URL=https://api.featherless.ai/v1",
            "FEATHERLESS_PRIMARY_MODEL=Qwen/Qwen3-Coder-Next",
            "FEATHERLESS_FALLBACK_MODEL=Qwen/Qwen3-32B",
            "TZ=America/New_York",
            "",
        ]
    )


@pytest.fixture
def valid_env_file(tmp_path: Path, valid_env_text: str) -> Path:
    path = tmp_path / ".env.dev"
    path.write_text(valid_env_text, encoding="utf-8")
    return path
