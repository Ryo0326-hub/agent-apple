import json
from types import SimpleNamespace
from typing import Any

import pytest

from thetatrap.agent import AgentContext, AgentOutcome, QwenAgent, local_tool_definitions
from thetatrap.errors import PolicyError
from thetatrap.policy import payload_hash
from thetatrap.settings import load_settings


READ_NAMES = [
    "get_account_info",
    "get_account_config",
    "get_clock",
    "get_orders",
    "get_all_positions",
    "get_news",
    "get_candidate",
]


def intent() -> dict[str, Any]:
    return {
        "qty": "1",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": "-1.25",
        "client_order_id": "tt-v1-panw-entry",
        "order_class": "mleg",
        "legs": [],
    }


def context() -> AgentContext:
    return AgentContext(
        symbol="PANW",
        event={"status": "verified"},
        candidate={"eligible": True},
        run_summary={"state": "AI_REVIEW"},
        order_intent_id="intent-1",
        order_arguments=intent(),
    )


def tool_call(name: str, arguments: dict[str, Any], index: int) -> Any:
    return SimpleNamespace(
        id=f"call-{index}",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def response(calls: list[Any] | None, content: str | None = None) -> Any:
    message = SimpleNamespace(tool_calls=calls, content=content)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


class FakeCompletions:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses

    async def create(self, **_: Any) -> Any:
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses: list[Any]) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))


class FakeTools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def definitions(self) -> list[dict[str, Any]]:
        definitions = local_tool_definitions()
        for name in set(READ_NAMES) - {"get_candidate"}:
            definitions.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": name,
                        "parameters": {
                            "type": "object",
                            "properties": {},
                            "required": [],
                            "additionalProperties": False,
                        },
                    },
                }
            )
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": "place_option_order",
                    "description": "exact intent only",
                    "parameters": {"type": "object"},
                },
            }
        )
        return definitions

    async def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        return {"_alpaca_mcp_security": {"trust": "untrusted_tool_output"}, "data": {}}


def read_response() -> Any:
    return response([tool_call(name, {}, index) for index, name in enumerate(READ_NAMES)])


@pytest.mark.asyncio
async def test_exact_tool_call_after_required_reads_allows(valid_env_file) -> None:
    tools = FakeTools()
    client = FakeClient(
        [read_response(), response([tool_call("place_option_order", intent(), 99)])]
    )
    decision = await QwenAgent(load_settings(valid_env_file), tools, client=client).review(context())
    assert decision.outcome == AgentOutcome.ALLOW
    assert decision.mutation_arguments == intent()
    assert decision.tool_calls == 8
    assert decision.trace[-1].arguments_hash == payload_hash(intent())


@pytest.mark.asyncio
async def test_mutation_must_be_the_only_call(valid_env_file) -> None:
    tools = FakeTools()
    mixed = [tool_call("place_option_order", intent(), 1), tool_call("get_clock", {}, 2)]
    client = FakeClient([read_response(), response(mixed)])
    with pytest.raises(PolicyError, match="sole"):
        await QwenAgent(load_settings(valid_env_file), tools, client=client).review(context())


@pytest.mark.asyncio
async def test_near_match_intent_is_rejected(valid_env_file) -> None:
    tools = FakeTools()
    changed = {**intent(), "limit_price": "-1.30"}
    client = FakeClient([read_response(), response([tool_call("place_option_order", changed, 9)])])
    with pytest.raises(PolicyError, match="differ"):
        await QwenAgent(load_settings(valid_env_file), tools, client=client).review(context())


@pytest.mark.asyncio
async def test_exact_stringified_legs_are_returned_as_native_array(valid_env_file) -> None:
    tools = FakeTools()
    stringified = {**intent(), "legs": json.dumps(intent()["legs"])}
    client = FakeClient(
        [read_response(), response([tool_call("place_option_order", stringified, 9)])]
    )
    decision = await QwenAgent(load_settings(valid_env_file), tools, client=client).review(context())
    assert decision.outcome == AgentOutcome.ALLOW
    assert decision.mutation_arguments == intent()
    assert isinstance(decision.mutation_arguments["legs"], list)


@pytest.mark.asyncio
async def test_prose_allow_is_not_executable(valid_env_file) -> None:
    tools = FakeTools()
    client = FakeClient([response(None, "ALLOW")])
    decision = await QwenAgent(load_settings(valid_env_file), tools, client=client).review(context())
    assert decision.outcome == AgentOutcome.ERROR
    assert decision.reason_code == "NO_TOOL_DECISION"


@pytest.mark.asyncio
async def test_finite_veto_is_recorded(valid_env_file) -> None:
    tools = FakeTools()
    rejection = {
        "reason_code": "TRADING_HALT",
        "explanation": "Current evidence reports a halt.",
        "evidence": ["timestamped item"],
    }
    client = FakeClient([response([tool_call("record_candidate_rejection", rejection, 1)])])
    decision = await QwenAgent(load_settings(valid_env_file), tools, client=client).review(context())
    assert decision.outcome == AgentOutcome.VETO
    assert tools.calls == [("record_candidate_rejection", rejection)]
