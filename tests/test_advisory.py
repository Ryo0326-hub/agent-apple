from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest
from pydantic import SecretStr

from thetatrap.advisory import (
    ADVISORY_ASSESSMENTS,
    AdvisoryDecision,
    AdvisoryReadOnlyTools,
    QwenAdvisoryAgent,
    run_persisted_advisory_review,
    run_rejected_candidate_advisory,
    select_best_rejected_candidate,
)
from thetatrap.errors import AgentError, PolicyError
from thetatrap.mcp.contract import QWEN_READ_TOOLS
from thetatrap.policy import payload_hash
from thetatrap.settings import load_settings
from thetatrap.storage import ADVISORY_MODE, StorageInvariantError, Store


NOW = datetime(2026, 9, 1, 19, 10, tzinfo=UTC)
STRATEGY_DATE = date(2026, 9, 1)


def _tool_call(name: str, arguments: dict[str, Any], index: int) -> Any:
    return SimpleNamespace(
        id=f"call-{index}",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _response(calls: list[Any] | None, content: str | None = None) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(tool_calls=calls, content=content)
            )
        ],
        usage=SimpleNamespace(prompt_tokens=20, completion_tokens=10),
    )


class _FakeClient:
    def __init__(self, responses: list[Any]) -> None:
        async def create(**_: Any) -> Any:
            return responses.pop(0)

        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=create)
        )


