from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from thetatrap import worker
from thetatrap.agent_smoke import SmokeDecision, SmokeTraceItem
from thetatrap.errors import AccountIdentityError, PolicyError
from thetatrap.mcp.contract import SETUP_READ_TOOLS
from thetatrap.policy import payload_hash
from thetatrap.settings import load_settings
from thetatrap.storage import Store
from thetatrap.worker import (
    run_account_discovery,
    run_agent_smoke,
    run_read_cycle,
    run_worker,
)


class RegistryStub:
    required_schema_hash = "hash"
    tool_count = 20


class WrongAccountConnection:
    registry = RegistryStub()

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call_read(self, name: str) -> dict[str, Any]:
        self.calls.append(name)
        return {
            "_alpaca_mcp_security": {},
            "data": {"id": "wrong-account-id", "status": "ACTIVE"},
        }


class SuccessfulConnection:
    registry = RegistryStub()

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call_read(self, name: str) -> dict[str, Any]:
        self.calls.append(name)
        payloads: dict[str, Any] = {
            "get_account_info": {
                "id": "dev-account-id",
                "status": "ACTIVE",
                "options_trading_level": 3,
            },
            "get_account_config": {},
            "get_clock": {"is_open": False},
            "get_orders": {"result": []},
            "get_all_positions": {"result": []},
        }
        return {"_alpaca_mcp_security": {}, "data": payloads[name]}


class SmokeConnection:
    registry = RegistryStub()

    async def call_discovery_read(self) -> dict[str, Any]:
        return {
            "_alpaca_mcp_security": {},
            "data": {
                "id": "dev-account-id",
                "account_number": "PA-DO-NOT-PERSIST",
                "status": "ACTIVE",
                "options_trading_level": 3,
            },
        }

    def smoke_openai_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": name,
                    "parameters": {"type": "object"},
                },
            }
            for name in sorted(SETUP_READ_TOOLS)
        ]

    async def call_agent_smoke_read(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        payloads: dict[str, Any] = {
            "get_account_info": {
                "id": "dev-account-id",
                "account_number": "PA-DO-NOT-PERSIST",
                "status": "ACTIVE",
            },
            "get_account_config": {"suspend_trade": False},
            "get_clock": {"is_open": False},
            "get_orders": [],
            "get_all_positions": [],
        }
        return {"_alpaca_mcp_security": {}, "data": payloads[name]}


class DeterministicSmokeAgent:
    def __init__(self, settings: Any, tools: Any) -> None:
        self.settings = settings
        self.tools = tools

    async def run(self) -> SmokeDecision:
        trace = []
        for index, name in enumerate(sorted(SETUP_READ_TOOLS)):
            arguments = (
                {"status": "open", "nested": True, "limit": 100}
                if name == "get_orders"
                else {}
            )
            result = await self.tools.execute(name, arguments)
            trace.append(
                SmokeTraceItem(
                    turn=1,
                    tool_name=name,
                    arguments_hash=payload_hash(arguments),
                    result_hash=payload_hash(result),
                    status="ok",
                )
            )
        return SmokeDecision(
            model=self.settings.featherless_primary_model,
            turns=1,
            tool_calls=5,
            prompt_tokens=12,
            completion_tokens=5,
            read_tools=tuple(sorted(SETUP_READ_TOOLS)),
            readiness="READY",
            reasons=(),
            trace=tuple(trace),
        )


@pytest.mark.asyncio
async def test_wrong_account_stops_before_other_reads(
    tmp_path: Path, valid_env_file: Path
) -> None:
    settings = load_settings(valid_env_file)
    store = Store(tmp_path / "identity.sqlite3")
    store.initialize()
    connection = WrongAccountConnection()
    with pytest.raises(AccountIdentityError):
        await run_read_cycle(settings, store, connection)  # type: ignore[arg-type]
    assert connection.calls == ["get_account_info"]


@pytest.mark.asyncio
async def test_report_understands_generated_mcp_response_shape(
    tmp_path: Path, valid_env_file: Path
) -> None:
    settings = load_settings(valid_env_file)
    store = Store(tmp_path / "successful.sqlite3")
    store.initialize()
    connection = SuccessfulConnection()

    report = await run_read_cycle(settings, store, connection)  # type: ignore[arg-type]

    assert report["open_order_count"] == 0
    assert report["position_count"] == 0
    assert report["options_level"] == 3
    assert connection.calls == [
        "get_account_info",
        "get_account_config",
        "get_clock",
        "get_orders",
        "get_all_positions",
    ]


@pytest.mark.asyncio
async def test_armed_worker_once_is_rejected_before_any_mcp_connection(
    valid_env_file: Path,
) -> None:
    settings = load_settings(valid_env_file)
    object.__setattr__(settings, "read_only", False)
    object.__setattr__(settings, "execution_enabled", True)

    with pytest.raises(PolicyError, match="long-lived worker"):
        await run_worker(settings, once=True)


@pytest.mark.asyncio
async def test_account_discovery_does_not_bind_database(
    monkeypatch: pytest.MonkeyPatch, valid_env_file: Path
) -> None:
    settings = load_settings(valid_env_file)

    @asynccontextmanager
    async def fake_open(*_: Any, **__: Any):
        yield SmokeConnection()

    monkeypatch.setattr(worker, "open_alpaca_mcp", fake_open)
    report = await run_account_discovery(settings)

    assert report["account_id"] == "dev-account-id"
    assert report["env_assignment"] == "THETATRAP_EXPECTED_ACCOUNT_ID=dev-account-id"
    assert Store(settings.database_path).get_metadata("account_id") is None


@pytest.mark.asyncio
async def test_agent_smoke_binds_identity_and_persists_redacted_trace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    valid_env_text: str,
) -> None:
    env_file = tmp_path / ".env.dev"
    env_file.write_text(
        valid_env_text.replace("FEATHERLESS_API_KEY=", "FEATHERLESS_API_KEY=smoke-key"),
        encoding="utf-8",
    )
    settings = load_settings(env_file)

    @asynccontextmanager
    async def fake_open(*_: Any, **__: Any):
        yield SmokeConnection()

    monkeypatch.setattr(worker, "open_alpaca_mcp", fake_open)
    monkeypatch.setattr(worker, "ReadOnlySmokeAgent", DeterministicSmokeAgent)
    report = await run_agent_smoke(settings)

    assert report["outcome"] == "PASS"
    assert report["tool_calls"] == 5
    assert report["readiness"] == "READY"
    assert report["reasons"] == []
    assert report["mutation_tools_exposed"] == 0
    store = Store(settings.database_path)
    assert store.get_metadata("account_id") == "dev-account-id"
    with store.connect() as connection:
        run = connection.execute("SELECT * FROM agent_smoke_runs").fetchone()
        trace_count = connection.execute(
            "SELECT COUNT(*) FROM agent_smoke_trace"
        ).fetchone()[0]
    assert run["status"] == "COMPLETED"
    assert trace_count == 5
    assert b"PA-DO-NOT-PERSIST" not in settings.database_path.read_bytes()
