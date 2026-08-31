from datetime import UTC, datetime, timedelta

import pytest

from thetatrap.errors import PolicyError
from thetatrap.policy import (
    exact_arguments,
    make_entry_permit,
    normalize_exact_model_arguments,
    validate_mleg_arguments,
)


def valid_entry() -> dict:
    return {
        "qty": "1",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": "-1.25",
        "client_order_id": "tt-v1-panw-entry",
        "order_class": "mleg",
        "legs": [
            {
                "symbol": "PANW260904P00300000",
                "ratio_qty": "1",
                "side": "buy",
                "position_intent": "buy_to_open",
            },
            {
                "symbol": "PANW260904P00305000",
                "ratio_qty": "1",
                "side": "sell",
                "position_intent": "sell_to_open",
            },
            {
                "symbol": "PANW260904C00345000",
                "ratio_qty": "1",
                "side": "sell",
                "position_intent": "sell_to_open",
            },
            {
                "symbol": "PANW260904C00350000",
                "ratio_qty": "1",
                "side": "buy",
                "position_intent": "buy_to_open",
            },
        ],
    }


def test_valid_bounded_entry_and_exact_permit() -> None:
    arguments = valid_entry()
    issued = datetime(2026, 9, 1, 19, 0, tzinfo=UTC)
    permit = make_entry_permit(intent_id="intent-1", arguments=arguments, now=issued)
    permit.assert_call(
        tool_name="place_option_order",
        principal="agent",
        arguments=arguments,
        now=issued + timedelta(seconds=30),
    )


def test_near_match_model_payload_is_rejected() -> None:
    expected = valid_entry()
    observed = {**expected, "limit_price": "-1.30"}
    with pytest.raises(PolicyError, match="differ"):
        exact_arguments(expected, observed)


def test_string_encoded_legs_are_rejected() -> None:
    arguments = {**valid_entry(), "legs": "[]"}
    with pytest.raises(PolicyError, match="four native"):
        validate_mleg_arguments(arguments, action="entry")


def test_only_exact_stringified_model_legs_are_normalized() -> None:
    expected = valid_entry()
    observed = {**expected, "legs": __import__("json").dumps(expected["legs"])}
    normalized = normalize_exact_model_arguments(expected, observed)
    assert normalized == expected
    assert isinstance(normalized["legs"], list)

    changed = {**observed, "legs": observed["legs"].replace("buy_to_open", "sell_to_open", 1)}
    with pytest.raises(PolicyError, match="differ"):
        normalize_exact_model_arguments(expected, changed)


def test_asymmetric_wings_are_rejected() -> None:
    arguments = valid_entry()
    arguments["legs"][3]["symbol"] = "PANW260904C00355000"
    with pytest.raises(PolicyError, match="equal"):
        validate_mleg_arguments(arguments, action="entry")


def test_wings_wider_than_five_dollars_are_rejected() -> None:
    arguments = valid_entry()
    arguments["legs"][0]["symbol"] = "PANW260904P00295000"
    arguments["legs"][3]["symbol"] = "PANW260904C00355000"
    arguments["limit_price"] = "-0.50"
    with pytest.raises(PolicyError, match="no wider"):
        validate_mleg_arguments(arguments, action="entry")


def test_permit_expires_and_cannot_change_principal() -> None:
    arguments = valid_entry()
    issued = datetime(2026, 9, 1, 19, 0, tzinfo=UTC)
    permit = make_entry_permit(intent_id="intent-1", arguments=arguments, now=issued)
    with pytest.raises(PolicyError, match="expired"):
        permit.assert_call(
            tool_name="place_option_order",
            principal="agent",
            arguments=arguments,
            now=issued + timedelta(seconds=61),
        )
    with pytest.raises(PolicyError, match="principal"):
        permit.assert_call(
            tool_name="place_option_order",
            principal="system",
            arguments=arguments,
            now=issued,
        )