class _ReadOnlyConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def qwen_openai_tools(
        self, *, include_entry: bool = False
    ) -> list[dict[str, Any]]:
        assert include_entry is False
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": name,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in sorted(QWEN_READ_TOOLS)
        ]

    async def call_agent_read(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((name, arguments or {}))
        if name == "get_news":
            data: Any = {
                "news": [
                    {
                        "headline": "Company schedules earnings release",
                        "summary": "No additional binary event was reported.",
                        "source": "wire",
                    }
                ]
            }
        elif name in {"get_orders", "get_all_positions"}:
            data = {"result": []}
        elif name == "get_account_info":
            data = {"status": "ACTIVE", "options_trading_level": 3}
        elif name == "get_clock":
            data = {"is_open": True, "timestamp": NOW.isoformat()}
        else:
            data = {}
        return {"data": data}


def _read_arguments(name: str) -> dict[str, Any]:
    if name == "get_orders":
        return {
            "status": "open",
            "asset_class": ["us_option"],
            "nested": True,
            "limit": 100,
        }
    if name == "get_news":
        return {
            "symbols": "PANW",
            "include_content": False,
            "limit": 5,
            "sort": "desc",
        }
    return {}


def _rejected_payload(*codes: str) -> dict[str, Any]:
    return {
        "symbol": "PANW",
        "candidate": None,
        "failures": [
            {"code": code, "detail": f"deterministic failure {code}"}
            for code in codes
        ],
    }


def _store_with_rejection(tmp_path: Path) -> tuple[Store, dict[str, Any]]:
    store = Store(tmp_path / "advisory.sqlite3")
    store.initialize()
    run = store.create_strategy_run(
        "run-1",
        environment="development",
        strategy_date=STRATEGY_DATE.isoformat(),
        strategy_version="1.2",
        config_hash="config",
    )
    store.transition_strategy_run("run-1", "SCREENING", "TEST_SCAN")
    store.record_candidate(
        "rejected-1",
        run_id="run-1",
        symbol="PANW",
        eligible=False,
        payload=_rejected_payload("OPTION_SPREAD_WIDE"),
    )
    return store, run


def test_advisory_tools_expose_only_bounded_mcp_reads() -> None:
    connection = _ReadOnlyConnection()
    tools = AdvisoryReadOnlyTools(connection, symbol="PANW")
    definitions = {
        item["function"]["name"]: item["function"]
        for item in tools.definitions()
    }

    assert set(definitions) == QWEN_READ_TOOLS
    assert "place_option_order" not in definitions
    assert definitions["get_news"]["parameters"]["properties"]["symbols"] == {
        "type": "string",
        "const": "PANW",
    }
    assert definitions["get_orders"]["parameters"]["properties"]["limit"] == {
        "type": "integer",
        "const": 100,
    }


@pytest.mark.asyncio
async def test_advisory_tools_reject_cross_symbol_news_and_mutations() -> None:
    connection = _ReadOnlyConnection()
    tools = AdvisoryReadOnlyTools(connection, symbol="PANW")

    with pytest.raises(PolicyError, match="fixed symbol"):
        await tools.execute(
            "get_news",
            {
                "symbols": "MDB",
                "include_content": False,
                "limit": 5,
                "sort": "desc",
            },
            turn=1,
        )
    with pytest.raises(PolicyError, match="denies non-read"):
        await tools.execute("place_option_order", {}, turn=1)

    assert connection.calls == []


@pytest.mark.asyncio
async def test_qwen_advisory_requires_all_reads_and_non_authorizing_json(
    valid_env_file: Path,
) -> None:
    connection = _ReadOnlyConnection()
    tools = AdvisoryReadOnlyTools(connection, symbol="PANW")
    calls = [
        _tool_call(name, _read_arguments(name), index)
        for index, name in enumerate(sorted(QWEN_READ_TOOLS))
    ]
    result = {
        "assessment": "REJECTION_CONTEXT_CLEAR",
        "summary": "The deterministic spread rejection remains final.",
        "evidence": ["The account is flat and current news adds no override."],
    }
    client = _FakeClient([_response(calls), _response(None, json.dumps(result))])
    settings = load_settings(valid_env_file)
    decision = await QwenAdvisoryAgent(
        settings, tools, client=client
    ).review(
        SimpleNamespace(
            symbol="PANW",
            event={"symbol": "PANW", "status": "verified"},
            rejection=_rejected_payload("OPTION_SPREAD_WIDE"),
        )
    )

    assert decision.assessment in ADVISORY_ASSESSMENTS
    assert decision.tool_calls == 6
    assert {name for name, _ in connection.calls} == QWEN_READ_TOOLS
    assert len(decision.trace) == 6


def test_advisory_result_schema_cannot_return_allow_or_order_fields() -> None:
    from thetatrap import advisory

    with pytest.raises(AgentError, match="invalid assessment"):
        advisory._parse_advisory(
            json.dumps(
                {
                    "assessment": "ALLOW",
                    "summary": "approve",
                    "evidence": ["none"],
                }
            )
        )
    with pytest.raises(AgentError, match="invalid non-authorizing schema"):
        advisory._parse_advisory(
            json.dumps(
                {
                    "assessment": "REJECTION_CONTEXT_CLEAR",
                    "summary": "rejected",
                    "evidence": ["gate failed"],
                    "order": {"qty": 1},
                }
            )
        )


def test_advisory_module_has_no_order_builder_or_mutation_dispatch_path() -> None:
    source = (Path("src/thetatrap") / "advisory.py").read_text(encoding="utf-8")
    assert "build_entry_order_intent" not in source
    assert "call_mutation(" not in source
    assert 'include_entry=True' not in source


def test_best_rejected_candidate_selection_is_deterministic() -> None:
    candidates = [
        {
            "candidate_id": "b",
            "symbol": "MDB",
            "eligible": False,
            "payload": _rejected_payload("OPTION_SPREAD_WIDE"),
        },
        {
            "candidate_id": "a",
            "symbol": "PANW",
            "eligible": False,
            "payload": _rejected_payload("OPTION_SPREAD_WIDE"),
        },
        {
            "candidate_id": "c",
            "symbol": "CRDO",
            "eligible": False,
            "payload": _rejected_payload(
                "OPTION_SPREAD_WIDE", "OPEN_INTEREST_LOW"
            ),
        },
        {"candidate_id": "eligible", "symbol": "AI", "eligible": True},
    ]

    selected = select_best_rejected_candidate(list(reversed(candidates)))

    assert selected is not None
    assert selected["candidate_id"] == "b"


def test_storage_labels_advisory_and_rejects_eligible_or_mutation_trace(
    tmp_path: Path,
) -> None:
    store, _ = _store_with_rejection(tmp_path)
    started = store.start_advisory_run(
        "advisory-1",
        run_id="run-1",
        candidate_id="rejected-1",
        model="qwen",
        prompt_hash=payload_hash("prompt"),
        config_hash=payload_hash("config"),
        started_at=NOW,
    )
    assert started["mode"] == ADVISORY_MODE
    with pytest.raises(StorageInvariantError, match="fixed read-only"):
        store.record_advisory_tool_call(
            "advisory-1",
            0,
            turn=1,
            tool_name="place_option_order",
            arguments_hash=payload_hash({}),
            result_hash=None,
            status="error",
            duration_ms=0,
            called_at=NOW,
        )

    store.create_strategy_run(
        "run-2",
        environment="development",
        strategy_date="2026-09-02",
        strategy_version="1.2",
        config_hash="config",
    )
    store.record_candidate(
        "eligible-2",
        run_id="run-2",
        symbol="AVGO",
        eligible=True,
        payload={"symbol": "AVGO"},
    )
    with pytest.raises(StorageInvariantError, match="rejected candidate"):
        store.start_advisory_run(
            "advisory-2",
            run_id="run-2",
            candidate_id="eligible-2",
            model="qwen",
            prompt_hash=payload_hash("prompt"),
            config_hash=payload_hash("config"),
        )


class _ImmediateAdvisory:
    def __init__(self, _: Any, tools: AdvisoryReadOnlyTools) -> None:
        self.tools = tools

    async def review(self, _: Any) -> AdvisoryDecision:
        for name in sorted(QWEN_READ_TOOLS):
            await self.tools.execute(name, _read_arguments(name), turn=1)
        return AdvisoryDecision(
            assessment="REJECTION_CONTEXT_CLEAR",
            summary="The deterministic rejection remains final.",
            evidence=("All bounded reads completed.",),
            model="test-qwen",
            turns=2,
            tool_calls=6,
            prompt_tokens=20,
            completion_tokens=10,
        )


@pytest.mark.asyncio
async def test_advisory_runs_once_per_strategy_run_and_never_creates_order_state(
    tmp_path: Path, valid_env_file: Path
) -> None:
    store, _ = _store_with_rejection(tmp_path)
    connection = _ReadOnlyConnection()
    settings = load_settings(valid_env_file)
    events = [
        {
            "symbol": "PANW",
            "event_date": STRATEGY_DATE.isoformat(),
            "release_timing": "after_market_close",
            "status": "verified",
        }
    ]

    first = await run_rejected_candidate_advisory(
        settings,
        store,
        connection,  # type: ignore[arg-type]
        run_id="run-1",
        events=events,
        now=NOW,
        agent_factory=_ImmediateAdvisory,
    )
    second = await run_rejected_candidate_advisory(
        settings,
        store,
        connection,  # type: ignore[arg-type]
        run_id="run-1",
        events=events,
        now=NOW,
        agent_factory=_ImmediateAdvisory,
    )

    assert first is not None and first["status"] == "COMPLETED"
    assert first["non_authorizing"] is True
    assert second is not None and second["skipped_existing"] is True
    assert len(connection.calls) == 6
    with store.connect() as database:
        assert database.execute("SELECT COUNT(*) FROM advisory_runs").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM advisory_tool_trace").fetchone()[0] == 6
        assert database.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM order_chains").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM order_attempts").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_advisory_audit_timestamps_use_actual_utc_event_times(
    tmp_path: Path, valid_env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from thetatrap import advisory

    store, _ = _store_with_rejection(tmp_path)
    settings = load_settings(valid_env_file)
    connection = _ReadOnlyConnection()
    events = [
        {
            "symbol": "PANW",
            "event_date": STRATEGY_DATE.isoformat(),
            "release_timing": "after_market_close",
            "status": "verified",
        }
    ]
    event_times = [
        NOW + timedelta(minutes=1, microseconds=index)
        for index in range(8)
    ]
    remaining_times = iter(event_times)

    class _AuditClock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            assert tz is UTC
            return next(remaining_times)

    monkeypatch.setattr(advisory, "datetime", _AuditClock)
    result = await run_rejected_candidate_advisory(
        settings,
        store,
        connection,  # type: ignore[arg-type]
        run_id="run-1",
        events=events,
        now=NOW,
        agent_factory=_ImmediateAdvisory,
    )

    assert result is not None and result["status"] == "COMPLETED"
    persisted = store.advisory_run_for_strategy("run-1")
    assert persisted is not None
    trace = store.list_advisory_tool_calls(persisted["advisory_run_id"])
    assert persisted["started_at"] == event_times[0].isoformat(timespec="microseconds")
    assert [item["called_at"] for item in trace] == [
        value.isoformat(timespec="microseconds") for value in event_times[1:7]
    ]
    assert persisted["ended_at"] == event_times[7].isoformat(timespec="microseconds")
    assert persisted["started_at"] != NOW.isoformat(timespec="microseconds")


@pytest.mark.asyncio
async def test_persisted_advisory_cli_helper_is_idempotent_and_state_preserving(
    tmp_path: Path, valid_env_file: Path
) -> None:
    settings = load_settings(valid_env_file).model_copy(
        update={
            "database_path": tmp_path / "advisory.sqlite3",
            "featherless_api_key": SecretStr("test-key"),
        }
    )
    store, _ = _store_with_rejection(tmp_path)
    assert store.path == settings.database_path
    connection = _ReadOnlyConnection()
    context_entries = 0

    @asynccontextmanager
    async def factory(*_: Any) -> AsyncIterator[_ReadOnlyConnection]:
        nonlocal context_entries
        context_entries += 1
        yield connection

    first = await run_persisted_advisory_review(
        settings,
        STRATEGY_DATE,
        connection_factory=factory,  # type: ignore[arg-type]
        agent_factory=_ImmediateAdvisory,
    )
    second = await run_persisted_advisory_review(
        settings,
        STRATEGY_DATE,
        connection_factory=factory,  # type: ignore[arg-type]
        agent_factory=_ImmediateAdvisory,
    )

    assert first["outcome"] == "QWEN_ADVISORY_RECORDED"
    assert first["strategy_order_state_unchanged"] is True
    assert first["mutation_tools_exposed"] == 0
    assert first["mutation_dispatches"] == 0
    assert len(first["bounded_tool_trace"]) == 6
    assert second["advisory"]["skipped_existing"] is True
    assert context_entries == 1
