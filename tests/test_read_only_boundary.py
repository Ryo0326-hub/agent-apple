from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from thetatrap.errors import MCPContractError
from thetatrap.mcp.client import MCPConnection
from thetatrap.mcp.contract import REQUIRED_TOOLS, ToolRegistry
from thetatrap.storage import Store


class FakeTool:
    def __init__(self, name: str):
        self.name = name

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.name,
            "inputSchema": {"type": "object", "properties": {}},
        }


class NeverCalledSession:
    async def call_tool(self, *_: Any, **__: Any) -> None:
        raise AssertionError("mutation reached ClientSession.call_tool")


@pytest.mark.asyncio
async def test_checkpoint_one_denies_mutations_before_dispatch(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    store.initialize()
    connection = MCPConnection(
        session=NeverCalledSession(),  # type: ignore[arg-type]
        registry=ToolRegistry.from_tools([FakeTool(name) for name in REQUIRED_TOOLS]),
        session_id="not-used",
        store=store,
        initialization={},
    )
    with pytest.raises(MCPContractError, match="denies"):
        await connection.call_read("place_option_order", {})


def test_source_has_no_direct_alpaca_client_or_url() -> None:
    banned = (
        "alpaca_trade_api",
        "from alpaca.",
        "import alpaca.",
        "paper-api.alpaca.markets",
        "data.alpaca.markets",
    )
    for path in Path("src/thetatrap").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for marker in banned:
            assert marker not in source, f"{path} contains forbidden direct broker marker {marker}"


def test_compose_keeps_secrets_out_of_ui() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8"))
    worker = compose["services"]["worker"]
    ui = compose["services"]["ui"]
    ui_environment = ui.get("environment", {})
    assert worker["init"] is True
    assert ui["init"] is True
    assert ui["ports"] == ["127.0.0.1:8501:8501"]
    assert not any(
        token in name for name in ui_environment for token in ("ALPACA", "FEATHERLESS")
    )
