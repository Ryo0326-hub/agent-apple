from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from thetatrap.agent_smoke import (
    ReadOnlySmokeAgent,
    SMOKE_MAX_TOOL_CALLS,
)
from thetatrap.errors import AgentError, PolicyError
from thetatrap.mcp.contract import SETUP_READ_TOOLS
from thetatrap.settings import load_settings


def _tool_call(name: str, arguments: dict[str, Any], index: int) -> Any:
    return SimpleNamespace(
        id=f"smoke-{index}",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _response(calls: list[Any] | None, content: str | None = None) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(tool_calls=calls, content=content)
            )
        ],
        usage=SimpleNamespace(prompt_tokens=8, completion_tokens=3),
    )


class FakeCompletions:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses

    async def create(self, **_: Any) -> Any:
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses: list[Any]) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))


class FakeSmokeTools:
    def __init__(self) -> None:
        self.calls: list[SimpleNamespace] = []

    def definitions(self) -> list[dict[str, Any]]:
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

    async def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append(SimpleNamespace(name=name, arguments=arguments))
        return {"data": {"observed": True, "tool": name}}


def _complete_calls() -> list[Any]:
    result = []
    for index, name in enumerate(sorted(SETUP_READ_TOOLS)):
        arguments = (
            {"status": "open", "nested": True, "limit": 100}
            if name == "get_orders"
            else {}
        )
        result.append(_tool_call(name, arguments, index))
    return result


@pytest.mark.asyncio
async def test_smoke_succeeds_only_after_exact_five_read_calls(valid_env_file) -> None:
    tools = FakeSmokeTools()
    decision = await ReadOnlySmokeAgent(
        load_settings(valid_env_file),
        tools,
        client=FakeClient(
            [
                _response(_complete_calls()),
                _response([], '{"readiness":"READY","reasons":[]}'),
            ]
        ),
    ).run()

    assert decision.tool_calls == SMOKE_MAX_TOOL_CALLS == 5
    assert set(decision.read_tools) == SETUP_READ_TOOLS
    assert decision.readiness == "READY"
    assert decision.reasons == ()
    assert {call.name for call in tools.calls} == SETUP_READ_TOOLS
    assert all(item.status == "ok" for item in decision.trace)


@pytest.mark.asyncio
async def test_smoke_reports_structured_not_ready_reason(valid_env_file) -> None:
    tools = FakeSmokeTools()
    decision = await ReadOnlySmokeAgent(
        load_settings(valid_env_file),
        tools,
        client=FakeClient(
            [
                _response(_complete_calls()),
                _response(
                    [],
                    '{"readiness":"NOT_READY","reasons":["OPEN_POSITION"]}',
                ),
            ]
        ),
    ).run()

    assert decision.readiness == "NOT_READY"
    assert decision.reasons == ("OPEN_POSITION",)


@pytest.mark.asyncio
async def test_smoke_rejects_unstructured_readiness_result(valid_env_file) -> None:
    tools = FakeSmokeTools()

    with pytest.raises(AgentError, match="structured readiness"):
        await ReadOnlySmokeAgent(
            load_settings(valid_env_file),
            tools,
            client=FakeClient(
                [_response(_complete_calls()), _response([], None)]
            ),
        ).run()


@pytest.mark.asyncio
async def test_smoke_rejects_mutation_before_tool_dispatch(valid_env_file) -> None:
    tools = FakeSmokeTools()
    mutation = _tool_call("place_option_order", {"qty": "1"}, 0)

    with pytest.raises(PolicyError, match="unapproved tool"):
        await ReadOnlySmokeAgent(
            load_settings(valid_env_file),
            tools,
            client=FakeClient([_response([mutation])]),
        ).run()
    assert tools.calls == []


@pytest.mark.asyncio
async def test_smoke_fails_closed_above_five_call_budget(valid_env_file) -> None:
    tools = FakeSmokeTools()
    calls = _complete_calls() + [_tool_call("get_clock", {}, 99)]

    with pytest.raises(AgentError, match="five-call budget"):
        await ReadOnlySmokeAgent(
            load_settings(valid_env_file),
            tools,
            client=FakeClient([_response(calls)]),
        ).run()
    assert tools.calls == []
