from __future__ import annotations

import json
import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import APIStatusError

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
        self.requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> Any:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


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


def _status_error(status_code: int) -> APIStatusError:
    request = httpx.Request("POST", "https://api.featherless.ai/v1/chat/completions")
    response = httpx.Response(status_code, request=request)
    return APIStatusError("provider error", response=response, body={})


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
async def test_smoke_retries_5xx_only_before_any_tool_dispatch(valid_env_file) -> None:
    tools = FakeSmokeTools()
    client = FakeClient(
        [
            _status_error(500),
            _response(_complete_calls()),
            _response([], '{"readiness":"READY","reasons":[]}'),
        ]
    )

    decision = await ReadOnlySmokeAgent(
        load_settings(valid_env_file), tools, client=client
    ).run()

    assert decision.tool_calls == SMOKE_MAX_TOOL_CALLS
    assert decision.turns == 2
    assert len(client.chat.completions.requests) == 3
    assert all(
        request["model"] == "Qwen/Qwen3-Coder-Next"
        for request in client.chat.completions.requests
    )
    assert client.chat.completions.requests[0]["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_smoke_does_not_fallback_for_auth_status(valid_env_file) -> None:
    tools = FakeSmokeTools()
    client = FakeClient(
        [
            _status_error(401),
            _response(_complete_calls()),
            _response([], '{"readiness":"READY","reasons":[]}'),
        ]
    )

    with pytest.raises(AgentError, match="non-fallback HTTP 401"):
        await ReadOnlySmokeAgent(
            load_settings(valid_env_file), tools, client=client
        ).run()

    assert tools.calls == []
    assert len(client.chat.completions.requests) == 1


@pytest.mark.asyncio
async def test_smoke_routes_unavailable_primary_to_fallback(valid_env_file) -> None:
    tools = FakeSmokeTools()
    client = FakeClient(
        [
            _status_error(404),
            _response(_complete_calls()),
            _response([], '{"readiness":"READY","reasons":[]}'),
        ]
    )

    decision = await ReadOnlySmokeAgent(
        load_settings(valid_env_file), tools, client=client
    ).run()

    assert decision.model == "Qwen/Qwen3-32B"
    assert [
        request["model"] for request in client.chat.completions.requests
    ] == ["Qwen/Qwen3-Coder-Next", "Qwen/Qwen3-32B", "Qwen/Qwen3-32B"]
    assert decision.tool_calls == SMOKE_MAX_TOOL_CALLS


@pytest.mark.asyncio
async def test_smoke_never_retries_provider_failure_after_a_read(valid_env_file) -> None:
    tools = FakeSmokeTools()
    first_call = _complete_calls()[0]
    client = FakeClient([_response([first_call]), _status_error(503)])

    with pytest.raises(AgentError, match="refusing to replay"):
        await ReadOnlySmokeAgent(
            load_settings(valid_env_file), tools, client=client
        ).run()

    assert len(tools.calls) == 1
    assert len(client.chat.completions.requests) == 2


@pytest.mark.asyncio
async def test_smoke_global_deadline_includes_mcp_reads(
    valid_env_file, monkeypatch
) -> None:
    class SlowSmokeTools(FakeSmokeTools):
        async def execute(self, name: str, arguments: dict[str, Any]) -> Any:
            await asyncio.sleep(0.05)
            return await super().execute(name, arguments)

    monkeypatch.setattr("thetatrap.agent_smoke.SMOKE_MAX_SECONDS", 0.01)
    tools = SlowSmokeTools()

    with pytest.raises(AgentError, match="MCP read exceeded its time budget"):
        await ReadOnlySmokeAgent(
            load_settings(valid_env_file),
            tools,
            client=FakeClient([_response([_complete_calls()[0]])]),
        ).run()

    assert len(tools.calls) == 0


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
async def test_smoke_recovers_once_when_model_omits_remaining_read(
    valid_env_file,
) -> None:
    tools = FakeSmokeTools()
    calls = _complete_calls()
    client = FakeClient(
        [
            _response(calls[:-1]),
            _response([], "I am finished."),
            _response([calls[-1]]),
            _response([], '{"readiness":"READY","reasons":[]}'),
        ]
    )
    decision = await ReadOnlySmokeAgent(
        load_settings(valid_env_file),
        tools,
        client=client,
    ).run()

    assert decision.tool_calls == SMOKE_MAX_TOOL_CALLS
    assert set(decision.read_tools) == SETUP_READ_TOOLS
    assert decision.turns == 4
    incomplete_requests = [
        request for request in client.chat.completions.requests if "tools" in request
    ]
    assert all(request["tool_choice"] == "auto" for request in incomplete_requests)
    assert all(
        {
            definition["function"]["name"]
            for definition in request["tools"]
        }
        == SETUP_READ_TOOLS
        for request in incomplete_requests
    )


@pytest.mark.asyncio
async def test_smoke_fails_closed_after_second_empty_tool_response(
    valid_env_file,
) -> None:
    tools = FakeSmokeTools()

    with pytest.raises(AgentError, match="ended before completing required reads"):
        await ReadOnlySmokeAgent(
            load_settings(valid_env_file),
            tools,
            client=FakeClient([_response([]), _response([])]),
        ).run()
    assert tools.calls == []


@pytest.mark.asyncio
async def test_retry_and_fallback_do_not_expand_global_turn_budget(
    valid_env_file,
) -> None:
    tools = FakeSmokeTools()
    calls = _complete_calls()
    client = FakeClient(
        [
            _response([]),
            _status_error(503),
            _status_error(503),
            _status_error(503),
            *[_response([call]) for call in calls],
            _response([], '{"readiness":"READY","reasons":[]}'),
        ]
    )

    with pytest.raises(AgentError, match="missing readiness result"):
        await ReadOnlySmokeAgent(
            load_settings(valid_env_file), tools, client=client
        ).run()

    assert len(tools.calls) == SMOKE_MAX_TOOL_CALLS
    assert len(client.chat.completions.requests) == 9
    assert len(client.chat.completions.responses) == 1


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
